"""Discord payload construction, error handling and secret redaction."""

import json
import unittest
from datetime import datetime, timezone

from src.matching import MatchResult
from src.models import Job
from src.notify import EMBEDS_PER_MESSAGE, Notifier, build_embed

WEBHOOK = "https://discord.com/api/webhooks/123456789/SUPER-SECRET-TOKEN"


def make_job(**overrides) -> Job:
    values = {
        "board": "linkedin",
        "title": "Security Analyst",
        "company": "Example Company",
        "location": "Toronto, Ontario, Canada",
        "url": "https://ca.linkedin.com/jobs/view/security-analyst-1",
        "description": "Run the SIEM and handle incident response.",
        "employment_type": "full-time",
        "salary_min": 85000,
        "salary_max": 95000,
        "salary_currency": "CAD",
        "posted_at": datetime(2026, 8, 7, 12, 0, tzinfo=timezone.utc),
    }
    values.update(overrides)
    return Job(**values)


MATCH = MatchResult(matched=True, score=4, search_name="Cybersecurity")


class FakeResponse:
    def __init__(self, status_code=204, text="", headers=None, payload=None):
        self.status_code = status_code
        self.text = text
        self.headers = headers or {}
        self._payload = payload

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class FakeSession:
    def __init__(self, responses=None):
        self.responses = list(responses or [FakeResponse()])
        self.calls = []

    def post(self, url, json=None, timeout=None):
        self.calls.append((url, json))
        return self.responses.pop(0) if self.responses else FakeResponse()


class EmbedTests(unittest.TestCase):
    def test_embed_carries_the_useful_fields(self):
        embed = build_embed(make_job(), MATCH)
        self.assertEqual(embed["title"], "Security Analyst")
        self.assertEqual(embed["url"], "https://ca.linkedin.com/jobs/view/security-analyst-1")
        names = [field["name"] for field in embed["fields"]]
        self.assertEqual(
            names, ["Company", "Location", "Employment", "Salary", "Posted", "Job board"]
        )
        values = {field["name"]: field["value"] for field in embed["fields"]}
        self.assertEqual(values["Company"], "Example Company")
        self.assertEqual(values["Employment"], "Full Time")
        self.assertEqual(values["Salary"], "85,000–95,000 CAD")
        self.assertEqual(values["Job board"], "LinkedIn")
        self.assertIn("SIEM", embed["description"])
        self.assertIn("View job", embed["description"])
        self.assertIn("Cybersecurity", embed["footer"]["text"])

    def test_missing_optional_fields_are_left_out(self):
        job = make_job(
            employment_type=None,
            salary_min=None,
            salary_max=None,
            salary_currency=None,
            salary_text="",
            posted_at=None,
            description="",
        )
        embed = build_embed(job, MATCH)
        names = [field["name"] for field in embed["fields"]]
        self.assertEqual(names, ["Company", "Location", "Job board"])
        self.assertIn("View job", embed["description"])

    def test_empty_values_never_produce_an_empty_field(self):
        embed = build_embed(make_job(company="", location=""), MATCH)
        for field in embed["fields"]:
            self.assertTrue(field["value"].strip())

    def test_long_text_is_truncated_within_discord_limits(self):
        job = make_job(title="T" * 400, description="D" * 5000)
        embed = build_embed(job, MATCH)
        self.assertLessEqual(len(embed["title"]), 256)
        self.assertLessEqual(len(embed["description"]), 2000)
        for field in embed["fields"]:
            self.assertLessEqual(len(field["value"]), 1024)

    def test_feed_sources_are_named_by_their_feed(self):
        job = make_job(board="rss:we-work-remotely", board_label="We Work Remotely")
        values = {field["name"]: field["value"] for field in build_embed(job, MATCH)["fields"]}
        self.assertEqual(values["Job board"], "We Work Remotely")

    def test_the_payload_is_json_serialisable(self):
        json.dumps({"embeds": [build_embed(make_job(), MATCH)]})


