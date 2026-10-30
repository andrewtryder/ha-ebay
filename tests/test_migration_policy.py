"""Tests for the non-production existing-user migration policy and readiness foundation."""

from __future__ import annotations

from typing import Any
import pytest

from custom_components.ebay.const import (
    CONF_CLIENT_ID,
    CONF_CLIENT_SECRET,
    CONF_ENTITY_MODE,
    CONF_ENVIRONMENT,
    CONF_PINNED_ITEM_IDS,
    CONF_PINNED_ITEM_PRICE_TARGETS,
    CONF_POLL_INTERVAL,
    CONF_REFRESH_TOKEN,
    CONF_RUNAME,
    CONF_SITE_ID,
    ENV_PRODUCTION,
)
from custom_components.ebay.ending_soon_store import (
    deserialize_ending_soon_fired_key,
    serialize_ending_soon_fired_key,
)
from custom_components.ebay.migration_policy import (
    FeatureKey,
    FeatureReadiness,
    MigrationPolicy,
    ProviderType,
    ReadinessEvidence,
    ReadinessState,
    sanitize_diagnostic_token,
)


def test_default_provider_for_all_features_is_trading_xml() -> None:
    """Every supported feature domain must default to TRADING_XML."""
    for feature in FeatureKey:
        default_p = MigrationPolicy.get_default_provider(feature)
        assert default_p == ProviderType.TRADING_XML


def test_get_active_provider_fallback_to_trading_xml() -> None:
    """Active provider must strictly fallback to TRADING_XML when unconfigured."""
    assert (
        MigrationPolicy.get_active_provider(FeatureKey.WATCHLIST)
        == ProviderType.TRADING_XML
    )
    assert (
        MigrationPolicy.get_active_provider(FeatureKey.WATCHLIST, {})
        == ProviderType.TRADING_XML
    )
    assert (
        MigrationPolicy.get_active_provider(
            FeatureKey.WATCHLIST, {"provider_watchlist": "invalid_provider"}
        )
        == ProviderType.TRADING_XML
    )
    # When explicitly configured with a supported enum value
    assert (
        MigrationPolicy.get_active_provider(
            FeatureKey.WATCHLIST, {"provider_watchlist": ProviderType.GRAPHQL}
        )
        == ProviderType.GRAPHQL
    )


def test_evaluate_readiness_trading_xml_baseline_always_ready() -> None:
    """Trading XML is our production baseline and always evaluates to READY."""
    evidence = ReadinessEvidence(fallback_available=True)
    res = MigrationPolicy.evaluate_readiness(
        FeatureKey.WATCHLIST, ProviderType.TRADING_XML, evidence
    )
    assert res.is_ready is True
    assert res.state == ReadinessState.READY
    assert res.blocking_reasons == ()
    assert "production_trading_xml_baseline" in res.sanitized_diagnostics.get(
        "provider", ""
    )


@pytest.mark.parametrize(
    ("error_code", "granted"),
    [
        ("FORBIDDEN", False),
        ("ACCESS_DENIED", False),
        ("INSUFFICIENT_SCOPE", False),
        ("UNAUTHORIZED", False),
        (None, False),
    ],
)
def test_evaluate_readiness_blocked_authorization(
    error_code: str | None, granted: bool
) -> None:
    """Authorization failure or explicit FORBIDDEN code strictly blocks readiness."""
    evidence = ReadinessEvidence(
        schema_validated=True,
        api_permission_granted=granted,
        permission_error_code=error_code,
        response_complete=True,
        pagination_verified=True,
        field_parity_verified=True,
        fallback_available=True,
    )
    res = MigrationPolicy.evaluate_readiness(
        FeatureKey.WATCHLIST, ProviderType.GRAPHQL, evidence
    )
    assert res.is_ready is False
    assert res.state == ReadinessState.BLOCKED_AUTHORIZATION
    assert any(
        "entitlement or permission not verified" in r for r in res.blocking_reasons
    )


def test_evaluate_readiness_blocked_missing_scope() -> None:
    """Missing required OAuth scopes must fail closed with BLOCKED_SCOPE."""
    evidence = ReadinessEvidence(
        schema_validated=True,
        required_scopes=("buy.watchlist.read",),
        verified_scopes=(),
        api_permission_granted=True,
        response_complete=True,
        pagination_verified=True,
        field_parity_verified=True,
        fallback_available=True,
    )
    res = MigrationPolicy.evaluate_readiness(
        FeatureKey.WATCHLIST, ProviderType.GRAPHQL, evidence
    )
    assert res.is_ready is False
    assert res.state == ReadinessState.BLOCKED_SCOPE
    assert any("buy.watchlist.read" in r for r in res.blocking_reasons)


