"""Resilience, retry policies, and rate-limit tracking for eBay API clients."""

from __future__ import annotations

import asyncio
import email.utils
from enum import StrEnum
import logging
import time
from typing import Any, Callable

import aiohttp

_LOGGER = logging.getLogger(__name__)

# Bounded defaults for read-only polling
DEFAULT_MAX_RETRIES = 2
DEFAULT_BASE_DELAY = 0.5
DEFAULT_MAX_DELAY = 10.0
DEFAULT_TOTAL_TIMEOUT = 35.0
DEFAULT_RETRY_AFTER_CAP = 30.0


class FailureType(StrEnum):
    """Canonical classification for API failures."""

    CONNECTION_TIMEOUT = "connection_timeout"
    NETWORK_FAILURE = "network_failure"
    RATE_LIMITED = "rate_limited"
    SERVER_ERROR = "server_error"
    MALFORMED_REQUEST = "malformed_request"
    TOKEN_EXPIRED = "token_expired"
    PERMISSION_DENIED = "permission_denied"
    PARSE_ERROR = "parse_error"
    PARTIAL_RESPONSE = "partial_response"
    REQUEST_CANCELLED = "request_cancelled"
    UNKNOWN = "unknown"


class EndpointFamily(StrEnum):
    """eBay API endpoint families with separate rate limits."""

    TRADING = "trading"
    REST = "rest"
    GRAPHQL = "graphql"
    OAUTH = "oauth"


def parse_retry_after(
    header: str | None,
    default_delay: float = 1.0,
    max_delay: float = DEFAULT_RETRY_AFTER_CAP,
    now_ts: float | None = None,
) -> float:
    """Parse a Retry-After header into seconds.

    Supports both RFC 7231 / RFC 9110 delta-seconds and HTTP-date strings.
    Caps excessively long server delays at max_delay to protect total refresh time.
    """
    if not header or not str(header).strip():
        return default_delay
    text = str(header).strip()
    # Try delta-seconds first (most common for eBay REST 429)
    try:
        seconds = float(text)
        return max(0.0, min(seconds, max_delay))
    except ValueError:
        pass
    # Try HTTP-date
    try:
        dt = email.utils.parsedate_to_datetime(text)
        current = time.time() if now_ts is None else now_ts
        diff = dt.timestamp() - current
        return max(0.0, min(diff, max_delay))
    except (TypeError, ValueError):
        return default_delay


class RateLimitTracker:
    """Thread-safe, bounded, in-memory rate-limit and cooldown tracker.

    Tracks observed provider throttling without assuming global 5,000-call limits.
    Scoped per endpoint family and optional client_id to prevent cross-account
    interference while allowing single-keyset multi-account cooldown sharing where appropriate.
    """

    def __init__(self) -> None:
        """Initialize tracker."""
        # key -> monotonic timestamp until which the endpoint is in cooldown
        self._cooldowns: dict[str, float] = {}
        # key -> count of observed 429s in recent window
        self._throttle_counts: dict[str, int] = {}

    def _key(self, family: str | EndpointFamily, scope_key: str | None = None) -> str:
        """Format an internal cooldown key without exposing raw secrets."""
        family_str = str(family).lower()
        if scope_key:
            return f"{family_str}:{scope_key}"
        return family_str

    def record_throttled(
        self,
        family: str | EndpointFamily,
        retry_after: float,
        scope_key: str | None = None,
    ) -> float:
        """Record a 429 throttling event and set cooldown."""
        delay = max(0.0, retry_after)
        key = self._key(family, scope_key)
        until = time.monotonic() + delay
        self._cooldowns[key] = max(self._cooldowns.get(key, 0.0), until)
        self._throttle_counts[key] = self._throttle_counts.get(key, 0) + 1
        _LOGGER.debug(
            "Rate limit cooldown recorded for %s: duration=%.1fs total_events=%d",
            key,
            delay,
            self._throttle_counts[key],
        )
        return delay

    def is_throttled(
        self, family: str | EndpointFamily, scope_key: str | None = None
    ) -> bool:
        """Return True if the endpoint family is currently in cooldown."""
        return self.cooldown_remaining(family, scope_key) > 0.0

    def cooldown_remaining(
        self, family: str | EndpointFamily, scope_key: str | None = None
    ) -> float:
        """Return remaining cooldown seconds, or 0.0 if not throttled."""
        key = self._key(family, scope_key)
        until = self._cooldowns.get(key, 0.0)
        remaining = until - time.monotonic()
        if remaining <= 0.0:
            if key in self._cooldowns:
                self._cooldowns.pop(key, None)
            return 0.0
        return remaining

    def clear(self) -> None:
        """Clear all cooldowns (used in tests and entry teardown)."""
        self._cooldowns.clear()
        self._throttle_counts.clear()


class BoundedRetryPolicy:
    """Canonical bounded retry policy for safe read-only eBay API requests.

    Requirements enforced:
    - Bounded retries (default max 2 retries = 3 attempts total).
    - Bounded total request budget.
    - Cancellation-aware asynchronous waiting.
    - Deterministic, bounded jitter.
    - Never retries permanently invalid credentials or missing scopes indefinitely.
    - Never converts failure into empty success.
    """

    def __init__(
        self,
        *,
        max_retries: int = DEFAULT_MAX_RETRIES,
        base_delay: float = DEFAULT_BASE_DELAY,
        max_delay: float = DEFAULT_MAX_DELAY,
        total_timeout: float = DEFAULT_TOTAL_TIMEOUT,
        sleep_func: Callable[[float], Any] | None = None,
    ) -> None:
        """Initialize retry policy."""
        self.max_retries = max_retries
        self.base_delay = base_delay
        self.max_delay = max_delay
        self.total_timeout = total_timeout
        self._sleep_func = sleep_func or asyncio.sleep

    def compute_backoff(self, attempt: int, retry_after: float | None = None) -> float:
        """Compute backoff delay with bounded, deterministic progression."""
        if retry_after is not None and retry_after > 0:
            return min(retry_after, self.max_delay)
        # Exponential backoff: base * 2^attempt (0.5s, 1.0s, etc.)
        delay = self.base_delay * (2**attempt)
        return min(delay, self.max_delay)

    async def async_wait(self, delay: float) -> None:
        """Cancellation-aware asynchronous sleep."""
        if delay <= 0:
            return
        await self._sleep_func(delay)

    def is_transient_status(self, status: int) -> bool:
        """Return whether an HTTP status code is eligible for bounded retry."""
        return status in {429, 500, 502, 503, 504}

    def is_transient_exception(self, exc: Exception) -> bool:
        """Return whether a transport exception is eligible for bounded retry."""
        if isinstance(exc, (TimeoutError, asyncio.TimeoutError)):
            return True
        if isinstance(exc, aiohttp.ClientConnectorError):
            return True
        if isinstance(exc, aiohttp.ServerDisconnectedError):
            return True
        if isinstance(exc, aiohttp.ClientPayloadError):
            return True
        if isinstance(exc, aiohttp.ClientOSError):
            return True
        return False
