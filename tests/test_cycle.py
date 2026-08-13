"""The whole cycle, with fake sources: isolation of failures and no repeats."""

import tempfile
import unittest
from pathlib import Path

from src.config import Config, Filters, Search, SourceSettings
from src.main import clear_state, parse_args, run_cycle, select_sources
from src.models import Job
from src.notify import Notifier
from src.sources import Source
from src.state import Store
from tests.test_notify import FakeSession


def make_job(index: int = 1, **overrides) -> Job:
    values = {
        "board": "fake",
        "external_id": str(index),
        "title": "Security Analyst",
        "company": "Example Company",
        "location": "Toronto, Ontario",
        "url": f"https://example.com/jobs/{index}",
        "description": "Run the SIEM.",
    }
    values.update(overrides)
    return Job(**values)


class FakeSource(Source):
    board = "fake"

    def __init__(self, jobs=(), error=None, name="Fake"):
        self.jobs = list(jobs)
        self.error = error
        self._name = name
        self.enrich_calls = 0

    @property
    def name(self):
        return self._name

    def fetch(self, terms):
        if self.error:
            raise self.error
        return list(self.jobs)

    def enrich(self, job):
        self.enrich_calls += 1
        job.description = "Run the SIEM and the EDR."
        return True


def make_config(**overrides) -> Config:
    values = {
        "interval_minutes": 60,
        "webhook_url": "https://discord.com/api/webhooks/1/token",
        "notify_on_first_run": True,
        "searches": (Search(name="Test", titles=Filters(include=("Security Analyst",))),),
    }
    values.update(overrides)
    return Config(**values)


class CycleTests(unittest.TestCase):
    def setUp(self):
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.store = Store(Path(self._directory.name) / "state.sqlite3")
        self.addCleanup(self.store.close)
        self.session = FakeSession([_ok() for _ in range(20)])
        self.notifier = Notifier("https://discord.com/api/webhooks/1/token", self.session)

    def sent_titles(self):
        return [
            embed["title"]
            for _url, payload in self.session.calls
            for embed in payload.get("embeds", [])
        ]

    def test_matching_jobs_are_sent_once_and_never_again(self):
        source = FakeSource([make_job(1), make_job(2, title="Truck Driver")])
        cfg = make_config()

        self.assertTrue(run_cycle(cfg, [source], self.store, self.notifier))
        self.assertEqual(self.sent_titles(), ["Security Analyst"])

        self.session.calls.clear()
        self.assertTrue(run_cycle(cfg, [source], self.store, self.notifier))
        self.assertEqual(self.sent_titles(), [])

    def test_state_survives_a_restart(self):
        source = FakeSource([make_job(1)])
        cfg = make_config(state_path=self.store.path)
        run_cycle(cfg, [source], self.store, self.notifier)
        self.store.close()

        self.session.calls.clear()
        with Store(self.store.path) as reopened:
            run_cycle(cfg, [source], reopened, self.notifier)
        self.assertEqual(self.sent_titles(), [])

    def test_one_broken_source_does_not_stop_the_others(self):
        broken = FakeSource(error=RuntimeError("HTTP 403 from example.com"), name="Broken")
        working = FakeSource([make_job(1)], name="Working")
        with self.assertLogs("jobjacker", level="ERROR") as logs:
            delivered_everything = run_cycle(
                make_config(), [broken, working], self.store, self.notifier
            )
        self.assertTrue(delivered_everything)
        self.assertEqual(self.sent_titles(), ["Security Analyst"])
        self.assertIn("Broken", " ".join(logs.output))
        self.assertIn("403", " ".join(logs.output))

    def test_undelivered_jobs_are_retried_next_cycle(self):
        self.session.responses = [_fail(), _ok()]
        source = FakeSource([make_job(1)])
        cfg = make_config()

        with self.assertLogs("src.notify", level="ERROR"), self.assertLogs("jobjacker", "WARNING"):
            self.assertFalse(run_cycle(cfg, [source], self.store, self.notifier))
        # Discord refused it, so it must not be recorded as sent.
        self.assertTrue(self.store.is_empty())

        self.assertTrue(run_cycle(cfg, [source], self.store, self.notifier))
        self.assertEqual(self.sent_titles(), ["Security Analyst", "Security Analyst"])
        self.assertEqual(self.store.count(), 1)

    def test_first_run_records_without_flooding_the_channel(self):
        source = FakeSource([make_job(index) for index in range(5)])
        cfg = make_config(notify_on_first_run=False)
        run_cycle(cfg, [source], self.store, self.notifier)
        self.assertEqual(self.sent_titles(), [])
        self.assertIn("watching", self.session.calls[0][1]["content"].lower())
        self.assertEqual(self.store.count(), 5)

    def test_details_are_only_fetched_for_new_matching_jobs(self):
        source = FakeSource([make_job(1), make_job(2, title="Chef")])
        cfg = make_config()
        run_cycle(cfg, [source], self.store, self.notifier)
        self.assertEqual(source.enrich_calls, 1)  # not the chef, and not twice

        source.enrich_calls = 0
        run_cycle(cfg, [source], self.store, self.notifier)
        self.assertEqual(source.enrich_calls, 0)  # already sent, so no more requests

    def test_details_can_change_a_decision(self):
        """A keyword filter is only truly testable once the description arrives."""
        source = FakeSource([make_job(1)])
        cfg = make_config(
            searches=(
                Search(
                    name="Test",
                    titles=Filters(include=("Security Analyst",)),
                    keywords=Filters(exclude=("EDR",)),
                ),
            )
        )
        run_cycle(cfg, [source], self.store, self.notifier)
        self.assertEqual(self.sent_titles(), [])

    def test_dry_run_leaves_the_state_file_untouched(self):
        source = FakeSource([make_job(1)])
        notifier = Notifier("https://discord.com/api/webhooks/1/token", self.session, dry_run=True)
        with self.assertLogs("src.notify", level="INFO"):
            run_cycle(make_config(), [source], self.store, notifier)
        self.assertEqual(self.session.calls, [])
        self.assertTrue(self.store.is_empty())

    def test_dry_run_shows_matches_even_on_a_fresh_state_file(self):
        """A dry run records nothing, so it must not use the quiet first-run path."""
        source = FakeSource([make_job(1)])
        notifier = Notifier("https://discord.com/api/webhooks/1/token", self.session, dry_run=True)
        cfg = make_config(notify_on_first_run=False)
        with self.assertLogs("src.notify", level="INFO") as logs:
            run_cycle(cfg, [source], self.store, notifier)
        self.assertIn("would send", " ".join(logs.output))

    def test_a_source_returning_nothing_is_fine(self):
        self.assertTrue(run_cycle(make_config(), [FakeSource([])], self.store, self.notifier))
        self.assertEqual(self.sent_titles(), [])

    def test_malformed_jobs_are_ignored_rather_than_crashing(self):
        titleless = Job(board="fake", title="", company="", url="https://example.com/x")
        linkless = Job(board="fake", title="Security Analyst", company="", url="")
        source = FakeSource([titleless, linkless, make_job(1)])
        self.assertTrue(run_cycle(make_config(), [source], self.store, self.notifier))
        self.assertEqual(self.sent_titles(), ["Security Analyst"])


