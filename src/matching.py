"""Keyword matching.

The rules:

1. Every configured include/exclude list is a hard rule. A job must satisfy all
   of them to be sent. The score is informational (and drives `min_score`).
2. Title, company and location are judged strictly, because every board publishes
   them.
3. Description keywords, employment type and salary are optional detail. When a
   board does not publish them, that filter is skipped instead of failing. A board
   with no salary field would otherwise match nothing at all.

Matching is case-insensitive, punctuation-insensitive and matches whole words or
whole phrases, so "SOC" does not match "social" and "Microsoft Sentinel" still
matches "microsoft-sentinel".
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache

from .models import annualize, normalize_employment_type

SCORE_TITLE = 3
SCORE_COMPANY = 2
SCORE_KEYWORD = 1
SCORE_LOCATION = 1


def normalize(text: str) -> str:
    """Lowercase and reduce punctuation to spaces, keeping +, # and . for C++/C#/.NET."""
    lowered = (text or "").lower()
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9+#.]+", " ", lowered)).strip()


@lru_cache(maxsize=2048)
def _term_pattern(term: str) -> re.Pattern[str] | None:
    """Whole-word/phrase pattern for a configured term."""
    normalized = normalize(term)
    if not normalized:
        return None
    body = re.escape(normalized).replace(r"\ ", r"\s+")
    return re.compile(rf"(?<![a-z0-9]){body}(?![a-z0-9])")


def matches_any(text: str, terms: tuple[str, ...]) -> list[str]:
    """Return the terms present in `text`."""
    if not terms or not text:
        return []
    haystack = normalize(text)
    hits = []
    for term in terms:
        pattern = _term_pattern(term)
        if pattern and pattern.search(haystack):
            hits.append(term)
    return hits


@dataclass
class MatchResult:
    matched: bool
    score: int = 0
    search_name: str = ""
    reasons: list[str] = field(default_factory=list)
    rejected_by: str = ""

    def __bool__(self) -> bool:
        return self.matched


def _check(label: str, text: str, filters, points: int, result: MatchResult) -> bool:
    """Apply one include/exclude pair strictly. False means the job is rejected."""
    blocked = matches_any(text, filters.exclude)
    if blocked:
        result.rejected_by = f"excluded {label}: {', '.join(blocked)}"
        return False
    if not filters.include:
        return True
    hits = matches_any(text, filters.include)
    if not hits:
        result.rejected_by = f"no {label} match"
        return False
    result.score += points
    result.reasons.append(f"{label}: {', '.join(hits)}")
    return True


def evaluate(job, search) -> MatchResult:
    """Test one job against one search block."""
    result = MatchResult(matched=False, search_name=search.name)

    for label, text, filters, points in (
        ("title", job.title, search.titles, SCORE_TITLE),
        ("company", job.company, search.companies, SCORE_COMPANY),
        ("location", job.location, search.locations, SCORE_LOCATION),
    ):
        if not _check(label, text, filters, points, result):
            return result

    if not _keywords_ok(job, search, result):
        return result
    if not _employment_ok(job, search, result):
        return result
    if not _salary_ok(job, search, result):
        return result

    if result.score < search.min_score:
        result.rejected_by = f"score {result.score} below min_score {search.min_score}"
        return result

    result.matched = True
    return result


def _keywords_ok(job, search, result: MatchResult) -> bool:
    """Keywords are looked for in the title, description and tags together.

    A board that publishes no description (LinkedIn's results list, for example)
    would otherwise be filtered out entirely, so the include list is skipped in
    that case. Turn on `fetch_details` for such a source to get real filtering.
    """
    text = job.searchable_text
    blocked = matches_any(text, search.keywords.exclude)
    if blocked:
        result.rejected_by = f"excluded keyword: {', '.join(blocked)}"
        return False
    if not search.keywords.include:
        return True
    hits = matches_any(text, search.keywords.include)
    if hits:
        result.score += min(len(hits), 5) * SCORE_KEYWORD
        result.reasons.append(f"keywords: {', '.join(hits)}")
        return True
    if not job.description and not job.tags:
        result.reasons.append("no description published by this board; keywords not checked")
        return True
    result.rejected_by = "no keyword match"
    return False


def _employment_ok(job, search, result: MatchResult) -> bool:
    include = tuple(
        canonical
        for canonical in (normalize_employment_type(term) for term in search.employment_types.include)
        if canonical
    )
    exclude = tuple(
        canonical
        for canonical in (normalize_employment_type(term) for term in search.employment_types.exclude)
        if canonical
    )
    if not include and not exclude:
        return True
    if job.employment_type is None:
        result.reasons.append("employment type not provided by board")
        return True
    if job.employment_type in exclude:
        result.rejected_by = f"excluded employment type: {job.employment_type}"
        return False
    if include and job.employment_type not in include:
        result.rejected_by = f"employment type {job.employment_type} not requested"
        return False
    if include:
        result.reasons.append(f"employment type: {job.employment_type}")
    return True


def _salary_ok(job, search, result: MatchResult) -> bool:
    """Salary is only ever a reason to reject when we have a real figure."""
    if search.salary_minimum is None:
        return True
    if job.salary_is_estimate:
        result.reasons.append("salary is an estimate; not filtered")
        return True
    best = job.salary_max if job.salary_max is not None else job.salary_min
    annual = annualize(best, job.salary_period)
    if annual is None:
        result.reasons.append("salary not provided by board")
        return True
    if (
        search.salary_currency
        and job.salary_currency
        and search.salary_currency.upper() != job.salary_currency.upper()
    ):
        result.reasons.append(
            f"salary in {job.salary_currency}, cannot compare to {search.salary_currency}"
        )
        return True
    if annual < search.salary_minimum:
        result.rejected_by = f"salary below {search.salary_minimum:,.0f}"
        return False
    result.reasons.append(f"salary meets minimum ({job.salary_text})")
    return True


def best_match(job, searches) -> MatchResult | None:
    """Highest-scoring search that accepts this job, or None if none do."""
    best: MatchResult | None = None
    for search in searches:
        result = evaluate(job, search)
        if result.matched and (best is None or result.score > best.score):
            best = result
    return best
