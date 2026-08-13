"""The normalized job record that every source must produce."""

from __future__ import annotations

import hashlib
import html
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

EMPLOYMENT_TYPES = (
    "full-time",
    "part-time",
    "contract",
    "temporary",
    "internship",
    "volunteer",
)

# Ordered longest/most specific first so "part time" is not swallowed by "time",
# and "intern" does not steal a match from "internal audit contract".
_EMPLOYMENT_PATTERNS: tuple[tuple[str, str], ...] = (
    ("part time", "part-time"),
    ("parttime", "part-time"),
    ("full time", "full-time"),
    ("fulltime", "full-time"),
    ("permanent", "full-time"),
    ("regular", "full-time"),
    ("internship", "internship"),
    ("intern", "internship"),
    ("co op", "internship"),
    ("coop", "internship"),
    ("apprentice", "internship"),
    ("contract to hire", "contract"),
    ("contractor", "contract"),
    ("contract", "contract"),
    ("freelance", "contract"),
    ("fixed term", "contract"),
    ("temporary", "temporary"),
    ("temp", "temporary"),
    ("seasonal", "temporary"),
    ("volunteer", "volunteer"),
)

# Query parameters that identify a click, not a job. Dropped so the same posting
# seen through two different search result pages produces one fingerprint.
_TRACKING_PARAMS = frozenset(
    {
        "position",
        "pagenum",
        "refid",
        "trackingid",
        "trk",
        "trkinfo",
        "originalsubdomain",
        "src",
        "source",
        "ref",
        "referrer",
        "utm_source",
        "utm_medium",
        "utm_campaign",
        "utm_content",
        "utm_term",
    }
)

_TAGS = re.compile(r"<[^>]+>")
_WHITESPACE = re.compile(r"\s+")
_PERIOD_MULTIPLIERS = {
    "hour": 2080,
    "day": 260,
    "week": 52,
    "month": 12,
    "year": 1,
}


def html_to_text(raw: str | None) -> str:
    """Flatten an HTML (or already-plain) description into a single-line string."""
    if not raw:
        return ""
    # Some boards return HTML that is itself entity-escaped, hence unescaping twice.
    text = html.unescape(html.unescape(raw))
    text = _TAGS.sub(" ", text)
    return _WHITESPACE.sub(" ", text).strip()


def normalize_employment_type(raw: str | None) -> str | None:
    """Map a board's free-form employment wording onto a canonical value."""
    if not raw:
        return None
    probe = _WHITESPACE.sub(" ", re.sub(r"[^a-z0-9]+", " ", raw.lower())).strip()
    if not probe:
        return None
    for needle, canonical in _EMPLOYMENT_PATTERNS:
        if needle in probe:
            return canonical
    return None


def canonical_url(url: str | None) -> str:
    """Strip tracking parameters and fragments so URLs compare reliably."""
    if not url:
        return ""
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return url.strip()
    kept = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=False)
        if key.lower() not in _TRACKING_PARAMS
    ]
    return urlunsplit(
        (parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip("/"), urlencode(kept), "")
    )


def parse_salary_text(text: str | None) -> tuple[float | None, float | None, str | None]:
    """Best-effort salary extraction from free text such as "$80,000 - $95k CAD".

    Returns (minimum, maximum, currency); any part may be None. Ambiguous or
    unparseable text yields all None so that it is never used to reject a job.
    """
    if not text:
        return None, None, None
    lowered = text.lower()
    currency = None
    for code in ("cad", "usd", "eur", "gbp", "aud"):
        if code in lowered:
            currency = code.upper()
            break
    if currency is None:
        if "£" in text:
            currency = "GBP"
        elif "€" in text:
            currency = "EUR"
        elif "$" in text:
            currency = "USD"

    amounts: list[float] = []
    for match in re.finditer(r"(\d[\d,\.]*)\s*(k\b)?", lowered):
        digits = match.group(1).replace(",", "")
        if not digits or digits.count(".") > 1:
            continue
        try:
            value = float(digits)
        except ValueError:
            continue
        if match.group(2):
            value *= 1000
        # Ignore stray numbers like years or percentages.
        if value >= 1000:
            amounts.append(value)
    if not amounts:
        return None, None, currency
    return min(amounts), max(amounts) if len(amounts) > 1 else None, currency