def test_evaluate_readiness_blocked_schema_and_census() -> None:
    """Unvalidated schema and incomplete census must block readiness."""
    # 1. Schema unvalidated
    ev_schema = ReadinessEvidence(
        schema_validated=False,
        api_permission_granted=True,
        response_complete=True,
        pagination_verified=True,
        field_parity_verified=True,
        fallback_available=True,
    )
    res_schema = MigrationPolicy.evaluate_readiness(
        FeatureKey.SELLER_LISTINGS, ProviderType.GRAPHQL, ev_schema
    )
    assert res_schema.is_ready is False
    assert res_schema.state == ReadinessState.BLOCKED_SCHEMA

    # 2. Incomplete census / response incomplete
    ev_census = ReadinessEvidence(
        schema_validated=True,
        api_permission_granted=True,
        response_complete=False,
        pagination_verified=True,
        field_parity_verified=True,
        fallback_available=True,
    )
    res_census = MigrationPolicy.evaluate_readiness(
        FeatureKey.SELLER_LISTINGS, ProviderType.GRAPHQL, ev_census
    )
    assert res_census.is_ready is False
    assert res_census.state == ReadinessState.BLOCKED_COMPLETENESS


def test_evaluate_readiness_blocked_pagination_and_parity() -> None:
    """Unverified pagination and unverified field parity must block readiness."""
    # 1. Pagination unverified
    ev_pag = ReadinessEvidence(
        schema_validated=True,
        api_permission_granted=True,
        response_complete=True,
        pagination_verified=False,
        field_parity_verified=True,
        fallback_available=True,
    )
    res_pag = MigrationPolicy.evaluate_readiness(
        FeatureKey.BIDDING, ProviderType.GRAPHQL, ev_pag
    )
    assert res_pag.is_ready is False
    assert res_pag.state == ReadinessState.BLOCKED_PAGINATION

    # 2. Missing/unverified fields
    ev_parity = ReadinessEvidence(
        schema_validated=True,
        api_permission_granted=True,
        response_complete=True,
        pagination_verified=True,
        field_parity_verified=False,
        missing_fields=("current_price",),
        unverified_fields=("views",),
        fallback_available=True,
    )
    res_parity = MigrationPolicy.evaluate_readiness(
        FeatureKey.BIDDING, ProviderType.GRAPHQL, ev_parity
    )
    assert res_parity.is_ready is False
    assert res_parity.state == ReadinessState.BLOCKED_PARITY
    assert any("current_price" in r for r in res_parity.blocking_reasons)
    assert any("views" in r for r in res_parity.blocking_reasons)


def test_evaluate_readiness_blocked_identity_and_no_fallback() -> None:
    """Account identity mismatch and absent fallback must block readiness."""
    # 1. Account identity mismatch
    ev_ident = ReadinessEvidence(
        schema_validated=True,
        api_permission_granted=True,
        response_complete=True,
        pagination_verified=True,
        field_parity_verified=True,
        account_identity_consistent=False,
        fallback_available=True,
    )
    res_ident = MigrationPolicy.evaluate_readiness(
        FeatureKey.OFFERS, ProviderType.REST, ev_ident
    )
    assert res_ident.is_ready is False
    assert res_ident.state == ReadinessState.BLOCKED_IDENTITY

    # 2. Missing fallback
    ev_nofallback = ReadinessEvidence(
        schema_validated=True,
        api_permission_granted=True,
        response_complete=True,
        pagination_verified=True,
        field_parity_verified=True,
        fallback_available=False,
    )
    res_nofallback = MigrationPolicy.evaluate_readiness(
        FeatureKey.OFFERS, ProviderType.REST, ev_nofallback
    )
    assert res_nofallback.is_ready is False
    assert res_nofallback.state == ReadinessState.BLOCKED_NO_FALLBACK


def test_evaluate_readiness_fully_proven_modern_provider() -> None:
    """When all evidence criteria are satisfied, readiness is granted."""
    evidence = ReadinessEvidence(
        schema_validated=True,
        required_scopes=(
            "https://api.ebay.com/oauth/api_scope/sell.inventory.readonly",
        ),
        verified_scopes=(
            "https://api.ebay.com/oauth/api_scope/sell.inventory.readonly",
        ),
        api_permission_granted=True,
        response_complete=True,
        pagination_verified=True,
        field_parity_verified=True,
        account_identity_consistent=True,
        fallback_available=True,
        notes="Verified in sandbox test harness",
    )
    res = MigrationPolicy.evaluate_readiness(
        FeatureKey.SELLER_LISTINGS, ProviderType.GRAPHQL, evidence
    )
    assert res.is_ready is True
    assert res.state == ReadinessState.READY
    assert res.blocking_reasons == ()
    opt_dict = res.to_options_dict()
    assert opt_dict["is_ready"] is True
    assert opt_dict["state"] == "ready"


