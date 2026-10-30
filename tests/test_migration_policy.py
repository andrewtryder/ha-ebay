"""Tests for the non-production existing-user migration policy and readiness foundation."""

from __future__ import annotations

from typing import Any
import pytest

from custom_components.ebay.const import (
    BIDDING_EVENT_TYPES,
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
    SELLER_OPS_EVENT_TYPES,
    SELLING_EVENT_TYPES,
    WATCHING_EVENT_TYPES,
)
from custom_components.ebay.ending_soon_store import (
    deserialize_ending_soon_fired_key,
    serialize_ending_soon_fired_key,
)
from custom_components.ebay.migration_policy import (
    REDACTED_DIAGNOSTIC,
    _ITEM_SENSOR_SUFFIXES_BY_KIND,
    FeatureKey,
    FeatureReadiness,
    MigrationPolicy,
    ProviderType,
    ReadinessEvidence,
    ReadinessState,
    sanitize_diagnostic_token,
)
from custom_components.ebay.sensor import ITEM_SENSOR_FIELDS


def test_production_default_providers_ground_truth() -> None:
    """Verify that default baseline providers match the real production architecture."""
    # XML baseline domains (Buying and Selling listings)
    for xml_feat in (
        FeatureKey.WATCHLIST,
        FeatureKey.BIDDING,
        FeatureKey.SELLER_LISTINGS,
        FeatureKey.OFFERS,
        FeatureKey.SOLD_UNSOLD,
    ):
        assert (
            MigrationPolicy.get_default_provider(xml_feat) == ProviderType.TRADING_XML
        )

    # Hybrid baseline domain (GetUser XML + REST Feedback)
    assert (
        MigrationPolicy.get_default_provider(FeatureKey.FEEDBACK) == ProviderType.HYBRID
    )

    # REST baseline domains (Fulfillment orders, standards, messages, analytics, finances, privileges)
    for rest_feat in (
        FeatureKey.ORDERS,
        FeatureKey.SELLER_STANDARDS,
        FeatureKey.MESSAGES,
        FeatureKey.ANALYTICS,
        FeatureKey.FINANCES,
        FeatureKey.ACCOUNT_PRIVILEGES,
    ):
        assert MigrationPolicy.get_default_provider(rest_feat) == ProviderType.REST


