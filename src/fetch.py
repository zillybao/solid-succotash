"""HTTP fetching with retries, timeouts, and a real User-Agent."""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

logger = logging.getLogger(__name__)

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (compatible; InternFinder/0.1; +https://github.com/intern-finder)"
)
DEFAULT_TIMEOUT = 30.0
# JSON ATS endpoints (Greenhouse, Workday, …) tolerate a short gap; HTML/SSR
# career pages (Apple, Google, TalentBrew job pages) are more bot-gated.
JSON_DELAY_SECONDS = 0.4
HTML_DELAY_SECONDS = 1.5
DEFAULT_DELAY_SECONDS = JSON_DELAY_SECONDS


class FetchError(Exception):
    """Raised when a request fails after retries."""


class Fetcher:
    """Sequential HTTP client with inter-request delay."""

    def __init__(
        self,
        *,
        user_agent: str = DEFAULT_USER_AGENT,
        timeout: float = DEFAULT_TIMEOUT,
        delay_seconds: float = JSON_DELAY_SECONDS,
        html_delay_seconds: float = HTML_DELAY_SECONDS,
    ) -> None:
        self._delay = delay_seconds
        self._html_delay = html_delay_seconds
        self._last_request_at: float | None = None
        self._client = httpx.Client(
            headers={"User-Agent": user_agent, "Accept": "*/*"},
            timeout=timeout,
            follow_redirects=True,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> Fetcher:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def _throttle(self, delay: float) -> None:
        if self._last_request_at is None:
            return
        elapsed = time.monotonic() - self._last_request_at
        remaining = delay - elapsed
        if remaining > 0:
            time.sleep(remaining)

    @retry(
        retry=retry_if_exception_type((httpx.TransportError, httpx.TimeoutException)),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=8),
        reraise=True,
    )
    def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        delay = float(kwargs.pop("delay", self._delay))
        self._throttle(delay)
        response = self._client.request(method, url, **kwargs)
        self._last_request_at = time.monotonic()
        return response

    def get_text(self, url: str) -> str:
        """GET and return response body as text. Raises FetchError on HTTP errors."""
        try:
            response = self._request("GET", url, delay=self._html_delay)
            response.raise_for_status()
            return response.text
        except httpx.HTTPError as exc:
            raise FetchError(f"GET {url} failed: {exc}") from exc

    def get_json(self, url: str) -> Any:
        """GET and parse JSON. Raises FetchError on HTTP/JSON errors."""
        try:
            response = self._request("GET", url, delay=self._delay)
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise FetchError(f"GET JSON {url} failed: {exc}") from exc

    def post_json(self, url: str, payload: dict[str, Any]) -> Any:
        """POST JSON and parse the response body."""
        try:
            response = self._request(
                "POST",
                url,
                json=payload,
                headers={"Accept": "application/json", "Content-Type": "application/json"},
                delay=self._delay,
            )
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise FetchError(f"POST JSON {url} failed: {exc}") from exc
