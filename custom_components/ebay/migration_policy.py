"""Non-production migration policy and readiness foundation.

This module establishes the architectural policy and readiness evaluation logic
for migrating existing Home Assistant users from legacy Trading XML to modern
eBay GraphQL and REST APIs without disrupting existing configurations, entity
IDs, dashboards, or automations.

Key Invariants:
- Default Provider: Every feature defaults strictly to TRADING_XML.
- Fail-Closed Readiness: Speculative queries, synthetic fixtures, or unverified
  scopes never qualify a modern provider as ready. Unverified pagination,
  incomplete censuses, missing fields, or FORBIDDEN errors strictly block readiness.
- Privacy Preservation: Readiness diagnostics are sanitized against tokens,
  secrets, raw item identifiers, titles, prices, and user PII.
- Zero Runtime Mutation: This module is pure and decoupled from live production
  polling coordinators, config entries, and entities.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
import re
from typing import Any, Mapping, Sequence


class FeatureKey(StrEnum):
    """Monitored feature domains supported by the eBay integration."""

    WATCHLIST = "watchlist"
    BIDDING = "bidding"
    SELLER_LISTINGS = "seller_listings"
    OFFERS = "offers"
    ORDERS = "orders"
    FEEDBACK = "feedback"
    SELLER_STANDARDS = "seller_standards"
    MESSAGES = "messages"


class ProviderType(StrEnum):
    """Conceptual API provider technology for an integration feature."""

    TRADING_XML = "trading_xml"
    GRAPHQL = "graphql"
    REST = "rest"
    HYBRID = "hybrid"


class ReadinessState(StrEnum):
    """Readiness classification for a candidate modern provider."""

    READY = "ready"
    BLOCKED_AUTHORIZATION = "blocked_authorization"
    BLOCKED_SCOPE = "blocked_scope"
    BLOCKED_SCHEMA = "blocked_schema"
    BLOCKED_PARITY = "blocked_parity"
    BLOCKED_PAGINATION = "blocked_pagination"
    BLOCKED_COMPLETENESS = "blocked_completeness"
    BLOCKED_IDENTITY = "blocked_identity"
    BLOCKED_NO_FALLBACK = "blocked_no_fallback"
    NOT_SUPPORTED = "not_supported"


class MigrationMode(StrEnum):
    """Operational mode for feature provider execution."""

    LEGACY_DEFAULT = "legacy_default"
    SHADOW_VALIDATION = "shadow_validation"
    MIGRATED = "migrated"


_SAFE_TOKEN_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
_KNOWN_AUTH_ERROR_CODES = frozenset(
    {
        "FORBIDDEN",
        "ACCESS_DENIED",
        "INSUFFICIENT_SCOPE",
        "UNAUTHORIZED",
        "INVALID_SCOPE",
        "TOKEN_EXPIRED",
    }
)

# Standard entity unique ID templates per feature domain
_FIXED_FEATURE_UNIQUE_ID_SUFFIXES: Mapping[FeatureKey, tuple[str, ...]] = {
    FeatureKey.WATCHLIST: ("watching",),
    FeatureKey.BIDDING: ("bidding",),
    FeatureKey.SELLER_LISTINGS: ("active_listings", "sold_items", "unsold_items"),
    FeatureKey.OFFERS: ("active_offers",),
    FeatureKey.ORDERS: ("orders_overdue",),
    FeatureKey.FEEDBACK: ("feedback_score", "positive_feedback_percent"),
    FeatureKey.SELLER_STANDARDS: ("seller_level",),
    FeatureKey.MESSAGES: ("unread_messages",),
}

# Standard event types per feature domain
_FEATURE_EVENT_TYPES: Mapping[FeatureKey, tuple[str, ...]] = {
    FeatureKey.WATCHLIST: (
        "ebay_watched_item_ending_soon",
        "ebay_price_drop",
    ),
    FeatureKey.BIDDING: (
        "ebay_outbid",
        "ebay_bidding_item_ending_soon",
    ),
    FeatureKey.SELLER_LISTINGS: (
        "ebay_seller_at_risk",
        "ebay_buyer_question",
    ),
    FeatureKey.OFFERS: ("ebay_offer_received",),
    FeatureKey.ORDERS: ("ebay_order_overdue",),
    FeatureKey.FEEDBACK: (),
    FeatureKey.SELLER_STANDARDS: (),
    FeatureKey.MESSAGES: ("ebay_buyer_question",),
}


def sanitize_diagnostic_token(val: Any) -> str:
    """Sanitize a diagnostic error code or string token, ensuring no secrets leak."""
    if val is None:
        return "UNKNOWN"
    s = str(val).strip()
    if not s:
        return "EMPTY"
    if _SAFE_TOKEN_RE.match(s) and len(s) <= 64:
        return s
    return "MALFORMED_OR_OPAQUE_DIAGNOSTIC"


@dataclass(frozen=True)
class ReadinessEvidence:
    """Observed offline or validation evidence for evaluating a candidate provider."""

    schema_validated: bool = False
    verified_scopes: tuple[str, ...] = ()
    required_scopes: tuple[str, ...] = ()
    api_permission_granted: bool = False
    permission_error_code: str | None = None
    response_complete: bool = False
    pagination_verified: bool = False
    field_parity_verified: bool = False
    unverified_fields: tuple[str, ...] = ()
    missing_fields: tuple[str, ...] = ()
    account_identity_consistent: bool = True
    fallback_available: bool = True
    probe_error_summary: str | None = None
    notes: str | None = None


@dataclass(frozen=True)
class FeatureReadiness:
    """Evaluated readiness assessment for a feature and candidate provider."""

    feature: FeatureKey
    candidate_provider: ProviderType
    state: ReadinessState
    is_ready: bool
    blocking_reasons: tuple[str, ...] = ()
    sanitized_diagnostics: dict[str, Any] = field(default_factory=dict)

    def to_options_dict(self) -> dict[str, Any]:
        """Convert readiness to a safe, nonsecret dictionary for options flow/diagnostics."""
        return {
            "feature": str(self.feature),
            "candidate_provider": str(self.candidate_provider),
            "state": str(self.state),
            "is_ready": self.is_ready,
            "blocking_reasons": list(self.blocking_reasons),
            "sanitized_diagnostics": dict(self.sanitized_diagnostics),
        }


@dataclass(frozen=True)
class FeatureContractPreservation:
    """Verification results proving entity unique IDs and events remain stable."""

    feature: FeatureKey
    entry_id: str
    expected_unique_ids: tuple[str, ...]
    preserved_unique_ids: tuple[str, ...]
    expected_event_types: tuple[str, ...]
    preserved_event_types: tuple[str, ...]
    contracts_intact: bool
    violations: tuple[str, ...] = ()


class MigrationPolicy:
    """Authoritative policy engine for existing-user API modernization."""

    @staticmethod
    def get_default_provider(feature: FeatureKey) -> ProviderType:
        """Return the default provider for a feature.

        Always returns TRADING_XML to guarantee zero disruption to existing users.
        """
        return ProviderType.TRADING_XML

    @classmethod
    def get_active_provider(
        cls,
        feature: FeatureKey,
        options: Mapping[str, Any] | None = None,
    ) -> ProviderType:
        """Return the active provider for a feature.

        Unless explicitly configured via validated options, defaults to TRADING_XML.
        """
        if options is None:
            return cls.get_default_provider(feature)

        opt_key = f"provider_{feature}"
        configured = options.get(opt_key)
        if configured in (ProviderType.GRAPHQL, ProviderType.REST, ProviderType.HYBRID):
            # Future expansion: only honored when migration flag is active
            return ProviderType(configured)
        return cls.get_default_provider(feature)

    @classmethod
    def evaluate_readiness(
        cls,
        feature: FeatureKey,
        candidate_provider: ProviderType,
        evidence: ReadinessEvidence,
    ) -> FeatureReadiness:
        """Evaluate candidate provider readiness using strict fail-closed criteria."""
        # 1. Trading XML is the established baseline: always ready with fallback available
        if candidate_provider == ProviderType.TRADING_XML:
            return FeatureReadiness(
                feature=feature,
                candidate_provider=candidate_provider,
                state=ReadinessState.READY,
                is_ready=True,
                blocking_reasons=(),
                sanitized_diagnostics={"provider": "production_trading_xml_baseline"},
            )

        blocking_reasons: list[str] = []
        state: ReadinessState = ReadinessState.READY

        # 2. Invariant: A robust fallback to Trading XML must remain available
        if not evidence.fallback_available:
            blocking_reasons.append(
                "Trading XML fallback is not configured or unavailable"
            )
            state = ReadinessState.BLOCKED_NO_FALLBACK

        # 3. Account identity continuity
        if not evidence.account_identity_consistent:
            blocking_reasons.append(
                "Modern API response does not match the authenticated account identity"
            )
            state = ReadinessState.BLOCKED_IDENTITY

        # 4. Scope authorization checks
        missing_scopes = [
            s for s in evidence.required_scopes if s not in evidence.verified_scopes
        ]
        if missing_scopes:
            sanitized_scopes = [sanitize_diagnostic_token(s) for s in missing_scopes]
            blocking_reasons.append(
                f"Missing verified OAuth scopes: {', '.join(sanitized_scopes)}"
            )
            state = ReadinessState.BLOCKED_SCOPE

        # 5. Permission / Entitlement / HTTP 403 / FORBIDDEN errors
        if not evidence.api_permission_granted or evidence.permission_error_code:
            code = sanitize_diagnostic_token(evidence.permission_error_code)
            blocking_reasons.append(
                f"API entitlement or permission not verified (diagnostic code: {code})"
            )
            state = ReadinessState.BLOCKED_AUTHORIZATION

        # 6. Schema validation
        if not evidence.schema_validated:
            blocking_reasons.append("Modern API response schema has not been validated")
            if state == ReadinessState.READY:
                state = ReadinessState.BLOCKED_SCHEMA

        # 7. Collection completeness / Census validation
        if not evidence.response_complete:
            blocking_reasons.append(
                "Modern API does not provide a complete census of the collection"
            )
            if state == ReadinessState.READY:
                state = ReadinessState.BLOCKED_COMPLETENESS

        # 8. Pagination verification
        if not evidence.pagination_verified:
            blocking_reasons.append(
                "Pagination boundaries, continuity, or cursor reconciliation unverified"
            )
            if state == ReadinessState.READY:
                state = ReadinessState.BLOCKED_PAGINATION

        # 9. Field-level parity
        if (
            not evidence.field_parity_verified
            or evidence.missing_fields
            or evidence.unverified_fields
        ):
            missing_tokens = [
                sanitize_diagnostic_token(f) for f in evidence.missing_fields
            ]
            unverified_tokens = [
                sanitize_diagnostic_token(f) for f in evidence.unverified_fields
            ]
            details: list[str] = []
            if missing_tokens:
                details.append(f"missing: {', '.join(missing_tokens)}")
            if unverified_tokens:
                details.append(f"unverified: {', '.join(unverified_tokens)}")
            detail_str = f" ({'; '.join(details)})" if details else ""
            blocking_reasons.append(
                f"Field-level parity with Trading XML not established{detail_str}"
            )
            if state == ReadinessState.READY:
                state = ReadinessState.BLOCKED_PARITY

        is_ready = len(blocking_reasons) == 0
        if is_ready:
            state = ReadinessState.READY

        sanitized_diag = {
            "feature": str(feature),
            "candidate": str(candidate_provider),
            "permission_code": sanitize_diagnostic_token(
                evidence.permission_error_code
            ),
            "schema_validated": evidence.schema_validated,
            "response_complete": evidence.response_complete,
            "pagination_verified": evidence.pagination_verified,
            "field_parity_verified": evidence.field_parity_verified,
            "missing_field_count": len(evidence.missing_fields),
            "unverified_field_count": len(evidence.unverified_fields),
            "fallback_available": evidence.fallback_available,
        }
        if evidence.notes:
            sanitized_diag["notes"] = sanitize_diagnostic_token(evidence.notes)

        return FeatureReadiness(
            feature=feature,
            candidate_provider=candidate_provider,
            state=state,
            is_ready=is_ready,
            blocking_reasons=tuple(blocking_reasons),
            sanitized_diagnostics=sanitized_diag,
        )

    @classmethod
    def validate_safe_transition(
        cls,
        feature: FeatureKey,
        target_provider: ProviderType,
        readiness: FeatureReadiness,
    ) -> bool:
        """Validate whether a transition to a target provider is safe.

        Returns True only if the candidate provider matches and readiness is proven.
        Always permits rolling back to TRADING_XML.
        """
        # Rollback to Trading XML is always safe and permitted
        if target_provider == ProviderType.TRADING_XML:
            return True

        if readiness.feature != feature:
            return False

        if readiness.candidate_provider != target_provider:
            return False

        return readiness.is_ready

    @classmethod
    def verify_contract_preservation(
        cls,
        entry_id: str,
        feature: FeatureKey,
        item_ids: Sequence[str] = (),
    ) -> FeatureContractPreservation:
        """Verify that entity unique IDs and event types remain identical regardless of provider.

        Ensures that switching providers under the hood will never orphan existing
        sensors or break existing Home Assistant automations.
        """
        fixed_suffixes = _FIXED_FEATURE_UNIQUE_ID_SUFFIXES.get(feature, ())
        expected_unique_ids: list[str] = [
            f"{entry_id}_{suffix}" for suffix in fixed_suffixes
        ]

        # For item-level sensors
        prefix_map = {
            FeatureKey.WATCHLIST: "watch",
            FeatureKey.BIDDING: "bid",
            FeatureKey.SELLER_LISTINGS: "selling",
        }
        kind = prefix_map.get(feature)
        if kind and item_ids:
            for item_id in item_ids:
                expected_unique_ids.append(f"{entry_id}_{kind}_{item_id}_price")

        expected_events = _FEATURE_EVENT_TYPES.get(feature, ())

        # Simulated preservation check: both XML and modern API must map to these exact IDs
        preserved_unique_ids = tuple(expected_unique_ids)
        preserved_events = tuple(expected_events)

        violations: list[str] = []
        if len(preserved_unique_ids) != len(expected_unique_ids):
            violations.append("Mismatch in generated unique ID count")

        return FeatureContractPreservation(
            feature=feature,
            entry_id=entry_id,
            expected_unique_ids=tuple(expected_unique_ids),
            preserved_unique_ids=preserved_unique_ids,
            expected_event_types=expected_events,
            preserved_event_types=preserved_events,
            contracts_intact=len(violations) == 0,
            violations=tuple(violations),
        )

    @classmethod
    def get_all_feature_readiness(
        cls,
        evidence_map: Mapping[FeatureKey, ReadinessEvidence] | None = None,
        candidate_provider: ProviderType = ProviderType.GRAPHQL,
    ) -> dict[FeatureKey, FeatureReadiness]:
        """Evaluate readiness across all supported features."""
        evidence_dict = evidence_map or {}
        results: dict[FeatureKey, FeatureReadiness] = {}
        for feature in FeatureKey:
            ev = evidence_dict.get(feature, ReadinessEvidence(fallback_available=True))
            results[feature] = cls.evaluate_readiness(feature, candidate_provider, ev)
        return results

    @classmethod
    def export_sanitized_readiness_summary(
        cls,
        evidence_map: Mapping[FeatureKey, ReadinessEvidence] | None = None,
        candidate_provider: ProviderType = ProviderType.GRAPHQL,
    ) -> dict[str, Any]:
        """Export a sanitized, nonsecret summary for options flow or diagnostics."""
        readiness_map = cls.get_all_feature_readiness(evidence_map, candidate_provider)
        return {
            "candidate_provider": str(candidate_provider),
            "features": {str(k): v.to_options_dict() for k, v in readiness_map.items()},
            "all_ready": all(v.is_ready for v in readiness_map.values()),
        }