def test_get_active_provider_fails_closed_and_requires_verified_readiness() -> None:
    """Active provider must fail closed without explicit opt-in and verified readiness."""
    # 1. Unconfigured or None options returns default provider
    assert (
        MigrationPolicy.get_active_provider(FeatureKey.WATCHLIST)
        == ProviderType.TRADING_XML
    )
    assert MigrationPolicy.get_active_provider(FeatureKey.ORDERS) == ProviderType.REST
    assert (
        MigrationPolicy.get_active_provider(FeatureKey.WATCHLIST, {})
        == ProviderType.TRADING_XML
    )

    # 2. Options specify provider, but NO explicit migration opt-in flag -> fails closed
    assert (
        MigrationPolicy.get_active_provider(
            FeatureKey.WATCHLIST,
            {"provider_watchlist": ProviderType.GRAPHQL},
        )
        == ProviderType.TRADING_XML
    )

    # 3. Explicit opt-in present, but NO readiness assessment provided -> fails closed
    assert (
        MigrationPolicy.get_active_provider(
            FeatureKey.WATCHLIST,
            {
                "enable_modern_api_migration": True,
                "provider_watchlist": ProviderType.GRAPHQL,
            },
            readiness=None,
        )
        == ProviderType.TRADING_XML
    )

    # 4. Explicit opt-in present, but provider_<feature> missing or empty
    assert (
        MigrationPolicy.get_active_provider(
            FeatureKey.WATCHLIST,
            {"enable_modern_api_migration": True},
        )
        == ProviderType.TRADING_XML
    )

    # 5. Explicit opt-in present, but invalid provider string
    assert (
        MigrationPolicy.get_active_provider(
            FeatureKey.WATCHLIST,
            {
                "enable_modern_api_migration": True,
                "provider_watchlist": "invalid_unknown_provider",
            },
        )
        == ProviderType.TRADING_XML
    )

    # 6. Explicit opt-in present, but candidate matches default provider
    assert (
        MigrationPolicy.get_active_provider(
            FeatureKey.WATCHLIST,
            {
                "enable_modern_api_migration": True,
                "provider_watchlist": ProviderType.TRADING_XML,
            },
        )
        == ProviderType.TRADING_XML
    )

    # 7. Explicit opt-in present, but readiness indicates BLOCKED -> fails closed
    unready = FeatureReadiness(
        feature=FeatureKey.WATCHLIST,
        candidate_provider=ProviderType.GRAPHQL,
        state=ReadinessState.BLOCKED_AUTHORIZATION,
        is_ready=False,
        blocking_reasons=("FORBIDDEN",),
    )
    assert (
        MigrationPolicy.get_active_provider(
            FeatureKey.WATCHLIST,
            {
                "enable_modern_api_migration": True,
                "provider_watchlist": ProviderType.GRAPHQL,
            },
            readiness=unready,
        )
        == ProviderType.TRADING_XML
    )

    # 8. Unsupported candidate provider (e.g. TRADING_XML on REST-native ORDERS) -> fails closed
    ready_bogus = FeatureReadiness(
        feature=FeatureKey.ORDERS,
        candidate_provider=ProviderType.TRADING_XML,
        state=ReadinessState.READY,
        is_ready=True,
    )
    assert (
        MigrationPolicy.get_active_provider(
            FeatureKey.ORDERS,
            {
                "enable_modern_api_migration": True,
                "provider_orders": ProviderType.TRADING_XML,
            },
            readiness=ready_bogus,
        )
        == ProviderType.REST
    )

    # 9. Opt-in present, candidate supported, and readiness is proven -> safely activated
    ready_proven = FeatureReadiness(
        feature=FeatureKey.WATCHLIST,
        candidate_provider=ProviderType.GRAPHQL,
        state=ReadinessState.READY,
        is_ready=True,
    )
    assert (
        MigrationPolicy.get_active_provider(
            FeatureKey.WATCHLIST,
            {
                "enable_modern_api_migration": True,
                "provider_watchlist": ProviderType.GRAPHQL,
            },
            readiness=ready_proven,
        )
        == ProviderType.GRAPHQL
    )


def test_evaluate_readiness_baseline_always_ready() -> None:
    """The established baseline provider for any domain always evaluates to READY."""
    evidence = ReadinessEvidence(fallback_available=True)
    res_xml = MigrationPolicy.evaluate_readiness(
        FeatureKey.WATCHLIST, ProviderType.TRADING_XML, evidence
    )
    assert res_xml.is_ready is True
    assert res_xml.state == ReadinessState.READY
    assert res_xml.blocking_reasons == ()
    assert "production_trading_xml_baseline" in res_xml.sanitized_diagnostics.get(
        "provider", ""
    )

    res_rest = MigrationPolicy.evaluate_readiness(
        FeatureKey.ORDERS, ProviderType.REST, evidence
    )
    assert res_rest.is_ready is True
    assert res_rest.state == ReadinessState.READY
    assert res_rest.blocking_reasons == ()
    assert "production_rest_baseline" in res_rest.sanitized_diagnostics.get(
        "provider", ""
    )


def test_evaluate_readiness_unsupported_candidate_provider() -> None:
    """Trading XML is not a valid candidate for REST-native features."""
    evidence = ReadinessEvidence(fallback_available=True)
    res = MigrationPolicy.evaluate_readiness(
        FeatureKey.ORDERS, ProviderType.TRADING_XML, evidence
    )
    assert res.is_ready is False
    assert res.state == ReadinessState.NOT_SUPPORTED
    assert any("not supported for feature domain" in r for r in res.blocking_reasons)


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
        scopes_contract_established=True,
        required_scopes=("https://api.ebay.com/oauth/api_scope",),
        verified_scopes=("https://api.ebay.com/oauth/api_scope",),
        api_permission_granted=granted,
        permission_error_code=error_code,
        response_complete=True,
        pagination_verified=True,
        field_parity_verified=True,
        account_identity_consistent=True,
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
        scopes_contract_established=True,
        required_scopes=("buy.watchlist.read",),
        verified_scopes=(),
        api_permission_granted=True,
        response_complete=True,
        pagination_verified=True,
        field_parity_verified=True,
        account_identity_consistent=True,
        fallback_available=True,
    )
    res = MigrationPolicy.evaluate_readiness(
        FeatureKey.WATCHLIST, ProviderType.GRAPHQL, evidence
    )
    assert res.is_ready is False
    assert res.state == ReadinessState.BLOCKED_SCOPE
    assert any("buy.watchlist.read" in r for r in res.blocking_reasons)


