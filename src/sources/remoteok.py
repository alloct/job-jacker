"""Remote OK, via its free public API: https://remoteok.com/api

The response begins with a legal/terms object rather than a job, which is skipped.
Remote OK's API terms ask that listings link back to the posting on Remote OK and
name Remote OK as the source; notifications do both (the embed title links to the
Remote OK URL and the "Job board" field names it).
"""

from __future__ import annotations

import logging
from typing import Sequence

from ..http_client import NotModified
from ..models import Job, html_to_text, parse_timestamp
from . import Source

log = logging.getLogger(__name__)

API_URL = "https://remoteok.com/api"


class RemoteOkSource(Source):
    board = "remoteok"
    options = frozenset()

    @property
    def name(self) -> str:
        return "Remote OK"

    def fetch(self, terms: Sequence[str]) -> list[Job]:
        try:
            payload = self.client.get_json(API_URL, conditional=True)
        except NotModified:
            log.info("Remote OK is unchanged since the last check; not downloading it again")
            return []
        return parse_jobs(payload)


def parse_jobs(payload) -> list[Job]:
    if not isinstance(payload, list):
        return []
    jobs = []
    for entry in payload:
        if not isinstance(entry, dict) or "legal" in entry:
            continue
        title = str(entry.get("position") or "").strip()
        url = str(entry.get("url") or entry.get("apply_url") or "").strip()
        if not title or not url:
            continue
        # Remote OK uses 0 to mean "not disclosed".
        minimum = _positive(entry.get("salary_min"))
        maximum = _positive(entry.get("salary_max"))
        tags = tuple(str(tag) for tag in entry.get("tags") or [] if tag)
        jobs.append(
            Job(
                board="remoteok",
                external_id=str(entry.get("id")) if entry.get("id") else None,
                title=title,
                company=str(entry.get("company") or ""),
                location=str(entry.get("location") or ""),
                url=url,
                description=html_to_text(entry.get("description")),
                salary_min=minimum,
                salary_max=maximum,
                salary_currency="USD" if (minimum or maximum) else None,
                posted_at=parse_timestamp(entry.get("date") or entry.get("epoch")),
                remote=True,
                tags=tags,
                company_logo=str(entry.get("company_logo") or entry.get("logo") or ""),
            )
        )
    return jobs


def _positive(value) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None
