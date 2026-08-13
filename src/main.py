"""Command line entry point and the monitoring loop.

    python -m src.main --run-once --verbose

The cycle runs start to finish in one line: fetch, filter, deduplicate, enrich the
survivors, filter again, notify, record. A failure in one source is logged and the
cycle continues with the others.
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import time
from pathlib import Path

import requests

from . import config as config_module
from . import sources as sources_module
from .config import ConfigError
from .http_client import HttpClient
from .matching import best_match
from .notify import Notifier, send_test_message
from .state import Store

log = logging.getLogger("jobjacker")


class _Formatter(logging.Formatter):
    """[12:00:01] plain for normal output, with the level shown when it matters."""

    def format(self, record: logging.LogRecord) -> str:
        prefix = "" if record.levelno == logging.INFO else f"{record.levelname}: "
        return f"[{self.formatTime(record, '%H:%M:%S')}] {prefix}{record.getMessage()}"


def setup_logging(level: str, log_file: Path | None = None) -> None:
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(getattr(logging, level, logging.INFO))
    # Job titles contain dashes, accents and currency symbols. Without this, a
    # Windows console using a legacy code page mangles or refuses them.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if log_file:
        try:
            log_file.parent.mkdir(parents=True, exist_ok=True)
            handlers.append(logging.FileHandler(log_file, encoding="utf-8"))
        except OSError as exc:
            print(f"Warning: cannot write to log file {log_file}: {exc}", file=sys.stderr)
    for handler in handlers:
        handler.setFormatter(_Formatter())
        root.addHandler(handler)
    # urllib3 logs one line per connection at DEBUG, which drowns everything else.
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m src.main",
        description="Watch job boards and send matching postings to a Discord webhook.",
    )
    parser.add_argument("-c", "--config", default="config.yaml", help="path to the config file")
    parser.add_argument(
        "--run-once", action="store_true", help="run a single cycle and exit (good for cron)"
    )
    parser.add_argument(
        "--test-config", action="store_true", help="validate the config and exit without fetching"
    )
    parser.add_argument(
        "--test-webhook",
        action="store_true",
        help="post a sample job to Discord to check the webhook, then exit",
    )
    parser.add_argument(
        "--only",
        metavar="BOARD",
        action="append",
        help="check just this board, ignoring the others (repeatable), e.g. --only linkedin",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="log the jobs that would be sent instead of sending them, and leave the state file alone",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="log debug detail")
    return parser.parse_args(argv)


def select_sources(sources, only):
    """Narrow the configured sources to the boards named with --only."""
    if not only:
        return tuple(sources)
    wanted = {name.strip().lower() for name in only}
    return tuple(source for source in sources if source.board in wanted)


def run_cycle(cfg, sources, store: Store, notifier: Notifier) -> bool:
    """Run one full check.

    Returns False when a matched job was not delivered, which tells the caller to
    forget the HTTP cache so the next cycle cannot miss it. A source failing is
    logged but does not affect this: a failed fetch never stores a cache entry.
    """
    dry_run = notifier.dry_run
    log.info("Starting job check")
    terms = cfg.query_terms()
    fetched: list[tuple[object, list]] = []
    failed_sources = 0

    for source in sources:
        try:
            jobs = source.fetch(terms)
        except Exception as exc:  # noqa: BLE001 - one bad board must not end the cycle
            failed_sources += 1
            log.error("%s: no results (%s)", source.name, exc)
            log.debug("%s failed", source.name, exc_info=True)
            continue
        log.info("%s: %d jobs discovered", source.name, len(jobs))
        fetched.append((source, jobs))

    if failed_sources:
        log.warning(
            "%d of %d sources failed this cycle; carrying on with the rest",
            failed_sources,
            len(sources),
        )
    discovered = sum(len(jobs) for _, jobs in fetched)

    # Cheap pass first: filter on the fields every board gives us for free.
    candidates = []
    for source, jobs in fetched:
        for job in jobs:
            if not job.is_usable:
                log.debug("Skipping an entry from %s with no title or link", source.name)
                continue
            result = best_match(job, cfg.searches)
            if result:
                candidates.append((source, job, result))
    log.info("%d of %d jobs matched your searches", len(candidates), discovered)

    unsent = [item for item in candidates if not store.has_seen(item[1].fingerprint())]
    duplicates = len(candidates) - len(unsent)
    if duplicates:
        log.info("%d already sent previously, skipping", duplicates)

    # A dry run records nothing, so there is nothing to protect against here, and
    # the matches are what the user asked to see.
    seeding = store.is_empty() and not cfg.notify_on_first_run and not dry_run
    if unsent and not seeding:
        _enrich(unsent)

    # Second pass, now that descriptions and employment types may have arrived.
    confirmed = []
    for _source, job, result in unsent:
        final = best_match(job, cfg.searches)
        if final:
            confirmed.append((job, final))
        else:
            log.debug("%s at %s dropped after reading its details", job.title, job.company)

    if not confirmed:
        log.info("No new jobs to send")
        _prune(store, cfg, dry_run)
        return True

    if seeding:
        log.info(
            "First run: recording %d existing matches without notifying, so you are not "
            "flooded. Set notify_on_first_run: true to change this.",
            len(confirmed),
        )
        if not dry_run:
            store.mark_seen([job for job, _ in confirmed])
            notifier.send_text(
                f"**Job Jacker is watching.** Recorded {len(confirmed)} existing matching "
                f"postings without notifying. You will hear about new ones from now on."
            )
        _prune(store, cfg, dry_run)
        return True

    delivered = notifier.send(confirmed)
    log.info(
        "%d new %s sent to Discord", len(delivered), "job" if len(delivered) == 1 else "jobs"
    )
    if len(delivered) < len(confirmed):
        log.warning(
            "%d job(s) could not be delivered and will be retried next cycle",
            len(confirmed) - len(delivered),
        )
    if not dry_run:
        store.mark_seen([job for job, _ in delivered])
    _prune(store, cfg, dry_run)
    return len(delivered) == len(confirmed)


def _enrich(unsent) -> None:
    """Let sources add detail-page data for jobs that are new and already matching."""
    for source, job, _result in unsent:
        try:
            source.enrich(job)
        except Exception as exc:  # noqa: BLE001
            log.debug("Could not enrich %s: %s", job.url, exc)


def _prune(store: Store, cfg, dry_run: bool) -> None:
    if dry_run:
        return
    removed = store.prune(cfg.retention_days)
    if removed:
        log.debug("Forgot %d job(s) older than %d days", removed, cfg.retention_days)


def describe(cfg, sources) -> None:
    print("Configuration is valid.\n")
    print(f"  Interval        : every {cfg.interval_minutes} minutes")
    print(f"  State file      : {cfg.state_path}")
    print(f"  Discord webhook : {'set' if cfg.webhook_url else 'not set'}")
    print(f"  Sources ({len(sources)}):")
    for source in sources:
        print(f"      - {source.name}")
    print(f"  Searches ({len(cfg.searches)}):")
    for search in cfg.searches:
        parts = []
        for label, group in (
            ("titles", search.titles),
            ("keywords", search.keywords),
            ("companies", search.companies),
            ("locations", search.locations),
            ("types", search.employment_types),
        ):
            if group.include:
                parts.append(f"{label} +{len(group.include)}")
            if group.exclude:
                parts.append(f"{label} -{len(group.exclude)}")
        print(f"      - {search.name}: {', '.join(parts) or 'no filters'}")
    terms = cfg.query_terms()
    if terms:
        print(f"  Search-based boards will query for: {', '.join(terms)}")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    setup_logging("DEBUG" if args.verbose else "INFO")

    try:
        cfg = config_module.load(args.config, require_webhook=not args.dry_run)
    except ConfigError as exc:
        log.error("Configuration problem: %s", exc)
        return 1

    setup_logging("DEBUG" if args.verbose else cfg.log_level, cfg.log_file)

    session = requests.Session()
    session.headers.update({"User-Agent": cfg.http.user_agent})
    notifier = Notifier(cfg.webhook_url, session, dry_run=args.dry_run)

    try:
        if args.test_webhook:
            log.info("Checking the Discord webhook with a sample job")
            if not send_test_message(notifier):
                log.error("The sample job was not delivered; see the error above")
                return 1
            if not args.dry_run:
                log.info("Sent. Look in the channel the webhook points at.")
            return 0

        selected = select_sources(cfg.sources, args.only)
        if not selected:
            log.error(
                "--only %s matched none of the configured boards. Configured: %s",
                ", ".join(args.only),
                ", ".join(sorted({source.board for source in cfg.sources})),
            )
            return 1

        with Store(cfg.state_path) as store:
            client = HttpClient(
                user_agent=cfg.http.user_agent,
                timeout_seconds=cfg.http.timeout_seconds,
                delay_seconds=cfg.http.delay_seconds,
                max_retries=cfg.http.max_retries,
                # A dry run must leave the state file completely alone, which
                # includes not remembering ETags it would otherwise reuse.
                validators=None if args.dry_run else store,
            )
            try:
                built = sources_module.build_all(selected, client)
            except ConfigError as exc:
                log.error("Configuration problem: %s", exc)
                return 1

            if args.test_config:
                describe(cfg, built)
                return 0

            log.info(
                "Job Jacker started: %d source(s), %d search(es), checking every %d minutes"
                "%s",
                len(built),
                len(cfg.searches),
                cfg.interval_minutes,
                " (dry run)" if args.dry_run else "",
            )
            log.debug("State file holds %d previously sent job(s)", store.count())

            return _loop(cfg, built, store, notifier, client, args)
    finally:
        session.close()


def _loop(cfg, built, store, notifier, client, args) -> int:
    _install_signal_handlers()
    try:
        while True:
            started = time.monotonic()
            try:
                delivered_everything = run_cycle(cfg, built, store, notifier)
            except Exception:  # noqa: BLE001 - never let one bad cycle end the process
                log.exception("Unexpected error during the check; will try again next cycle")
                delivered_everything = False
            if not delivered_everything and not args.dry_run:
                # Do not let a cached 304 hide something we failed to deliver.
                store.clear_validators()
            if args.run_once:
                return 0
            wait = max(cfg.interval_minutes * 60 - (time.monotonic() - started), 60)
            log.info("Next check in %s", _friendly_duration(wait))
            time.sleep(wait)
    except (KeyboardInterrupt, SystemExit):
        log.info("Stopping")
        return 0
    finally:
        client.close()


def _install_signal_handlers() -> None:
    """Make `docker stop` and Ctrl+C exit tidily instead of aborting mid-write."""

    def handler(_signum, _frame):
        raise SystemExit(0)

    for name in ("SIGTERM", "SIGINT"):
        value = getattr(signal, name, None)
        if value is not None:
            try:
                signal.signal(value, handler)
            except (ValueError, OSError):
                pass  # not the main thread, or unsupported on this platform


def _friendly_duration(seconds: float) -> str:
    minutes = int(round(seconds / 60))
    if minutes < 60:
        return f"{minutes} minute{'s' if minutes != 1 else ''}"
    hours, remainder = divmod(minutes, 60)
    if remainder:
        return f"{hours}h {remainder}m"
    return f"{hours} hour{'s' if hours != 1 else ''}"


if __name__ == "__main__":
    sys.exit(main())
