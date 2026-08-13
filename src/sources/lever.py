"""Lever job boards, via the official public postings API.

  https://api.lever.co/v0/postings/<company>?mode=json

The company handle is the one in its careers URL (jobs.lever.co/<company>).
Public, documented, no key required.
"""

from __future__ import annotations

import logging
from typing import Sequence

from ..http_client import FetchError, NotModified
from ..models import Job, html_to_text, normalize_employment_type, parse_timestamp
from . import Source

log = logging.getLogger(__name__)

API_URL = "https://api.lever.co/v0/postings/{company}"


class LeverSource(Source):
    board = "lever"
    options = frozenset({"companies"})

    def configure(self) -> None:
        self.companies = self.opt_list("companies")
        if not self.companies:
            self._fail("companies", "is required (e.g. companies: [leverdemo])")

    @property
    def name(self) -> str:
        return f"Lever ({', '.join(self.companies)})"

    def fetch(self, terms: Sequence[str]) -> list[Job]:
        jobs: list[Job] = []
        failures = []
        for company in self.companies:
            try:
                payload = self.client.get_json(
                    API_URL.format(company=company), params={"mode": "json"}, conditional=True
                )
            except NotModified:
                log.info("Lever board %s is unchanged since the last check", company)
                continue
            except FetchError as exc:
                failures.append(f"{company}: {exc}")
                continue
            jobs.extend(parse_jobs(payload, company))

        for failure in failures:
            log.warning("Lever board failed for %s", failure)
        if failures and len(failures) == len(self.companies):
            raise FetchError("every Lever board failed")
        return jobs


def parse_jobs(payload, company: str) -> list[Job]:
    if not isinstance(payload, list):
        return []
    jobs = []
    for entry in payload:
        if not isinstance(entry, dict):
            continue
        title = str(entry.get("text") or "").strip()
        url = str(entry.get("hostedUrl") or entry.get("applyUrl") or "").strip()
        if not title or not url:
            continue
        categories = entry.get("categories") if isinstance(entry.get("categories"), dict) else {}
        location = str(categories.get("location") or "")
        workplace = str(entry.get("workplaceType") or "")
        description = " ".join(
            part
            for part in (
                html_to_text(entry.get("descriptionPlain") or entry.get("description")),
                html_to_text(entry.get("additionalPlain") or entry.get("additional")),
            )
            if part
        )
        tags = tuple(
            str(categories.get(key))
            for key in ("department", "team")
            if categories.get(key)
        )
        jobs.append(
            Job(
                board="lever",
                board_label=f"Lever · {company}",
                external_id=f"{company}:{entry.get('id')}" if entry.get("id") else None,
                title=title,
                company=company,
                location=location,
                url=url,
                description=description,
                employment_type=normalize_employment_type(categories.get("commitment")),
                posted_at=parse_timestamp(entry.get("createdAt")),
                remote=workplace.lower() == "remote" or "remote" in location.lower(),
                tags=tags,
            )
        )
    return jobs
