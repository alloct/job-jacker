"""Deduplication: fingerprints and the state file that survives restarts."""

import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.models import Job
from src.state import Store


def make_job(**overrides) -> Job:
    values = {
        "board": "linkedin",
        "external_id": "4448789855",
        "title": "Security Analyst",
        "company": "Example Company",
        "url": "https://ca.linkedin.com/jobs/view/security-analyst-at-example-4448789855",
    }
    values.update(overrides)
    return Job(**values)


class FingerprintTests(unittest.TestCase):
    def test_same_job_gives_the_same_fingerprint(self):
        self.assertEqual(make_job().fingerprint(), make_job().fingerprint())

    def test_tracking_parameters_do_not_change_identity(self):
        plain = make_job(external_id=None)
        tracked = make_job(external_id=None, url=plain.url + "?refId=abc&trackingId=xyz&position=4")
        self.assertEqual(plain.fingerprint(), tracked.fingerprint())

    def test_different_jobs_differ(self):
        self.assertNotEqual(make_job().fingerprint(), make_job(external_id="999").fingerprint())

    def test_falls_back_to_attributes_when_there_is_no_id_or_url(self):
        job = make_job(external_id=None, url="")
        self.assertTrue(job.fingerprint())
        self.assertEqual(job.fingerprint(), make_job(external_id=None, url="").fingerprint())

    def test_id_survives_a_changed_url_slug(self):
        renamed = make_job(url="https://ca.linkedin.com/jobs/view/renamed-role-4448789855")
        self.assertEqual(make_job().fingerprint(), renamed.fingerprint())


class StoreTests(unittest.TestCase):
    def setUp(self):
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.path = Path(self._directory.name) / "nested" / "state.sqlite3"

    def test_a_job_is_only_new_once(self):
        job = make_job()
        with Store(self.path) as store:
            self.assertFalse(store.has_seen(job.fingerprint()))
            store.mark_seen([job])
            self.assertTrue(store.has_seen(job.fingerprint()))

    def test_state_survives_a_restart(self):
        job = make_job()
        with Store(self.path) as store:
            store.mark_seen([job])
        with Store(self.path) as store:
            self.assertTrue(store.has_seen(job.fingerprint()))
            self.assertFalse(store.is_empty())

    def test_marking_twice_does_not_raise(self):
        job = make_job()
        with Store(self.path) as store:
            store.mark_seen([job])
            store.mark_seen([job])
            self.assertEqual(store.count(), 1)

    def test_is_empty_only_before_anything_is_recorded(self):
        with Store(self.path) as store:
            self.assertTrue(store.is_empty())
            store.mark_seen([make_job()])
            self.assertFalse(store.is_empty())

    def test_pruning_forgets_old_entries_and_keeps_recent_ones(self):
        recent, ancient = make_job(), make_job(external_id="ancient")
        with Store(self.path) as store:
            store.mark_seen([recent, ancient])
            stale = (datetime.now(timezone.utc) - timedelta(days=200)).isoformat(timespec="seconds")
            with store.connection:
                store.connection.execute(
                    "UPDATE sent_jobs SET sent_at = ? WHERE fingerprint = ?",
                    (stale, ancient.fingerprint()),
                )
            self.assertEqual(store.prune(90), 1)
            self.assertTrue(store.has_seen(recent.fingerprint()))
            self.assertFalse(store.has_seen(ancient.fingerprint()))

    def test_forgetting_everything_empties_the_sent_record(self):
        with Store(self.path) as store:
            store.mark_seen([make_job(), make_job(external_id="other")])
            store.set_validators("https://example.com/feed", '"etag-1"', None)
            self.assertEqual(store.forget_all(), 2)
            self.assertTrue(store.is_empty())
            # Only the sent jobs go; cached validators are a separate concern.
            self.assertEqual(store.get_validators("https://example.com/feed"), ('"etag-1"', None))

    def test_forgetting_an_empty_record_is_harmless(self):
        with Store(self.path) as store:
            self.assertEqual(store.forget_all(), 0)

    def test_a_corrupt_state_file_is_replaced_instead_of_crashing(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_bytes(b"this is not a database" * 100)
        with self.assertLogs("src.state", level="ERROR"):
            store = Store(self.path)
        self.addCleanup(store.close)
        store.mark_seen([make_job()])
        self.assertEqual(store.count(), 1)
        self.assertTrue(self.path.with_name(self.path.name + ".corrupt").exists())

    def test_http_validators_round_trip_and_can_be_cleared(self):
        with Store(self.path) as store:
            store.set_validators("https://example.com/feed", '"etag-1"', "Wed, 21 Oct 2026 07:28:00 GMT")
            self.assertEqual(
                store.get_validators("https://example.com/feed"),
                ('"etag-1"', "Wed, 21 Oct 2026 07:28:00 GMT"),
            )
            store.mark_seen([make_job()])
            self.assertEqual(store.clear_validators(), 1)
            self.assertEqual(store.get_validators("https://example.com/feed"), (None, None))
            # Clearing the cache must not touch what has already been sent.
            self.assertEqual(store.count(), 1)

    def test_unknown_url_has_no_validators(self):
        with Store(self.path) as store:
            self.assertEqual(store.get_validators("https://example.com/nothing"), (None, None))

    def test_the_file_is_a_real_sqlite_database(self):
        with Store(self.path) as store:
            store.mark_seen([make_job()])
        connection = sqlite3.connect(self.path)
        try:
            tables = {
                row[0]
                for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
            }
        finally:
            connection.close()
        self.assertIn("sent_jobs", tables)


if __name__ == "__main__":
    unittest.main()
