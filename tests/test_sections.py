"""SectionSpec registry and cadence helper tests."""

from __future__ import annotations

from custom_components.ebay.api import enabled_sections_for_options
from custom_components.ebay.const import (
    ALL_SECTIONS,
    SECTION_ACCOUNT_PRIVILEGES_INTERVAL_MINUTES,
    SECTION_ANALYTICS_INTERVAL_MINUTES,
    SECTION_FEEDBACK_INTERVAL_MINUTES,
    SECTION_FINANCES_INTERVAL_MINUTES,
    SECTION_SELLER_STANDARDS_INTERVAL_MINUTES,
    section_interval_minutes,
    section_retry_minutes,
)
from custom_components.ebay.sections import MESSAGES_POLL_MULTIPLIER, SECTION_SPECS


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
        buying_enabled=True,
        selling_enabled=False,
        analytics_enabled=True,
        fulfillment_enabled=True,
        seller_standards_enabled=False,
        feedback_enabled=False,
        messages_enabled=True,
        account_privileges_enabled=True,
        finances_enabled=False,
    ) == {"buying", "fulfillment", "messages", "account_privileges"}

    # Analytics requires selling.
    assert "analytics" not in enabled_sections_for_options(
        buying_enabled=False,
        selling_enabled=False,
        analytics_enabled=True,
        fulfillment_enabled=False,
        seller_standards_enabled=False,
        feedback_enabled=False,
        messages_enabled=False,
    )
    assert "analytics" in enabled_sections_for_options(
        buying_enabled=False,
        selling_enabled=True,
        analytics_enabled=True,
        fulfillment_enabled=False,
        seller_standards_enabled=False,
        feedback_enabled=False,
        messages_enabled=False,
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
