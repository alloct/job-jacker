"""Configuration loading and validation.

Errors here are raised as ConfigError with the offending path (e.g.
"searches[0].titles.include") so a typo is obvious without reading the source.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from .models import EMPLOYMENT_TYPES, normalize_employment_type

MIN_INTERVAL_MINUTES = 5
MIN_REQUEST_DELAY_SECONDS = 0.5
DEFAULT_USER_AGENT = "job-jacker/1.0 (periodic job watcher; +https://github.com/job-jacker)"
_ENV_PLACEHOLDER = re.compile(r"^\$\{([A-Z0-9_]+)\}$")
_WEBHOOK_PATTERN = re.compile(
    r"^https://(canary\.|ptb\.)?discord(app)?\.com/api/webhooks/\d+/[\w-]+$"
)


class ConfigError(Exception):
    """Raised for any invalid or missing configuration."""


@dataclass(frozen=True)
class Filters:
    include: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()


@dataclass(frozen=True)
class Search:
    name: str
    titles: Filters = Filters()
    keywords: Filters = Filters()
    companies: Filters = Filters()
    locations: Filters = Filters()
    employment_types: Filters = Filters()
    salary_minimum: float | None = None
    salary_currency: str | None = None
    min_score: int = 0


@dataclass(frozen=True)
class HttpSettings:
    user_agent: str = DEFAULT_USER_AGENT
    timeout_seconds: float = 20.0
    delay_seconds: float = 2.0
    max_retries: int = 2


@dataclass(frozen=True)
class SourceSettings:
    """A configured source instance. `options` are validated by the source itself."""

    board: str
    options: dict


@dataclass(frozen=True)
class Config:
    interval_minutes: int = 60
    webhook_url: str = ""
    state_path: Path = Path("data/state.sqlite3")
    retention_days: int = 90
    log_level: str = "INFO"
    log_file: Path | None = None
    notify_on_first_run: bool = False
    http: HttpSettings = HttpSettings()
    sources: tuple[SourceSettings, ...] = ()
    searches: tuple[Search, ...] = ()

    def query_terms(self) -> tuple[str, ...]:
        """Title terms sent to search-based boards, in config order, deduplicated."""
        seen: dict[str, None] = {}
        for search in self.searches:
            for term in search.titles.include:
                seen.setdefault(term, None)
        return tuple(seen)


def _expand_env(value):
    """Replace exact "${VAR}" strings with their environment value."""
    if isinstance(value, str):
        match = _ENV_PLACEHOLDER.match(value.strip())
        if match:
            return os.environ.get(match.group(1), "")
        return value
    if isinstance(value, dict):
        return {key: _expand_env(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_expand_env(item) for item in value]
    return value


def _as_mapping(value, path: str) -> dict:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ConfigError(f"{path} must be a mapping, got {type(value).__name__}")
    return value


def _as_terms(value, path: str) -> tuple[str, ...]:
    """Accept a single string or a list of strings; reject anything else."""
    if value is None:
        return ()
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        raise ConfigError(f"{path} must be a list of words, got {type(value).__name__}")
    terms = []
    for index, item in enumerate(value):
        if item is None:
            continue
        if isinstance(item, (int, float)):
            item = str(item)
        if not isinstance(item, str):
            raise ConfigError(f"{path}[{index}] must be text, got {type(item).__name__}")
        if item.strip():
            terms.append(item.strip())
    return tuple(terms)


def _as_filters(value, path: str) -> Filters:
    """Read an include/exclude block, or a bare list treated as includes."""
    if value is None:
        return Filters()
    if isinstance(value, (str, list)):
        return Filters(include=_as_terms(value, path))
    mapping = _as_mapping(value, path)
    unknown = set(mapping) - {"include", "exclude"}
    if unknown:
        raise ConfigError(f"{path} has unknown key(s): {', '.join(sorted(unknown))}")
    return Filters(
        include=_as_terms(mapping.get("include"), f"{path}.include"),
        exclude=_as_terms(mapping.get("exclude"), f"{path}.exclude"),
    )


def _as_positive_number(value, path: str, minimum: float, default: float) -> float:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{path} must be a number, got {type(value).__name__}")
    if value < minimum:
        raise ConfigError(f"{path} must be at least {minimum}, got {value}")
    return float(value)


def _parse_search(raw, index: int) -> Search:
    path = f"searches[{index}]"
    mapping = _as_mapping(raw, path)
    known = {
        "name",
        "titles",
        "keywords",
        "companies",
        "locations",
        "employment_types",
        "salary",
        "min_score",
    }
    unknown = set(mapping) - known
    if unknown:
        raise ConfigError(f"{path} has unknown key(s): {', '.join(sorted(unknown))}")

    name = str(mapping.get("name") or f"search {index + 1}").strip()
    employment_types = _as_filters(mapping.get("employment_types"), f"{path}.employment_types")
    for term in employment_types.include + employment_types.exclude:
        if normalize_employment_type(term) is None:
            raise ConfigError(
                f"{path}.employment_types does not recognise '{term}'. Use one of: "
                f"{', '.join(EMPLOYMENT_TYPES)}"
            )

    salary = _as_mapping(mapping.get("salary"), f"{path}.salary")
    minimum = salary.get("minimum")
    if minimum is not None and (isinstance(minimum, bool) or not isinstance(minimum, (int, float))):
        raise ConfigError(f"{path}.salary.minimum must be a number")

    search = Search(
        name=name,
        titles=_as_filters(mapping.get("titles"), f"{path}.titles"),
        keywords=_as_filters(mapping.get("keywords"), f"{path}.keywords"),
        companies=_as_filters(mapping.get("companies"), f"{path}.companies"),
        locations=_as_filters(mapping.get("locations"), f"{path}.locations"),
        employment_types=employment_types,
        salary_minimum=float(minimum) if minimum is not None else None,
        salary_currency=(str(salary["currency"]).upper() if salary.get("currency") else None),
        min_score=int(mapping.get("min_score") or 0),
    )
    if not any(
        (group.include or group.exclude)
        for group in (
            search.titles,
            search.keywords,
            search.companies,
            search.locations,
            search.employment_types,
        )
    ):
        raise ConfigError(
            f"{path} has no filters; add at least one include or exclude list "
            "(a search with no filters would match every job)"
        )
    return search


def _parse_sources(raw) -> tuple[SourceSettings, ...]:
    if raw is None:
        raise ConfigError("sources is required; add at least one job board")
    if not isinstance(raw, list) or not raw:
        raise ConfigError("sources must be a non-empty list")

    sources = []
    for index, item in enumerate(raw):
        path = f"sources[{index}]"
        # Allow the shorthand "- remotive" alongside full mappings.
        if isinstance(item, str):
            sources.append(SourceSettings(board=item.strip().lower(), options={}))
            continue
        mapping = _as_mapping(item, path)
        board = mapping.get("board")
        if not board or not isinstance(board, str):
            raise ConfigError(f"{path}.board is required (e.g. board: linkedin)")
        options = {key: value for key, value in mapping.items() if key != "board"}
        if options.get("enabled") is False:
            continue
        options.pop("enabled", None)
        sources.append(SourceSettings(board=board.strip().lower(), options=options))
    if not sources:
        raise ConfigError("every source is disabled; enable at least one")
    return tuple(sources)


def _resolve_webhook(discord: dict) -> str:
    """Environment variable wins, so servers never need the secret in a file."""
    url = (os.environ.get("DISCORD_WEBHOOK_URL") or discord.get("webhook_url") or "").strip()
    if not url:
        raise ConfigError(
            "Discord webhook is not set. Put it in discord.webhook_url or set the "
            "DISCORD_WEBHOOK_URL environment variable."
        )
    if not _WEBHOOK_PATTERN.match(url):
        # Deliberately does not echo the value back.
        raise ConfigError(
            "discord.webhook_url does not look like a Discord webhook URL. Expected "
            "https://discord.com/api/webhooks/<id>/<token>"
        )
    return url


def load(path: str | Path, require_webhook: bool = True) -> Config:
    """Read and validate a YAML config file."""
    config_path = Path(path)
    if not config_path.is_file():
        raise ConfigError(
            f"Config file not found: {config_path}. Copy config.example.yaml to "
            f"{config_path.name} and edit it."
        )
    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"{config_path} is not valid YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"{config_path} must contain a mapping at the top level")

    raw = _expand_env(raw)
    known = {
        "interval_minutes",
        "discord",
        "http",
        "state",
        "log",
        "notify_on_first_run",
        "sources",
        "searches",
    }
    unknown = set(raw) - known
    if unknown:
        raise ConfigError(f"unknown top-level key(s): {', '.join(sorted(unknown))}")

    interval = int(
        _as_positive_number(
            raw.get("interval_minutes"), "interval_minutes", MIN_INTERVAL_MINUTES, 60
        )
    )

    http_raw = _as_mapping(raw.get("http"), "http")
    http = HttpSettings(
        user_agent=str(http_raw.get("user_agent") or DEFAULT_USER_AGENT).strip(),
        timeout_seconds=_as_positive_number(
            http_raw.get("timeout_seconds"), "http.timeout_seconds", 1, 20
        ),
        delay_seconds=_as_positive_number(
            http_raw.get("delay_seconds"), "http.delay_seconds", MIN_REQUEST_DELAY_SECONDS, 2
        ),
        max_retries=int(_as_positive_number(http_raw.get("max_retries"), "http.max_retries", 0, 2)),
    )

    state_raw = _as_mapping(raw.get("state"), "state")
    log_raw = _as_mapping(raw.get("log"), "log")
    log_level = str(log_raw.get("level") or "INFO").upper()
    if log_level not in {"DEBUG", "INFO", "WARNING", "ERROR"}:
        raise ConfigError("log.level must be one of DEBUG, INFO, WARNING, ERROR")

    searches_raw = raw.get("searches")
    if not isinstance(searches_raw, list) or not searches_raw:
        raise ConfigError("searches is required and must be a non-empty list")

    webhook = _resolve_webhook(_as_mapping(raw.get("discord"), "discord")) if require_webhook else ""

    return Config(
        interval_minutes=interval,
        webhook_url=webhook,
        state_path=Path(str(state_raw.get("path") or "data/state.sqlite3")),
        retention_days=int(
            _as_positive_number(state_raw.get("retention_days"), "state.retention_days", 1, 90)
        ),
        log_level=log_level,
        log_file=Path(str(log_raw["file"])) if log_raw.get("file") else None,
        notify_on_first_run=bool(raw.get("notify_on_first_run", False)),
        http=http,
        sources=_parse_sources(raw.get("sources")),
        searches=tuple(_parse_search(item, index) for index, item in enumerate(searches_raw)),
    )
