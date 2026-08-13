"""Persistent state: which jobs were already sent, plus HTTP cache validators.

SQLite is in the standard library and tolerates a process being killed mid-write,
which is all this needs.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

log = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sent_jobs (
    fingerprint TEXT PRIMARY KEY,
    board       TEXT NOT NULL DEFAULT '',
    title       TEXT NOT NULL DEFAULT '',
    company     TEXT NOT NULL DEFAULT '',
    url         TEXT NOT NULL DEFAULT '',
    sent_at     TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS http_cache (
    url           TEXT PRIMARY KEY,
    etag          TEXT,
    last_modified TEXT
);
"""


class Store:
    """Thin wrapper over a single SQLite file. Use it from one thread only."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        if self.path.parent and str(self.path.parent) not in ("", "."):
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = self._connect()

    def _connect(self) -> sqlite3.Connection:
        try:
            return self._open()
        except sqlite3.DatabaseError as exc:
            # A truncated or corrupted file should not stop the watcher forever.
            # The cost of starting over is re-announcing jobs, not losing data.
            log.error("State file %s is unusable (%s); starting a fresh one", self.path, exc)
        self._quarantine()
        return self._open()

    def _open(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript(_SCHEMA)
            connection.commit()
        except sqlite3.DatabaseError:
            # Closing matters on Windows, where an open handle blocks the rename below.
            connection.close()
            raise
        return connection

    def _quarantine(self) -> None:
        """Move the unreadable file aside, or delete it if it cannot be moved."""
        if not self.path.exists():
            return
        backup = self.path.with_name(self.path.name + ".corrupt")
        try:
            backup.unlink(missing_ok=True)
            self.path.replace(backup)
            log.warning("Moved the unreadable state file aside to %s", backup.name)
            return
        except OSError as exc:
            log.warning("Could not move the unreadable state file aside (%s)", exc)
        try:
            self.path.unlink()
        except OSError as exc:
            raise sqlite3.DatabaseError(
                f"Cannot read or replace the state file {self.path}: {exc}. "
                "Delete it manually, or point state.path somewhere writable."
            ) from exc

    def close(self) -> None:
        try:
            self.connection.close()
        except sqlite3.Error:
            pass

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    def is_empty(self) -> bool:
        """True on the very first run, which suppresses the initial notification flood."""
        row = self.connection.execute("SELECT 1 FROM sent_jobs LIMIT 1").fetchone()
        return row is None

    def has_seen(self, fingerprint: str) -> bool:
        row = self.connection.execute(
            "SELECT 1 FROM sent_jobs WHERE fingerprint = ?", (fingerprint,)
        ).fetchone()
        return row is not None

    def mark_seen(self, jobs) -> int:
        """Record jobs as handled. Called only after a successful send."""
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        rows = [
            (job.fingerprint(), job.board, job.title[:200], job.company[:200], job.url[:500], now)
            for job in jobs
        ]
        if not rows:
            return 0
        with self.connection:
            self.connection.executemany(
                "INSERT OR IGNORE INTO sent_jobs "
                "(fingerprint, board, title, company, url, sent_at) VALUES (?, ?, ?, ?, ?, ?)",
                rows,
            )
        return len(rows)

    def prune(self, retention_days: int) -> int:
        """Forget old entries so the file does not grow without limit."""
        cutoff = (datetime.now(timezone.utc) - timedelta(days=retention_days)).isoformat(
            timespec="seconds"
        )
        with self.connection:
            cursor = self.connection.execute("DELETE FROM sent_jobs WHERE sent_at < ?", (cutoff,))
        return cursor.rowcount or 0

    def count(self) -> int:
        return int(self.connection.execute("SELECT COUNT(*) FROM sent_jobs").fetchone()[0])

    def forget_all(self) -> int:
        """Empty the sent-job record, so everything still open counts as new again."""
        with self.connection:
            cursor = self.connection.execute("DELETE FROM sent_jobs")
        return cursor.rowcount or 0

    def get_validators(self, url: str) -> tuple[str | None, str | None]:
        row = self.connection.execute(
            "SELECT etag, last_modified FROM http_cache WHERE url = ?", (url,)
        ).fetchone()
        return (row[0], row[1]) if row else (None, None)

    def set_validators(self, url: str, etag: str | None, last_modified: str | None) -> None:
        if not etag and not last_modified:
            return
        with self.connection:
            self.connection.execute(
                "INSERT INTO http_cache (url, etag, last_modified) VALUES (?, ?, ?) "
                "ON CONFLICT(url) DO UPDATE SET etag = excluded.etag, "
                "last_modified = excluded.last_modified",
                (url, etag, last_modified),
            )

    def clear_validators(self) -> int:
        """Force full refetching next cycle.

        Called after any cycle that failed to deliver something, so a cached 304
        can never hide a job we have not sent yet.
        """
        with self.connection:
            cursor = self.connection.execute("DELETE FROM http_cache")
        return cursor.rowcount or 0
