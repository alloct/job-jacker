"""LinkedIn, via its public guest job search.

Everything here uses endpoints LinkedIn serves to logged-out visitors:

  /jobs-guest/jobs/api/seeMoreJobPostings/search  -> a fragment of result cards
  /jobs/view/<slug>-<id>                          -> one public posting

Nothing here logs in or stores a cookie. If LinkedIn starts refusing anonymous
requests this source reports HTTP 403 and the rest of the cycle carries on without
it.

The search fragment has no description or employment type. Those live on the posting
page, inside its schema.org JobPosting JSON-LD block, which changes far less often
than the page's CSS classes. Reading it is opt-in (`fetch_details`) and only happens
for a job that is new and already matches on title, company and location.
"""

from __future__ import annotations

import json
import logging
from typing import Sequence

from bs4 import BeautifulSoup

from ..http_client import FetchError, NotModified
from ..models import Job, format_salary, html_to_text, normalize_employment_type, parse_timestamp
from . import Source

log = logging.getLogger(__name__)

SEARCH_URL = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
RESULTS_PER_PAGE = 10


class LinkedInSource(Source):
    board = "linkedin"
    options = frozenset(
        {"locations", "posted_within_days", "pages", "remote_only", "fetch_details", "max_queries"}
    )

    def configure(self) -> None:
        self.locations = self.opt_list("locations", ["Canada"])
        self.posted_within_days = self.opt_int("posted_within_days", 7, minimum=1, maximum=30)
        self.pages = self.opt_int("pages", 1, minimum=1, maximum=5)
        self.remote_only = self.opt_bool("remote_only", False)
        self.fetch_details = self.opt_bool("fetch_details", True)
        self.max_queries = self.opt_int("max_queries", 8, minimum=1, maximum=40)

    @property
    def name(self) -> str:
        return "LinkedIn"

    def fetch(self, terms: Sequence[str]) -> list[Job]:
        if not terms:
            log.warning("LinkedIn needs search terms; add titles.include to a search block")
            return []

        jobs: dict[str, Job] = {}
        failures = 0
        queries = [(term, location) for location in self.locations for term in terms]
        if len(queries) > self.max_queries:
            log.debug(
                "LinkedIn: %d term/location combinations exceed max_queries=%d; using the first %d",
                len(queries),
                self.max_queries,
                self.max_queries,
            )
            queries = queries[: self.max_queries]

        for term, location in queries:
            try:
                found = self._search(term, location)
            except FetchError as exc:
                failures += 1
                log.warning("LinkedIn search for %r in %r failed: %s", term, location, exc)
                continue
            for job in found:
                jobs.setdefault(job.fingerprint(), job)

        if failures and failures == len(queries):
            raise FetchError("every LinkedIn search failed")
        return list(jobs.values())

    def _search(self, term: str, location: str) -> list[Job]:
        jobs: list[Job] = []
        for page in range(self.pages):
            params = {
                "keywords": term,
                "location": location,
                "start": page * RESULTS_PER_PAGE,
                "sortBy": "DD",  # newest first, so one page is usually enough
                "f_TPR": f"r{self.posted_within_days * 86400}",
            }
            if self.remote_only:
                params["f_WT"] = "2"
            response = self.client.get(SEARCH_URL, params=params)
            page_jobs = parse_search_fragment(response.text)
            jobs.extend(page_jobs)
            if len(page_jobs) < RESULTS_PER_PAGE:
                break
        return jobs

    def enrich(self, job: Job) -> bool:
        """Add description, employment type and salary from the public posting page."""
        if not self.fetch_details or not job.url:
            return False
        try:
            response = self.client.get(job.url)
        except (FetchError, NotModified) as exc:
            log.debug("LinkedIn detail fetch failed for %s: %s", job.external_id, exc)
            return False
        try:
            return apply_detail_page(job, response.text)
        except Exception as exc:  # noqa: BLE001 - a layout change must not stop the cycle
            log.debug("Could not read the LinkedIn posting page for %s: %s", job.external_id, exc)
            return False