def test_validate_safe_transition_logic() -> None:
    """Test safe transition enforcement between providers."""
    ready_assessment = FeatureReadiness(
        feature=FeatureKey.WATCHLIST,
        candidate_provider=ProviderType.GRAPHQL,
        state=ReadinessState.READY,
        is_ready=True,
    )
    unready_assessment = FeatureReadiness(
        feature=FeatureKey.WATCHLIST,
        candidate_provider=ProviderType.GRAPHQL,
        state=ReadinessState.BLOCKED_AUTHORIZATION,
        is_ready=False,
        blocking_reasons=("FORBIDDEN",),
    )

    # Rollback to Trading XML is ALWAYS permitted
    assert (
        MigrationPolicy.validate_safe_transition(
            FeatureKey.WATCHLIST, ProviderType.TRADING_XML, unready_assessment
        )
        is True
    )

    # Transition to unready provider is rejected
    assert (
        MigrationPolicy.validate_safe_transition(
            FeatureKey.WATCHLIST, ProviderType.GRAPHQL, unready_assessment
        )
        is False
    )

    # Transition to ready provider is allowed
    assert (
        MigrationPolicy.validate_safe_transition(
            FeatureKey.WATCHLIST, ProviderType.GRAPHQL, ready_assessment
        )
        is True
    )

    # Feature mismatch rejects
    assert (
        MigrationPolicy.validate_safe_transition(
            FeatureKey.BIDDING, ProviderType.GRAPHQL, ready_assessment
        )
        is False
    )

    # Provider mismatch rejects
    assert (
        MigrationPolicy.validate_safe_transition(
            FeatureKey.WATCHLIST, ProviderType.REST, ready_assessment
        )
        is False
    )


def test_contract_preservation_entity_unique_ids_and_events() -> None:
    """Verify that entity unique IDs and events remain stable across providers."""
    entry_id = "test_entry_abcdef"

    # 1. Watchlist domain
    watchlist_contract = MigrationPolicy.verify_contract_preservation(
        entry_id, FeatureKey.WATCHLIST, item_ids=["1001", "1002"]
    )
    assert watchlist_contract.contracts_intact is True
    assert f"{entry_id}_watching" in watchlist_contract.expected_unique_ids
    assert f"{entry_id}_watch_1001_price" in watchlist_contract.expected_unique_ids
    assert f"{entry_id}_watch_1002_price" in watchlist_contract.expected_unique_ids
    assert "ebay_watched_item_ending_soon" in watchlist_contract.expected_event_types
    assert "ebay_price_drop" in watchlist_contract.expected_event_types

    # 2. Seller listings domain
    seller_contract = MigrationPolicy.verify_contract_preservation(
        entry_id, FeatureKey.SELLER_LISTINGS, item_ids=["2001"]
    )
    assert seller_contract.contracts_intact is True
    assert f"{entry_id}_active_listings" in seller_contract.expected_unique_ids
    assert f"{entry_id}_sold_items" in seller_contract.expected_unique_ids
    assert f"{entry_id}_unsold_items" in seller_contract.expected_unique_ids
    assert f"{entry_id}_selling_2001_price" in seller_contract.expected_unique_ids
    assert "ebay_seller_at_risk" in seller_contract.expected_event_types

    # 3. Bidding domain
    bidding_contract = MigrationPolicy.verify_contract_preservation(
        entry_id, FeatureKey.BIDDING, item_ids=["3001"]
    )
    assert bidding_contract.contracts_intact is True
    assert f"{entry_id}_bidding" in bidding_contract.expected_unique_ids
    assert f"{entry_id}_bid_3001_price" in bidding_contract.expected_unique_ids
    assert "ebay_outbid" in bidding_contract.expected_event_types