class SendTests(unittest.TestCase):
    def test_jobs_are_sent_and_reported_as_delivered(self):
        session = FakeSession()
        notifier = Notifier(WEBHOOK, session)
        delivered = notifier.send([(make_job(), MATCH)])
        self.assertEqual(len(delivered), 1)
        url, payload = session.calls[0]
        self.assertEqual(url, WEBHOOK)
        self.assertEqual(len(payload["embeds"]), 1)

    def test_large_batches_are_split_across_messages(self):
        session = FakeSession([FakeResponse() for _ in range(10)])
        notifier = Notifier(WEBHOOK, session)
        matches = [(make_job(url=f"https://example.com/{index}"), MATCH) for index in range(12)]
        delivered = notifier.send(matches)
        self.assertEqual(len(delivered), 12)
        self.assertGreater(len(session.calls), 1)
        for _url, payload in session.calls:
            self.assertLessEqual(len(payload["embeds"]), EMBEDS_PER_MESSAGE)

    def test_a_failed_send_is_not_reported_as_delivered(self):
        session = FakeSession([FakeResponse(status_code=500, text="server error")])
        notifier = Notifier(WEBHOOK, session)
        with self.assertLogs("src.notify", level="ERROR"):
            self.assertEqual(notifier.send([(make_job(), MATCH)]), [])

    def test_a_deleted_webhook_is_reported_clearly(self):
        session = FakeSession([FakeResponse(status_code=404, text="unknown webhook")])
        notifier = Notifier(WEBHOOK, session)
        with self.assertLogs("src.notify", level="ERROR") as logs:
            notifier.send([(make_job(), MATCH)])
        self.assertIn("webhook", " ".join(logs.output).lower())

    def test_rate_limiting_is_retried_once(self):
        session = FakeSession(
            [FakeResponse(status_code=429, headers={"Retry-After": "1"}), FakeResponse()]
        )
        notifier = Notifier(WEBHOOK, session)
        with self.assertLogs("src.notify", level="WARNING"):
            delivered = notifier.send([(make_job(), MATCH)])
        self.assertEqual(len(delivered), 1)
        self.assertEqual(len(session.calls), 2)

    def test_a_network_error_does_not_propagate(self):
        import requests

        class BrokenSession:
            def post(self, *_args, **_kwargs):
                raise requests.ConnectionError(f"failed to reach {WEBHOOK}")

        notifier = Notifier(WEBHOOK, BrokenSession())
        with self.assertLogs("src.notify", level="ERROR") as logs:
            self.assertEqual(notifier.send([(make_job(), MATCH)]), [])
        self.assertNotIn("SUPER-SECRET-TOKEN", " ".join(logs.output))

    def test_dry_run_sends_nothing_but_reports_delivery(self):
        session = FakeSession()
        notifier = Notifier(WEBHOOK, session, dry_run=True)
        with self.assertLogs("src.notify", level="INFO"):
            delivered = notifier.send([(make_job(), MATCH)])
        self.assertEqual(len(delivered), 1)
        self.assertEqual(session.calls, [])

    def test_redaction_removes_the_url_and_the_token(self):
        notifier = Notifier(WEBHOOK, FakeSession())
        cleaned = notifier.redact(f"POST {WEBHOOK} failed; token SUPER-SECRET-TOKEN rejected")
        self.assertNotIn("SUPER-SECRET-TOKEN", cleaned)
        self.assertNotIn(WEBHOOK, cleaned)

    def test_error_body_from_discord_is_redacted(self):
        session = FakeSession([FakeResponse(status_code=400, text=f"bad request for {WEBHOOK}")])
        notifier = Notifier(WEBHOOK, session)
        with self.assertLogs("src.notify", level="ERROR") as logs:
            notifier.send([(make_job(), MATCH)])
        self.assertNotIn("SUPER-SECRET-TOKEN", " ".join(logs.output))


if __name__ == "__main__":
    unittest.main()
