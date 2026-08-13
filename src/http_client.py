"""The shared HTTP client.

One session, one request at a time, a fixed delay between requests, modest retries
that honour Retry-After, and optional conditional requests so unchanged feeds are
not downloaded twice. It does not try to look like a browser.
"""

from __future__ import annotations

import logging
import time

import requests

log = logging.getLogger(__name__)

# Retrying these is pointless: the server is telling us "no" or "gone".
NO_RETRY_STATUSES = frozenset({400, 401, 403, 404, 410, 451})
MAX_RETRY_WAIT_SECONDS = 60.0


class FetchError(Exception):
    """A request failed in a way the caller should log and move on from."""


class NotModified(Exception):
    """The server answered 304; the caller already has this content."""


class HttpClient:
    def __init__(
        self,
        user_agent: str,
        timeout_seconds: float = 20.0,
        delay_seconds: float = 2.0,
        max_retries: int = 2,
        validators=None,
    ) -> None:
        """`validators` stores ETag/Last-Modified values between runs.

        It needs get_validators(url) and set_validators(url, etag, last_modified);
        state.Store provides both. Pass None to disable conditional requests.
        """
        self.timeout = timeout_seconds
        self.delay = delay_seconds
        self.max_retries = max_retries
        self.validators = validators
        self._last_request_at = 0.0
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": user_agent,
                "Accept-Language": "en-US,en;q=0.9",
                "Accept-Encoding": "gzip, deflate",
            }
        )

    def close(self) -> None:
        self.session.close()

    def _wait_turn(self) -> None:
        elapsed = time.monotonic() - self._last_request_at
        if self._last_request_at and elapsed < self.delay:
            time.sleep(self.delay - elapsed)

    def get(
        self,
        url: str,
        params: dict | None = None,
        headers: dict | None = None,
        conditional: bool = False,
    ) -> requests.Response:
        """GET a URL, retrying transient failures. Raises FetchError or NotModified."""
        request_headers = dict(headers or {})
        if conditional and self.validators is not None:
            etag, last_modified = self.validators.get_validators(url)
            if etag:
                request_headers["If-None-Match"] = etag
            if last_modified:
                request_headers["If-Modified-Since"] = last_modified

        last_error = "unknown error"
        for attempt in range(self.max_retries + 1):
            self._wait_turn()
            try:
                response = self.session.get(
                    url, params=params, headers=request_headers, timeout=self.timeout
                )
            except requests.RequestException as exc:
                self._last_request_at = time.monotonic()
                last_error = f"{type(exc).__name__}: {exc}"
                wait = min(self.delay * (2**attempt) + 1.0, MAX_RETRY_WAIT_SECONDS)
            else:
                self._last_request_at = time.monotonic()
                if response.status_code == 304:
                    raise NotModified(url)
                if response.ok:
                    if conditional and self.validators is not None:
                        self.validators.set_validators(
                            url,
                            response.headers.get("ETag"),
                            response.headers.get("Last-Modified"),
                        )
                    return response
                last_error = f"HTTP {response.status_code}"
                if response.status_code in NO_RETRY_STATUSES:
                    raise FetchError(
                        f"{last_error} from {_host(url)}{_explain(response.status_code)}"
                    )
                wait = self._retry_wait(response, attempt)

            if attempt < self.max_retries:
                log.debug("%s from %s, retrying in %.1fs", last_error, _host(url), wait)
                time.sleep(wait)

        raise FetchError(f"{last_error} from {_host(url)} after {self.max_retries + 1} attempt(s)")

    def get_json(self, url: str, params: dict | None = None, conditional: bool = False):
        response = self.get(
            url, params=params, headers={"Accept": "application/json"}, conditional=conditional
        )
        try:
            return response.json()
        except ValueError as exc:
            raise FetchError(f"{_host(url)} returned a non-JSON response ({exc})") from exc

    def _retry_wait(self, response: requests.Response, attempt: int) -> float:
        retry_after = response.headers.get("Retry-After")
        if retry_after:
            try:
                return min(float(retry_after), MAX_RETRY_WAIT_SECONDS)
            except ValueError:
                pass  # HTTP-date form; fall back to backoff
        return min(self.delay * (2 ** attempt) + 1.0, MAX_RETRY_WAIT_SECONDS)


def _host(url: str) -> str:
    """Host only, so query strings never reach the logs."""
    try:
        return url.split("//", 1)[1].split("/", 1)[0]
    except IndexError:
        return url


def _explain(status: int) -> str:
    if status == 403:
        return " (blocked; this source may no longer allow unauthenticated access)"
    if status == 404:
        return " (not found; check the company or feed name in your config)"
    if status == 401:
        return " (unauthorized; check the API credentials for this source)"
    return ""
