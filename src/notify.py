"""Discord webhook delivery.

Uses a plain incoming webhook, so no bot, token or gateway connection is needed.
The webhook URL is treated as a secret: it is never logged, and any error text
that might contain it is scrubbed.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone

import requests

from .matching import MatchResult
from .models import Job

log = logging.getLogger(__name__)

# Discord caps a message at 10 embeds and ~6000 characters across all of them.
EMBEDS_PER_MESSAGE = 5
MAX_MESSAGE_CHARACTERS = 5500
SECONDS_BETWEEN_MESSAGES = 1.0

BOARD_COLORS = {
    "linkedin": 0x0A66C2,
    "greenhouse": 0x24A47F,
    "lever": 0x5F5FFF,
    "remotive": 0x00B4A0,
    "remoteok": 0xE84A5F,
    "adzuna": 0xF57C00,
    "rss": 0xF26522,
}
DEFAULT_COLOR = 0x5865F2


class Notifier:
    def __init__(self, webhook_url: str, session: requests.Session, dry_run: bool = False) -> None:
        self._webhook_url = webhook_url
        self.session = session
        self.dry_run = dry_run

    def redact(self, text: str) -> str:
        """Remove the webhook URL (and its token) from arbitrary text."""
        if not text:
            return ""
        cleaned = text.replace(self._webhook_url, "<webhook>")
        token = self._webhook_url.rsplit("/", 1)[-1]
        if token:
            cleaned = cleaned.replace(token, "<token>")
        return cleaned

    def send(self, matches) -> list:
        """Send (job, match) pairs. Returns the pairs that were delivered.

        Anything not returned was not delivered and therefore stays un-deduplicated,
        so the next cycle retries it.
        """
        delivered: list = []
        for batch in _batches(matches):
            payload = {"embeds": [build_embed(job, match) for job, match in batch]}
            if self.dry_run:
                for job, match in batch:
                    log.info(
                        "[dry-run] would send: %s at %s (%s) score=%d %s",
                        job.title,
                        job.company,
                        job.board,
                        match.score,
                        job.url,
                    )
                log.debug("[dry-run] payload: %s", json.dumps(payload, ensure_ascii=False))
                delivered.extend(batch)
                continue
            if self._post(payload):
                delivered.extend(batch)
            else:
                # Keep the remaining batches for the next cycle rather than
                # hammering a webhook that is currently failing.
                break
            time.sleep(SECONDS_BETWEEN_MESSAGES)
        return delivered

    def send_text(self, content: str) -> bool:
        if self.dry_run:
            log.info("[dry-run] would send message: %s", content)
            return True
        return self._post({"content": content[:1900]})

    def _post(self, payload: dict) -> bool:
        """One webhook request, with a single retry for rate limits."""
        for attempt in range(2):
            try:
                response = self.session.post(self._webhook_url, json=payload, timeout=20)
            except requests.RequestException as exc:
                log.error("Discord request failed: %s", self.redact(f"{type(exc).__name__}: {exc}"))
                return False

            if response.status_code in (200, 204):
                return True
            if response.status_code == 429 and attempt == 0:
                wait = _retry_after(response)
                log.warning("Discord rate limited us; waiting %.1fs", wait)
                time.sleep(wait)
                continue
            if response.status_code in (401, 403, 404):
                log.error(
                    "Discord rejected the webhook (HTTP %s). The webhook may have been "
                    "deleted or the URL is wrong.",
                    response.status_code,
                )
                return False
            log.error(
                "Discord returned HTTP %s: %s",
                response.status_code,
                self.redact(response.text[:300]),
            )
            return False
        return False


def _retry_after(response: requests.Response) -> float:
    for value in (response.headers.get("Retry-After"), _json_retry_after(response)):
        try:
            if value is not None:
                return min(max(float(value), 1.0), 60.0)
        except (TypeError, ValueError):
            continue
    return 5.0


def _json_retry_after(response: requests.Response):
    try:
        return response.json().get("retry_after")
    except (ValueError, AttributeError):
        return None


def _batches(matches):
    """Group matches into messages that stay inside Discord's size limits."""
    batch: list = []
    size = 0
    for job, match in matches:
        cost = len(job.title) + len(job.short_description()) + len(job.company) + 200
        if batch and (len(batch) >= EMBEDS_PER_MESSAGE or size + cost > MAX_MESSAGE_CHARACTERS):
            yield batch
            batch, size = [], 0
        batch.append((job, match))
        size += cost
    if batch:
        yield batch


def build_embed(job, match) -> dict:
    """Build one Discord embed for a matched job."""
    fields = [
        {"name": "Company", "value": _field(job.company), "inline": True},
        {"name": "Location", "value": _field(job.location), "inline": True},
    ]
    if job.employment_type:
        fields.append(
            {"name": "Employment", "value": job.employment_type.replace("-", " ").title(), "inline": True}
        )
    if job.salary_text:
        fields.append({"name": "Salary", "value": _field(job.salary_text), "inline": True})
    if job.posted_at:
        fields.append(
            {
                "name": "Posted",
                "value": f"<t:{int(job.posted_at.timestamp())}:R>",
                "inline": True,
            }
        )
    fields.append({"name": "Job board", "value": _board_label(job), "inline": True})

    description = job.short_description()
    embed = {
        "title": _truncate(job.title or "Untitled role", 256),
        "color": BOARD_COLORS.get(job.board.split(":", 1)[0], DEFAULT_COLOR),
        "fields": fields,
        "footer": {"text": f"{match.search_name} · score {match.score}"},
    }
    if job.url:
        embed["url"] = job.url
        description = f"{description}\n\n[**View job \u2192**]({job.url})" if description else f"[**View job \u2192**]({job.url})"
    if description:
        embed["description"] = _truncate(description, 2000)
    if job.company_logo.startswith("https://"):
        embed["thumbnail"] = {"url": job.company_logo}
    return embed


def send_test_message(notifier: Notifier) -> bool:
    """Post one made-up job so the webhook can be checked before relying on it.

    This is what `--test-webhook` sends. It doubles as a preview of the formatting.
    """
    job = Job(
        board="linkedin",
        title="Security Analyst (sample posting)",
        company="Example Company",
        location="Toronto, Ontario, Canada",
        url="https://example.com/jobs/sample",
        description=(
            "This is a test notification from Job Jacker. If you can read this, the "
            "webhook works and real matches will arrive looking like this."
        ),
        employment_type="full-time",
        salary_min=85000,
        salary_max=95000,
        salary_currency="CAD",
        posted_at=datetime.now(timezone.utc),
    )
    match = MatchResult(matched=True, score=7, search_name="webhook test")
    return bool(notifier.send([(job, match)]))


def _board_label(job) -> str:
    if job.board_label:
        return _field(job.board_label)
    return {
        "linkedin": "LinkedIn",
        "remoteok": "Remote OK",
    }.get(job.board, job.board.replace("_", " ").title())


def _field(value: str) -> str:
    return _truncate(value.strip(), 1024) or "Not specified"


def _truncate(text: str, limit: int) -> str:
    text = text or ""
    return text if len(text) <= limit else text[: limit - 1] + "\u2026"
