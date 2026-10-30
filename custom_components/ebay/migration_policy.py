"""Non-production migration policy and readiness foundation.

This module establishes the architectural policy and readiness evaluation logic
for migrating existing Home Assistant users from legacy Trading XML to modern
eBay GraphQL and REST APIs without disrupting existing configurations, entity
IDs, dashboards, or automations.

Key Invariants:
- Grounded Production Baselines:
  - Buying (Watchlist, Bidding) and Selling listings (ActiveList, Offers,
    Sold/Unsold) baseline strictly on Trading XML.
  - Fulfillment Orders, Seller Standards, Messages, Analytics, Finances,
    and Account Privileges baseline strictly on modern eBay REST APIs.
    (Trading XML is NOT a fallback and XML-to-REST migration is NOT needed).
  - Feedback is hybrid (Trading XML GetUser username resolution + REST Feedback).
- Fail-Closed Readiness: Speculative queries, synthetic fixtures, or unverified
  scopes never qualify a candidate provider as ready. Unverified pagination,
  incomplete censuses, missing fields, or FORBIDDEN errors strictly block readiness.
- Privacy & Secret Redaction: Readiness diagnostics are strictly allowlisted.
  Arbitrary strings, client secrets, OAuth tokens, listing IDs, and usernames
  are always redacted to 'REDACTED_OR_OPAQUE_DIAGNOSTIC'.
- Zero Runtime Mutation: This module is pure and decoupled from live production
  polling coordinators, config entries, and entities.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Mapping, Sequence

from .const import (
    BIDDING_EVENT_TYPES,
    SELLING_EVENT_TYPES,
    WATCHING_EVENT_TYPES,
)


class FeatureKey(StrEnum):
    """Monitored feature domains supported by the eBay integration."""

    WATCHLIST = "watchlist"
    BIDDING = "bidding"
    SELLER_LISTINGS = "seller_listings"
    OFFERS = "offers"
    SOLD_UNSOLD = "sold_unsold"
    ORDERS = "orders"
    FEEDBACK = "feedback"
    SELLER_STANDARDS = "seller_standards"
    MESSAGES = "messages"
    ANALYTICS = "analytics"
    FINANCES = "finances"
    ACCOUNT_PRIVILEGES = "account_privileges"


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


# Strict privacy allowlists: only known symbolic error codes, known safe scopes,
# and known public schema field names are permitted in diagnostic outputs.
_KNOWN_AUTH_ERROR_CODES = frozenset(
    {
        "FORBIDDEN",
        "ACCESS_DENIED",
        "INSUFFICIENT_SCOPE",
        "UNAUTHORIZED",
        "INVALID_SCOPE",
        "TOKEN_EXPIRED",
        "BAD_REQUEST",
        "NOT_FOUND",
        "INTERNAL_SERVER_ERROR",
        "RATE_LIMITED",
        "INVALID_TOKEN",
        "METHOD_NOT_ALLOWED",
        "REQUEST_TIMEOUT",
        "SERVICE_UNAVAILABLE",
        "UNKNOWN",
        "EMPTY",
        "200",
        "400",
        "401",
        "403",
        "404",
        "409",
        "429",
        "500",
        "502",
        "503",
    }
)

_KNOWN_SAFE_SCOPES = frozenset(
    {
        "https://api.ebay.com/oauth/api_scope",
        "https://api.ebay.com/oauth/api_scope/sell.fulfillment.readonly",
        "https://api.ebay.com/oauth/api_scope/sell.fulfillment",
        "https://api.ebay.com/oauth/api_scope/sell.finances",
        "https://api.ebay.com/oauth/api_scope/sell.analytics.readonly",
        "https://api.ebay.com/oauth/api_scope/sell.account.readonly",
        "https://api.ebay.com/oauth/api_scope/sell.inventory",
        "https://api.ebay.com/oauth/api_scope/sell.inventory.readonly",
        "https://api.ebay.com/oauth/api_scope/commerce.feedback",
        "https://api.ebay.com/oauth/api_scope/commerce.message",
        "https://api.ebay.com/oauth/api_scope/sell.payment.dispute",
        "buy.watchlist.read",
        "buy.bidding.read",
        "core",
        "fulfillment",
        "analytics",
        "finances",
        "account",
        "feedback",
        "messages",
    }
)

_KNOWN_SAFE_FIELDS = frozenset(
    {
        "item_id",
        "itemId",
        "title",
        "current_price",
        "price",
        "currency",
        "bid_count",
        "bidCount",
        "end_time",
        "endTime",
        "listing_status",
        "listingStatus",
        "reserve_met",
        "reserveMet",
        "high_bidder",
        "highBidder",
        "quantity",
        "quantity_sold",
        "quantitySold",
        "quantity_available",
        "quantityAvailable",
        "watch_count",
        "watchCount",
        "view_count",
        "viewCount",
        "offer_count",
        "offerCount",
        "question_count",
        "questionCount",
        "format",
        "buy_it_now_price",
        "buyItNowPrice",
        "views",
        "watchers",
        "bids",
        "offers",
        "questions",
        "seconds_left",
        "pageInfo",
        "hasNextPage",
        "endCursor",
        "totalCount",
        "nodes",
        "edges",
        "node",
    }
)

_SAFE_ALLOWLIST = _KNOWN_AUTH_ERROR_CODES | _KNOWN_SAFE_SCOPES | _KNOWN_SAFE_FIELDS

REDACTED_DIAGNOSTIC = "REDACTED_OR_OPAQUE_DIAGNOSTIC"


def sanitize_diagnostic_token(val: Any) -> str:
    """Sanitize a diagnostic error code, scope, or field token against a strict allowlist.

    Any unrecognized string, client secret, OAuth token, raw item identifier,
    title, user handle, or free-form text is redacted to REDACTED_OR_OPAQUE_DIAGNOSTIC.
    """
    if val is None:
        return "UNKNOWN"
    s = str(val).strip()
    if not s:
        return "EMPTY"
    if s in _SAFE_ALLOWLIST:
        return s
    return REDACTED_DIAGNOSTIC


# Summary sensor description keys grounded in sensor.py SUMMARY_SENSORS
_SUMMARY_SENSOR_KEYS_BY_FEATURE: Mapping[FeatureKey, tuple[str, ...]] = {
    FeatureKey.WATCHLIST: ("watched_items", "watched_ending_soon"),
    FeatureKey.BIDDING: ("bidding_items",),
    FeatureKey.SELLER_LISTINGS: (
        "active_selling_items",
        "selling_total_bids",
        "selling_total_offers",
        "active_listings_quantity_sold",
        "sold_items_count",
        "unsold_items_count",
        "selling_total_watchers",
        "selling_total_views",
        "selling_ending_soon",
    ),
    FeatureKey.OFFERS: ("selling_total_offers",),
    FeatureKey.SOLD_UNSOLD: ("sold_items_count", "unsold_items_count"),
    FeatureKey.ORDERS: (
        "orders_overdue",
        "open_payment_disputes",
    ),
    FeatureKey.SELLER_STANDARDS: (
        "seller_level",
        "transaction_defect_rate",
        "late_shipment_rate",
        "cases_closed_without_seller_resolution",
        "item_not_received_metric",
        "item_not_as_described_metric",
        "seller_next_evaluation_date",
    ),
    FeatureKey.FEEDBACK: (
        "feedback_score",
        "positive_feedback_percent",
        "recent_positive_feedback",
        "recent_neutral_feedback",
        "recent_negative_feedback",
        "items_awaiting_feedback",
    ),
    FeatureKey.MESSAGES: (
        "unread_conversations",
        "buyer_question_conversations",
        "oldest_unanswered_message_hours",
    ),
    FeatureKey.ANALYTICS: (),
    FeatureKey.FINANCES: ("available_funds", "pending_funds"),
    FeatureKey.ACCOUNT_PRIVILEGES: (
        "listing_amount_remaining",
        "listing_amount_used",
        "listing_quantity_remaining",
        "listing_quantity_used",
    ),
}

# Per-item sensor kinds and suffixes grounded in sensor.py ITEM_SENSOR_FIELDS
_ITEM_SENSOR_KIND_MAP: Mapping[FeatureKey, str] = {
    FeatureKey.WATCHLIST: "watched",
    FeatureKey.BIDDING: "bidding",
    FeatureKey.SELLER_LISTINGS: "selling",
}

_ITEM_SENSOR_SUFFIXES_BY_KIND: Mapping[str, tuple[str, ...]] = {
    "watched": ("price", "bids", "seconds_left", "end_time"),
    "bidding": ("current_price", "max_bid", "seconds_left", "end_time"),
    "selling": (
        "price",
        "views",
        "analytics_views_30d",
        "watchers",
        "bids",
        "offers",
        "questions",
        "seconds_left",
        "end_time",
        "quantity_available",
        "quantity_sold",
    ),
}

# Event entities and their event types grounded in const.py & event.py
_FEATURE_EVENT_TYPES: Mapping[FeatureKey, tuple[str, ...]] = {
    FeatureKey.WATCHLIST: tuple(WATCHING_EVENT_TYPES),
    FeatureKey.BIDDING: tuple(BIDDING_EVENT_TYPES),
    FeatureKey.SELLER_LISTINGS: tuple(SELLING_EVENT_TYPES),
    FeatureKey.OFFERS: ("offer_received",),
    FeatureKey.SOLD_UNSOLD: (
        "item_sold",
        "item_ended_unsold",
        "item_sale_completed",
    ),
    FeatureKey.ORDERS: (
        "order_received",
        "order_paid",
        "shipment_due_soon",
        "shipment_overdue",
        "order_marked_shipped",
        "tracking_added",
        "payment_dispute_opened",
    ),
    FeatureKey.SELLER_STANDARDS: (
        "seller_level_changed",
        "seller_standard_at_risk",
        "service_metric_above_peer_benchmark",
    ),
    FeatureKey.FEEDBACK: (
        "feedback_received",
        "negative_feedback_received",
        "feedback_rating_changed",
    ),
    FeatureKey.MESSAGES: (
        "new_buyer_question",
        "new_message_received",
    ),
    FeatureKey.ANALYTICS: (),
    FeatureKey.FINANCES: (),
    FeatureKey.ACCOUNT_PRIVILEGES: (),
}

# Supported candidate providers per feature domain
_SUPPORTED_CANDIDATE_PROVIDERS: Mapping[FeatureKey, frozenset[ProviderType]] = {
    FeatureKey.WATCHLIST: frozenset({ProviderType.TRADING_XML, ProviderType.GRAPHQL}),
    FeatureKey.BIDDING: frozenset({ProviderType.TRADING_XML, ProviderType.GRAPHQL}),
    FeatureKey.SELLER_LISTINGS: frozenset(
        {ProviderType.TRADING_XML, ProviderType.GRAPHQL}
    ),
    FeatureKey.OFFERS: frozenset(
        {ProviderType.TRADING_XML, ProviderType.GRAPHQL, ProviderType.REST}
    ),
    FeatureKey.SOLD_UNSOLD: frozenset(
        {ProviderType.TRADING_XML, ProviderType.GRAPHQL, ProviderType.REST}
    ),
    FeatureKey.ORDERS: frozenset({ProviderType.REST}),
    FeatureKey.SELLER_STANDARDS: frozenset({ProviderType.REST}),
    FeatureKey.FEEDBACK: frozenset({ProviderType.HYBRID, ProviderType.REST}),
    FeatureKey.MESSAGES: frozenset({ProviderType.REST}),
    FeatureKey.ANALYTICS: frozenset({ProviderType.REST}),
    FeatureKey.FINANCES: frozenset({ProviderType.REST}),
    FeatureKey.ACCOUNT_PRIVILEGES: frozenset({ProviderType.REST}),
}


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
    """Verification results proving entity unique IDs and events match expected contracts."""

    feature: FeatureKey
    entry_id: str
    expected_unique_ids: tuple[str, ...]
    observed_unique_ids: tuple[str, ...]
    expected_event_types: tuple[str, ...]
    observed_event_types: tuple[str, ...]
    contracts_intact: bool
    violations: tuple[str, ...] = ()


class MigrationPolicy:
    """Authoritative policy engine for existing-user API modernization."""

    @staticmethod
    def get_default_provider(feature: FeatureKey) -> ProviderType:
        """Return the actual baseline production provider for a feature domain.

        Ground Truth Mapping:
        - Buying (Watchlist, Bidding) and Selling listings (ActiveList, Offers,
          Sold/Unsold) use Trading XML.
        - Orders (Fulfillment API), Seller Standards (Analytics API),
          Messages (REST Messages), Analytics (Traffic report),
          Finances (Finances API), and Account Privileges (Account API)
          already use modern REST APIs in production.
          (Trading XML is NOT a fallback and XML-to-REST migration is NOT needed).
        - Feedback uses a HYBRID model: username resolution via Trading XML GetUser
          and score/metric fetching via modern REST Feedback API.
        """
        if feature in (
            FeatureKey.WATCHLIST,
            FeatureKey.BIDDING,
            FeatureKey.SELLER_LISTINGS,
            FeatureKey.OFFERS,
            FeatureKey.SOLD_UNSOLD,
        ):
            return ProviderType.TRADING_XML
        if feature == FeatureKey.FEEDBACK:
            return ProviderType.HYBRID
        if feature in (
            FeatureKey.ORDERS,
            FeatureKey.SELLER_STANDARDS,
            FeatureKey.MESSAGES,
            FeatureKey.ANALYTICS,
            FeatureKey.FINANCES,
            FeatureKey.ACCOUNT_PRIVILEGES,
        ):
            return ProviderType.REST
        return ProviderType.TRADING_XML

    @classmethod
    def get_active_provider(
        cls,
        feature: FeatureKey,
        options: Mapping[str, Any] | None = None,
        readiness: FeatureReadiness | None = None,
    ) -> ProviderType:
        """Return the active provider for a feature.

        Strictly fails closed:
        - If options is None or migration is not explicitly enabled, returns default provider.
        - Requires an explicit opt-in flag in options ('enable_modern_api_migration'
          or 'migrate_<feature>').
        - Rejects unsupported providers for the feature.
        - Requires a validated, ready FeatureReadiness assessment before activating
          a candidate modern provider.
        - Never activates an unverified or unready candidate provider.
        """
        default_provider = cls.get_default_provider(feature)
        if options is None:
            return default_provider

        migration_opt_in = bool(
            options.get("enable_modern_api_migration")
            or options.get(f"migrate_{feature}")
        )
        if not migration_opt_in:
            return default_provider

        opt_key = f"provider_{feature}"
        raw_configured = options.get(opt_key)
        if not raw_configured:
            return default_provider

        try:
            candidate = ProviderType(raw_configured)
        except ValueError:
            return default_provider

        if candidate == default_provider:
            return default_provider

        supported = _SUPPORTED_CANDIDATE_PROVIDERS.get(feature, frozenset())
        if candidate not in supported:
            return default_provider

        if readiness is None or not cls.validate_safe_transition(
            feature, candidate, readiness
        ):
            return default_provider

        return candidate

    @classmethod
    def evaluate_readiness(
        cls,
        feature: FeatureKey,
        candidate_provider: ProviderType,
        evidence: ReadinessEvidence,
    ) -> FeatureReadiness:
        """Evaluate candidate provider readiness using strict fail-closed criteria."""
        default_provider = cls.get_default_provider(feature)

        # 1. Candidate is the established production baseline: always ready
        if candidate_provider == default_provider:
            return FeatureReadiness(
                feature=feature,
                candidate_provider=candidate_provider,
                state=ReadinessState.READY,
                is_ready=True,
                blocking_reasons=(),
                sanitized_diagnostics={
                    "provider": f"production_{default_provider}_baseline"
                },
            )

        # 2. Candidate provider is unsupported for this feature domain
        supported = _SUPPORTED_CANDIDATE_PROVIDERS.get(feature, frozenset())
        if candidate_provider not in supported:
            return FeatureReadiness(
                feature=feature,
                candidate_provider=candidate_provider,
                state=ReadinessState.NOT_SUPPORTED,
                is_ready=False,
                blocking_reasons=(
                    f"Provider {candidate_provider} is not supported for feature domain {feature}",
                ),
                sanitized_diagnostics={
                    "supported_providers": [str(p) for p in supported]
                },
            )

        blocking_reasons: list[str] = []
        state: ReadinessState = ReadinessState.READY

        # 3. Invariant: For legacy baseline features, fallback to default provider must remain available
        if not evidence.fallback_available:
            blocking_reasons.append(
                f"Fallback to established provider {default_provider} is not configured or unavailable"
            )
            state = ReadinessState.BLOCKED_NO_FALLBACK

        # 4. Account identity continuity
        if not evidence.account_identity_consistent:
            blocking_reasons.append(
                "Modern API response does not match the authenticated account identity"
            )
            state = ReadinessState.BLOCKED_IDENTITY

        # 5. Scope authorization checks
        missing_scopes = [
            s for s in evidence.required_scopes if s not in evidence.verified_scopes
        ]
        if missing_scopes:
            sanitized_scopes = [sanitize_diagnostic_token(s) for s in missing_scopes]
            blocking_reasons.append(
                f"Missing verified OAuth scopes: {', '.join(sanitized_scopes)}"
            )
            state = ReadinessState.BLOCKED_SCOPE

        # 6. Permission / Entitlement / HTTP 403 / FORBIDDEN errors
        if not evidence.api_permission_granted or evidence.permission_error_code:
            code = sanitize_diagnostic_token(evidence.permission_error_code)
            blocking_reasons.append(
                f"API entitlement or permission not verified (diagnostic code: {code})"
            )
            state = ReadinessState.BLOCKED_AUTHORIZATION

        # 7. Schema validation
        if not evidence.schema_validated:
            blocking_reasons.append("Modern API response schema has not been validated")
            if state == ReadinessState.READY:
                state = ReadinessState.BLOCKED_SCHEMA

        # 8. Collection completeness / Census validation
        if not evidence.response_complete:
            blocking_reasons.append(
                "Modern API does not provide a complete census of the collection"
            )
            if state == ReadinessState.READY:
                state = ReadinessState.BLOCKED_COMPLETENESS

        # 9. Pagination verification
        if not evidence.pagination_verified:
            blocking_reasons.append(
                "Pagination boundaries, continuity, or cursor reconciliation unverified"
            )
            if state == ReadinessState.READY:
                state = ReadinessState.BLOCKED_PAGINATION

        # 10. Field-level parity
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
                f"Field-level parity with baseline provider not established{detail_str}"
            )
            if state == ReadinessState.READY:
                state = ReadinessState.BLOCKED_PARITY

        is_ready = len(blocking_reasons) == 0
        if is_ready:
            state = ReadinessState.READY

        sanitized_diag: dict[str, Any] = {
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
            "account_identity_consistent": evidence.account_identity_consistent,
            "has_notes": bool(evidence.notes),
        }

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
        Always permits rolling back to the default production provider.
        """
        # Rollback to baseline default provider is always safe and permitted
        if target_provider == cls.get_default_provider(feature):
            return True

        if readiness.feature != feature:
            return False

        if readiness.candidate_provider != target_provider:
            return False

        return readiness.is_ready

    @classmethod
    def get_expected_unique_ids(
        cls,
        entry_id: str,
        feature: FeatureKey,
        item_ids: Sequence[str] = (),
    ) -> tuple[str, ...]:
        """Compute the expected entity unique IDs for a feature domain."""
        fixed_keys = _SUMMARY_SENSOR_KEYS_BY_FEATURE.get(feature, ())
        expected: list[str] = [f"{entry_id}_{key}" for key in fixed_keys]

        kind = _ITEM_SENSOR_KIND_MAP.get(feature)
        if kind and item_ids:
            suffixes = _ITEM_SENSOR_SUFFIXES_BY_KIND.get(kind, ())
            for item_id in item_ids:
                for suffix in suffixes:
                    expected.append(f"{entry_id}_{kind}_{item_id}_{suffix}")

        return tuple(expected)

    @classmethod
    def get_expected_event_types(cls, feature: FeatureKey) -> tuple[str, ...]:
        """Return the expected EventEntity event types for a feature domain."""
        return _FEATURE_EVENT_TYPES.get(feature, ())

    @classmethod
    def verify_contract_preservation(
        cls,
        entry_id: str,
        feature: FeatureKey,
        item_ids: Sequence[str] = (),
        observed_unique_ids: Sequence[str] | None = None,
        observed_event_types: Sequence[str] | None = None,
    ) -> FeatureContractPreservation:
        """Verify that observed entity unique IDs and event types match expected contracts.

        Grounded in real Home Assistant entity registry and event constants.
        Compares independently supplied observed outputs against expected contracts.
        Fails when any expected ID or event is missing, extra, or mismatched.
        """
        expected_unique_ids = cls.get_expected_unique_ids(entry_id, feature, item_ids)
        expected_event_types = cls.get_expected_event_types(feature)

        violations: list[str] = []

        if observed_unique_ids is None:
            violations.append("No observed unique IDs supplied for verification")
            actual_observed_ids: tuple[str, ...] = ()
        else:
            actual_observed_ids = tuple(observed_unique_ids)
            expected_id_set = set(expected_unique_ids)
            observed_id_set = set(actual_observed_ids)
            missing_ids = expected_id_set - observed_id_set
            extra_ids = observed_id_set - expected_id_set
            if missing_ids:
                violations.append(f"Missing expected unique IDs: {sorted(missing_ids)}")
            if extra_ids:
                violations.append(f"Unexpected extra unique IDs: {sorted(extra_ids)}")

        if observed_event_types is None:
            violations.append("No observed event types supplied for verification")
            actual_observed_events: tuple[str, ...] = ()
        else:
            actual_observed_events = tuple(observed_event_types)
            expected_event_set = set(expected_event_types)
            observed_event_set = set(actual_observed_events)
            missing_events = expected_event_set - observed_event_set
            extra_events = observed_event_set - expected_event_set
            if missing_events:
                violations.append(
                    f"Missing expected event types: {sorted(missing_events)}"
                )
            if extra_events:
                violations.append(
                    f"Unexpected extra event types: {sorted(extra_events)}"
                )

        contracts_intact = len(violations) == 0

        return FeatureContractPreservation(
            feature=feature,
            entry_id=entry_id,
            expected_unique_ids=expected_unique_ids,
            observed_unique_ids=actual_observed_ids,
            expected_event_types=expected_event_types,
            observed_event_types=actual_observed_events,
            contracts_intact=contracts_intact,
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
