"""Read-only eBay GraphQL transport client mixin."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Any

import aiohttp

from ..marketplace import marketplace_id_for_site
from ..models import ApiFailure
from ..parsers.xml import _duration_ms
from .endpoints import EbayEndpoints
from .errors import (
    EbayApiError,
    EbayAuthError,
    EbayGraphQLError,
    EbayParseError,
    build_api_failure,
    classify_rest_status,
    map_rest_failure_category,
    sanitize_graphql_errors,
)

_LOGGER = logging.getLogger(__name__)

GRAPHQL_TIMEOUT = 30  # seconds
MAX_GRAPHQL_RETRIES = 3

_AUTH_EXPIRED_CODES = {
    "UNAUTHENTICATED",
    "UNAUTHORIZED",
    "INVALID_TOKEN",
    "TOKEN_EXPIRED",
    "INVALID_ACCESS_TOKEN",
}

_AUTH_PERMISSION_CODES = {
    "ACCESS_DENIED",
    "FORBIDDEN",
    "INSUFFICIENT_SCOPE",
    "MISSING_SCOPE",
    "NOT_AUTHORIZED",
}

_RATE_LIMIT_CODES = {
    "RATE_LIMITED",
    "TOO_MANY_REQUESTS",
    "RATE_LIMIT_EXCEEDED",
    "THROTTLED",
}

_SYSTEM_ERROR_CODES = {
    "INTERNAL_SERVER_ERROR",
    "SERVICE_UNAVAILABLE",
    "SYSTEM_ERROR",
}

_STRIP_GRAPHQL_LITERALS_RE = re.compile(
    r'"""(?:\\"""|[\s\S])*?"""|"(?:\\.|[^"\\])*"|#[^\r\n]*'
)


@dataclass(frozen=True)
class GraphQLResponse:
    """Parsed eBay GraphQL execution result."""

    data: dict[str, Any] | None
    errors: list[dict[str, Any]] | None = None
    extensions: dict[str, Any] | None = None
    raw_has_errors: bool = False

    @property
    def has_errors(self) -> bool:
        """Return True if errors are present."""
        return self.raw_has_errors or bool(self.errors)


def _strip_graphql_strings_and_comments(query: str) -> str:
    """Strip string literals and comments from a GraphQL query string."""
    return _STRIP_GRAPHQL_LITERALS_RE.sub(" ", query)


def _parse_graphql_definitions(cleaned: str) -> list[tuple[str, str | None]]:
    """Parse top-level definition tokens at depth 0 from a stripped GraphQL document."""
    defs: list[tuple[str, str | None]] = []
    tokens: list[str] = []
    cur_token: list[str] = []
    brace_depth = 0

    for c in cleaned:
        if c == "{":
            if brace_depth == 0:
                if cur_token:
                    tokens.append("".join(cur_token).strip())
                    cur_token = []
                if not tokens:
                    defs.append(("query", None))
                else:
                    first = tokens[0].lower()
                    name = None
                    if (
                        len(tokens) > 1
                        and not tokens[1].startswith("(")
                        and not tokens[1].startswith("@")
                    ):
                        name = tokens[1]
                    defs.append((first, name))
                tokens = []
            brace_depth += 1
        elif c == "}":
            brace_depth -= 1
            if brace_depth < 0:
                raise ValueError("Unbalanced braces in GraphQL document")
        elif brace_depth == 0:
            if c.isspace() or c in "(),:@!=":
                if cur_token:
                    tokens.append("".join(cur_token))
                    cur_token = []
            else:
                cur_token.append(c)

    if brace_depth != 0:
        raise ValueError("Unbalanced braces in GraphQL document")
    return defs