def test_existing_config_entry_identity_preservation() -> None:
    """Verify that existing config entry data, credentials, and options are preserved."""
    entry_id = "entry_existing_123"
    raw_data: dict[str, Any] = {
        CONF_ENVIRONMENT: ENV_PRODUCTION,
        CONF_CLIENT_ID: "client_id_abc",
        CONF_CLIENT_SECRET: "secret_xyz",
        CONF_RUNAME: "runame_test",
        CONF_REFRESH_TOKEN: "refresh_token_123",
        CONF_SITE_ID: "0",
    }
    raw_options: dict[str, Any] = {
        CONF_PINNED_ITEM_IDS: ["123456789012", "987654321098"],
        CONF_PINNED_ITEM_PRICE_TARGETS: {"123456789012": 45.0},
        CONF_POLL_INTERVAL: 45,
        CONF_ENTITY_MODE: "detailed",
    }

    # Preserving data and options without mutation
    saved_data = dict(raw_data)
    saved_options = dict(raw_options)

    # Active provider query does not mutate data or options
    active_watchlist = MigrationPolicy.get_active_provider(
        FeatureKey.WATCHLIST, saved_options
    )
    assert active_watchlist == ProviderType.TRADING_XML
    assert saved_data == raw_data
    assert saved_options == raw_options

    # Contract verification operates purely on entry_id and item_ids
    contract = MigrationPolicy.verify_contract_preservation(
        entry_id, FeatureKey.WATCHLIST, saved_options[CONF_PINNED_ITEM_IDS]
    )
    assert contract.contracts_intact is True
    assert f"{entry_id}_watching" in contract.preserved_unique_ids
    assert f"{entry_id}_watch_123456789012_price" in contract.preserved_unique_ids


def test_sanitization_diagnostics_privacy() -> None:
    """Ensure diagnostic tokens and error summaries never leak secrets or PII."""
    assert sanitize_diagnostic_token("FORBIDDEN") == "FORBIDDEN"
    assert sanitize_diagnostic_token("INVALID_SCOPE") == "INVALID_SCOPE"
    assert sanitize_diagnostic_token(None) == "UNKNOWN"
    assert sanitize_diagnostic_token("") == "EMPTY"
    # Long or malformed token with punctuation or secrets
    assert (
        sanitize_diagnostic_token("v1//token#secret-string-with-punctuation!@#$%^&*()")
        == "MALFORMED_OR_OPAQUE_DIAGNOSTIC"
    )
    assert sanitize_diagnostic_token("a" * 100) == "MALFORMED_OR_OPAQUE_DIAGNOSTIC"


def test_get_all_feature_readiness_and_export_summary() -> None:
    """Verify evaluating all features and exporting sanitized summary."""
    summary = MigrationPolicy.export_sanitized_readiness_summary()
    assert summary["candidate_provider"] == "graphql"
    assert summary["all_ready"] is False
    assert len(summary["features"]) == len(FeatureKey)

    # With all features verified
    ev_all_ready = {
        feat: ReadinessEvidence(
            schema_validated=True,
            api_permission_granted=True,
            response_complete=True,
            pagination_verified=True,
            field_parity_verified=True,
            fallback_available=True,
        )
        for feat in FeatureKey
    }
    summary_ready = MigrationPolicy.export_sanitized_readiness_summary(ev_all_ready)
    assert summary_ready["all_ready"] is True
    for feat_dict in summary_ready["features"].values():
        assert feat_dict["is_ready"] is True
        assert feat_dict["state"] == "ready"


def test_contract_preservation_for_orders_and_feedback() -> None:
    """Test contract preservation for non-item features like orders and feedback."""
    entry_id = "test_entry_non_item"
    orders_contract = MigrationPolicy.verify_contract_preservation(
        entry_id, FeatureKey.ORDERS, item_ids=["ignored_item_id"]
    )
    assert orders_contract.contracts_intact is True
    assert f"{entry_id}_orders_overdue" in orders_contract.expected_unique_ids
    assert "ebay_order_overdue" in orders_contract.expected_event_types

    feedback_contract = MigrationPolicy.verify_contract_preservation(
        entry_id, FeatureKey.FEEDBACK
    )
    assert feedback_contract.contracts_intact is True
    assert f"{entry_id}_feedback_score" in feedback_contract.expected_unique_ids
    assert (
        f"{entry_id}_positive_feedback_percent" in feedback_contract.expected_unique_ids
    )
    assert feedback_contract.expected_event_types == ()


def test_deduplication_stores_remain_compatible() -> None:
    """Verify that persistent stores continue to key off (entry_id, section, item_id, end_time) identically."""
    from datetime import datetime, timezone

    entry_id = "test_entry_store_1"
    item_id = "1122334455"
    end_time_str = datetime(2026, 11, 1, 12, 0, 0, tzinfo=timezone.utc).isoformat()
    key = (entry_id, "watching", item_id, 60, end_time_str)

    serialized = serialize_ending_soon_fired_key(key)
    deserialized = deserialize_ending_soon_fired_key(serialized)
    assert deserialized == key