def test_evaluate_readiness_blocked_scope_contract_unestablished() -> None:
    """Unestablished OAuth scope contract must block readiness even with empty required scopes."""
    evidence = ReadinessEvidence(
        schema_validated=True,
        scopes_contract_established=False,
        required_scopes=(),
        verified_scopes=(),
        api_permission_granted=True,
        response_complete=True,
        pagination_verified=True,
        field_parity_verified=True,
        account_identity_consistent=True,
        fallback_available=True,
    )
    res = MigrationPolicy.evaluate_readiness(
        FeatureKey.WATCHLIST, ProviderType.GRAPHQL, evidence
    )
    assert res.is_ready is False
    assert res.state == ReadinessState.BLOCKED_SCOPE
    assert any(
        "OAuth scope contract has not been established" in r
        for r in res.blocking_reasons
    )


def test_evaluate_readiness_blocked_empty_required_scopes() -> None:
    """Established scope contract with empty required scopes fails closed."""
    evidence = ReadinessEvidence(
        schema_validated=True,
        scopes_contract_established=True,
        required_scopes=(),
        verified_scopes=(),
        api_permission_granted=True,
        response_complete=True,
        pagination_verified=True,
        field_parity_verified=True,
        account_identity_consistent=True,
        fallback_available=True,
    )
    res = MigrationPolicy.evaluate_readiness(
        FeatureKey.WATCHLIST, ProviderType.GRAPHQL, evidence
    )
    assert res.is_ready is False
    assert res.state == ReadinessState.BLOCKED_SCOPE
    assert any(
        "OAuth scope requirements are unknown" in r for r in res.blocking_reasons
    )


def test_evaluate_readiness_minimal_false_ready_bypass_rejected() -> None:
    """Minimal evidence with all other booleans True fails closed when identity/fallback/scopes are omitted."""
    evidence = ReadinessEvidence(
        schema_validated=True,
        api_permission_granted=True,
        response_complete=True,
        pagination_verified=True,
        field_parity_verified=True,
        # omitted: scopes_contract_established (defaults to False)
        # omitted: account_identity_consistent (defaults to False)
        # omitted: fallback_available (defaults to False)
    )
    res = MigrationPolicy.evaluate_readiness(
        FeatureKey.WATCHLIST, ProviderType.GRAPHQL, evidence
    )
    assert res.is_ready is False
    assert res.state != ReadinessState.READY
    # All three fail-closed requirements must be flagged in blocking reasons
    assert any("Fallback to established provider" in r for r in res.blocking_reasons)
    assert any(
        "does not match the authenticated account identity" in r
        for r in res.blocking_reasons
    )
    assert any(
        "OAuth scope contract has not been established" in r
        for r in res.blocking_reasons
    )


