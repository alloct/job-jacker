"""Adzuna, via its official API: https://developer.adzuna.com

Adzuna aggregates listings from a lot of employers and job boards, so it covers
similar ground to Indeed but has an API you are allowed to use. It needs a free
application id and key:

    - board: adzuna
      country: ca
      app_id: "${ADZUNA_APP_ID}"
      app_key: "${ADZUNA_APP_KEY}"

Adzuna guesses at a salary when the employer did not publish one. Those are flagged
as estimates, so a salary filter never rejects a job based on the guess.
"""

from __future__ import annotations

import logging
from typing import Sequence

from ..http_client import FetchError
from ..models import Job, html_to_text, normalize_employment_type, parse_timestamp
from . import Source

log = logging.getLogger(__name__)

API_URL = "https://api.adzuna.com/v1/api/jobs/{country}/search/1"
COUNTRIES = frozenset(
    "gb us at au be br ca ch de es fr in it mx nl nz pl ru sg za".split()
)


class AdzunaSource(Source):
    board = "adzuna"
    options = frozenset(
        {"country", "app_id", "app_key", "where", "distance_km", "max_days_old", "results_per_query", "max_queries"}
    )

    def configure(self) -> None:
        self.country = self.opt_str("country", "ca").lower()
        if self.country not in COUNTRIES:
            self._fail("country", f"must be one of: {', '.join(sorted(COUNTRIES))}")
        self.app_id = self.opt_str("app_id", required=True)
        self.app_key = self.opt_str("app_key", required=True)
        self.where = self.opt_list("where", [""])
        self.distance_km = self.opt_int("distance_km", 0, minimum=0, maximum=500)
        self.max_days_old = self.opt_int("max_days_old", 7, minimum=1, maximum=90)
        self.results_per_query = self.opt_int("results_per_query", 50, minimum=1, maximum=50)
        self.max_queries = self.opt_int("max_queries", 8, minimum=1, maximum=40)

    @property
    def name(self) -> str:
        return f"Adzuna ({self.country.upper()})"

    def fetch(self, terms: Sequence[str]) -> list[Job]:
        if not terms:
            log.warning("Adzuna needs search terms; add titles.include to a search block")
            return []

        jobs: dict[str, Job] = {}
        queries = [(term, where) for where in self.where for term in terms]
        if len(queries) > self.max_queries:
            # Skipping searches silently is how jobs go missing without explanation.
            log.warning(
                "Adzuna: %d titles across %d place(s) needs %d searches, but max_queries is "
                "%d, so %d are skipped this cycle. Raise max_queries, or use fewer places.",
                len(terms),
                len(self.where),
                len(queries),
                self.max_queries,
                len(queries) - self.max_queries,
            )
            queries = queries[: self.max_queries]
        failures = 0
        for term, where in queries:
            params = {
                "app_id": self.app_id,
                "app_key": self.app_key,
                "results_per_page": self.results_per_query,
                "what": term,
                "max_days_old": self.max_days_old,
                "sort_by": "date",
                "content-type": "application/json",
            }
            if where:
                params["where"] = where
                if self.distance_km:
                    params["distance"] = self.distance_km
            try:
                payload = self.client.get_json(API_URL.format(country=self.country), params=params)
            except FetchError as exc:
                failures += 1
                log.warning("Adzuna search for %r failed: %s", term, exc)
                continue
            for job in parse_jobs(payload, self.country):
                jobs.setdefault(job.fingerprint(), job)

        if failures and failures == len(queries):
            raise FetchError("every Adzuna search failed")
        return list(jobs.values())


def parse_jobs(payload, country: str) -> list[Job]:
    if not isinstance(payload, dict):
        return []
    jobs = []
    for entry in payload.get("results") or []:
        if not isinstance(entry, dict):
            continue
        title = str(entry.get("title") or "").strip()
        url = str(entry.get("redirect_url") or "").strip()
        if not title or not url:
            continue
        company = ""
        if isinstance(entry.get("company"), dict):
            company = str(entry["company"].get("display_name") or "")
        location = ""
        if isinstance(entry.get("location"), dict):
            location = str(entry["location"].get("display_name") or "")
        employment = normalize_employment_type(
            entry.get("contract_time") or entry.get("contract_type")
        )
        category = ""
        if isinstance(entry.get("category"), dict):
            category = str(entry["category"].get("label") or "")

        jobs.append(
            Job(
                board="adzuna",
                external_id=str(entry.get("id")) if entry.get("id") else None,
                title=html_to_text(title),
                company=company,
                location=location,
                url=url,
                description=html_to_text(entry.get("description")),
                employment_type=employment,
                salary_min=_number(entry.get("salary_min")),
                salary_max=_number(entry.get("salary_max")),
                salary_currency=_CURRENCIES.get(country, ""),
                salary_is_estimate=str(entry.get("salary_is_predicted") or "0") == "1",
                posted_at=parse_timestamp(entry.get("created")),
                remote="remote" in f"{title} {location}".lower(),
                tags=(category,) if category else (),
            )
        )
    return jobs


_CURRENCIES = {
    "ca": "CAD",
    "us": "USD",
    "gb": "GBP",
    "au": "AUD",
    "nz": "NZD",
    "in": "INR",
    "za": "ZAR",
    "sg": "SGD",
    "ch": "CHF",
    "pl": "PLN",
    "br": "BRL",
    "mx": "MXN",
    "ru": "RUB",
}


def _number(value) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None
