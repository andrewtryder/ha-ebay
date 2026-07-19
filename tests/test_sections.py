"""SectionSpec registry and cadence helper tests."""

from __future__ import annotations

from custom_components.ebay.const import (
    CONF_ACCOUNT_PRIVILEGES_ENABLED,
    CONF_ANALYTICS_ENABLED,
    CONF_BUYING_ENABLED,
    CONF_FEEDBACK_ENABLED,
    CONF_FINANCES_ENABLED,
    CONF_FULFILLMENT_ENABLED,
    CONF_MESSAGES_ENABLED,
    CONF_SELLER_STANDARDS_ENABLED,
    CONF_SELLING_ENABLED,
    SECTION_ACCOUNT_PRIVILEGES_INTERVAL_MINUTES,
    SECTION_ANALYTICS_INTERVAL_MINUTES,
    SECTION_FEEDBACK_INTERVAL_MINUTES,
    SECTION_FINANCES_INTERVAL_MINUTES,
    SECTION_SELLER_STANDARDS_INTERVAL_MINUTES,
    section_interval_minutes,
    section_retry_minutes,
)
from custom_components.ebay.sections import (
    ALL_SECTIONS,
    MESSAGES_POLL_MULTIPLIER,
    MISSING_SCOPE_PARTIAL_FAILURES,
    PARTIAL_FAILURE_CATEGORIES_BY_SECTION,
    PARTIAL_FAILURE_SECTION,
    SECTION_SPECS,
    TRUNCATED_KEYS_BY_SECTION,
    TRUNCATION_COLLECTION_SECTION,
    enabled_sections_for_options,
    module_scopes,
)


def test_section_specs_cover_all_sections() -> None:
    assert set(SECTION_SPECS) == set(ALL_SECTIONS)
    for key, spec in SECTION_SPECS.items():
        assert spec.key == key
        assert spec.option_key is not None
        assert isinstance(spec.scopes, tuple)
        assert isinstance(spec.partial_failure_categories, tuple)


def test_section_interval_minutes_from_specs() -> None:
    assert section_interval_minutes("buying", 30) == 30
    assert section_interval_minutes("selling", 15) == 15
    assert section_interval_minutes("fulfillment", 30) == 30
    assert section_interval_minutes("messages", 30) == 30 * MESSAGES_POLL_MULTIPLIER
    assert section_interval_minutes("feedback", 30) == SECTION_FEEDBACK_INTERVAL_MINUTES
    assert (
        section_interval_minutes("analytics", 30) == SECTION_ANALYTICS_INTERVAL_MINUTES
    )
    assert (
        section_interval_minutes("seller_standards", 30)
        == SECTION_SELLER_STANDARDS_INTERVAL_MINUTES
    )
    assert (
        section_interval_minutes("account_privileges", 30)
        == SECTION_ACCOUNT_PRIVILEGES_INTERVAL_MINUTES
    )
    assert section_interval_minutes("finances", 30) == SECTION_FINANCES_INTERVAL_MINUTES
    assert section_interval_minutes("unknown", 30) == 30


def test_section_retry_minutes_from_specs() -> None:
    assert section_retry_minutes("buying", 30) == 30
    assert section_retry_minutes("messages", 30) == 30 * MESSAGES_POLL_MULTIPLIER
    assert section_retry_minutes("feedback", 30) == 60
    assert section_retry_minutes("analytics", 30) == 60
    assert section_retry_minutes("seller_standards", 30) == 60
    assert section_retry_minutes("account_privileges", 30) == 60
    assert section_retry_minutes("finances", 30) == 60
    assert section_retry_minutes("unknown", 30) == 30


def test_enabled_sections_for_options_uses_specs() -> None:
    assert enabled_sections_for_options(
        {
            CONF_BUYING_ENABLED: True,
            CONF_SELLING_ENABLED: False,
            CONF_ANALYTICS_ENABLED: True,
            CONF_FULFILLMENT_ENABLED: True,
            CONF_SELLER_STANDARDS_ENABLED: False,
            CONF_FEEDBACK_ENABLED: False,
            CONF_MESSAGES_ENABLED: True,
            CONF_ACCOUNT_PRIVILEGES_ENABLED: True,
            CONF_FINANCES_ENABLED: False,
        }
    ) == {"buying", "fulfillment", "messages", "account_privileges"}

    # Analytics requires selling.
    assert "analytics" not in enabled_sections_for_options(
        {
            CONF_ANALYTICS_ENABLED: True,
            CONF_SELLING_ENABLED: False,
        }
    )
    assert "analytics" in enabled_sections_for_options(
        {
            CONF_ANALYTICS_ENABLED: True,
            CONF_SELLING_ENABLED: True,
        }
    )


def test_section_specs_partial_failure_categories_nonempty_for_ops() -> None:
    for key in (
        "analytics",
        "fulfillment",
        "seller_standards",
        "feedback",
        "messages",
        "account_privileges",
        "finances",
    ):
        assert SECTION_SPECS[key].partial_failure_categories
        assert SECTION_SPECS[key].fetcher_name
    assert SECTION_SPECS["analytics"].marketplace_capability == "traffic_report"
    assert SECTION_SPECS["finances"].marketplace_capability == "finances"


def test_module_scopes_derived_from_specs() -> None:
    scopes = module_scopes()
    assert CONF_BUYING_ENABLED not in scopes
    assert CONF_SELLING_ENABLED not in scopes
    assert scopes[CONF_ANALYTICS_ENABLED] == SECTION_SPECS["analytics"].scopes
    assert scopes[CONF_FINANCES_ENABLED] == SECTION_SPECS["finances"].scopes
    assert scopes[CONF_FULFILLMENT_ENABLED] == SECTION_SPECS["fulfillment"].scopes


def test_partial_failure_and_truncation_maps_derived_from_specs() -> None:
    assert PARTIAL_FAILURE_CATEGORIES_BY_SECTION["buying"] == frozenset(
        SECTION_SPECS["buying"].partial_failure_categories
    )
    assert PARTIAL_FAILURE_SECTION["finances_missing_scope"] == "finances"
    assert TRUNCATED_KEYS_BY_SECTION["buying"] == ("watched", "bidding")
    assert TRUNCATION_COLLECTION_SECTION["orders"] == "fulfillment"
    assert "finances_missing_scope" in MISSING_SCOPE_PARTIAL_FAILURES
    assert "analytics_views" not in MISSING_SCOPE_PARTIAL_FAILURES
