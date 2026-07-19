"""OAuth token + REST GET client mixin."""

from __future__ import annotations

import asyncio
import base64
from datetime import datetime, timedelta, timezone
import json
import logging
import time
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
    EbayAuthError,
    EbayAuthTransientError,
    EbayPartialFailure,
    EbayParseError,
    _PARTIAL_FAILURE_DETAILS_MAX,
    _safe_ebay_error_payload,
    build_api_failure,
    classify_rest_status,
    map_rest_failure_category,
)

_LOGGER = logging.getLogger(__name__)

TOKEN_REFRESH_MARGIN = timedelta(minutes=5)


def _basic_auth_header(client_id: str, client_secret: str) -> str:
    basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    return f"Basic {basic}"


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
        """GET a JSON REST resource with auth retry."""
        for attempt in range(2):
            try:
                return await self._async_rest_get_once(
                    url,
                    params=params,
                    partial_category=partial_category,
                    allow_404=allow_404,
                    operation=operation,
                    accept_language=accept_language,
                )
            except EbayAuthError:
                self._clear_access_token()
                if attempt == 0:
                    continue
                raise
        raise EbayAuthError(f"eBay REST auth failed for {partial_category}")

    async def _async_rest_get_once(
        self,
        url: str,
        *,
        params: dict[str, Any] | list[tuple[str, str]] | None = None,
        partial_category: str,
        allow_404: bool = False,
        operation: str | None = None,
        accept_language: str | None = None,
    ) -> dict[str, Any]:
        """GET a JSON REST resource without auth retry."""
        op = operation or f"{partial_category}.get"
        language = (
            accept_language if accept_language is not None else self._accept_language
        )
        access_token = await self.async_get_access_token()
        request_start = time.monotonic()
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
                timeout=aiohttp.ClientTimeout(total=30),
            ) as response:
                if response.status == 401:
                    raise EbayAuthError(
                        f"eBay REST auth failed with HTTP {response.status}"
                    )
                if response.status == 204:
                    return {}
                if allow_404 and response.status == 404:
                    return {}

                if response.status >= 400:
                    error_payload: dict[str, Any] | None = None
                    try:
                        error_payload = _safe_ebay_error_payload(
                            await response.json(content_type=None)
                        )
                    except (aiohttp.ContentTypeError, json.JSONDecodeError):
                        error_payload = None

                    if response.status == 429:
                        retry_after = response.headers.get("Retry-After")
                        sleep_s = 0
                        if retry_after is not None:
                            try:
                                sleep_s = min(int(retry_after), 5)
                            except (TypeError, ValueError):
                                sleep_s = 0
                        if sleep_s > 0:
                            await asyncio.sleep(sleep_s)

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
                        "REST GET soft-failed category=%s mapped=%s status=%s "
                        "classification=%s operation=%s duration_ms=%.1f",
                        partial_category,
                        mapped,
                        response.status,
                        classification,
                        op,
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
                        "REST GET completed category=%s status=%s empty_body=true "
                        "duration_ms=%.1f",
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
        except EbayAuthError:
            raise
        except EbayPartialFailure:
            raise
        except (aiohttp.ClientError, TimeoutError) as exc:
            failure = build_api_failure(
                op,
                url,
                None,
                None,
                partial_category,
                "transient",
            )
            _LOGGER.debug(
                "REST GET transport soft-failed category=%s operation=%s "
                "error=%s duration_ms=%.1f",
                partial_category,
                op,
                type(exc).__name__,
                _duration_ms(request_start),
            )
            self._raise_partial_failure(partial_category, failure=failure)