def annualize(amount: float | None, period: str | None) -> float | None:
    """Convert a salary figure to a yearly equivalent for comparison."""
    if amount is None:
        return None
    return amount * _PERIOD_MULTIPLIERS.get((period or "year").lower(), 1)


@dataclass
class Job:
    """A single posting, normalized. Sources must not add their own fields."""

    board: str
    title: str
    company: str
    url: str
    external_id: str | None = None
    location: str = ""
    description: str = ""
    employment_type: str | None = None
    salary_min: float | None = None
    salary_max: float | None = None
    salary_currency: str | None = None
    salary_period: str = "year"
    salary_text: str = ""
    salary_is_estimate: bool = False
    posted_at: datetime | None = None
    remote: bool = False
    tags: tuple[str, ...] = ()
    company_logo: str = ""
    # How the board should be named in notifications, when the id is not pretty.
    board_label: str = ""

    def __post_init__(self) -> None:
        self.title = _WHITESPACE.sub(" ", (self.title or "").strip())
        self.company = _WHITESPACE.sub(" ", (self.company or "").strip())
        self.location = _WHITESPACE.sub(" ", (self.location or "").strip())
        self.url = (self.url or "").strip()
        if self.remote and "remote" not in self.location.lower():
            # Lets a plain `locations: [Remote]` filter work on boards that flag
            # remoteness separately from the location string.
            self.location = f"Remote{' / ' + self.location if self.location else ''}"
        if not self.salary_text:
            self.salary_text = format_salary(self)

    @property
    def is_usable(self) -> bool:
        """A job with no title or no link is not worth notifying anyone about."""
        return bool(self.title and self.url)

    @property
    def searchable_text(self) -> str:
        """Text that description keyword filters run against."""
        return " ".join(part for part in (self.title, self.description, *self.tags) if part)

    @property
    def canonical_url(self) -> str:
        return canonical_url(self.url)

    def fingerprint(self) -> str:
        """Stable identity used for deduplication across restarts."""
        if self.external_id:
            basis = f"{self.board}:{self.external_id}"
        elif self.canonical_url:
            basis = f"url:{self.canonical_url}"
        else:
            basis = "attrs:" + "|".join(
                _WHITESPACE.sub(" ", value.lower().strip())
                for value in (self.company, self.title, self.location)
            )
        return hashlib.sha256(basis.encode("utf-8", "replace")).hexdigest()[:40]

    def short_description(self, limit: int = 350) -> str:
        text = self.description.strip()
        if len(text) <= limit:
            return text
        return text[: limit - 1].rsplit(" ", 1)[0] + "…"


def format_salary(job: Job) -> str:
    """Human-readable salary line, or "" when the board gave us nothing."""
    if job.salary_min is None and job.salary_max is None:
        return ""
    currency = f" {job.salary_currency}" if job.salary_currency else ""
    period = "" if job.salary_period == "year" else f"/{job.salary_period}"

    def money(value: float) -> str:
        return f"{value:,.0f}" if value >= 1000 else f"{value:,.2f}"

    if job.salary_min is not None and job.salary_max is not None and job.salary_max > job.salary_min:
        body = f"{money(job.salary_min)}–{money(job.salary_max)}"
    else:
        body = money(job.salary_min if job.salary_min is not None else job.salary_max)
    estimate = " (estimated)" if job.salary_is_estimate else ""
    return f"{body}{currency}{period}{estimate}"


def parse_timestamp(value: object) -> datetime | None:
    """Parse the assorted date formats boards use. Returns UTC-aware datetimes."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, (int, float)):
        seconds = float(value)
        if seconds > 1e11:  # milliseconds
            seconds /= 1000
        try:
            return datetime.fromtimestamp(seconds, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    text = str(value).strip()
    if not text:
        return None
    candidate = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(candidate)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        pass
    for fmt in ("%a, %d %b %Y %H:%M:%S %z", "%a, %d %b %Y %H:%M:%S %Z", "%Y-%m-%d", "%d %b %Y"):
        try:
            parsed = datetime.strptime(text, fmt)
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None