class CommandLineTests(unittest.TestCase):
    def test_only_narrows_the_boards_that_run(self):
        configured = (
            SourceSettings(board="linkedin", options={}),
            SourceSettings(board="rss", options={}),
            SourceSettings(board="remotive", options={}),
        )
        chosen = select_sources(configured, ["LinkedIn", " rss "])
        self.assertEqual([source.board for source in chosen], ["linkedin", "rss"])

    def test_without_only_every_board_runs(self):
        configured = (SourceSettings(board="linkedin", options={}),)
        self.assertEqual(select_sources(configured, None), configured)

    def test_a_board_that_is_not_configured_selects_nothing(self):
        configured = (SourceSettings(board="linkedin", options={}),)
        self.assertEqual(select_sources(configured, ["indeed"]), ())


class ClearStateTests(unittest.TestCase):
    def setUp(self):
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.store = Store(Path(self._directory.name) / "state.sqlite3")
        self.addCleanup(self.store.close)
        self.store.mark_seen([make_job(1)])
        self.store.set_validators("https://example.com/feed", '"etag-1"', None)

    def test_both_flags_empty_both_tables(self):
        args = parse_args(["--forget-jobs", "--clear-http-cache"])
        with self.assertLogs("jobjacker", level="INFO"):
            self.assertEqual(clear_state(args, self.store), 0)
        self.assertTrue(self.store.is_empty())
        self.assertEqual(self.store.get_validators("https://example.com/feed"), (None, None))

    def test_forgetting_jobs_keeps_the_http_cache(self):
        with self.assertLogs("jobjacker", level="INFO"):
            clear_state(parse_args(["--forget-jobs"]), self.store)
        self.assertTrue(self.store.is_empty())
        self.assertEqual(self.store.get_validators("https://example.com/feed"), ('"etag-1"', None))

    def test_clearing_the_http_cache_keeps_the_sent_jobs(self):
        with self.assertLogs("jobjacker", level="INFO"):
            clear_state(parse_args(["--clear-http-cache"]), self.store)
        self.assertEqual(self.store.count(), 1)
        self.assertEqual(self.store.get_validators("https://example.com/feed"), (None, None))

    def test_a_dry_run_clears_nothing(self):
        args = parse_args(["--forget-jobs", "--clear-http-cache", "--dry-run"])
        with self.assertLogs("jobjacker", level="INFO") as logs:
            clear_state(args, self.store)
        self.assertIn("left alone", " ".join(logs.output))
        self.assertEqual(self.store.count(), 1)


def _ok():
    from tests.test_notify import FakeResponse

    return FakeResponse(status_code=204)


def _fail():
    from tests.test_notify import FakeResponse

    return FakeResponse(status_code=500, text="server error")


if __name__ == "__main__":
    unittest.main()
