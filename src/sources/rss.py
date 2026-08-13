"""Any job board that publishes an RSS or Atom feed.

Feeds are published for exactly this kind of polling, so they are the best thing to
read when a board offers one. Configure any number of them:

    - board: rss
      feeds:
        - name: We Work Remotely
          url: https://weworkremotely.com/remote-jobs.rss

Some feeds add their own elements (We Work Remotely publishes <type> and
<region>, for instance). Those are read when present and ignored when absent.
"""

from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ElementTree
from typing import Sequence

from ..config import ConfigError
from ..http_client import FetchError, NotModified
from ..models import Job, html_to_text, normalize_employment_type, parse_timestamp
from . import Source

log = logging.getLogger(__name__)

ATOM = "{http://www.w3.org/2005/Atom}"
_SLUG = re.compile(r"[^a-z0-9]+")


class RssSource(Source):
    board = "rss"
    options = frozenset({"feeds"})

    def configure(self) -> None:
        raw_feeds = self._options.get("feeds")
        if not isinstance(raw_feeds, list) or not raw_feeds:
            self._fail("feeds", "is required and must be a list of {name, url} entries")
        self.feeds = []
        for index, entry in enumerate(raw_feeds):
            if not isinstance(entry, dict):
                raise ConfigError(
                    f"{self._path}.feeds[{index}] must be a mapping with 'name' and 'url'"
                )
            url = str(entry.get("url") or "").strip()
            name = str(entry.get("name") or "").strip()
            if not url.startswith(("http://", "https://")):
                raise ConfigError(f"{self._path}.feeds[{index}].url must be an http(s) URL")
            unknown = set(entry) - {"name", "url"}
            if unknown:
                raise ConfigError(
                    f"{self._path}.feeds[{index}] has unknown key(s): {', '.join(sorted(unknown))}"
                )
            self.feeds.append((name or _host(url), url))

    @property
    def name(self) -> str:
        return f"RSS ({', '.join(name for name, _ in self.feeds)})"

    def fetch(self, terms: Sequence[str]) -> list[Job]:
        jobs: list[Job] = []
        failures = []
        for name, url in self.feeds:
            try:
                response = self.client.get(url, conditional=True)
            except NotModified:
                log.info("Feed %s is unchanged since the last check", name)
                continue
            except FetchError as exc:
                failures.append(f"{name}: {exc}")
                continue
            try:
                jobs.extend(parse_feed(response.content, name))
            except ElementTree.ParseError as exc:
                failures.append(f"{name}: feed is not valid XML ({exc})")

        for failure in failures:
            log.warning("Feed failed for %s", failure)
        if failures and len(failures) == len(self.feeds):
            raise FetchError("every configured feed failed")
        return jobs


def parse_feed(content: bytes, feed_name: str) -> list[Job]:
    """Parse RSS 2.0 or Atom into jobs. Raises ElementTree.ParseError on bad XML."""
    root = ElementTree.fromstring(content)
    entries = root.findall("./channel/item") or root.findall(f"./{ATOM}entry")
    slug = _SLUG.sub("-", feed_name.lower()).strip("-") or "feed"

    jobs = []
    for entry in entries:
        job = _parse_entry(entry, feed_name, slug)
        if job is not None:
            jobs.append(job)
    return jobs


def _parse_entry(entry, feed_name: str, slug: str) -> Job | None:
    raw_title = _text(entry, "title") or _text(entry, f"{ATOM}title")
    url = _text(entry, "link") or _link_from_atom(entry)
    if not raw_title or not url:
        return None

    # Feeds commonly encode the title as "Company: Job Title".
    company = _text(entry, "company") or _text(entry, "{http://purl.org/dc/elements/1.1/}creator")
    title = raw_title
    if not company and ": " in raw_title:
        company, title = (part.strip() for part in raw_title.split(": ", 1))

    description = html_to_text(
        _text(entry, "description") or _text(entry, f"{ATOM}summary") or _text(entry, f"{ATOM}content")
    )
    location = _text(entry, "region") or _text(entry, "location") or _text(entry, "state")
    guid = _text(entry, "guid") or _text(entry, f"{ATOM}id")
    category = _text(entry, "category") or _text(entry, "skills")

    return Job(
        board=f"rss:{slug}",
        board_label=feed_name,
        external_id=guid or None,
        title=title,
        company=company,
        location=location,
        url=url,
        description=description,
        employment_type=normalize_employment_type(_text(entry, "type")),
        posted_at=parse_timestamp(
            _text(entry, "pubDate") or _text(entry, f"{ATOM}updated") or _text(entry, f"{ATOM}published")
        ),
        remote="remote" in f"{location} {raw_title}".lower() or "anywhere" in location.lower(),
        tags=(category,) if category else (),
    )


def _link_from_atom(entry) -> str:
    for link in entry.findall(f"{ATOM}link"):
        if link.get("rel") in (None, "alternate") and link.get("href"):
            return link.get("href").strip()
    return ""


def _text(entry, tag: str) -> str:
    node = entry.find(tag)
    if node is None:
        return ""
    return " ".join((node.text or "").split())


def _host(url: str) -> str:
    try:
        return url.split("//", 1)[1].split("/", 1)[0]
    except IndexError:
        return url
