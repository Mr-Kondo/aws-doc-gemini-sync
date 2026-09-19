"""HTTP transport with a retry policy.

Retry rules live here rather than in each fetcher so that the policy is testable
in isolation and identical across backends.

Policy: retry on transport errors, 429, and 5xx. Never retry other 4xx -- a 404
means the page is gone and a 403 means we are unwelcome; repeating either is both
useless and rude. Backoff is exponential with full jitter, and ``Retry-After`` is
honoured when the server sends one.

Conditional requests are opt-in per call. A 304 is returned to the caller as a
normal response rather than raised: it is the server answering the question that
was asked, not a failure. It is only ever produced when validators were actually
sent, so an unconditional ``get`` can still treat everything outside 2xx as
something that went wrong.
"""

from __future__ import annotations

import email.utils
import random
import time
from dataclasses import dataclass
from typing import Any

import httpx

from ..domain.errors import FetchError, NotFoundError
from ..domain.models import HttpValidators
from ..logging_setup import get_logger

log = get_logger("http")

RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504, 507, 509})


def validators_from(response: httpx.Response, url: str) -> HttpValidators:
    """Read cache validators off a response."""
    return HttpValidators(
        url=url,
        etag=response.headers.get("ETag"),
        last_modified=response.headers.get("Last-Modified"),
    )


def refresh_validators(
    response: httpx.Response, url: str, sent: HttpValidators | None
) -> HttpValidators:
    """Validators to store after a 304.

    RFC 9110 says a 304 SHOULD carry an updated ETag, but not every origin does.
    Falling back to the ones that were sent keeps the next request conditional
    either way, instead of silently degrading to a full fetch forever.
    """
    fresh = validators_from(response, url)
    return fresh if fresh.usable else (sent or fresh)


@dataclass(frozen=True)
class RetryPolicy:
    max_retries: int = 4
    backoff_base_seconds: float = 0.5
    backoff_max_seconds: float = 30.0
    #: Injected so tests can assert delays deterministically.
    jitter: bool = True

    def should_retry(self, *, attempt: int, status: int | None, transport_error: bool) -> bool:
        if attempt >= self.max_retries:
            return False
        if transport_error:
            return True
        if status is None:
            return False
        return status in RETRYABLE_STATUS

    def delay_for(self, attempt: int, *, retry_after: float | None = None) -> float:
        """Seconds to wait before retry number ``attempt`` (0-based)."""
        if retry_after is not None and retry_after >= 0:
            return min(retry_after, self.backoff_max_seconds)
        raw = self.backoff_base_seconds * (2**attempt)
        capped = min(raw, self.backoff_max_seconds)
        if not self.jitter:
            return capped
        # Full jitter: avoids a retry stampede when many sources fail together.
        return random.uniform(0.0, capped)


def parse_retry_after(value: str | None) -> float | None:
    """Parse a ``Retry-After`` header in either seconds or HTTP-date form."""
    if not value:
        return None
    value = value.strip()
    try:
        return float(value)
    except ValueError:
        pass
    try:
        when = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when is None:
        return None
    import datetime as _dt

    now = _dt.datetime.now(tz=when.tzinfo or _dt.UTC)
    return max(0.0, (when - now).total_seconds())


class HttpClient:
    """Thin, retrying wrapper around ``httpx.Client``.

    Also applies a simple request-rate ceiling so a large registry does not turn
    into a burst against docs.aws.amazon.com.
    """

    def __init__(
        self,
        *,
        client: httpx.Client | None = None,
        policy: RetryPolicy | None = None,
        requests_per_second: float | None = 3.0,
        sleep: Any = time.sleep,
        user_agent: str = "aws-doc-sync/0.1",
        timeout: float = 30.0,
        connect_timeout: float = 10.0,
        follow_redirects: bool = True,
    ) -> None:
        self.policy = policy or RetryPolicy()
        self._sleep = sleep
        self._min_interval = (1.0 / requests_per_second) if requests_per_second else 0.0
        self._last_request_at = 0.0
        self._owns_client = client is None
        self._client = client or httpx.Client(
            timeout=httpx.Timeout(timeout, connect=connect_timeout),
            follow_redirects=follow_redirects,
            headers={"User-Agent": user_agent, "Accept-Encoding": "gzip, deflate"},
        )

    def __enter__(self) -> HttpClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def _throttle(self) -> None:
        if self._min_interval <= 0:
            return
        elapsed = time.monotonic() - self._last_request_at
        if elapsed < self._min_interval:
            self._sleep(self._min_interval - elapsed)
        self._last_request_at = time.monotonic()

    def get(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        validators: HttpValidators | None = None,
    ) -> httpx.Response:
        """GET ``url``, retrying per policy.

        Args:
            validators: when they apply to ``url``, ``If-None-Match`` and
                ``If-Modified-Since`` are sent and a 304 response is returned to
                the caller instead of raising.

        Raises:
            NotFoundError: on 404/410.
            FetchError: on any other non-2xx status or exhausted retries.
        """
        request_headers = dict(headers or {})
        conditional = validators is not None and validators.applies_to(url)
        if conditional:
            assert validators is not None
            request_headers.update(validators.headers())

        attempt = 0
        last_error: Exception | None = None

        while True:
            self._throttle()
            status: int | None = None
            transport_error = False
            retry_after: float | None = None
            try:
                response = self._client.get(url, headers=request_headers)
                status = response.status_code
                if 200 <= status < 300:
                    return response
                if status == 304 and conditional:
                    # The answer to the question we asked, not a failure.
                    return response
                if status in (404, 410):
                    raise NotFoundError(f"HTTP {status} for {url}", source_url=url)
                retry_after = parse_retry_after(response.headers.get("Retry-After"))
                last_error = FetchError(f"HTTP {status} for {url}", source_url=url)
            except NotFoundError:
                raise
            except httpx.HTTPError as exc:
                transport_error = True
                last_error = FetchError(f"{type(exc).__name__}: {exc}", source_url=url)

            if not self.policy.should_retry(
                attempt=attempt, status=status, transport_error=transport_error
            ):
                assert last_error is not None
                raise last_error

            delay = self.policy.delay_for(attempt, retry_after=retry_after)
            log.warning(
                "http_retry_scheduled",
                extra={
                    "url": url,
                    "attempt": attempt + 1,
                    "max_retries": self.policy.max_retries,
                    "status": status,
                    "delay_seconds": round(delay, 3),
                },
            )
            self._sleep(delay)
            attempt += 1
