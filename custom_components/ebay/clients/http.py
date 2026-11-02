"""OAuth token + REST GET client mixin."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any, NoReturn

import aiohttp

from ..models import ApiFailure
from ..oauth_errors import (
    OAuth2TokenRequestError,
    OAuth2TokenRequestReauthError,
    OAuth2TokenRequestTransientError,
    oauth_token_error,
    oauth_token_request_error,
    oauth_token_transient_error,
)
from ..parsers.xml import _duration_ms, _to_int
from .endpoints import EbayEndpoints, extract_authorization_code
from .errors import (
    _PARTIAL_FAILURE_DETAILS_MAX,
    EbayAuthError,
    EbayAuthTransientError,
    EbayParseError,
    EbayPartialFailure,
    _safe_ebay_error_payload,
    build_api_failure,
    classify_rest_status,
    map_rest_failure_category,
)
from .resilience import (
    BoundedRetryPolicy,
    EndpointFamily,
    RateLimitTracker,
    parse_retry_after,
)

_LOGGER = logging.getLogger(__name__)

TOKEN_REFRESH_MARGIN = timedelta(minutes=5)


def basic_auth_header(client_id: str, client_secret: str) -> str:
    """Build an HTTP Basic Authorization header value."""
    basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    return f"Basic {basic}"


_basic_auth_header = basic_auth_header


class RestClientMixin:
    """Token management and authenticated REST GET helpers."""

    _session: aiohttp.ClientSession
    _oauth_session: Any | None
    client_id: str
    _client_secret: str
    runame: str
    refresh_token: str | None
    _endpoints: EbayEndpoints
    _access_token: str | None
    _access_token_expires_at: datetime | None
    _feedback_user_id: str | None
    _partial_failure_details: list[dict[str, Any]]
    _last_api_failure: ApiFailure | None
    _accept_language: str | None

    def _record_partial_failure_detail(self, detail: dict[str, Any] | None) -> None:
        """Store a sanitized soft-failure detail for diagnostics."""
        if not detail:
            return
        self._partial_failure_details.append(detail)
        if len(self._partial_failure_details) > _PARTIAL_FAILURE_DETAILS_MAX:
            self._partial_failure_details = self._partial_failure_details[
                -_PARTIAL_FAILURE_DETAILS_MAX:
            ]

    def _raise_partial_failure(
        self,
        category: str,
        *,
        details: dict[str, Any] | None = None,
        failure: ApiFailure | None = None,
    ) -> NoReturn:
        """Record optional details and raise EbayPartialFailure."""
        if failure is not None:
            self._last_api_failure = failure
            if details is None:
                details = failure.to_dict()
        self._record_partial_failure_detail(details)
        raise EbayPartialFailure(category, details=details, failure=failure)

    async def async_exchange_authorization_code(self, code: str) -> dict[str, Any]:
        """Exchange an authorization code for OAuth tokens."""
        try:
            payload = await self._token_request(
                {
                    "grant_type": "authorization_code",
                    "code": extract_authorization_code(code),
                    "redirect_uri": self.runame,
                }
            )
        except OAuth2TokenRequestError as exc:
            raise EbayAuthError("OAuth authorization code exchange failed") from exc
        if not payload.get("access_token") or not payload.get("refresh_token"):
            raise EbayAuthError(
                "OAuth response did not include access and refresh tokens"
            )
        self._store_access_token(payload)
        self.refresh_token = payload["refresh_token"]
        return payload

    async def async_get_access_token(self) -> str:
        """Return a fresh access token."""
        if self._oauth_session is not None:
            try:
                await self._oauth_session.async_ensure_token_valid()
            except OAuth2TokenRequestReauthError as exc:
                raise EbayAuthError("OAuth token refresh failed") from exc
            except (OAuth2TokenRequestTransientError, OAuth2TokenRequestError) as exc:
                raise EbayAuthTransientError(
                    "OAuth token refresh failed temporarily"
                ) from exc
            access_token = self._oauth_session.token.get("access_token")
            if not access_token:
                raise EbayAuthError("OAuth token data does not include access token")
            return access_token
        if self._access_token and not self._access_token_expiring():
            return self._access_token
        if not self.refresh_token:
            raise EbayAuthError("Missing eBay refresh token")
        try:
            payload = await self._token_request(
                {
                    "grant_type": "refresh_token",
                    "refresh_token": self.refresh_token,
                }
            )
        except OAuth2TokenRequestReauthError as exc:
            raise EbayAuthError("OAuth token refresh failed") from exc
        except (OAuth2TokenRequestTransientError, OAuth2TokenRequestError) as exc:
            raise EbayAuthTransientError(
                "OAuth token refresh failed temporarily"
            ) from exc
        access_token = payload.get("access_token")
        if not access_token:
            raise EbayAuthError("OAuth refresh response did not include access token")
        self._store_access_token(payload)
        self.refresh_token = payload.get("refresh_token", self.refresh_token)
        return access_token

    def _access_token_expiring(self) -> bool:
        """Return whether the cached access token is close to expiry."""
        if self._access_token_expires_at is None:
            return True
        return (
            datetime.now(timezone.utc) + TOKEN_REFRESH_MARGIN
            >= self._access_token_expires_at
        )

    def _store_access_token(self, payload: dict[str, Any]) -> None:
        """Cache an access token and its expiry."""
        self._access_token = payload["access_token"]
        expires_in = _to_int(payload.get("expires_in"))
        if expires_in is None:
            self._access_token_expires_at = datetime.now(timezone.utc)
            return
        self._access_token_expires_at = datetime.now(timezone.utc) + timedelta(
            seconds=expires_in
        )

    def _clear_access_token(self) -> None:
        """Clear cached access token state."""
        self._access_token = None
        self._access_token_expires_at = None
        # User id is account-scoped; clear so a reauth cannot reuse a stale handle.
        self._feedback_user_id = None

    @property
    def _refresh_lock(self) -> asyncio.Lock:
        """Return the client-level token refresh lock, creating if needed."""
        if not hasattr(self, "_token_refresh_lock") or self._token_refresh_lock is None:
            self._token_refresh_lock = asyncio.Lock()
        return self._token_refresh_lock

    @property
    def rate_limit_tracker(self) -> RateLimitTracker:
        """Return the shared or instance RateLimitTracker."""
        if not hasattr(self, "_rate_limit_tracker") or self._rate_limit_tracker is None:
            self._rate_limit_tracker = RateLimitTracker()
        return self._rate_limit_tracker

    @property
    def retry_policy(self) -> BoundedRetryPolicy:
        """Return the BoundedRetryPolicy."""
        if not hasattr(self, "_retry_policy") or self._retry_policy is None:
            self._retry_policy = BoundedRetryPolicy()
        return self._retry_policy

    def get_rate_limiting_diagnostics(self) -> dict[str, Any]:
        """Return non-sensitive rate-limiting and retry diagnostics."""
        tracker = self.rate_limit_tracker
        rest_remaining = tracker.cooldown_remaining(
            EndpointFamily.REST, getattr(self, "client_id", None)
        )
        trading_remaining = tracker.cooldown_remaining(
            EndpointFamily.TRADING, getattr(self, "client_id", None)
        )
        return {
            "throttled": (rest_remaining > 0.0 or trading_remaining > 0.0),
            "rest_cooldown_remaining_seconds": round(rest_remaining, 1),
            "trading_cooldown_remaining_seconds": round(trading_remaining, 1),
            "last_retry_count": getattr(self, "_last_retry_count", 0),
        }

    async def async_refresh_access_token(
        self, *, failed_token: str | None = None
    ) -> str:
        """Force a fresh access token from the OAuth provider with locking and deduplication."""
        if self._oauth_session is not None:
            token_lock = getattr(self._oauth_session, "_token_lock", None)
            async with self._refresh_lock:
                if isinstance(token_lock, asyncio.Lock):
                    async with token_lock:
                        return await self._async_refresh_oauth2_session(
                            failed_token=failed_token
                        )
                return await self._async_refresh_oauth2_session(
                    failed_token=failed_token
                )

        async with self._refresh_lock:
            # If another coroutine already refreshed the token, return it
            if (
                failed_token is not None
                and self._access_token
                and self._access_token != failed_token
                and not self._access_token_expiring()
            ):
                return self._access_token
            self._clear_access_token()
            return await self.async_get_access_token()

    async def _async_refresh_oauth2_session(
        self, *, failed_token: str | None = None
    ) -> str:
        """Execute OAuth2Session token refresh under lock with double-checked caching."""
        token_data = getattr(self._oauth_session, "token", None)
        current_token = (
            token_data.get("access_token") if isinstance(token_data, dict) else None
        )
        if failed_token is not None and current_token and current_token != failed_token:
            return str(current_token)

        self._clear_access_token()
        try:
            new_token = await self._oauth_session.implementation.async_refresh_token(
                self._oauth_session.token
            )
        except OAuth2TokenRequestReauthError as exc:
            config_entry = getattr(self._oauth_session, "config_entry", None)
            hass = getattr(self._oauth_session, "hass", None)
            if config_entry is not None and hass is not None:
                if hasattr(config_entry, "async_start_reauth_if_available"):
                    config_entry.async_start_reauth_if_available(hass)
                elif hasattr(config_entry, "async_start_reauth"):
                    config_entry.async_start_reauth(hass)
            raise EbayAuthError("OAuth token refresh failed") from exc
        except (OAuth2TokenRequestTransientError, OAuth2TokenRequestError) as exc:
            raise EbayAuthTransientError(
                "OAuth token refresh failed temporarily"
            ) from exc

        if not isinstance(new_token, dict) or not new_token.get("access_token"):
            raise EbayAuthError("OAuth token data does not include access token")

        persisted_token = (
            {**token_data, **new_token}
            if isinstance(token_data, dict)
            else dict(new_token)
        )
        if "expires_at" not in persisted_token and "expires_in" in persisted_token:
            expires_in = _to_int(persisted_token.get("expires_in")) or 0
            persisted_token["expires_in"] = expires_in
            persisted_token["expires_at"] = time.time() + expires_in

        if hasattr(self._oauth_session, "token"):
            if isinstance(self._oauth_session.token, dict):
                self._oauth_session.token.update(persisted_token)
            else:
                try:
                    self._oauth_session.token = persisted_token
                except AttributeError:
                    pass

        hass = getattr(self._oauth_session, "hass", None)
        config_entry = getattr(self._oauth_session, "config_entry", None)
        config_entries = getattr(hass, "config_entries", None)
        if (
            config_entries is not None
            and config_entry is not None
            and hasattr(config_entries, "async_update_entry")
        ):
            config_entries.async_update_entry(
                config_entry,
                data={**getattr(config_entry, "data", {}), "token": persisted_token},
            )

        self._store_access_token(persisted_token)
        return str(persisted_token["access_token"])

    async def _token_request(self, data: dict[str, str]) -> dict[str, Any]:
        headers = {
            "Authorization": _basic_auth_header(self.client_id, self._client_secret),
            "Content-Type": "application/x-www-form-urlencoded",
        }
        try:
            async with self._session.post(
                self._endpoints.token,
                headers=headers,
                data=data,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as response:
                try:
                    payload = await response.json(content_type=None)
                except (aiohttp.ContentTypeError, json.JSONDecodeError) as exc:
                    raise oauth_token_request_error(
                        f"eBay OAuth response was not valid JSON; HTTP {response.status}",
                        status=response.status,
                        token_url=self._endpoints.token,
                    ) from exc
                if response.status >= 400:
                    error_code = (
                        payload.get("error") if isinstance(payload, dict) else None
                    )
                    raise oauth_token_error(
                        response.status,
                        error_code=error_code,
                        token_url=self._endpoints.token,
                    )
                return payload
        except OAuth2TokenRequestError:
            raise
        except (aiohttp.ClientError, TimeoutError) as exc:
            raise oauth_token_transient_error(
                "eBay OAuth token request failed temporarily",
                token_url=self._endpoints.token,
            ) from exc

    async def _async_rest_get(
        self,
        url: str,
        *,
        params: dict[str, Any] | list[tuple[str, str]] | None = None,
        partial_category: str,
        allow_404: bool = False,
        operation: str | None = None,
        accept_language: str | None = None,
    ) -> dict[str, Any]:
        """GET a JSON REST resource with auth retry and bounded retries."""
        op = operation or f"{partial_category}.get"
        policy = getattr(self, "retry_policy", None) or BoundedRetryPolicy()
        max_attempts = policy.max_retries + 1
        deadline = time.monotonic() + policy.total_timeout
        auth_refreshed = False
        self._last_retry_count = 0

        for attempt in range(max_attempts):
            remaining_budget = deadline - time.monotonic()
            if remaining_budget <= 0.0:
                failure = build_api_failure(
                    op,
                    url,
                    504,
                    {"error": "request_timeout_budget_exhausted"},
                    partial_category,
                    "transient",
                )
                self._raise_partial_failure(partial_category, failure=failure)

            if attempt > 0:
                self._last_retry_count = getattr(self, "_last_retry_count", 0) + 1

            try:
                return await self._async_rest_get_once(
                    url,
                    params=params,
                    partial_category=partial_category,
                    allow_404=allow_404,
                    operation=operation,
                    accept_language=accept_language,
                    deadline=deadline,
                )
            except EbayAuthError as exc:
                if not auth_refreshed and (attempt + 1 < max_attempts):
                    auth_refreshed = True
                    rejected_token = getattr(exc, "failed_token", None) or getattr(
                        self, "_access_token", None
                    )
                    await self.async_refresh_access_token(failed_token=rejected_token)
                    if time.monotonic() < deadline:
                        continue
                raise
            except EbayPartialFailure as exc:
                status = exc.failure.http_status if exc.failure else None
                is_transient_5xx = status in {500, 502, 503, 504}
                is_transient_transport = (
                    status is None
                    and exc.failure is not None
                    and exc.failure.classification == "transient"
                )
                if (
                    (is_transient_5xx or is_transient_transport)
                    and (attempt + 1 < max_attempts)
                    and (time.monotonic() < deadline)
                ):
                    delay = policy.compute_backoff(attempt)
                    if time.monotonic() + delay < deadline:
                        _LOGGER.debug(
                            "REST GET transient error category=%s status=%s attempt=%d retrying in %.2fs",
                            partial_category,
                            status,
                            attempt + 1,
                            delay,
                        )
                        await policy.async_wait(delay)
                        continue
                raise
            except (asyncio.CancelledError, EbayParseError):
                raise
            except Exception as exc:
                if policy.is_transient_exception(exc):
                    if (attempt + 1 < max_attempts) and (time.monotonic() < deadline):
                        delay = policy.compute_backoff(attempt)
                        if time.monotonic() + delay < deadline:
                            _LOGGER.debug(
                                "REST GET transient transport error category=%s exc=%s attempt=%d retrying in %.2fs",
                                partial_category,
                                type(exc).__name__,
                                attempt + 1,
                                delay,
                            )
                            await policy.async_wait(delay)
                            continue
                failure = build_api_failure(
                    op,
                    url,
                    None,
                    {"error": type(exc).__name__},
                    partial_category,
                    (
                        "transient"
                        if policy.is_transient_exception(exc)
                        else "unexpected_error"
                    ),
                )
                self._raise_partial_failure(partial_category, failure=failure)

        failure = build_api_failure(
            op,
            url,
            504,
            {"error": "retry_budget_exhausted"},
            partial_category,
            "transient",
        )
        self._raise_partial_failure(partial_category, failure=failure)

    async def _async_rest_get_once(
        self,
        url: str,
        *,
        params: dict[str, Any] | list[tuple[str, str]] | None = None,
        partial_category: str,
        allow_404: bool = False,
        operation: str | None = None,
        accept_language: str | None = None,
        deadline: float | None = None,
    ) -> dict[str, Any]:
        """GET a JSON REST resource once without retrying."""
        op = operation or f"{partial_category}.get"
        language = (
            accept_language if accept_language is not None else self._accept_language
        )
        tracker = self.rate_limit_tracker
        client_id = getattr(self, "client_id", None)
        cooldown = tracker.cooldown_remaining(EndpointFamily.REST, client_id)
        if cooldown > 2.0:
            failure = build_api_failure(
                op,
                url,
                429,
                {
                    "error": "throttled_cooldown_active",
                    "retry_after": round(cooldown, 1),
                },
                partial_category,
                "transient",
            )
            _LOGGER.debug(
                "REST GET throttled fast-fail category=%s cooldown_remaining=%.1fs",
                partial_category,
                cooldown,
            )
            self._raise_partial_failure(partial_category, failure=failure)
        elif cooldown > 0.0:
            await asyncio.sleep(cooldown)

        policy = getattr(self, "retry_policy", None) or BoundedRetryPolicy()
        request_start = time.monotonic()
        remaining_budget = (
            (deadline - time.monotonic()) if deadline is not None else 30.0
        )
        if remaining_budget <= 0.0:
            failure = build_api_failure(
                op,
                url,
                504,
                {"error": "request_timeout_budget_exhausted"},
                partial_category,
                "transient",
            )
            self._raise_partial_failure(partial_category, failure=failure)

        attempt_timeout = aiohttp.ClientTimeout(
            total=max(0.1, min(30.0, remaining_budget))
        )
        access_token = await self.async_get_access_token()
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        if language:
            headers["Accept-Language"] = language

        try:
            async with self._session.get(
                url,
                headers=headers,
                params=params or {},
                timeout=attempt_timeout,
            ) as response:
                if response.status == 401:
                    # 401 MUST raise EbayAuthError immediately to allow outer auth refresh.
                    # Never multiply retries here.
                    raise EbayAuthError(
                        f"eBay REST auth failed with HTTP {response.status}",
                        failed_token=access_token,
                    )
                if response.status == 204:
                    return {}
                if allow_404 and response.status == 404:
                    return {}

                if response.status == 429:
                    retry_after_hdr = response.headers.get("Retry-After")
                    retry_after_s = parse_retry_after(
                        retry_after_hdr, default_delay=60.0
                    )
                    tracker.record_throttled(
                        EndpointFamily.REST,
                        retry_after_s,
                        client_id,
                    )
                    error_payload = None
                    try:
                        error_payload = _safe_ebay_error_payload(
                            await response.json(content_type=None)
                        )
                    except (aiohttp.ContentTypeError, json.JSONDecodeError):
                        error_payload = None

                    classification = classify_rest_status(
                        response.status, error_payload, partial_category
                    )
                    mapped = map_rest_failure_category(
                        partial_category,
                        response.status,
                        error_payload,
                        classification=classification,
                    )
                    failure = build_api_failure(
                        op,
                        url,
                        response.status,
                        error_payload,
                        mapped,
                        classification,
                    )
                    _LOGGER.debug(
                        "REST GET throttled category=%s mapped=%s status=429 retry_after=%.1fs duration_ms=%.1f",
                        partial_category,
                        mapped,
                        retry_after_s,
                        _duration_ms(request_start),
                    )
                    self._raise_partial_failure(mapped, failure=failure)

                if response.status >= 400:
                    error_payload = None
                    try:
                        error_payload = _safe_ebay_error_payload(
                            await response.json(content_type=None)
                        )
                    except (aiohttp.ContentTypeError, json.JSONDecodeError):
                        error_payload = None

                    classification = classify_rest_status(
                        response.status, error_payload, partial_category
                    )
                    mapped = map_rest_failure_category(
                        partial_category,
                        response.status,
                        error_payload,
                        classification=classification,
                    )
                    failure = build_api_failure(
                        op,
                        url,
                        response.status,
                        error_payload,
                        mapped,
                        classification,
                    )
                    _LOGGER.debug(
                        "REST GET failed category=%s mapped=%s status=%s classification=%s duration_ms=%.1f",
                        partial_category,
                        mapped,
                        response.status,
                        classification,
                        _duration_ms(request_start),
                    )
                    self._raise_partial_failure(mapped, failure=failure)

                try:
                    text = await response.text()
                except (aiohttp.ClientError, UnicodeDecodeError) as exc:
                    raise EbayParseError(
                        f"Could not read eBay REST body for {partial_category}"
                    ) from exc

                if not text.strip():
                    _LOGGER.debug(
                        "REST GET completed category=%s status=%s empty_body=true duration_ms=%.1f",
                        partial_category,
                        response.status,
                        _duration_ms(request_start),
                    )
                    return {}
                try:
                    payload = json.loads(text)
                except json.JSONDecodeError as exc:
                    raise EbayParseError(
                        f"Could not parse eBay REST JSON for {partial_category}"
                    ) from exc
                _LOGGER.debug(
                    "REST GET completed category=%s status=%s duration_ms=%.1f",
                    partial_category,
                    response.status,
                    _duration_ms(request_start),
                )
                return payload if isinstance(payload, dict) else {}
        except (
            EbayAuthError,
            EbayPartialFailure,
            EbayParseError,
            asyncio.CancelledError,
        ):
            raise
        except Exception as exc:
            failure = build_api_failure(
                op,
                url,
                None,
                {"error": type(exc).__name__},
                partial_category,
                (
                    "transient"
                    if policy.is_transient_exception(exc)
                    else "unexpected_error"
                ),
            )
            _LOGGER.debug(
                "REST GET transport soft-failed category=%s operation=%s error=%s duration_ms=%.1f",
                partial_category,
                op,
                type(exc).__name__,
                _duration_ms(request_start),
            )
            self._raise_partial_failure(partial_category, failure=failure)
