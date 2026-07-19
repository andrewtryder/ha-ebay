"""Shared data models for the eBay integration."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Literal

FailureClassification = Literal[
    "transient",
    "unsupported",
    "missing_scope",
    "malformed_request",
    "permission",
    "unknown",
]

_CLASSIFICATIONS = frozenset(
    {
        "transient",
        "unsupported",
        "missing_scope",
        "malformed_request",
        "permission",
        "unknown",
    }
)


@dataclass(frozen=True)
class ApiFailure:
    """Diagnostics-safe structured API failure.

    Never include tokens, usernames, item titles, message bodies, or full
    query strings.
    """

    operation: str
    endpoint_key: str
    http_status: int | None
    ebay_error_id: int | str | None
    ebay_domain: str | None
    ebay_category: str | None
    message: str | None
    classification: FailureClassification
    mapped_category: str

    def to_diagnostics(self) -> dict[str, Any]:
        """Return the public diagnostics shape for this failure."""
        return {
            "operation": self.operation,
            "endpoint_key": self.endpoint_key,
            "status": self.http_status,
            "ebay_error_id": self.ebay_error_id,
            "domain": self.ebay_domain,
            "category": self.ebay_category,
            "message": self.message,
            "transient": self.classification == "transient",
            "classification": self.classification,
            "mapped_category": self.mapped_category,
        }

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable dict for coordinator payloads."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> ApiFailure | None:
        """Parse an ApiFailure from a stored dict when fields are valid."""
        if not isinstance(data, dict):
            return None
        operation = data.get("operation")
        endpoint_key = data.get("endpoint_key")
        mapped = data.get("mapped_category")
        if not isinstance(operation, str) or not isinstance(endpoint_key, str):
            return None
        if not isinstance(mapped, str):
            return None
        classification = data.get("classification") or "unknown"
        if classification not in _CLASSIFICATIONS:
            classification = "unknown"
        status = data.get("http_status", data.get("status"))
        domain = data.get("ebay_domain", data.get("domain"))
        cat = data.get("ebay_category")
        if cat is None and isinstance(data.get("category"), str):
            # Prefer ebay_category; fall back to category when it is not the
            # repair mapped_category field (diagnostics uses "category" for eBay).
            cat = data.get("category")
        message = data.get("message")
        return cls(
            operation=operation,
            endpoint_key=endpoint_key,
            http_status=status if isinstance(status, int) else None,
            ebay_error_id=data.get("ebay_error_id"),
            ebay_domain=domain if isinstance(domain, str) else None,
            ebay_category=cat if isinstance(cat, str) else None,
            message=message if isinstance(message, str) else None,
            classification=classification,  # type: ignore[arg-type]
            mapped_category=mapped,
        )
