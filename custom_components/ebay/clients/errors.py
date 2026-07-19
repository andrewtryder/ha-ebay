"""eBay API exceptions and REST failure classification helpers."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

from ..models import ApiFailure, FailureClassification
from ..parsers.xml import _sanitize_warning_text

_PARTIAL_FAILURE_DETAILS_MAX = 20
_PARTIAL_FAILURE_ERRORS_MAX = 5
API_WARNINGS_STATE_ATTR_MAX = 10


class EbayError(Exception):
    """Base eBay integration error."""


class EbayAuthError(EbayError):
    """Authentication or authorization failed."""


class EbayAuthTransientError(EbayError):
    """Authentication refresh failed temporarily."""


class EbayApiError(EbayError):
    """eBay API returned an error."""


class EbayPartialFailure(EbayError):
    """Optional eBay API section failed."""

    def __init__(
        self,
        category: str,
        *,
        details: dict[str, Any] | None = None,
        failure: ApiFailure | None = None,
    ) -> None:
        super().__init__(category)
        self.category = category
        self.details = details
        self.failure = failure


class EbayParseError(EbayError):
    """eBay response could not be parsed."""


_ELIGIBILITY_OR_PERMISSION_MARKERS = (
    "not eligible",
    "not permitted",
    "not authorized",
    "access denied",
    "insufficient permission",
    "does not have permission",
    "not allowed to access",
    "unavailable for this account",
    "account is not",
)

_UNSUPPORTED_MARKERS = (
    "not eligible",
    "unavailable for this account",
    "not available for this marketplace",
    "not supported",
)


def _safe_ebay_error_payload(payload: Any) -> dict[str, Any] | None:
    """Return a dict error payload when present."""
    return payload if isinstance(payload, dict) else None


def endpoint_key_from_url(url: str) -> str:
    """Return the URL path without query string for diagnostics."""
    parsed = urlparse(url)
    path = parsed.path or "/"
    return path


def sanitize_ebay_rest_errors(
    payload: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """Extract sanitized errorId/message fields from an eBay REST error payload."""
    if not payload:
        return []
    errors = payload.get("errors")
    if not isinstance(errors, list):
        return []
    sanitized: list[dict[str, Any]] = []
    for err in errors:
        if not isinstance(err, dict):
            continue
        message = err.get("message")
        long_message = err.get("longMessage")
        entry = {
            "error_id": err.get("errorId"),
            "domain": err.get("domain") if isinstance(err.get("domain"), str) else None,
            "category": err.get("category")
            if isinstance(err.get("category"), str)
            else None,
            "message": _sanitize_warning_text(
                str(message) if message is not None else None
            ),
            "long_message": _sanitize_warning_text(
                str(long_message) if long_message is not None else None
            ),
        }
        if (
            entry["error_id"] is not None
            or entry["message"]
            or entry["long_message"]
            or entry["domain"]
            or entry["category"]
        ):
            sanitized.append(entry)
        if len(sanitized) >= _PARTIAL_FAILURE_ERRORS_MAX:
            break
    return sanitized


def classify_rest_status(
    status: int,
    error_payload: dict[str, Any] | None,
    partial_category: str,
) -> FailureClassification:
    """Classify an HTTP status (+ optional eBay errors) for soft-fail handling."""
    _ = partial_category  # reserved for category-specific overrides
    if status == 429 or status >= 500:
        return "transient"
    if status == 400:
        return "malformed_request"
    if status == 403:
        if _is_unsupported_error(error_payload):
            return "unsupported"
        if _is_eligibility_or_permission_error(error_payload):
            return "permission"
        return "permission"
    if status == 404:
        return "unknown"
    return "unknown"


def build_api_failure(
    operation: str,
    url: str,
    status: int | None,
    error_payload: dict[str, Any] | None,
    mapped_category: str,
    classification: FailureClassification,
) -> ApiFailure:
    """Build a diagnostics-safe ApiFailure from a REST response."""
    errors = sanitize_ebay_rest_errors(error_payload)
    first = errors[0] if errors else {}
    message = first.get("message") or first.get("long_message")
    return ApiFailure(
        operation=operation,
        endpoint_key=endpoint_key_from_url(url),
        http_status=status,
        ebay_error_id=first.get("error_id"),
        ebay_domain=first.get("domain"),
        ebay_category=first.get("category"),
        message=message if isinstance(message, str) else None,
        classification=classification,
        mapped_category=mapped_category,
    )


def build_partial_failure_detail(
    *,
    mapped_category: str | None = None,
    request_category: str | None = None,
    http_status: int | None = None,
    errors: list[dict[str, Any]] | None = None,
    source: str | None = None,
    reason: str | None = None,
    failure: ApiFailure | None = None,
) -> dict[str, Any]:
    """Build a diagnostics-safe soft-failure detail record."""
    if failure is not None:
        return failure.to_dict()
    detail: dict[str, Any] = {
        "mapped_category": mapped_category,
        "request_category": request_category,
        "http_status": http_status,
        "errors": list(errors or []),
    }
    if source:
        detail["source"] = source
    if reason:
        detail["reason"] = reason
    return detail


def merge_partial_failure_details(
    previous: list[dict[str, Any]] | None,
    new_details: list[dict[str, Any]],
    active_categories: list[str] | set[str],
) -> list[dict[str, Any]]:
    """Retain details for active failure categories, preferring newest per category."""
    active = set(active_categories)
    by_category: dict[str, dict[str, Any]] = {}
    for detail in list(previous or []) + list(new_details):
        if not isinstance(detail, dict):
            continue
        category = detail.get("mapped_category")
        if not isinstance(category, str) or category not in active:
            continue
        by_category[category] = detail
    ordered_keys = (
        list(active_categories)
        if isinstance(active_categories, list)
        else sorted(active)
    )
    ordered = [
        by_category[category] for category in ordered_keys if category in by_category
    ]
    return ordered[:_PARTIAL_FAILURE_DETAILS_MAX]


def _error_text_blob(payload: dict[str, Any] | None) -> str:
    """Lowercased concatenation of eBay error text fields."""
    if not payload:
        return ""
    errors = payload.get("errors")
    if not isinstance(errors, list):
        return ""
    parts: list[str] = []
    for err in errors:
        if not isinstance(err, dict):
            continue
        parts.extend(
            str(err.get(key) or "")
            for key in ("message", "longMessage", "domain", "errorId")
        )
    return " ".join(parts).lower()


def _is_unsupported_error(payload: dict[str, Any] | None) -> bool:
    """Return True when eBay errors indicate marketplace/API unsupported."""
    blob = _error_text_blob(payload)
    return any(marker in blob for marker in _UNSUPPORTED_MARKERS)


def _is_eligibility_or_permission_error(payload: dict[str, Any] | None) -> bool:
    """Return True when eBay error details indicate eligibility/permission denial."""
    if not payload:
        return False
    errors = payload.get("errors")
    if not isinstance(errors, list):
        return False
    for err in errors:
        if not isinstance(err, dict):
            continue
        category = str(err.get("category") or "").upper()
        if category in {"AUTH", "AUTHENTICATION", "AUTHORIZATION"}:
            return True
        blob = " ".join(
            str(err.get(key) or "")
            for key in ("message", "longMessage", "domain", "errorId")
        ).lower()
        if any(marker in blob for marker in _ELIGIBILITY_OR_PERMISSION_MARKERS):
            return True
    return False


def map_rest_failure_category(
    category: str,
    status: int,
    error_payload: dict[str, Any] | None = None,
    *,
    classification: FailureClassification | None = None,
) -> str:
    """Map HTTP status + safe eBay errors to a repair-friendly failure category.

    Feedback 400s are commonly invalid requests (for example a missing user_id),
    not account unavailability. Reserve the plain ``feedback`` category for
    explicit eligibility/permission denials so Repairs wording stays accurate.

    ``classification`` is accepted for callers that already classified the
    response; mapped repair categories remain stable for streak continuity.
    """
    _ = classification  # reserved; mapped categories stay independent of class
    if category != "feedback":
        return category
    if status == 400:
        return "feedback_invalid_request"
    if status == 403:
        if _is_eligibility_or_permission_error(error_payload):
            return "feedback"
        return "feedback_forbidden"
    if status == 404:
        return "feedback_not_found"
    return category
