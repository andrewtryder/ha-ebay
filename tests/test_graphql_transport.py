"""Unit tests for eBay GraphQL transport layer."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import AsyncMock, patch

import aiohttp
import pytest

from custom_components.ebay.api import (
    EbayApiClient,
    EbayApiError,
    EbayAuthError,
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
    assert sanitize_graphql_errors(["not a dict"]) == []  # type: ignore[list-item]

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
