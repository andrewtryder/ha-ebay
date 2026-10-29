"""Unit tests for eBay GraphQL transport layer."""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest
from homeassistant.helpers.config_entry_oauth2_flow import (
    OAuth2Session,
    OAuth2TokenRequestReauthError,
    OAuth2TokenRequestTransientError,
)

from custom_components.ebay import _async_update_listener
from custom_components.ebay.api import (
    EbayApiClient,
    EbayApiError,
    EbayAuthError,
    EbayAuthTransientError,
    EbayGraphQLError,
    EbayParseError,
    EbayPartialFailure,
    GraphQLResponse,
    endpoints_for,
    is_graphql_auth_error,
    is_graphql_rate_limit_error,
    sanitize_graphql_errors,
    validate_read_only_query,
)


class _Response:
    """Mock aiohttp HTTP response."""

    def __init__(
        self,
        *,
        status: int = 200,
        text: str | None = None,
        json_payload: Any | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status = status
        self.headers = headers or {}
        self._text = text
        self._json_payload = json_payload

    async def text(self) -> str:
        if self._text is not None:
            return self._text
        if self._json_payload is not None:
            return json.dumps(self._json_payload)
        return ""

    async def json(self, *, content_type: str | None = None) -> Any:
        if self._json_payload is not None:
            return self._json_payload
        if self._text is not None and self._text.strip():
            return json.loads(self._text)
        return {}


class _Context:
    """Mock aiohttp context manager."""

    def __init__(self, response: _Response) -> None:
        self._response = response

    async def __aenter__(self) -> _Response:
        return self._response

    async def __aexit__(self, *args: object) -> None:
        return None


def _make_client(
    session: Any, site_id: str = "0", environment: str = "production"
) -> EbayApiClient:
    """Helper to create a configured test EbayApiClient."""
    client = EbayApiClient(
        session,
        environment=environment,
        client_id="test_client_id",
        client_secret="test_secret",
        runame="test_runame",
        refresh_token="test_refresh_token",
        site_id=site_id,
    )
    client._access_token = "mock_valid_token"
    client._access_token_expires_at = datetime.now(timezone.utc) + timedelta(hours=1)
    return client


# -----------------------------------------------------------------------------
# 1. Query Validation & Read-Only Enforcement Tests
# -----------------------------------------------------------------------------


def test_validate_read_only_query_valid() -> None:
    """Valid read-only GraphQL queries pass validation."""
    validate_read_only_query("query { userWatchList { items { itemId } } }")
    validate_read_only_query("query userWatchListQuery { userWatchList { total } }")
    validate_read_only_query("{ userBiddingActivity { items { itemId } } }")
    validate_read_only_query("# comment\nquery { userWatchList { items { title } } }")
    validate_read_only_query("  \n  query { sellerListingSearch { total } }  \n  ")


def test_validate_read_only_query_rejects_empty_or_non_string() -> None:
    """Empty, whitespace, or non-string queries raise ValueError."""
    with pytest.raises(ValueError, match="non-empty string"):
        validate_read_only_query("")
    with pytest.raises(ValueError, match="non-empty string"):
        validate_read_only_query("   \n\t  ")
    with pytest.raises(ValueError, match="non-empty string"):
        validate_read_only_query(None)  # type: ignore[arg-type]


def test_validate_read_only_query_rejects_mutations() -> None:
    """Mutations are strictly rejected at the transport level."""
    with pytest.raises(ValueError, match="Mutations are strictly prohibited"):
        validate_read_only_query(
            "mutation addToWatchList($input: AddToWatchListInput!) { addToWatchList(input: $input) { itemId } }"
        )
    with pytest.raises(ValueError, match="Mutations are strictly prohibited"):
        validate_read_only_query(
            '  mutation { removeFromWatchList(itemId: "123") { success } }'
        )
    with pytest.raises(ValueError, match="Mutations are strictly prohibited"):
        validate_read_only_query(
            "# Add to watch\nmutation { placeProxyBid { status } }"
        )
    with pytest.raises(ValueError, match="Mutations are strictly prohibited"):
        validate_read_only_query("MUTATION { createListing { listingId } }")


# -----------------------------------------------------------------------------
# 2. Error Classifier & Sanitizer Tests
# -----------------------------------------------------------------------------


def test_is_graphql_auth_error_classifiers() -> None:
    """Test classification of GraphQL auth errors."""
    assert is_graphql_auth_error({"status_code": 401}) is True
    assert is_graphql_auth_error({"status_code": 403}) is True
    assert is_graphql_auth_error({"error_code": "UNAUTHENTICATED"}) is True
    assert is_graphql_auth_error({"error_code": "UNAUTHORIZED"}) is True
    assert is_graphql_auth_error({"error_code": "ACCESS_DENIED"}) is True
    assert is_graphql_auth_error({"error_code": "FORBIDDEN"}) is True
    assert is_graphql_auth_error({"error_code": "INVALID_TOKEN"}) is True
    assert is_graphql_auth_error({"error_code": "TOKEN_EXPIRED"}) is True
    assert is_graphql_auth_error({"message": "Invalid access token provided"}) is True
    assert is_graphql_auth_error({"message": "User is unauthenticated"}) is True
    assert (
        is_graphql_auth_error({"message": "Insufficient scope for this resource"})
        is True
    )
    assert is_graphql_auth_error({"message": "Access denied to field"}) is True

    # Non-auth errors
    assert is_graphql_auth_error({"message": "Item not found"}) is False
    assert is_graphql_auth_error({"error_code": "NOT_FOUND"}) is False
    assert is_graphql_auth_error({}) is False


def test_is_graphql_rate_limit_error_classifiers() -> None:
    """Test classification of GraphQL rate limit errors."""
    assert is_graphql_rate_limit_error({"status_code": 429}) is True
    assert is_graphql_rate_limit_error({"error_code": "TOO_MANY_REQUESTS"}) is True
    assert is_graphql_rate_limit_error({"error_code": "RATE_LIMIT_EXCEEDED"}) is True
    assert is_graphql_rate_limit_error({"error_code": "THROTTLED"}) is True
    assert is_graphql_rate_limit_error({"message": "Rate limit exceeded"}) is True
    assert (
        is_graphql_rate_limit_error({"message": "Request throttled due to high volume"})
        is True
    )

    # Non-rate-limit errors
    assert is_graphql_rate_limit_error({"message": "Syntax error"}) is False
    assert is_graphql_rate_limit_error({}) is False


def test_sanitize_graphql_errors() -> None:
    """Test sanitization of GraphQL error arrays."""
    assert sanitize_graphql_errors(None) == []
    assert sanitize_graphql_errors([]) == []

    malformed = sanitize_graphql_errors(["not a dict"])  # type: ignore[list-item]
    assert len(malformed) == 1
    assert malformed[0]["message"] == "not a dict"
    assert malformed[0]["error_code"] == "MALFORMED_ERROR"

    raw_errors = [
        {
            "message": "Field 'unknown' doesn't exist",
            "path": ["userWatchList", "unknown"],
            "locations": [{"line": 3, "column": 5}],
            "extensions": {
                "errorId": 1001,
                "errorCode": "FIELD_NOT_FOUND",
                "domain": "GRAPHQL",
                "category": "REQUEST",
                "statusCode": 400,
            },
        }
    ]
    sanitized = sanitize_graphql_errors(raw_errors)
    assert len(sanitized) == 1
    assert sanitized[0]["message"] == "Field 'unknown' doesn't exist"
    assert sanitized[0]["path"] == ["userWatchList", "unknown"]
    assert sanitized[0]["locations"] == [{"line": 3, "column": 5}]
    assert sanitized[0]["error_id"] == 1001
    assert sanitized[0]["error_code"] == "FIELD_NOT_FOUND"
    assert sanitized[0]["domain"] == "GRAPHQL"
    assert sanitized[0]["category"] == "REQUEST"
    assert sanitized[0]["status_code"] == 400


# -----------------------------------------------------------------------------
# 3. Endpoint Configuration Tests
# -----------------------------------------------------------------------------


def test_graphql_endpoints_production_and_sandbox() -> None:
    """GraphQL endpoint is correctly set for production and sandbox."""
    prod = endpoints_for("production")
    assert prod.graphql == "https://graphqlapi.ebay.com/graphql"

    sandbox = endpoints_for("sandbox")
    assert sandbox.graphql == "https://graphqlapi.sandbox.ebay.com/graphql"


# -----------------------------------------------------------------------------
# 4. Request Construction & Headers Tests
# -----------------------------------------------------------------------------


def test_headers_and_payload_construction() -> None:
    """Verify headers, marketplace ID, accept-language, and payload structure."""
    captured: dict[str, Any] = {}

    class MockSession:
        def post(self, url: str, **kwargs: Any) -> _Context:
            captured["url"] = url
            captured["headers"] = kwargs.get("headers")
            captured["data"] = kwargs.get("data")
            captured["timeout"] = kwargs.get("timeout")
            resp = _Response(
                status=200, json_payload={"data": {"userWatchList": {"total": 5}}}
            )
            return _Context(resp)

    session = MockSession()
    # site_id="3" maps to EBAY_GB
    client = _make_client(session, site_id="3", environment="production")

    query = "query GetWatchList($limit: Int) { userWatchList(limit: $limit) { total } }"
    variables = {"limit": 50}

    data = asyncio.run(
        client.async_post_graphql(
            query,
            variables=variables,
            operation_name="GetWatchList",
        )
    )

    assert data == {"userWatchList": {"total": 5}}
    assert captured["url"] == "https://graphqlapi.ebay.com/graphql"
    headers = captured["headers"]
    assert headers["Authorization"] == "Bearer mock_valid_token"
    assert headers["Content-Type"] == "application/json"
    assert headers["Accept"] == "application/json"
    assert headers["X-EBAY-C-MARKETPLACE-ID"] == "EBAY_GB"

    # Verify JSON payload sent in body
    body = json.loads(captured["data"].decode("utf-8"))
    assert body["query"] == query
    assert body["variables"] == variables
    assert body["operationName"] == "GetWatchList"


def test_headers_locale_language_propagation() -> None:
    """Verify Accept-Language header is propagated when site_id has a locale."""
    captured: dict[str, Any] = {}

    class MockSession:
        def post(self, url: str, **kwargs: Any) -> _Context:
            captured["headers"] = kwargs.get("headers")
            return _Context(_Response(status=200, json_payload={"data": {"ok": True}}))

    session = MockSession()
    # site_id="210" is Canada French -> Accept-Language: fr-CA
    client = _make_client(session, site_id="210")

    asyncio.run(client.async_post_graphql("query { test }"))
    assert captured["headers"]["Accept-Language"] == "fr-CA"
    assert captured["headers"]["X-EBAY-C-MARKETPLACE-ID"] == "EBAY_CA"


# -----------------------------------------------------------------------------
# 5. Success and Response Handling Tests
# -----------------------------------------------------------------------------


def test_successful_graphql_query_returns_data() -> None:
    """Clean GraphQL response with data returns the data dictionary."""
    mock_payload = {
        "data": {
            "userWatchList": {
                "total": 2,
                "items": [
                    {
                        "itemId": "111",
                        "title": "Item One",
                        "price": {"value": 10.5, "currency": "USD"},
                    },
                    {
                        "itemId": "222",
                        "title": "Item Two",
                        "price": {"value": 25.0, "currency": "USD"},
                    },
                ],
            }
        }
    }

    class MockSession:
        def post(self, url: str, **kwargs: Any) -> _Context:
            return _Context(_Response(status=200, json_payload=mock_payload))

    client = _make_client(MockSession())
    result = asyncio.run(client.async_post_graphql("query { userWatchList { total } }"))

    assert "userWatchList" in result
    assert result["userWatchList"]["total"] == 2
    assert len(result["userWatchList"]["items"]) == 2


def test_async_execute_graphql_returns_graphql_response() -> None:
    """async_execute_graphql returns a complete GraphQLResponse instance."""
    mock_payload = {
        "data": {"userBiddingActivity": {"total": 1}},
        "extensions": {"durationMs": 42},
    }

    class MockSession:
        def post(self, url: str, **kwargs: Any) -> _Context:
            return _Context(_Response(status=200, json_payload=mock_payload))

    client = _make_client(MockSession())
    resp = asyncio.run(
        client.async_execute_graphql("query { userBiddingActivity { total } }")
    )

    assert isinstance(resp, GraphQLResponse)
    assert resp.data == {"userBiddingActivity": {"total": 1}}
    assert resp.errors is None
    assert resp.has_errors is False
    assert resp.extensions == {"durationMs": 42}


# -----------------------------------------------------------------------------
# 6. Top-Level and Partial Errors Safeguards Tests
# -----------------------------------------------------------------------------


def test_graphql_top_level_error_raises_graphql_error() -> None:
    """When GraphQL returns top-level errors and null data, EbayGraphQLError is raised."""
    mock_payload = {
        "data": None,
        "errors": [
            {
                "message": "Field 'nonExistent' is not defined on type 'Query'",
                "locations": [{"line": 1, "column": 9}],
            }
        ],
    }

    class MockSession:
        def post(self, url: str, **kwargs: Any) -> _Context:
            return _Context(_Response(status=200, json_payload=mock_payload))

    client = _make_client(MockSession())

    with pytest.raises(EbayGraphQLError) as exc_info:
        asyncio.run(client.async_post_graphql("query { nonExistent }"))

    assert "Field 'nonExistent' is not defined" in str(exc_info.value)
    assert len(exc_info.value.errors) == 1
    assert exc_info.value.data is None


def test_graphql_partial_error_rejects_by_default_to_preserve_safeguards() -> None:
    """Never treat incomplete GraphQL collections as successful empty collections."""
    mock_payload = {
        "data": {"userWatchList": None},
        "errors": [
            {
                "message": "Downstream timeout fetching watchlist items",
                "path": ["userWatchList"],
                "extensions": {"errorCode": "SERVICE_UNAVAILABLE"},
            }
        ],
    }

    class MockSession:
        def post(self, url: str, **kwargs: Any) -> _Context:
            return _Context(_Response(status=200, json_payload=mock_payload))

    client = _make_client(MockSession())

    # By default, allow_partial_data=False prevents returning {"userWatchList": None}
    with pytest.raises(EbayGraphQLError) as exc_info:
        asyncio.run(
            client.async_post_graphql("query { userWatchList { items { itemId } } }")
        )

    assert "Downstream timeout fetching watchlist items" in str(exc_info.value)
    assert exc_info.value.errors[0]["path"] == ["userWatchList"]


def test_graphql_partial_error_accepted_when_flag_enabled() -> None:
    """When allow_partial_data is True, return partial data and log warnings."""
    mock_payload = {
        "data": {
            "userWatchList": {"total": 1, "items": [{"itemId": "123"}]},
            "userCustomLists": None,
        },
        "errors": [
            {
                "message": "Custom lists unavailable",
                "path": ["userCustomLists"],
            }
        ],
    }

    class MockSession:
        def post(self, url: str, **kwargs: Any) -> _Context:
            return _Context(_Response(status=200, json_payload=mock_payload))

    client = _make_client(MockSession())

    data = asyncio.run(
        client.async_post_graphql(
            "query { userWatchList { total } userCustomLists { total } }",
            allow_partial_data=True,
        )
    )

    assert data["userWatchList"]["total"] == 1
    assert data["userCustomLists"] is None
    # Warnings and partial failure details recorded
    assert any(
        "Custom lists unavailable" in str(w.get("message"))
        for w in client._api_warnings
    )
    assert len(client._partial_failure_details) > 0


def test_graphql_null_data_without_errors_raises_graphql_error() -> None:
    """A response with data=null and errors=[] raises EbayGraphQLError."""
    mock_payload = {"data": None, "errors": []}

    class MockSession:
        def post(self, url: str, **kwargs: Any) -> _Context:
            return _Context(_Response(status=200, json_payload=mock_payload))

    client = _make_client(MockSession())
    with pytest.raises(EbayGraphQLError, match="contained null data"):
        asyncio.run(client.async_post_graphql("query { userWatchList { total } }"))


# -----------------------------------------------------------------------------
# 7. Authentication, Expiration, and Scope Errors Tests
# -----------------------------------------------------------------------------


def test_graphql_errors_with_auth_failure_triggers_auth_error() -> None:
    """GraphQL errors indicating authentication/scope failure raise EbayAuthError."""
    mock_payload = {
        "data": None,
        "errors": [
            {
                "message": "Invalid access token or token expired",
                "extensions": {"errorCode": "UNAUTHENTICATED", "statusCode": 401},
            }
        ],
    }

    class MockSession:
        def post(self, url: str, **kwargs: Any) -> _Context:
            return _Context(_Response(status=200, json_payload=mock_payload))

    client = _make_client(MockSession())
    client.async_refresh_access_token = AsyncMock(return_value="mock_refreshed_token")  # type: ignore[assignment]
    client.async_get_access_token = AsyncMock(return_value="mock_refreshed_token")  # type: ignore[assignment]

    with pytest.raises(EbayAuthError, match="authentication error"):
        asyncio.run(client.async_post_graphql("query { userWatchList { total } }"))


def test_graphql_auth_retry_clears_token_and_succeeds() -> None:
    """HTTP 401 on initial attempt invalidates token, refreshes, and succeeds on retry."""
    attempts = 0

    class MockSession:
        def post(self, url: str, **kwargs: Any) -> _Context:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                return _Context(_Response(status=401, text="Unauthorized"))
            return _Context(
                _Response(
                    status=200, json_payload={"data": {"userWatchList": {"total": 10}}}
                )
            )

    client = _make_client(MockSession())

    async def _mock_refresh() -> str:
        client._access_token = "fresh_new_token"
        return "fresh_new_token"

    client.async_get_access_token = _mock_refresh  # type: ignore[assignment]

    result = asyncio.run(client.async_post_graphql("query { userWatchList { total } }"))

    assert attempts == 2
    assert result == {"userWatchList": {"total": 10}}


def test_graphql_persistent_auth_failure_raises_after_retry() -> None:
    """HTTP 401 on both attempts raises EbayAuthError."""
    attempts = 0

    class MockSession:
        def post(self, url: str, **kwargs: Any) -> _Context:
            nonlocal attempts
            attempts += 1
            return _Context(_Response(status=401, text="Unauthorized"))

    client = _make_client(MockSession())
    client.async_get_access_token = AsyncMock(return_value="refreshed_token")

    with pytest.raises(EbayAuthError, match="auth failed with HTTP 401"):
        asyncio.run(client.async_post_graphql("query { userWatchList { total } }"))

    assert attempts == 2


def test_graphql_http_403_forbidden_raises_auth_error() -> None:
    """HTTP 403 Forbidden raises EbayAuthError."""

    class MockSession:
        def post(self, url: str, **kwargs: Any) -> _Context:
            return _Context(_Response(status=403, text="Forbidden"))

    client = _make_client(MockSession())
    client.async_get_access_token = AsyncMock(return_value="mock_token")

    with pytest.raises(EbayAuthError, match="forbidden with HTTP 403"):
        asyncio.run(client.async_post_graphql("query { userWatchList { total } }"))


# -----------------------------------------------------------------------------
# 8. Rate Limiting (429) & Server Errors (5xx) Retries Tests
# -----------------------------------------------------------------------------


def test_graphql_http_429_honors_retry_after() -> None:
    """HTTP 429 rate limit honors Retry-After header and succeeds on retry."""
    attempts = 0

    class MockSession:
        def post(self, url: str, **kwargs: Any) -> _Context:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                return _Context(_Response(status=429, headers={"Retry-After": "0"}))
            return _Context(
                _Response(status=200, json_payload={"data": {"success": True}})
            )

    client = _make_client(MockSession())

    with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        result = asyncio.run(client.async_post_graphql("query { success }"))
        assert attempts == 2
        assert result == {"success": True}
        assert mock_sleep.called


def test_graphql_http_429_exhausted_raises_api_error() -> None:
    """Exhausting retries on HTTP 429 raises EbayApiError."""

    class MockSession:
        def post(self, url: str, **kwargs: Any) -> _Context:
            return _Context(_Response(status=429, headers={"Retry-After": "0"}))

    client = _make_client(MockSession())

    with (
        patch("asyncio.sleep", new_callable=AsyncMock),
        pytest.raises(EbayApiError, match="rate limit exceeded"),
    ):
        asyncio.run(client.async_post_graphql("query { success }", max_retries=2))


def test_graphql_http_500_retries_and_succeeds() -> None:
    """HTTP 500 server error retries with exponential backoff and succeeds."""
    attempts = 0

    class MockSession:
        def post(self, url: str, **kwargs: Any) -> _Context:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                return _Context(_Response(status=500, text="Internal Server Error"))
            return _Context(
                _Response(status=200, json_payload={"data": {"recovered": True}})
            )

    client = _make_client(MockSession())

    with patch("asyncio.sleep", new_callable=AsyncMock):
        result = asyncio.run(client.async_post_graphql("query { recovered }"))
        assert attempts == 2
        assert result == {"recovered": True}


def test_graphql_http_5xx_exhausted_raises_api_error() -> None:
    """Exhausting retries on HTTP 503 raises EbayApiError."""

    class MockSession:
        def post(self, url: str, **kwargs: Any) -> _Context:
            return _Context(_Response(status=503, text="Service Unavailable"))

    client = _make_client(MockSession())

    with (
        patch("asyncio.sleep", new_callable=AsyncMock),
        pytest.raises(EbayApiError, match="server error HTTP 503"),
    ):
        asyncio.run(client.async_post_graphql("query { test }", max_retries=2))


def test_graphql_network_timeout_retries_and_raises() -> None:
    """Network connection errors retry and raise EbayApiError on failure."""

    class MockSession:
        def post(self, url: str, **kwargs: Any) -> _Context:
            raise aiohttp.ClientConnectionError("Connection reset by peer")

    client = _make_client(MockSession())

    with (
        patch("asyncio.sleep", new_callable=AsyncMock),
        pytest.raises(EbayApiError, match="failed after 2 retries"),
    ):
        asyncio.run(client.async_post_graphql("query { test }", max_retries=2))


# -----------------------------------------------------------------------------
# 9. Parsing & Malformed Response Tests
# -----------------------------------------------------------------------------


def test_graphql_empty_response_body_raises_parse_error() -> None:
    """Empty body from GraphQL endpoint raises EbayParseError."""

    class MockSession:
        def post(self, url: str, **kwargs: Any) -> _Context:
            return _Context(_Response(status=200, text="   "))

    client = _make_client(MockSession())
    with pytest.raises(EbayParseError, match="Empty response body"):
        asyncio.run(client.async_post_graphql("query { test }"))


def test_graphql_invalid_json_raises_parse_error() -> None:
    """Non-JSON response text raises EbayParseError."""

    class MockSession:
        def post(self, url: str, **kwargs: Any) -> _Context:
            return _Context(
                _Response(status=200, text="<html><body>Gateway Error</body></html>")
            )

    client = _make_client(MockSession())
    with pytest.raises(EbayParseError, match="Could not parse eBay GraphQL JSON"):
        asyncio.run(client.async_post_graphql("query { test }"))


def test_graphql_non_dict_json_raises_parse_error() -> None:
    """JSON response that is an array or primitive raises EbayParseError."""

    class MockSession:
        def post(self, url: str, **kwargs: Any) -> _Context:
            return _Context(_Response(status=200, text="[1, 2, 3]"))

    client = _make_client(MockSession())
    with pytest.raises(EbayParseError, match="must be a JSON object"):
        asyncio.run(client.async_post_graphql("query { test }"))


def test_graphql_missing_data_and_errors_raises_parse_error() -> None:
    """JSON response missing both 'data' and 'errors' raises EbayParseError."""

    class MockSession:
        def post(self, url: str, **kwargs: Any) -> _Context:
            return _Context(_Response(status=200, json_payload={"otherField": "value"}))

    client = _make_client(MockSession())
    with pytest.raises(EbayParseError, match="missing both 'data' and 'errors'"):
        asyncio.run(client.async_post_graphql("query { test }"))


def test_graphql_invalid_data_type_raises_parse_error() -> None:
    """'data' field that is a list or number raises EbayParseError."""

    class MockSession:
        def post(self, url: str, **kwargs: Any) -> _Context:
            return _Context(
                _Response(status=200, json_payload={"data": ["list", "not", "dict"]})
            )

    client = _make_client(MockSession())
    with pytest.raises(
        EbayParseError, match="'data' field must be a JSON object or null"
    ):
        asyncio.run(client.async_post_graphql("query { test }"))


def test_graphql_invalid_errors_type_raises_parse_error() -> None:
    """'errors' field that is not a list raises EbayParseError."""

    class MockSession:
        def post(self, url: str, **kwargs: Any) -> _Context:
            return _Context(
                _Response(status=200, json_payload={"errors": "not an array"})
            )

    client = _make_client(MockSession())
    with pytest.raises(EbayParseError, match="'errors' field must be an array or null"):
        asyncio.run(client.async_post_graphql("query { test }"))


# -----------------------------------------------------------------------------
# 10. Partial Category & Diagnostics Integration Tests
# -----------------------------------------------------------------------------


def test_graphql_partial_category_raises_partial_failure() -> None:
    """When partial_category is passed and query fails, EbayPartialFailure is raised."""

    class MockSession:
        def post(self, url: str, **kwargs: Any) -> _Context:
            return _Context(_Response(status=500, text="Internal Error"))

    client = _make_client(MockSession())

    with patch("asyncio.sleep", new_callable=AsyncMock):
        with pytest.raises(EbayPartialFailure) as exc_info:
            asyncio.run(
                client.async_post_graphql(
                    "query { userWatchList { total } }",
                    partial_category="buying",
                    max_retries=1,
                )
            )

        assert exc_info.value.category == "buying"
        assert exc_info.value.failure is not None
        assert exc_info.value.failure.classification == "transient"


def test_graphql_errors_with_partial_category_raises_partial_failure() -> None:
    """GraphQL response with errors and partial_category raises EbayPartialFailure."""

    class MockSession:
        def post(self, url: str, **kwargs: Any) -> _Context:
            return _Context(
                _Response(
                    status=200,
                    json_payload={
                        "data": {"userWatchList": None},
                        "errors": [{"message": "Field error"}],
                    },
                )
            )

    client = _make_client(MockSession())

    with pytest.raises(EbayPartialFailure) as exc_info:
        asyncio.run(
            client.async_post_graphql(
                "query { userWatchList { total } }",
                partial_category="buying",
            )
        )
    assert exc_info.value.category == "buying"


def test_graphql_null_data_with_partial_category_raises_partial_failure() -> None:
    """GraphQL response with null data and partial_category raises EbayPartialFailure."""

    class MockSession:
        def post(self, url: str, **kwargs: Any) -> _Context:
            return _Context(_Response(status=200, json_payload={"data": None}))

    client = _make_client(MockSession())

    with pytest.raises(EbayPartialFailure) as exc_info:
        asyncio.run(
            client.async_post_graphql(
                "query { userWatchList { total } }",
                partial_category="buying",
            )
        )
    assert exc_info.value.category == "buying"


def test_graphql_http_400_raises_api_error_or_partial_failure() -> None:
    """HTTP 400 Bad Request raises EbayPartialFailure or EbayApiError."""

    class MockSession:
        def post(self, url: str, **kwargs: Any) -> _Context:
            return _Context(
                _Response(
                    status=400,
                    json_payload={"errors": [{"message": "Malformed syntax"}]},
                )
            )

    client = _make_client(MockSession())

    with pytest.raises(EbayApiError, match="HTTP 400"):
        asyncio.run(client.async_post_graphql("query { userWatchList { total } }"))

    with pytest.raises(EbayPartialFailure):
        asyncio.run(
            client.async_post_graphql(
                "query { userWatchList { total } }",
                partial_category="buying",
            )
        )


def test_graphql_client_mixin_stubs() -> None:
    """Test stub methods on raw GraphQLClientMixin raise NotImplementedError."""
    from custom_components.ebay.clients.graphql import GraphQLClientMixin

    mixin = GraphQLClientMixin()
    with pytest.raises(NotImplementedError):
        asyncio.run(mixin.async_get_access_token())
    with pytest.raises(NotImplementedError):
        mixin._clear_access_token()
    with pytest.raises(NotImplementedError):
        mixin._raise_partial_failure("test")
    with pytest.raises(NotImplementedError):
        mixin._record_partial_failure_detail({})


# -----------------------------------------------------------------------------
# 11. Security, Multi-Operation, and Review Regression Tests
# -----------------------------------------------------------------------------


def test_validate_read_only_rejects_multi_operation_documents() -> None:
    """Multi-operation GraphQL documents are rejected before dispatch."""
    with pytest.raises(
        ValueError, match="Multi-operation GraphQL documents are not permitted"
    ):
        validate_read_only_query("query A { total } query B { total }")

    with pytest.raises(ValueError, match="Mutations are strictly prohibited"):
        validate_read_only_query("query Safe { total } mutation Unsafe { delete }")

    with pytest.raises(ValueError, match="Mutations are strictly prohibited"):
        validate_read_only_query("mutation Unsafe { delete } query Safe { total }")

    with pytest.raises(ValueError, match="Subscriptions are strictly prohibited"):
        validate_read_only_query("subscription OnUpdate { item }")


def test_validate_read_only_operation_name_handling() -> None:
    """operation_name must match the single named query and cannot select mutations."""
    # Matching operation name passes
    validate_read_only_query(
        "query MyQuery { userWatchList { total } }", operation_name="MyQuery"
    )

    # Mismatched operation name rejected
    with pytest.raises(ValueError, match="does not match query operation"):
        validate_read_only_query(
            "query MyQuery { userWatchList { total } }", operation_name="OtherQuery"
        )

    # Anonymous query with operation_name rejected
    with pytest.raises(ValueError, match="provided for anonymous GraphQL query"):
        validate_read_only_query(
            "{ userWatchList { total } }", operation_name="NamedQuery"
        )


def test_validate_read_only_allows_strings_comments_fragments() -> None:
    """Queries containing 'mutation' within strings, comments, or fragments pass validation."""
    # Word 'mutation' in comment
    validate_read_only_query(
        "# Note: this does not perform a mutation\nquery Safe { userWatchList { total } }"
    )

    # Word 'mutation' in string literal
    validate_read_only_query(
        'query Safe { item(search: "this is a mutation test") { id } }'
    )

    # Word 'mutation' in block string
    validate_read_only_query(
        'query Safe { item(desc: """block string with mutation keyword""") { id } }'
    )

    # Query with leading fragment
    validate_read_only_query(
        "fragment ItemFields on Item { id title } query GetItems { items { ...ItemFields } }"
    )

    # Query with trailing fragment
    validate_read_only_query(
        "query GetItems { items { ...ItemFields } } fragment ItemFields on Item { id title }"
    )


def test_validate_read_only_rejects_before_network_dispatch() -> None:
    """Unsafe queries raise ValueError immediately and never dispatch an HTTP request."""
    post_called = False

    class MockSession:
        def post(self, url: str, **kwargs: Any) -> _Context:
            nonlocal post_called
            post_called = True
            return _Context(_Response(status=200, json_payload={"data": {}}))

    client = _make_client(MockSession())

    with pytest.raises(ValueError, match="Mutations are strictly prohibited"):
        asyncio.run(
            client.async_post_graphql(
                "query Safe { total } mutation Dangerous { deleteEverything }"
            )
        )

    assert post_called is False


def test_graphql_200_token_expired_triggers_refresh_and_retry_success() -> None:
    """HTTP 200 with token expired error triggers refresh and succeeds on retry."""
    calls = 0

    class MockSession:
        def post(self, url: str, **kwargs: Any) -> _Context:
            nonlocal calls
            calls += 1
            if calls == 1:
                return _Context(
                    _Response(
                        status=200,
                        json_payload={
                            "errors": [
                                {
                                    "message": "Access Token is expired or invalid",
                                    "extensions": {
                                        "code": 1001,
                                        "category": "REQUEST",
                                    },
                                }
                            ]
                        },
                    )
                )
            return _Context(
                _Response(
                    status=200,
                    json_payload={"data": {"userWatchList": {"total": 12}}},
                )
            )

    client = _make_client(MockSession())
    refresh_called = False

    async def _mock_refresh(*args: Any, **kwargs: Any) -> str:
        nonlocal refresh_called
        refresh_called = True
        client._access_token = "refreshed_token_val"
        return "refreshed_token_val"

    client.async_refresh_access_token = _mock_refresh  # type: ignore[assignment]
    client.async_get_access_token = _mock_refresh  # type: ignore[assignment]

    result = asyncio.run(client.async_post_graphql("query { userWatchList { total } }"))

    assert calls == 2
    assert refresh_called is True
    assert result == {"userWatchList": {"total": 12}}


def test_graphql_200_rate_limit_triggers_backoff_and_succeeds() -> None:
    """HTTP 200 with RATE_LIMITED error backs off and succeeds on subsequent attempt."""
    calls = 0

    class MockSession:
        def post(self, url: str, **kwargs: Any) -> _Context:
            nonlocal calls
            calls += 1
            if calls == 1:
                return _Context(
                    _Response(
                        status=200,
                        json_payload={
                            "errors": [
                                {
                                    "message": "You have exceeded the call limit for this operation.",
                                    "extensions": {
                                        "code": "RATE_LIMITED",
                                        "category": "REQUEST",
                                        "domain": "BUY_WATCHLIST",
                                    },
                                }
                            ]
                        },
                    )
                )
            return _Context(
                _Response(
                    status=200,
                    json_payload={"data": {"userWatchList": {"total": 7}}},
                )
            )

    client = _make_client(MockSession())

    with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        result = asyncio.run(
            client.async_post_graphql("query { userWatchList { total } }")
        )

    assert calls == 2
    assert mock_sleep.await_count == 1
    assert result == {"userWatchList": {"total": 7}}


def test_graphql_200_rate_limit_exhausted_raises_transient_api_error() -> None:
    """HTTP 200 persistent rate limit raises EbayApiError with transient classification."""

    class MockSession:
        def post(self, url: str, **kwargs: Any) -> _Context:
            return _Context(
                _Response(
                    status=200,
                    json_payload={
                        "errors": [
                            {
                                "message": "Call limit exceeded",
                                "extensions": {
                                    "code": "RATE_LIMITED",
                                    "category": "REQUEST",
                                },
                            }
                        ]
                    },
                )
            )

    client = _make_client(MockSession())

    with patch("asyncio.sleep", new_callable=AsyncMock):
        with pytest.raises(EbayApiError, match="rate limit exceeded"):
            asyncio.run(
                client.async_post_graphql(
                    "query { userWatchList { total } }",
                    max_retries=2,
                )
            )

        with pytest.raises(EbayPartialFailure) as exc_info:
            asyncio.run(
                client.async_post_graphql(
                    "query { userWatchList { total } }",
                    partial_category="buying",
                    max_retries=2,
                )
            )

    assert exc_info.value.category == "buying"
    assert exc_info.value.failure is not None
    assert exc_info.value.failure.classification == "transient"


def test_graphql_200_permanent_permission_error_does_not_retry() -> None:
    """HTTP 200 with missing scope or permission denial raises immediately without retry."""
    calls = 0

    class MockSession:
        def post(self, url: str, **kwargs: Any) -> _Context:
            nonlocal calls
            calls += 1
            return _Context(
                _Response(
                    status=200,
                    json_payload={
                        "errors": [
                            {
                                "message": "Missing required scope https://api.ebay.com/oauth/api_scope/buy.watchlist",
                                "extensions": {
                                    "code": "INSUFFICIENT_SCOPE",
                                    "category": "REQUEST",
                                },
                            }
                        ]
                    },
                )
            )

    client = _make_client(MockSession())
    refresh_called = False

    async def _mock_refresh() -> str:
        nonlocal refresh_called
        refresh_called = True
        return "fresh"

    client.async_refresh_access_token = _mock_refresh  # type: ignore[assignment]

    with pytest.raises(EbayAuthError, match="insufficient scope"):
        asyncio.run(client.async_post_graphql("query { userWatchList { total } }"))

    # Crucial: only 1 attempt dispatched; no token refresh attempted for permanent scope failure
    assert calls == 1
    assert refresh_called is False


def test_graphql_403_forbidden_does_not_retry() -> None:
    """HTTP 403 Forbidden raises EbayAuthError immediately without token refresh or retry."""
    calls = 0

    class MockSession:
        def post(self, url: str, **kwargs: Any) -> _Context:
            nonlocal calls
            calls += 1
            return _Context(_Response(status=403, text="Forbidden"))

    client = _make_client(MockSession())
    refresh_called = False

    async def _mock_refresh() -> str:
        nonlocal refresh_called
        refresh_called = True
        return "fresh"

    client.async_refresh_access_token = _mock_refresh  # type: ignore[assignment]

    with pytest.raises(EbayAuthError, match="HTTP 403"):
        asyncio.run(client.async_post_graphql("query { userWatchList { total } }"))

    assert calls == 1
    assert refresh_called is False


def test_graphql_oauth2_session_force_refresh_avoids_stale_token_reuse() -> None:
    """Ensure OAuth2Session integration requests a new token on 401 instead of reusing stale token."""
    captured_tokens: list[str] = []

    class MockSession:
        def post(self, url: str, **kwargs: Any) -> _Context:
            auth_header = kwargs.get("headers", {}).get("Authorization", "")
            token = auth_header.replace("Bearer ", "")
            captured_tokens.append(token)
            if token == "stale_token_abc":
                return _Context(_Response(status=401, text="Unauthorized"))
            return _Context(
                _Response(
                    status=200,
                    json_payload={"data": {"userWatchList": {"total": 42}}},
                )
            )

    client = _make_client(MockSession())

    # Set up mock Home Assistant OAuth2Session
    entry_data = {
        "token": {
            "access_token": "stale_token_abc",
            "expires_at": time.time() + 3600,
        }
    }

    class MockConfigEntry:
        def __init__(self) -> None:
            self.data = entry_data

    class MockConfigEntries:
        def async_update_entry(self, entry: Any, *, data: dict[str, Any]) -> None:
            entry.data = data
            entry_data.update(data)

    class MockHass:
        def __init__(self) -> None:
            self.config_entries = MockConfigEntries()

    class MockImplementation:
        async def async_refresh_token(self, token: dict[str, Any]) -> dict[str, Any]:
            return {
                "access_token": "fresh_token_xyz",
                "refresh_token": "new_refresh",
                "expires_in": 7200,
            }

    class MockOAuth2Session:
        def __init__(self) -> None:
            self.config_entry = MockConfigEntry()
            self.hass = MockHass()
            self.implementation = MockImplementation()

        @property
        def token(self) -> dict[str, Any]:
            return self.config_entry.data["token"]

        async def async_ensure_token_valid(self) -> None:
            pass

    client._oauth_session = MockOAuth2Session()
    client._access_token = None

    result = asyncio.run(client.async_post_graphql("query { userWatchList { total } }"))

    assert result == {"userWatchList": {"total": 42}}
    assert len(captured_tokens) == 2
    assert captured_tokens[0] == "stale_token_abc"
    # Proof that the rejected token was NOT reused on the second attempt:
    assert captured_tokens[1] == "fresh_token_xyz"


def test_graphql_malformed_errors_array_non_object_fails_closed() -> None:
    """Non-object items in errors array fail closed with EbayParseError."""

    class MockSession:
        def post(self, url: str, **kwargs: Any) -> _Context:
            return _Context(
                _Response(
                    status=200,
                    json_payload={
                        "data": {"userWatchList": None},
                        "errors": ["not_a_valid_error_object"],
                    },
                )
            )

    client = _make_client(MockSession())

    with pytest.raises(EbayParseError, match="must be objects"):
        asyncio.run(client.async_post_graphql("query { userWatchList { total } }"))


def test_sanitize_graphql_errors_preserves_code_category_domain() -> None:
    """Verify extensions.code, category, and domain match documented eBay structure."""
    raw = [
        {
            "message": "Token expired",
            "extensions": {
                "code": 1001,
                "category": "REQUEST",
                "domain": "BUY_WATCHLIST",
            },
        },
        {
            "message": "Rate limit exceeded",
            "extensions": {
                "code": "RATE_LIMITED",
                "category": "REQUEST",
                "domain": "BUY_WATCHLIST",
            },
        },
    ]

    sanitized = sanitize_graphql_errors(raw)
    assert len(sanitized) == 2
    assert sanitized[0]["code"] == 1001
    assert sanitized[0]["category"] == "REQUEST"
    assert sanitized[0]["domain"] == "BUY_WATCHLIST"
    assert sanitized[1]["code"] == "RATE_LIMITED"
    assert sanitized[1]["category"] == "REQUEST"


def test_validate_read_only_query_edge_cases() -> None:
    """Test syntax error edge cases in validate_read_only_query."""
    # Extra closing brace (negative brace depth)
    with pytest.raises(ValueError, match="Unbalanced braces"):
        validate_read_only_query("query Safe { total } }")

    # Only comments
    with pytest.raises(ValueError, match="must contain executable content"):
        validate_read_only_query("# only comments\n")

    # Unsupported definition type (schema definition)
    with pytest.raises(ValueError, match="Unsupported definition type"):
        validate_read_only_query("schema { query: Query }")

    # Fragment only (no executable query)
    with pytest.raises(ValueError, match="must contain a query operation"):
        validate_read_only_query("fragment ItemFields on Item { id }")


def test_graphql_200_null_data_system_error_triggers_retry_and_succeeds() -> None:
    """HTTP 200 with null data and category SYSTEM triggers retry and succeeds."""
    calls = 0

    class MockSession:
        def post(self, url: str, **kwargs: Any) -> _Context:
            nonlocal calls
            calls += 1
            if calls == 1:
                return _Context(
                    _Response(
                        status=200,
                        json_payload={
                            "data": None,
                            "errors": [
                                {
                                    "message": "Internal system failure",
                                    "extensions": {"category": "SYSTEM"},
                                }
                            ],
                        },
                    )
                )
            return _Context(
                _Response(
                    status=200,
                    json_payload={"data": {"userWatchList": {"total": 99}}},
                )
            )

    client = _make_client(MockSession())

    with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        data = asyncio.run(
            client.async_post_graphql("query { userWatchList { total } }")
        )

    assert calls == 2
    assert mock_sleep.await_count == 1
    assert data == {"userWatchList": {"total": 99}}


def test_clear_access_token_does_not_mutate_config_entry_or_trigger_reload() -> None:
    """Ensure _clear_access_token() does not write to config entry or fire listeners."""
    update_called = False

    class MockConfigEntries:
        def async_update_entry(self, entry: Any, *, data: dict[str, Any]) -> None:
            nonlocal update_called
            update_called = True

    class MockHass:
        def __init__(self) -> None:
            self.config_entries = MockConfigEntries()

    class MockOAuth2Session:
        def __init__(self) -> None:
            self.config_entry = type(
                "Entry", (), {"data": {"token": {"access_token": "abc"}}}
            )()
            self.hass = MockHass()
            self.token = {"access_token": "abc", "expires_at": time.time() + 3600}

    client = _make_client(None)
    client._oauth_session = MockOAuth2Session()
    client._access_token = "cached_token"
    client._access_token_expires_at = datetime.now(timezone.utc) + timedelta(hours=1)

    # Calling _clear_access_token directly must only reset client in-memory state
    client._clear_access_token()
    assert client._access_token is None
    assert client._access_token_expires_at is None
    assert update_called is False


def test_graphql_concurrent_token_refresh_deduplicates() -> None:
    """Multiple concurrent GraphQL calls with expired token trigger only one refresh."""
    refresh_calls = 0
    current_token_val = "old_expired_tok"

    class MockImplementation:
        async def async_refresh_token(self, token: dict[str, Any]) -> dict[str, Any]:
            nonlocal refresh_calls, current_token_val
            refresh_calls += 1
            await asyncio.sleep(0.01)  # Simulate network latency
            current_token_val = "new_fresh_tok"
            return {
                "access_token": "new_fresh_tok",
                "refresh_token": "new_refresh",
                "expires_in": 7200,
            }

    class MockConfigEntry:
        def __init__(self) -> None:
            self.data = {
                "token": {
                    "access_token": "old_expired_tok",
                    "expires_at": time.time() + 3600,
                }
            }

    class MockConfigEntries:
        def async_update_entry(self, entry: Any, *, data: dict[str, Any]) -> None:
            entry.data = data

    class MockHass:
        def __init__(self) -> None:
            self.config_entries = MockConfigEntries()

    class MockSession:
        def post(self, url: str, **kwargs: Any) -> _Context:
            auth_header = kwargs.get("headers", {}).get("Authorization", "")
            token = auth_header.replace("Bearer ", "")
            if token == "old_expired_tok":
                return _Context(_Response(status=401, text="Unauthorized"))
            return _Context(
                _Response(
                    status=200,
                    json_payload={"data": {"userWatchList": {"total": 100}}},
                )
            )

    client = _make_client(MockSession())
    client._oauth_session = OAuth2Session(
        MockHass(),  # type: ignore[arg-type]
        MockConfigEntry(),  # type: ignore[arg-type]
        MockImplementation(),  # type: ignore[arg-type]
    )
    client._access_token = None

    async def _run_concurrent() -> list[dict[str, Any]]:
        tasks = [
            client.async_post_graphql("query { userWatchList { total } }")
            for _ in range(5)
        ]
        return await asyncio.gather(*tasks)

    results = asyncio.run(_run_concurrent())

    assert len(results) == 5
    for r in results:
        assert r == {"userWatchList": {"total": 100}}
    # Crucial: Exactly 1 refresh call was made to the OAuth provider, not 5!
    assert refresh_calls == 1


def test_graphql_refresh_reauth_triggers_ha_reauth() -> None:
    """When refresh fails with OAuth2TokenRequestReauthError, HA reauth is triggered."""
    reauth_started = False

    class MockConfigEntry:
        def __init__(self) -> None:
            self.data = {
                "token": {
                    "access_token": "expired_tok",
                    "expires_at": time.time() + 3600,
                }
            }

        def async_start_reauth_if_available(self, hass: Any) -> None:
            nonlocal reauth_started
            reauth_started = True

    class MockImplementation:
        async def async_refresh_token(self, token: dict[str, Any]) -> dict[str, Any]:
            raise OAuth2TokenRequestReauthError(domain="ebay", request_info=MagicMock())

    class MockSession:
        def post(self, url: str, **kwargs: Any) -> _Context:
            return _Context(_Response(status=401, text="Unauthorized"))

    client = _make_client(MockSession())
    client._oauth_session = OAuth2Session(
        object(),  # type: ignore[arg-type]
        MockConfigEntry(),  # type: ignore[arg-type]
        MockImplementation(),  # type: ignore[arg-type]
    )
    client._access_token = None

    with pytest.raises(EbayAuthError, match="OAuth token refresh failed"):
        asyncio.run(client.async_post_graphql("query { userWatchList { total } }"))

    assert reauth_started is True


def test_graphql_refresh_reauth_triggers_ha_2026_3_fallback() -> None:
    """When async_start_reauth_if_available is absent (HA 2026.3), async_start_reauth is called."""
    reauth_started = False

    class MockConfigEntry:
        def __init__(self) -> None:
            self.data = {
                "token": {
                    "access_token": "expired_tok",
                    "expires_at": time.time() + 3600,
                }
            }

        def async_start_reauth(self, hass: Any) -> None:
            nonlocal reauth_started
            reauth_started = True

    class MockImplementation:
        async def async_refresh_token(self, token: dict[str, Any]) -> dict[str, Any]:
            raise OAuth2TokenRequestReauthError(domain="ebay", request_info=MagicMock())

    class MockSession:
        def post(self, url: str, **kwargs: Any) -> _Context:
            return _Context(_Response(status=401, text="Unauthorized"))

    client = _make_client(MockSession())
    client._oauth_session = OAuth2Session(
        object(),  # type: ignore[arg-type]
        MockConfigEntry(),  # type: ignore[arg-type]
        MockImplementation(),  # type: ignore[arg-type]
    )
    client._access_token = None

    with pytest.raises(EbayAuthError, match="OAuth token refresh failed"):
        asyncio.run(client.async_post_graphql("query { userWatchList { total } }"))

    assert reauth_started is True


def test_graphql_refresh_transient_error_does_not_trigger_reauth() -> None:
    """When refresh fails with OAuth2TokenRequestTransientError, reauth is NOT triggered."""
    reauth_started = False

    class MockConfigEntry:
        def __init__(self) -> None:
            self.data = {
                "token": {
                    "access_token": "expired_tok",
                    "expires_at": time.time() + 3600,
                }
            }

        def async_start_reauth_if_available(self, hass: Any) -> None:
            nonlocal reauth_started
            reauth_started = True

    class MockImplementation:
        async def async_refresh_token(self, token: dict[str, Any]) -> dict[str, Any]:
            raise OAuth2TokenRequestTransientError(
                domain="ebay", request_info=MagicMock()
            )

    class MockSession:
        def post(self, url: str, **kwargs: Any) -> _Context:
            return _Context(_Response(status=401, text="Unauthorized"))

    client = _make_client(MockSession())
    client._oauth_session = OAuth2Session(
        object(),  # type: ignore[arg-type]
        MockConfigEntry(),  # type: ignore[arg-type]
        MockImplementation(),  # type: ignore[arg-type]
    )
    client._access_token = None

    with pytest.raises(EbayAuthTransientError, match="refresh failed temporarily"):
        asyncio.run(client.async_post_graphql("query { userWatchList { total } }"))

    assert reauth_started is False


def test_validate_read_only_query_anonymous_with_variables_and_trailing_tokens() -> (
    None
):
    """Anonymous queries with variables are treated as anonymous; trailing tokens are rejected."""
    # Valid anonymous query with variables
    validate_read_only_query(
        "query ($filter: WatchListFilterInput) { userWatchList(input: $filter) { total } }"
    )
    # Providing operation_name for an anonymous query with variables must raise ValueError
    with pytest.raises(ValueError, match="anonymous GraphQL query"):
        validate_read_only_query(
            "query ($filter: WatchListFilterInput) { userWatchList(input: $filter) { total } }",
            operation_name="GetWatchList",
        )
    # Trailing tokens after selection set must raise ValueError
    with pytest.raises(ValueError, match="Unexpected trailing tokens"):
        validate_read_only_query("query { userWatchList { total } } mutation Evil")


def test_token_only_update_does_not_reload_integration() -> None:
    """Updating token without changing options does not invoke async_reload."""
    from custom_components.ebay.const import DEFAULT_OPTIONS

    reload_called = False

    class MockConfigEntries:
        async def async_reload(self, entry_id: str) -> None:
            nonlocal reload_called
            reload_called = True

    class MockHass:
        def __init__(self) -> None:
            self.config_entries = MockConfigEntries()

    class MockCoordinator:
        def __init__(self, options: dict[str, Any]) -> None:
            self.options = options

    class MockEntry:
        def __init__(self) -> None:
            self.entry_id = "test_entry_123"
            self.options = {"poll_interval": 60}
            self.runtime_data = MockCoordinator(
                {**DEFAULT_OPTIONS, "poll_interval": 60}
            )

    hass = MockHass()
    entry = MockEntry()

    # 1. Token update with same options: should NOT reload
    asyncio.run(_async_update_listener(hass, entry))  # type: ignore[arg-type]
    assert reload_called is False

    # 2. Options actually change: SHOULD reload
    entry.options = {"poll_interval": 120}
    asyncio.run(_async_update_listener(hass, entry))  # type: ignore[arg-type]
    assert reload_called is True
