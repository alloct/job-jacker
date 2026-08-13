"""Remotive, via its free public API: https://remotive.com/api/remote-jobs

Remote-only listings. The API asks callers to keep request volume low, which the
shared polite client already does.
"""

from __future__ import annotations

import logging
from typing import Sequence

from ..http_client import NotModified
from ..models import Job, html_to_text, normalize_employment_type, parse_salary_text, parse_timestamp
from . import Source

log = logging.getLogger(__name__)

API_URL = "https://remotive.com/api/remote-jobs"


class RemotiveSource(Source):
    board = "remotive"
    options = frozenset({"category", "limit"})

    def configure(self) -> None:
        self.category = self.opt_str("category")
        self.limit = self.opt_int("limit", 100, minimum=1, maximum=500)

    @property
    def name(self) -> str:
        return "Remotive"

    def fetch(self, terms: Sequence[str]) -> list[Job]:
        params = {"limit": self.limit}
        if self.category:
            params["category"] = self.category
        try:
            payload = self.client.get_json(API_URL, params=params, conditional=True)
        except NotModified:
            log.info("Remotive is unchanged since the last check; not downloading it again")
            return []
        return parse_jobs(payload)


def parse_jobs(payload) -> list[Job]:
    if not isinstance(payload, dict):
        return []
    jobs = []
    for entry in payload.get("jobs") or []:
        if not isinstance(entry, dict):
            continue
        title = str(entry.get("title") or "").strip()
        url = str(entry.get("url") or "").strip()
        if not title or not url:
            continue
        salary_text = str(entry.get("salary") or "").strip()
        minimum, maximum, currency = parse_salary_text(salary_text)
        tags = tuple(str(tag) for tag in entry.get("tags") or [] if tag)
        jobs.append(
            Job(
                board="remotive",
                external_id=str(entry.get("id")) if entry.get("id") else None,
                title=title,
                company=str(entry.get("company_name") or ""),
                location=str(entry.get("candidate_required_location") or ""),
                url=url,
                description=html_to_text(entry.get("description")),
                employment_type=normalize_employment_type(entry.get("job_type")),
                salary_min=minimum,
                salary_max=maximum,
                salary_currency=currency,
                salary_text=salary_text,
                posted_at=parse_timestamp(entry.get("publication_date")),
                remote=True,
                tags=tags,
                company_logo=str(entry.get("company_logo") or ""),
            )
        )
    return jobs