def validate_read_only_query(query: str, operation_name: str | None = None) -> None:
    """Validate that a query string is non-empty, strictly read-only, and single-operation.

    Raises:
        ValueError: If query contains mutations, subscriptions, multiple operations,
                    unbalanced braces, or fails operation_name alignment.
    """
    if not isinstance(query, str) or not query.strip():
        raise ValueError("GraphQL query must be a non-empty string")

    cleaned = _strip_graphql_strings_and_comments(query).strip()
    if not cleaned:
        raise ValueError("GraphQL query must contain executable content")

    defs = _parse_graphql_definitions(cleaned)
    if not defs:
        raise ValueError("No executable definitions found in GraphQL document")

    for def_type, _ in defs:
        if def_type == "mutation":
            raise ValueError(
                "Mutations are strictly prohibited in ha-ebay read-only client"
            )
        if def_type == "subscription":
            raise ValueError(
                "Subscriptions are strictly prohibited in ha-ebay read-only client"
            )
        if def_type not in ("query", "fragment"):
            raise ValueError(f"Unsupported definition type: {def_type}")

    query_ops = [(t, n) for t, n in defs if t == "query"]
    if len(query_ops) == 0:
        raise ValueError("GraphQL document must contain a query operation")
    if len(query_ops) > 1:
        raise ValueError("Multi-operation GraphQL documents are not permitted")

    _, op_name = query_ops[0]
    if operation_name is not None:
        if op_name is None:
            raise ValueError(
                f'operation_name "{operation_name}" provided for anonymous GraphQL query'
            )
        if op_name != operation_name:
            raise ValueError(
                f'operation_name "{operation_name}" does not match query operation "{op_name}"'
            )


def is_graphql_token_expired(error: dict[str, Any]) -> bool:
    """Return True if error represents an expired or invalid token eligible for refresh."""
    if is_graphql_permission_error(error):
        return False
    status_code = error.get("status_code")
    if status_code == 401:
        return True
    code = error.get("code")
    if code == 1001 or code == "1001":
        return True
    error_code = str(error.get("error_code") or code or "").upper()
    if error_code in _AUTH_EXPIRED_CODES:
        return True
    message = str(error.get("message") or "").lower()
    return any(
        marker in message
        for marker in (
            "token expired",
            "token has expired",
            "access token is expired",
            "invalid access token",
            "unauthenticated",
            "invalid_token",
        )
    )


def is_graphql_permission_error(error: dict[str, Any]) -> bool:
    """Return True if error represents permanent permission, scope, or access denial."""
    status_code = error.get("status_code")
    if status_code == 403:
        return True
    code = error.get("code")
    error_code = str(error.get("error_code") or code or "").upper()
    if error_code in _AUTH_PERMISSION_CODES:
        return True
    message = str(error.get("message") or "").lower()
    return any(
        marker in message
        for marker in (
            "insufficient scope",
            "missing scope",
            "not authorized",
            "access denied",
            "permission denied",
            "forbidden",
            "does not have permission",
        )
    )


def is_graphql_rate_limit_error(error: dict[str, Any]) -> bool:
    """Return True if error represents a rate-limiting / throttling issue."""
    status_code = error.get("status_code")
    if status_code == 429:
        return True
    code = error.get("code")
    error_code = str(error.get("error_code") or code or "").upper()
    if error_code in _RATE_LIMIT_CODES:
        return True
    message = str(error.get("message") or "").lower()
    return any(
        marker in message
        for marker in (
            "rate limit",
            "rate_limited",
            "throttled",
            "too many requests",
            "call limit",
            "exceeded the call limit",
        )
    )


def is_graphql_system_error(error: dict[str, Any]) -> bool:
    """Return True if error represents an upstream system or backend service failure."""
    status_code = error.get("status_code")
    if isinstance(status_code, int) and status_code >= 500:
        return True
    category = str(error.get("category") or "").upper()
    if category == "SYSTEM":
        return True
    code = error.get("code")
    error_code = str(error.get("error_code") or code or "").upper()
    if error_code in _SYSTEM_ERROR_CODES:
        return True
    message = str(error.get("message") or "").lower()
    return "internal server error" in message or "service unavailable" in message


def is_graphql_auth_error(error: dict[str, Any]) -> bool:
    """Return True if a GraphQL error dictionary represents an auth or scope issue."""
    return is_graphql_token_expired(error) or is_graphql_permission_error(error)


