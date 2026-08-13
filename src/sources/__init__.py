"""Job source adapters.

Every source turns one board's response into a list of `models.Job` objects and
nothing else. Keep board-specific HTML and JSON keys inside the board's own module,
so that a board changing its layout only affects one file here.

To add a board, write a module with a `Source` subclass and register it in BOARDS
at the bottom of this file. See README.md, "Adding a job source".
"""

from __future__ import annotations

import logging
from typing import Sequence

from ..config import ConfigError
from ..http_client import HttpClient
from ..models import Job

log = logging.getLogger(__name__)

# Boards that cannot be supported without defeating access controls. Listing them
# explicitly gives a clear answer instead of a mysterious 403.
UNSUPPORTED_BOARDS = {
    "indeed": (
        "Indeed serves HTTP 403 to any request that is not a real browser, on both "
        "its search pages and its old RSS endpoint. Monitoring it would mean "
        "defeating that bot protection, which this project does not do. Use the "
        "'adzuna' source for comparable aggregated listings via an official API."
    ),
    "glassdoor": (
        "Glassdoor requires an account and blocks unauthenticated access. There is "
        "no public listing endpoint to poll."
    ),
    "ziprecruiter": (
        "ZipRecruiter blocks unauthenticated listing requests and its partner API "
        "is not open to individuals."
    ),
}


class Source:
    """Base class for a configured board."""

    board = ""
    # Option keys this source accepts; anything else in the config is a typo.
    options: frozenset[str] = frozenset()

    def __init__(self, client: HttpClient, options: dict, path: str = "sources") -> None:
        self.client = client
        self._options = options or {}
        self._path = path
        unknown = set(self._options) - set(self.options)
        if unknown:
            raise ConfigError(
                f"{path} (board: {self.board}) has unknown option(s): "
                f"{', '.join(sorted(unknown))}. Valid options: "
                f"{', '.join(sorted(self.options)) or 'none'}"
            )
        self.configure()

    def configure(self) -> None:
        """Read and validate options. Called once at startup."""

    @property
    def name(self) -> str:
        """Label used in logs."""
        return self.board

    def fetch(self, terms: Sequence[str]) -> list[Job]:
        raise NotImplementedError

    def enrich(self, job: Job) -> bool:
        """Optionally add detail-page data to a job that already passed filtering.

        Returns True when the job was changed. Only called for jobs that are new
        and already look like a match, which keeps extra requests rare.
        """
        return False

    # Option readers -----------------------------------------------------

    def _fail(self, key: str, message: str):
        raise ConfigError(f"{self._path} (board: {self.board}) option '{key}' {message}")

    def opt_str(self, key: str, default: str = "", required: bool = False) -> str:
        value = self._options.get(key, default)
        if value is None:
            value = default
        if not isinstance(value, str):
            value = str(value)
        value = value.strip()
        if required and not value:
            self._fail(key, "is required")
        return value

    def opt_list(self, key: str, default: Sequence[str] = ()) -> tuple[str, ...]:
        value = self._options.get(key)
        if value is None:
            return tuple(default)
        if isinstance(value, str):
            value = [value]
        if not isinstance(value, list):
            self._fail(key, "must be a list")
        return tuple(str(item).strip() for item in value if str(item).strip())

    def opt_int(self, key: str, default: int, minimum: int = 0, maximum: int = 10_000) -> int:
        value = self._options.get(key, default)
        if value is None:
            return default
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            self._fail(key, "must be a whole number")
        value = int(value)
        if not minimum <= value <= maximum:
            self._fail(key, f"must be between {minimum} and {maximum}")
        return value

    def opt_bool(self, key: str, default: bool) -> bool:
        value = self._options.get(key, default)
        if not isinstance(value, bool):
            self._fail(key, "must be true or false")
        return value


def build(settings, client: HttpClient, path: str = "sources") -> Source:
    """Instantiate the source described by a SourceSettings entry."""
    board = settings.board
    if board in UNSUPPORTED_BOARDS:
        raise ConfigError(f"'{board}' is not supported. {UNSUPPORTED_BOARDS[board]}")
    source_class = BOARDS.get(board)
    if source_class is None:
        raise ConfigError(
            f"unknown board '{board}'. Available boards: {', '.join(sorted(BOARDS))}"
        )
    return source_class(client, settings.options, path)


def build_all(sources_settings, client: HttpClient) -> list[Source]:
    return [
        build(settings, client, f"sources[{index}]")
        for index, settings in enumerate(sources_settings)
    ]


from .adzuna import AdzunaSource  # noqa: E402  (imported here to keep the registry last)
from .greenhouse import GreenhouseSource  # noqa: E402
from .lever import LeverSource  # noqa: E402
from .linkedin import LinkedInSource  # noqa: E402
from .remoteok import RemoteOkSource  # noqa: E402
from .remotive import RemotiveSource  # noqa: E402
from .rss import RssSource  # noqa: E402

BOARDS: dict[str, type[Source]] = {
    "adzuna": AdzunaSource,
    "greenhouse": GreenhouseSource,
    "lever": LeverSource,
    "linkedin": LinkedInSource,
    "remoteok": RemoteOkSource,
    "remotive": RemotiveSource,
    "rss": RssSource,
}