def parse_search_fragment(html: str) -> list[Job]:
    """Turn the guest search HTML fragment into jobs, skipping unreadable cards."""
    soup = BeautifulSoup(html, "html.parser")
    jobs = []
    for card in soup.select("div.base-card, li div.base-search-card"):
        job = _parse_card(card)
        if job is not None:
            jobs.append(job)
    return jobs


def _parse_card(card) -> Job | None:
    title = _text(card, "h3.base-search-card__title")
    link = card.select_one("a.base-card__full-link") or card.select_one("a[href*='/jobs/view/']")
    url = (link.get("href") or "").strip() if link else ""
    if not title or not url:
        return None

    urn = card.get("data-entity-urn") or ""
    external_id = urn.rsplit(":", 1)[-1] if "jobPosting" in urn else None

    logo = card.select_one("img[data-delayed-url]")
    time_tag = card.select_one("time")

    return Job(
        board="linkedin",
        external_id=external_id,
        title=title,
        company=_text(card, "h4.base-search-card__subtitle"),
        location=_text(card, "span.job-search-card__location"),
        url=url.split("?")[0],
        posted_at=parse_timestamp(time_tag.get("datetime")) if time_tag else None,
        company_logo=(logo.get("data-delayed-url") or "").split("?")[0] if logo else "",
    )


def apply_detail_page(job: Job, html: str) -> bool:
    """Fill in the fields the search fragment omits. Returns True if anything changed."""
    soup = BeautifulSoup(html, "html.parser")
    posting = _find_job_posting(soup)
    changed = False

    if posting:
        description = html_to_text(posting.get("description"))
        if description:
            job.description = description
            changed = True
        employment = normalize_employment_type(_first_string(posting.get("employmentType")))
        if employment:
            job.employment_type = employment
            changed = True
        posted = parse_timestamp(posting.get("datePosted"))
        if posted and not job.posted_at:
            job.posted_at = posted
            changed = True
        if _apply_salary(job, posting.get("baseSalary")):
            changed = True

    if not job.description:
        node = soup.select_one("div.show-more-less-html__markup, div.description__text")
        if node:
            job.description = html_to_text(node.decode_contents())
            changed = True

    if not job.employment_type:
        criteria = soup.select("ul.description__job-criteria-list li")
        for item in criteria:
            label = _text(item, "h3.description__job-criteria-subheader") or item.get_text(" ")
            if "employment type" in label.lower():
                employment = normalize_employment_type(
                    _text(item, "span.description__job-criteria-text") or ""
                )
                if employment:
                    job.employment_type = employment
                    changed = True
                break
    return changed


def _find_job_posting(soup) -> dict | None:
    """Locate the schema.org JobPosting object in the page's JSON-LD blocks."""
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw = script.string or script.get_text() or ""
        if not raw.strip():
            continue
        try:
            data = json.loads(raw)
        except ValueError:
            continue
        for candidate in _walk(data):
            if candidate.get("@type") == "JobPosting":
                return candidate
    return None


def _walk(data):
    """Yield every mapping inside an arbitrarily nested JSON-LD document."""
    if isinstance(data, dict):
        yield data
        for value in data.values():
            yield from _walk(value)
    elif isinstance(data, list):
        for item in data:
            yield from _walk(item)


def _apply_salary(job: Job, base_salary) -> bool:
    if not isinstance(base_salary, dict):
        return False
    value = base_salary.get("value")
    if not isinstance(value, dict):
        return False
    minimum = _number(value.get("minValue"))
    maximum = _number(value.get("maxValue")) or _number(value.get("value"))
    if minimum is None and maximum is None:
        return False
    job.salary_min = minimum
    job.salary_max = maximum
    job.salary_currency = (base_salary.get("currency") or "").upper() or None
    unit = str(value.get("unitText") or "YEAR").lower()
    job.salary_period = unit if unit in {"hour", "day", "week", "month", "year"} else "year"
    job.salary_text = format_salary(job)
    return True


def _number(value) -> float | None:
    try:
        return float(str(value).replace(",", "")) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _first_string(value) -> str:
    if isinstance(value, list):
        return str(value[0]) if value else ""
    return str(value or "")


def _text(node, selector: str) -> str:
    found = node.select_one(selector)
    return found.get_text(" ", strip=True) if found else ""