class GraphQLClientMixin:
    """Read-only GraphQL POST client mixin for eBay modernized APIs."""

    _session: aiohttp.ClientSession
    _oauth_session: Any | None
    site_id: str
    _endpoints: EbayEndpoints
    _accept_language: str | None
    _api_warnings: list[dict[str, str | None]]
    _partial_failure_details: list[dict[str, Any]]
    _last_api_failure: ApiFailure | None

    async def async_get_access_token(self) -> str:
        """Return a valid OAuth access token (implemented in RestClientMixin)."""
        raise NotImplementedError

    def _clear_access_token(self) -> None:
        """Clear cached access token on auth failure (implemented in RestClientMixin)."""
        raise NotImplementedError

    async def async_refresh_access_token(self) -> str:
        """Force a fresh access token from the OAuth provider (implemented in RestClientMixin)."""
        raise NotImplementedError

    def _raise_partial_failure(
        self,
        category: str,
        *,
        details: dict[str, Any] | None = None,
        failure: ApiFailure | None = None,
    ) -> None:
        """Record diagnostic details and raise EbayPartialFailure."""
        raise NotImplementedError

    def _record_partial_failure_detail(self, detail: dict[str, Any] | None) -> None:
        """Store a sanitized failure detail for diagnostics."""
        raise NotImplementedError

    async def _refresh_and_clear_token(self) -> None:
        """Invalidate and force refresh the access token before retry."""
        if hasattr(self, "async_refresh_access_token"):
            await self.async_refresh_access_token()
        else:
            self._clear_access_token()

    async def async_post_graphql(
        self,
        query: str,
        *,
        variables: dict[str, Any] | None = None,
        operation_name: str | None = None,
        partial_category: str | None = None,
        allow_partial_data: bool = False,
        max_retries: int = MAX_GRAPHQL_RETRIES,
    ) -> dict[str, Any]:
        """Execute a read-only GraphQL query and return its 'data' mapping.

        If errors are present in the response and allow_partial_data is False,
        raises EbayGraphQLError (or EbayPartialFailure if partial_category is set)
        to prevent partial or failed collections from being misinterpreted as empty.

        Returns:
            The dict under 'data' from the response.

        Raises:
            ValueError: If a mutation or invalid query is provided.
            EbayAuthError: If authentication or scope permissions fail.
            EbayGraphQLError: If the query fails or returns unhandled errors.
            EbayPartialFailure: If partial_category is set and the query fails.
            EbayParseError: If the response is not valid JSON or lacks data/errors.
            EbayApiError: On network, HTTP 5xx, or other API failures.
        """
        response = await self.async_execute_graphql(
            query,
            variables=variables,
            operation_name=operation_name,
            partial_category=partial_category,
            max_retries=max_retries,
        )

        if response.has_errors:
            # Check for authentication errors first
            for err in response.errors or []:
                if is_graphql_token_expired(err):
                    raise EbayAuthError(
                        f"eBay GraphQL authentication error (token expired): {err.get('message')}",
                        is_expired=True,
                        is_permission_error=False,
                    )
                if is_graphql_permission_error(err):
                    raise EbayAuthError(
                        f"eBay GraphQL permission error (insufficient scope): {err.get('message')}",
                        is_expired=False,
                        is_permission_error=True,
                    )

            if not allow_partial_data:
                first_msg = (
                    response.errors[0].get("message")
                    if response.errors
                    else "Unknown GraphQL error"
                )
                msg = f"eBay GraphQL query returned errors: {first_msg}"
                classification = "malformed_request"
                if any(
                    is_graphql_system_error(err) for err in (response.errors or [])
                ) or any(
                    is_graphql_rate_limit_error(err) for err in (response.errors or [])
                ):
                    classification = "transient"

                if partial_category:
                    failure = build_api_failure(
                        operation_name or "graphql.query",
                        self._endpoints.graphql,
                        200,
                        {"errors": response.errors},
                        partial_category,
                        classification,
                    )
                    self._raise_partial_failure(partial_category, failure=failure)
                raise EbayGraphQLError(
                    msg,
                    errors=response.errors,
                    data=response.data,
                    extensions=response.extensions,
                )

            # Record warnings when partial data is accepted
            for err in response.errors:
                if err.get("message"):
                    self._api_warnings.append({"message": err["message"]})
            self._record_partial_failure_detail(
                {"operation": operation_name or "graphql", "errors": response.errors}
            )

        if response.data is None:
            msg = "eBay GraphQL response contained null data"
            if partial_category:
                failure = build_api_failure(
                    operation_name or "graphql.query",
                    self._endpoints.graphql,
                    200,
                    {"errors": response.errors},
                    partial_category,
                    "malformed_request",
                )
                self._raise_partial_failure(partial_category, failure=failure)
            raise EbayGraphQLError(
                msg,
                errors=response.errors,
                data=None,
                extensions=response.extensions,
            )

        return response.data

    async def async_execute_graphql(
        self,
        query: str,
        *,
        variables: dict[str, Any] | None = None,
        operation_name: str | None = None,
        partial_category: str | None = None,
        max_retries: int = MAX_GRAPHQL_RETRIES,
    ) -> GraphQLResponse:
        """Execute a read-only GraphQL query with auth retry and return GraphQLResponse."""
        validate_read_only_query(query, operation_name=operation_name)

        for attempt in range(2):
            try:
                return await self._async_execute_graphql_once(
                    query,
                    variables=variables,
                    operation_name=operation_name,
                    partial_category=partial_category,
                    max_retries=max_retries,
                )
            except EbayAuthError as exc:
                if (
                    attempt == 0
                    and getattr(exc, "is_expired", True)
                    and not getattr(exc, "is_permission_error", False)
                ):
                    _LOGGER.debug(
                        "GraphQL auth token expired; requesting fresh token for retry"
                    )
                    await self._refresh_and_clear_token()
                    continue
                raise
        raise EbayAuthError(
            "eBay GraphQL authentication error after token refresh",
            is_expired=True,
            is_permission_error=False,
        )

    async def _async_execute_graphql_once(
        self,
        query: str,
        *,
        variables: dict[str, Any] | None = None,
        operation_name: str | None = None,
        partial_category: str | None = None,
        max_retries: int = MAX_GRAPHQL_RETRIES,
    ) -> GraphQLResponse:
        """Send a single authenticated GraphQL request with transient HTTP retries."""
        marketplace_id = marketplace_id_for_site(self.site_id) or "EBAY_US"
        op = operation_name or "graphql.query"
        url = self._endpoints.graphql
        start = time.monotonic()

        payload: dict[str, Any] = {"query": query}
        if variables is not None:
            payload["variables"] = variables
        if operation_name is not None:
            payload["operationName"] = operation_name

        body_bytes = json.dumps(payload).encode("utf-8")
        last_transient_exc: Exception | None = None

        for retry in range(max_retries):
            access_token = await self.async_get_access_token()
            headers: dict[str, str] = {
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "X-EBAY-C-MARKETPLACE-ID": marketplace_id,
            }
            if self._accept_language:
                headers["Accept-Language"] = self._accept_language

            try:
                async with self._session.post(
                    url,
                    headers=headers,
                    data=body_bytes,
                    timeout=aiohttp.ClientTimeout(total=GRAPHQL_TIMEOUT),
                ) as response:
                    # 401 Unauthorized (expired or invalid token)
                    if response.status == 401:
                        _LOGGER.debug(
                            "GraphQL auth failed status=401 operation=%s duration_ms=%.1f",
                            op,
                            _duration_ms(start),
                        )
                        raise EbayAuthError(
                            f"eBay GraphQL auth failed with HTTP {response.status}",
                            is_expired=True,
                            is_permission_error=False,
                        )

                    # 403 Forbidden (missing scope or permanent permission denial)
                    if response.status == 403:
                        _LOGGER.debug(
                            "GraphQL forbidden status=403 operation=%s duration_ms=%.1f",
                            op,
                            _duration_ms(start),
                        )
                        raise EbayAuthError(
                            f"eBay GraphQL forbidden with HTTP 403: insufficient scope or permissions for {op}",
                            is_expired=False,
                            is_permission_error=True,
                        )

                    # 429 Rate Limit
                    if response.status == 429:
                        retry_after = response.headers.get("Retry-After")
                        sleep_s = 0.5 * (2**retry)
                        if retry_after is not None:
                            try:
                                sleep_s = min(int(retry_after), 30)
                            except (TypeError, ValueError):
                                pass
                        _LOGGER.debug(
                            "GraphQL rate-limited status=429 retry=%d/%d sleep=%.1fs operation=%s",
                            retry + 1,
                            max_retries,
                            sleep_s,
                            op,
                        )
                        if retry < max_retries - 1:
                            await asyncio.sleep(sleep_s)
                            continue
                        classification = "transient"
                        failure = build_api_failure(
                            op,
                            url,
                            429,
                            None,
                            partial_category or "graphql",
                            classification,
                        )
                        if partial_category:
                            self._raise_partial_failure(
                                partial_category, failure=failure
                            )
                        raise EbayApiError(
                            f"eBay GraphQL rate limit exceeded (HTTP 429) for {op}"
                        )

                    # 5xx Server Errors
                    if response.status >= 500:
                        sleep_s = 0.5 * (2**retry)
                        _LOGGER.debug(
                            "GraphQL server error status=%d retry=%d/%d sleep=%.1fs operation=%s",
                            response.status,
                            retry + 1,
                            max_retries,
                            sleep_s,
                            op,
                        )
                        if retry < max_retries - 1:
                            await asyncio.sleep(sleep_s)
                            continue
                        classification = "transient"
                        failure = build_api_failure(
                            op,
                            url,
                            response.status,
                            None,
                            partial_category or "graphql",
                            classification,
                        )
                        if partial_category:
                            self._raise_partial_failure(
                                partial_category, failure=failure
                            )
                        raise EbayApiError(
                            f"eBay GraphQL server error HTTP {response.status} for {op}"
                        )

                    # Other 4xx errors
                    if response.status >= 400:
                        error_json: dict[str, Any] | None = None
                        try:
                            error_json = await response.json(content_type=None)
                        except (aiohttp.ContentTypeError, json.JSONDecodeError):
                            error_json = None
                        classification = classify_rest_status(
                            response.status, error_json, partial_category or "graphql"
                        )
                        mapped = map_rest_failure_category(
                            partial_category or "graphql",
                            response.status,
                            error_json,
                            classification=classification,
                        )
                        failure = build_api_failure(
                            op,
                            url,
                            response.status,
                            error_json,
                            mapped,
                            classification,
                        )
                        if partial_category:
                            self._raise_partial_failure(mapped, failure=failure)
                        raise EbayApiError(
                            f"eBay GraphQL request failed with HTTP {response.status} for {op}"
                        )

                    # 200 OK — parse GraphQL response body
                    text = await response.text()
                    if not text.strip():
                        raise EbayParseError(
                            "Empty response body from eBay GraphQL endpoint"
                        )

                    try:
                        resp_json = json.loads(text)
                    except json.JSONDecodeError as exc:
                        raise EbayParseError(
                            "Could not parse eBay GraphQL JSON response"
                        ) from exc

                    if not isinstance(resp_json, dict):
                        raise EbayParseError(
                            "eBay GraphQL response must be a JSON object"
                        )

                    if "data" not in resp_json and "errors" not in resp_json:
                        raise EbayParseError(
                            "eBay GraphQL response missing both 'data' and 'errors'"
                        )

                    data = resp_json.get("data")
                    if data is not None and not isinstance(data, dict):
                        raise EbayParseError(
                            "GraphQL 'data' field must be a JSON object or null"
                        )

                    raw_errors = resp_json.get("errors")
                    if raw_errors is not None:
                        if not isinstance(raw_errors, list):
                            raise EbayParseError(
                                "GraphQL 'errors' field must be an array or null"
                            )
                        if not all(isinstance(err, dict) for err in raw_errors):
                            raise EbayParseError(
                                "GraphQL 'errors' array elements must be objects"
                            )

                    sanitized_errors = (
                        sanitize_graphql_errors(raw_errors)
                        if raw_errors is not None
                        else None
                    )
                    extensions = (
                        resp_json.get("extensions")
                        if isinstance(resp_json.get("extensions"), dict)
                        else None
                    )

                    # Inspect execution-time errors inside HTTP 200 response
                    if sanitized_errors:
                        # 1. Check for token expiration (eligible for refresh & retry)
                        for err in sanitized_errors:
                            if is_graphql_token_expired(err):
                                _LOGGER.debug(
                                    "GraphQL execution error indicated token expired: %s",
                                    err.get("message"),
                                )
                                raise EbayAuthError(
                                    f"eBay GraphQL authentication error (token expired): {err.get('message')}",
                                    is_expired=True,
                                    is_permission_error=False,
                                )

                        # 2. Check for permanent scope or permission error (no refresh/retry)
                        for err in sanitized_errors:
                            if is_graphql_permission_error(err):
                                _LOGGER.debug(
                                    "GraphQL execution error indicated permission/scope denial: %s",
                                    err.get("message"),
                                )
                                raise EbayAuthError(
                                    f"eBay GraphQL permission error (insufficient scope): {err.get('message')}",
                                    is_expired=False,
                                    is_permission_error=True,
                                )

                        # 3. Check for rate limit / throttling (transient)
                        rate_limited = False
                        for err in sanitized_errors:
                            if is_graphql_rate_limit_error(err):
                                rate_limited = True
                                sleep_s = 0.5 * (2**retry)
                                _LOGGER.debug(
                                    "GraphQL execution throttled retry=%d/%d sleep=%.1fs op=%s",
                                    retry + 1,
                                    max_retries,
                                    sleep_s,
                                    op,
                                )
                                if retry < max_retries - 1:
                                    await asyncio.sleep(sleep_s)
                                    break
                                failure = build_api_failure(
                                    op,
                                    url,
                                    200,
                                    {"errors": sanitized_errors},
                                    partial_category or "graphql",
                                    "transient",
                                )
                                if partial_category:
                                    self._raise_partial_failure(
                                        partial_category, failure=failure
                                    )
                                raise EbayApiError(
                                    f"eBay GraphQL rate limit exceeded for {op}"
                                )
                        if rate_limited and retry < max_retries - 1:
                            continue

                        # 4. Check for system / backend service error (transient when data is None)
                        if data is None:
                            system_error = False
                            for err in sanitized_errors:
                                if is_graphql_system_error(err):
                                    system_error = True
                                    sleep_s = 0.5 * (2**retry)
                                    _LOGGER.debug(
                                        "GraphQL system error retry=%d/%d sleep=%.1fs op=%s",
                                        retry + 1,
                                        max_retries,
                                        sleep_s,
                                        op,
                                    )
                                    if retry < max_retries - 1:
                                        await asyncio.sleep(sleep_s)
                                        break
                                    failure = build_api_failure(
                                        op,
                                        url,
                                        200,
                                        {"errors": sanitized_errors},
                                        partial_category or "graphql",
                                        "transient",
                                    )
                                    if partial_category:
                                        self._raise_partial_failure(
                                            partial_category, failure=failure
                                        )
                                    raise EbayApiError(
                                        f"eBay GraphQL system error for {op}: {err.get('message')}"
                                    )
                            if system_error and retry < max_retries - 1:
                                continue

                    _LOGGER.debug(
                        "GraphQL request completed operation=%s has_errors=%s duration_ms=%.1f",
                        op,
                        bool(sanitized_errors),
                        _duration_ms(start),
                    )
                    return GraphQLResponse(
                        data=data,
                        errors=sanitized_errors,
                        extensions=extensions,
                        raw_has_errors=bool(raw_errors),
                    )

            except (aiohttp.ClientError, TimeoutError) as exc:
                last_transient_exc = exc
                sleep_s = 0.5 * (2**retry)
                _LOGGER.debug(
                    "GraphQL transport error retry=%d/%d error=%s sleep=%.1fs operation=%s",
                    retry + 1,
                    max_retries,
                    type(exc).__name__,
                    sleep_s,
                    op,
                )
                if retry < max_retries - 1:
                    await asyncio.sleep(sleep_s)
                    continue

        # Transient retries exhausted
        failure = build_api_failure(
            op,
            url,
            None,
            None,
            partial_category or "graphql",
            "transient",
        )
        if partial_category:
            self._raise_partial_failure(partial_category, failure=failure)
        raise EbayApiError(
            f"eBay GraphQL request failed after {max_retries} retries for {op}"
        ) from last_transient_exc