def test_evaluate_readiness_blocked_schema_and_census() -> None:
    """Unvalidated schema and incomplete census must block readiness."""
    # 1. Schema unvalidated
    ev_schema = ReadinessEvidence(
        schema_validated=False,
        scopes_contract_established=True,
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
    )
    res_schema = MigrationPolicy.evaluate_readiness(
        FeatureKey.SELLER_LISTINGS, ProviderType.GRAPHQL, ev_schema
    )
    assert res_schema.is_ready is False
    assert res_schema.state == ReadinessState.BLOCKED_SCHEMA

    # 2. Incomplete census / response incomplete
    ev_census = ReadinessEvidence(
        schema_validated=True,
        scopes_contract_established=True,
        required_scopes=(
            "https://api.ebay.com/oauth/api_scope/sell.inventory.readonly",
        ),
        verified_scopes=(
            "https://api.ebay.com/oauth/api_scope/sell.inventory.readonly",
        ),
        api_permission_granted=True,
        response_complete=False,
        pagination_verified=True,
        field_parity_verified=True,
        account_identity_consistent=True,
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
        scopes_contract_established=True,
        required_scopes=("https://api.ebay.com/oauth/api_scope",),
        verified_scopes=("https://api.ebay.com/oauth/api_scope",),
        api_permission_granted=True,
        response_complete=True,
        pagination_verified=False,
        field_parity_verified=True,
        account_identity_consistent=True,
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
        scopes_contract_established=True,
        required_scopes=("https://api.ebay.com/oauth/api_scope",),
        verified_scopes=("https://api.ebay.com/oauth/api_scope",),
        api_permission_granted=True,
        response_complete=True,
        pagination_verified=True,
        field_parity_verified=False,
        missing_fields=("current_price",),
        unverified_fields=("views",),
        account_identity_consistent=True,
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
        scopes_contract_established=True,
        required_scopes=("https://api.ebay.com/oauth/api_scope",),
        verified_scopes=("https://api.ebay.com/oauth/api_scope",),
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
        scopes_contract_established=True,
        required_scopes=("https://api.ebay.com/oauth/api_scope",),
        verified_scopes=("https://api.ebay.com/oauth/api_scope",),
        api_permission_granted=True,
        response_complete=True,
        pagination_verified=True,
        field_parity_verified=True,
        account_identity_consistent=True,
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
        scopes_contract_established=True,
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
        notes="Offline test harness verification",
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
    assert opt_dict["sanitized_diagnostics"]["has_notes"] is True


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

    # Rollback to default baseline provider is ALWAYS permitted
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

    # Contradictory assessments: is_ready=True but state is BLOCKED_*
    contra_state = FeatureReadiness(
        feature=FeatureKey.WATCHLIST,
        candidate_provider=ProviderType.GRAPHQL,
        state=ReadinessState.BLOCKED_AUTHORIZATION,
        is_ready=True,
    )
    assert (
        MigrationPolicy.validate_safe_transition(
            FeatureKey.WATCHLIST, ProviderType.GRAPHQL, contra_state
        )
        is False
    )

    # Contradictory assessments: is_ready=True but blocking_reasons is non-empty
    contra_reasons = FeatureReadiness(
        feature=FeatureKey.WATCHLIST,
        candidate_provider=ProviderType.GRAPHQL,
        state=ReadinessState.READY,
        is_ready=True,
        blocking_reasons=("Some unexpected blocking reason",),
    )
    assert (
        MigrationPolicy.validate_safe_transition(
            FeatureKey.WATCHLIST, ProviderType.GRAPHQL, contra_reasons
        )
        is False
    )


def test_item_sensor_suffixes_match_sensor_module_constants() -> None:
    """Verify that migration policy item sensor suffixes exactly match sensor.py."""
    for kind, field_tuples in ITEM_SENSOR_FIELDS.items():
        expected_suffixes = tuple(suffix for suffix, _, _ in field_tuples)
        assert _ITEM_SENSOR_SUFFIXES_BY_KIND[kind] == expected_suffixes


def test_event_types_match_const_module_definitions() -> None:
    """Verify that expected event types match const.py EventEntity definitions."""
    assert MigrationPolicy.get_expected_event_types(FeatureKey.WATCHLIST) == tuple(
        WATCHING_EVENT_TYPES
    )
    assert MigrationPolicy.get_expected_event_types(FeatureKey.BIDDING) == tuple(
        BIDDING_EVENT_TYPES
    )
    assert MigrationPolicy.get_expected_event_types(
        FeatureKey.SELLER_LISTINGS
    ) == tuple(SELLING_EVENT_TYPES)
    # Check that orders, standards, feedback, and messages event types are all valid seller ops events
    for feat in (
        FeatureKey.ORDERS,
        FeatureKey.SELLER_STANDARDS,
        FeatureKey.FEEDBACK,
        FeatureKey.MESSAGES,
    ):
        events = MigrationPolicy.get_expected_event_types(feat)
        for ev in events:
            assert ev in SELLER_OPS_EVENT_TYPES


def test_contract_preservation_genuine_verification_passing() -> None:
    """Genuinely verify contract preservation when observed inputs match real constants."""
    entry_id = "test_entry_abcdef"

    # 1. Watchlist with observed matching expected
    exp_watchlist_ids = MigrationPolicy.get_expected_unique_ids(
        entry_id, FeatureKey.WATCHLIST, item_ids=["1001"]
    )
    exp_watchlist_events = MigrationPolicy.get_expected_event_types(
        FeatureKey.WATCHLIST
    )
    assert f"{entry_id}_watched_items" in exp_watchlist_ids
    assert f"{entry_id}_watched_ending_soon" in exp_watchlist_ids
    assert f"{entry_id}_watched_1001_price" in exp_watchlist_ids
    assert f"{entry_id}_watched_1001_bids" in exp_watchlist_ids
    assert f"{entry_id}_watched_1001_seconds_left" in exp_watchlist_ids
    assert f"{entry_id}_watched_1001_end_time" in exp_watchlist_ids

    res_watchlist = MigrationPolicy.verify_contract_preservation(
        entry_id,
        FeatureKey.WATCHLIST,
        item_ids=["1001"],
        observed_unique_ids=exp_watchlist_ids,
        observed_event_types=exp_watchlist_events,
    )
    assert res_watchlist.contracts_intact is True
    assert res_watchlist.violations == ()

    # 2. Seller listings with observed matching expected
    exp_seller_ids = MigrationPolicy.get_expected_unique_ids(
        entry_id, FeatureKey.SELLER_LISTINGS, item_ids=["2001"]
    )
    exp_seller_events = MigrationPolicy.get_expected_event_types(
        FeatureKey.SELLER_LISTINGS
    )
    assert f"{entry_id}_active_selling_items" in exp_seller_ids
    assert f"{entry_id}_selling_total_bids" in exp_seller_ids
    assert f"{entry_id}_selling_2001_price" in exp_seller_ids
    assert f"{entry_id}_selling_2001_views" in exp_seller_ids

    res_seller = MigrationPolicy.verify_contract_preservation(
        entry_id,
        FeatureKey.SELLER_LISTINGS,
        item_ids=["2001"],
        observed_unique_ids=exp_seller_ids,
        observed_event_types=exp_seller_events,
    )
    assert res_seller.contracts_intact is True
    assert res_seller.violations == ()


def test_contract_preservation_fails_on_missing_or_mismatched_contracts() -> None:
    """Non-tautological verification: missing or extra contracts must fail."""
    entry_id = "test_entry_mismatch"

    # Case 1: Caller supplies NO observed contracts -> fails
    res_none = MigrationPolicy.verify_contract_preservation(
        entry_id, FeatureKey.WATCHLIST, item_ids=["1001"]
    )
    assert res_none.contracts_intact is False
    assert any("NO_OBSERVED_UNIQUE_IDS" in v for v in res_none.violations)
    assert any("NO_OBSERVED_EVENT_TYPES" in v for v in res_none.violations)

    # Case 2: Observed unique IDs missing an expected entity
    exp_ids = MigrationPolicy.get_expected_unique_ids(entry_id, FeatureKey.WATCHLIST)
    exp_events = MigrationPolicy.get_expected_event_types(FeatureKey.WATCHLIST)
    incomplete_ids = exp_ids[:-1]  # drop one
    res_missing = MigrationPolicy.verify_contract_preservation(
        entry_id,
        FeatureKey.WATCHLIST,
        observed_unique_ids=incomplete_ids,
        observed_event_types=exp_events,
    )
    assert res_missing.contracts_intact is False
    assert any("MISSING_EXPECTED_UNIQUE_IDS" in v for v in res_missing.violations)

    # Case 3: Observed unique IDs contains unexpected altered ID
    corrupted_ids = (*exp_ids, f"{entry_id}_altered_entity_key")
    res_corrupted = MigrationPolicy.verify_contract_preservation(
        entry_id,
        FeatureKey.WATCHLIST,
        observed_unique_ids=corrupted_ids,
        observed_event_types=exp_events,
    )
    assert res_corrupted.contracts_intact is False
    assert any("UNEXPECTED_EXTRA_UNIQUE_IDS" in v for v in res_corrupted.violations)

    # Case 4: Observed event types missing an expected event
    incomplete_events = exp_events[:-1]
    res_missing_event = MigrationPolicy.verify_contract_preservation(
        entry_id,
        FeatureKey.WATCHLIST,
        observed_unique_ids=exp_ids,
        observed_event_types=incomplete_events,
    )
    assert res_missing_event.contracts_intact is False
    assert any(
        "MISSING_EXPECTED_EVENT_TYPES" in v for v in res_missing_event.violations
    )

    # Case 5: Observed event types has unexpected extra event
    extra_events = (*exp_events, "unexpected_event_foo")
    res_extra_event = MigrationPolicy.verify_contract_preservation(
        entry_id,
        FeatureKey.WATCHLIST,
        observed_unique_ids=exp_ids,
        observed_event_types=extra_events,
    )
    assert res_extra_event.contracts_intact is False
    assert any("UNEXPECTED_EXTRA_EVENT_TYPES" in v for v in res_extra_event.violations)


def test_contract_preservation_privacy_adversarial_protection() -> None:
    """Ensure contract preservation violations and diagnostics never leak private IDs or tokens."""
    entry_id = "entry_private_user_456"
    private_item_id = "112233445566"
    private_token = "v^1.1#i^1#r^0#p^3#secret_token_12345"
    private_username = "secret_ebay_user_99"

    # Adversarial observed IDs containing raw private item IDs and tokens
    adversarial_observed_ids = [
        f"{entry_id}_watched_items",
        f"corrupted_{private_item_id}_suffix",
        private_token,
        private_username,
    ]
    adversarial_observed_events = [
        "watched_item_ending_soon",
        f"event_{private_item_id}",
        private_token,
    ]

    res = MigrationPolicy.verify_contract_preservation(
        entry_id,
        FeatureKey.WATCHLIST,
        item_ids=[private_item_id],
        observed_unique_ids=adversarial_observed_ids,
        observed_event_types=adversarial_observed_events,
    )
    assert res.contracts_intact is False

    # Check violations: MUST NOT leak private item ID, token, username, or entry_id
    all_violations_text = " ".join(res.violations)
    assert private_item_id not in all_violations_text
    assert private_token not in all_violations_text
    assert private_username not in all_violations_text
    assert entry_id not in all_violations_text

    # Diagnostic dictionary export must be fully sanitized
    diag = res.to_diagnostics_dict()
    diag_str = str(diag)
    assert private_item_id not in diag_str
    assert private_token not in diag_str
    assert private_username not in diag_str
    assert entry_id not in diag_str
    assert diag["contracts_intact"] is False
    assert "violation_count" in diag
    assert "violations" in diag


def test_strict_privacy_allowlist_redacts_secrets_pii_and_arbitrary_strings() -> None:
    """Strict privacy allowlisting must redact secrets, tokens, usernames, and item IDs."""
    # Safe symbolic codes and HTTP status codes pass through
    assert sanitize_diagnostic_token("FORBIDDEN") == "FORBIDDEN"
    assert sanitize_diagnostic_token("ACCESS_DENIED") == "ACCESS_DENIED"
    assert sanitize_diagnostic_token("403") == "403"
    assert sanitize_diagnostic_token("500") == "500"
    assert sanitize_diagnostic_token(None) == "UNKNOWN"
    assert sanitize_diagnostic_token("") == "EMPTY"

    # Safe scopes and fields pass through
    assert (
        sanitize_diagnostic_token(
            "https://api.ebay.com/oauth/api_scope/sell.fulfillment.readonly"
        )
        == "https://api.ebay.com/oauth/api_scope/sell.fulfillment.readonly"
    )
    assert sanitize_diagnostic_token("current_price") == "current_price"
    assert sanitize_diagnostic_token("bid_count") == "bid_count"

    # Adversarial strings, client secrets, refresh tokens, usernames, item IDs must be REDACTED
    assert (
        sanitize_diagnostic_token("v^1.1#i^1#r^0#p^3#I^3#f^0#t^H4sIAAAAAAAAA")
        == REDACTED_DIAGNOSTIC
    )
    assert sanitize_diagnostic_token("client_secret_xyz12345") == REDACTED_DIAGNOSTIC
    assert sanitize_diagnostic_token("123456789012") == REDACTED_DIAGNOSTIC
    assert sanitize_diagnostic_token("cool_ebay_seller_99") == REDACTED_DIAGNOSTIC
    assert (
        sanitize_diagnostic_token("https://api.ebay.com/arbitrary/secret/url?token=xyz")
        == REDACTED_DIAGNOSTIC
    )
    assert sanitize_diagnostic_token("a" * 80) == REDACTED_DIAGNOSTIC


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
    exp_ids = MigrationPolicy.get_expected_unique_ids(
        entry_id, FeatureKey.WATCHLIST, saved_options[CONF_PINNED_ITEM_IDS]
    )
    exp_events = MigrationPolicy.get_expected_event_types(FeatureKey.WATCHLIST)
    contract = MigrationPolicy.verify_contract_preservation(
        entry_id,
        FeatureKey.WATCHLIST,
        saved_options[CONF_PINNED_ITEM_IDS],
        observed_unique_ids=exp_ids,
        observed_event_types=exp_events,
    )
    assert contract.contracts_intact is True
    assert f"{entry_id}_watched_items" in contract.expected_unique_ids
    assert f"{entry_id}_watched_123456789012_price" in contract.expected_unique_ids


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
    summary_ready = MigrationPolicy.export_sanitized_readiness_summary(
        ev_all_ready, candidate_provider=ProviderType.TRADING_XML
    )
    # XML baseline features are ready; REST-native features evaluate XML as NOT_SUPPORTED
    assert summary_ready["features"]["orders"]["state"] == str(
        ReadinessState.NOT_SUPPORTED
    )
    assert summary_ready["features"]["watchlist"]["state"] == str(ReadinessState.READY)


def test_contract_preservation_for_orders_and_feedback() -> None:
    """Test contract preservation for non-item features like orders and feedback."""
    entry_id = "test_entry_non_item"
    exp_orders_ids = MigrationPolicy.get_expected_unique_ids(
        entry_id, FeatureKey.ORDERS
    )
    exp_orders_events = MigrationPolicy.get_expected_event_types(FeatureKey.ORDERS)
    assert f"{entry_id}_orders_overdue" in exp_orders_ids
    assert f"{entry_id}_open_payment_disputes" in exp_orders_ids
    assert "shipment_overdue" in exp_orders_events
    assert "order_received" in exp_orders_events

    orders_contract = MigrationPolicy.verify_contract_preservation(
        entry_id,
        FeatureKey.ORDERS,
        observed_unique_ids=exp_orders_ids,
        observed_event_types=exp_orders_events,
    )
    assert orders_contract.contracts_intact is True

    exp_feedback_ids = MigrationPolicy.get_expected_unique_ids(
        entry_id, FeatureKey.FEEDBACK
    )
    exp_feedback_events = MigrationPolicy.get_expected_event_types(FeatureKey.FEEDBACK)
    assert f"{entry_id}_feedback_score" in exp_feedback_ids
    assert f"{entry_id}_positive_feedback_percent" in exp_feedback_ids
    assert "feedback_received" in exp_feedback_events

    feedback_contract = MigrationPolicy.verify_contract_preservation(
        entry_id,
        FeatureKey.FEEDBACK,
        observed_unique_ids=exp_feedback_ids,
        observed_event_types=exp_feedback_events,
    )
    assert feedback_contract.contracts_intact is True


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
