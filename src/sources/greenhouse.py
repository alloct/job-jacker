"""Greenhouse job boards, via the official public boards API.

  https://boards-api.greenhouse.io/v1/boards/<token>/jobs?content=true

The token is the company's board name, visible in the URL of its careers page
(job-boards.greenhouse.io/<token>). This is a documented public API: no key, no
scraping and no rate-limit games.
"""

from __future__ import annotations

import logging
from typing import Sequence

from ..http_client import FetchError, NotModified
from ..models import Job, html_to_text, parse_timestamp
from . import Source

log = logging.getLogger(__name__)

API_URL = "https://boards-api.greenhouse.io/v1/boards/{token}/jobs"


class GreenhouseSource(Source):
    board = "greenhouse"
    options = frozenset({"companies", "include_description"})

    def configure(self) -> None:
        self.companies = self.opt_list("companies")
        if not self.companies:
            self._fail("companies", "is required (e.g. companies: [gitlab, stripe])")
        self.include_description = self.opt_bool("include_description", True)

    @property
    def name(self) -> str:
        return f"Greenhouse ({', '.join(self.companies)})"

    def fetch(self, terms: Sequence[str]) -> list[Job]:
        jobs: list[Job] = []
        failures = []
        for token in self.companies:
            try:
                payload = self.client.get_json(
                    API_URL.format(token=token),
                    params={"content": "true"} if self.include_description else None,
                    conditional=True,
                )
            except NotModified:
                log.info("Greenhouse board %s is unchanged since the last check", token)
                continue
            except FetchError as exc:
                failures.append(f"{token}: {exc}")
                continue
            jobs.extend(parse_jobs(payload, token))

        for failure in failures:
            log.warning("Greenhouse board failed for %s", failure)
        if failures and len(failures) == len(self.companies):
            raise FetchError("every Greenhouse board failed")
        return jobs


def parse_jobs(payload, token: str) -> list[Job]:
    if not isinstance(payload, dict):
        return []
    jobs = []
    for entry in payload.get("jobs") or []:
        if not isinstance(entry, dict):
            continue
        title = str(entry.get("title") or "").strip()
        url = str(entry.get("absolute_url") or "").strip()
        if not title or not url:
            continue
        location = ""
        if isinstance(entry.get("location"), dict):
            location = str(entry["location"].get("name") or "")
        departments = tuple(
            str(item.get("name"))
            for item in entry.get("departments") or []
            if isinstance(item, dict) and item.get("name")
        )
        jobs.append(
            Job(
                board="greenhouse",
                board_label=f"Greenhouse · {entry.get('company_name') or token}",
                external_id=f"{token}:{entry.get('id')}" if entry.get("id") else None,
                title=title,
                company=str(entry.get("company_name") or token),
                location=location,
                url=url,
                description=html_to_text(entry.get("content")),
                posted_at=parse_timestamp(entry.get("first_published") or entry.get("updated_at")),
                remote="remote" in location.lower(),
                tags=departments,
            )
        )
    return jobs
