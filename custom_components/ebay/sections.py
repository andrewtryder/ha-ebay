"""SectionSpec registry for coordinator payload sections.

Single source of truth for refresh cadence, OAuth scopes, marketplace gates,
truncation keys, and partial-failure categories. Fetch orchestration lives in
``section_fetch.py``; per-API fetchers live under ``clients/``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from .clients.scopes import (
    CORE_SCOPE,
    SCOPE_ACCOUNT_READONLY,
    SCOPE_ANALYTICS_READONLY,
    SCOPE_FEEDBACK,
    SCOPE_FINANCES,
    SCOPE_FULFILLMENT_READONLY,
    SCOPE_MESSAGE,
    SCOPE_PAYMENT_DISPUTE,
)
from .const import (
    CONF_ACCOUNT_PRIVILEGES_ENABLED,
    CONF_ANALYTICS_ENABLED,
    CONF_BUYING_ENABLED,
    CONF_FEEDBACK_ENABLED,
    CONF_FINANCES_ENABLED,
    CONF_FULFILLMENT_ENABLED,
    CONF_MESSAGES_ENABLED,
    CONF_SELLER_STANDARDS_ENABLED,
    CONF_SELLING_ENABLED,
    SECTION_ACCOUNT_PRIVILEGES,
    SECTION_ACCOUNT_PRIVILEGES_INTERVAL_MINUTES,
    SECTION_ACCOUNT_PRIVILEGES_RETRY_MINUTES,
    SECTION_ANALYTICS,
    SECTION_ANALYTICS_INTERVAL_MINUTES,
    SECTION_ANALYTICS_RETRY_MINUTES,
    SECTION_BUYING,
    SECTION_FEEDBACK,
    SECTION_FEEDBACK_INTERVAL_MINUTES,
    SECTION_FEEDBACK_RETRY_MINUTES,
    SECTION_FINANCES,
    SECTION_FINANCES_INTERVAL_MINUTES,
    SECTION_FINANCES_RETRY_MINUTES,
    SECTION_FULFILLMENT,
    SECTION_MESSAGES,
    SECTION_SELLER_STANDARDS,
    SECTION_SELLER_STANDARDS_INTERVAL_MINUTES,
    SECTION_SELLER_STANDARDS_RETRY_MINUTES,
    SECTION_SELLING,
)

# Messages refresh at 2× the configured poll interval (not a fixed schedule).
MESSAGES_POLL_MULTIPLIER = 2


@dataclass(frozen=True)
class SectionSpec:
    """Declarative metadata for one coordinator payload section."""

    key: str
    option_key: str | None
    scopes: tuple[str, ...]
    interval_minutes: int | None  # None = use poll_interval
    retry_minutes: int | None
    marketplace_capability: str | None
    fetcher_name: str | None
    partial_failure_categories: tuple[str, ...]
    truncation_collections: tuple[str, ...] = ()
    # When True, section is only enabled if Selling is also enabled.
    requires_selling: bool = False


SECTION_SPECS: dict[str, SectionSpec] = {
    SECTION_BUYING: SectionSpec(
        key=SECTION_BUYING,
        option_key=CONF_BUYING_ENABLED,
        scopes=(CORE_SCOPE,),
        interval_minutes=None,
        retry_minutes=None,
        marketplace_capability=None,
        fetcher_name="_fetch_buying_pages",
        partial_failure_categories=("buying_truncated",),
        truncation_collections=("watched", "bidding"),
    ),
    SECTION_SELLING: SectionSpec(
        key=SECTION_SELLING,
        option_key=CONF_SELLING_ENABLED,
        scopes=(CORE_SCOPE,),
        interval_minutes=None,
        retry_minutes=None,
        marketplace_capability=None,
        fetcher_name="_fetch_selling_pages",
        partial_failure_categories=(
            "selling_truncated",
            "sold_unsold_truncated",
            "sold_unsold_classification",
            "seller_list_views_truncated",
            "seller_list_views",
            "active_offers_truncated",
            "active_offers",
        ),
        truncation_collections=(
            "selling",
            "seller_list_views",
            "active_offers",
            "sold_unsold",
        ),
    ),
    SECTION_ANALYTICS: SectionSpec(
        key=SECTION_ANALYTICS,
        option_key=CONF_ANALYTICS_ENABLED,
        scopes=(SCOPE_ANALYTICS_READONLY,),
        interval_minutes=SECTION_ANALYTICS_INTERVAL_MINUTES,
        retry_minutes=SECTION_ANALYTICS_RETRY_MINUTES,
        marketplace_capability="traffic_report",
        fetcher_name="async_fetch_analytics_views",
        partial_failure_categories=(
            "analytics_views_missing_scope",
            "analytics_views",
            "analytics_views_unsupported",
        ),
        requires_selling=True,
    ),
    SECTION_FULFILLMENT: SectionSpec(
        key=SECTION_FULFILLMENT,
        option_key=CONF_FULFILLMENT_ENABLED,
        scopes=(SCOPE_FULFILLMENT_READONLY, SCOPE_PAYMENT_DISPUTE),
        interval_minutes=None,
        retry_minutes=None,
        marketplace_capability="fulfillment",
        fetcher_name="async_fetch_orders",
        partial_failure_categories=(
            "orders_missing_scope",
            "payment_disputes_missing_scope",
            "orders_truncated",
            "orders",
            "payment_disputes_truncated",
            "payment_disputes",
        ),
        truncation_collections=("orders", "payment_disputes"),
    ),
    SECTION_SELLER_STANDARDS: SectionSpec(
        key=SECTION_SELLER_STANDARDS,
        option_key=CONF_SELLER_STANDARDS_ENABLED,
        scopes=(SCOPE_ANALYTICS_READONLY,),
        interval_minutes=SECTION_SELLER_STANDARDS_INTERVAL_MINUTES,
        retry_minutes=SECTION_SELLER_STANDARDS_RETRY_MINUTES,
        marketplace_capability="seller_standards",
        fetcher_name="async_fetch_seller_standards",
        partial_failure_categories=(
            "seller_standards_missing_scope",
            "seller_standards",
        ),
    ),
    SECTION_FEEDBACK: SectionSpec(
        key=SECTION_FEEDBACK,
        option_key=CONF_FEEDBACK_ENABLED,
        scopes=(SCOPE_FEEDBACK,),
        interval_minutes=SECTION_FEEDBACK_INTERVAL_MINUTES,
        retry_minutes=SECTION_FEEDBACK_RETRY_MINUTES,
        marketplace_capability="feedback",
        fetcher_name="async_fetch_feedback",
        partial_failure_categories=(
            "feedback_missing_scope",
            "feedback",
            "feedback_invalid_request",
            "feedback_user_lookup",
            "feedback_forbidden",
            "feedback_not_found",
        ),
    ),
    SECTION_MESSAGES: SectionSpec(
        key=SECTION_MESSAGES,
        option_key=CONF_MESSAGES_ENABLED,
        scopes=(SCOPE_MESSAGE,),
        # Cadence is poll_interval × MESSAGES_POLL_MULTIPLIER (see const helpers).
        interval_minutes=None,
        retry_minutes=None,
        marketplace_capability=None,
        fetcher_name="async_fetch_messages",
        partial_failure_categories=(
            "messages_missing_scope",
            "messages_truncated",
            "messages",
        ),
        truncation_collections=("messages",),
    ),
    SECTION_ACCOUNT_PRIVILEGES: SectionSpec(
        key=SECTION_ACCOUNT_PRIVILEGES,
        option_key=CONF_ACCOUNT_PRIVILEGES_ENABLED,
        scopes=(SCOPE_ACCOUNT_READONLY,),
        interval_minutes=SECTION_ACCOUNT_PRIVILEGES_INTERVAL_MINUTES,
        retry_minutes=SECTION_ACCOUNT_PRIVILEGES_RETRY_MINUTES,
        marketplace_capability="account_privileges",
        fetcher_name="async_fetch_account_privileges",
        partial_failure_categories=(
            "account_privileges_missing_scope",
            "account_privileges_unsupported",
            "account_privileges",
        ),
    ),
    SECTION_FINANCES: SectionSpec(
        key=SECTION_FINANCES,
        option_key=CONF_FINANCES_ENABLED,
        scopes=(SCOPE_FINANCES,),
        interval_minutes=SECTION_FINANCES_INTERVAL_MINUTES,
        retry_minutes=SECTION_FINANCES_RETRY_MINUTES,
        marketplace_capability="finances",
        fetcher_name="async_fetch_finances",
        partial_failure_categories=(
            "finances_missing_scope",
            "finances_unsupported",
            "finances",
        ),
    ),
}

ALL_SECTIONS: tuple[str, ...] = tuple(SECTION_SPECS)

PARTIAL_FAILURE_CATEGORIES_BY_SECTION: dict[str, frozenset[str]] = {
    key: frozenset(spec.partial_failure_categories)
    for key, spec in SECTION_SPECS.items()
}

PARTIAL_FAILURE_SECTION: dict[str, str] = {
    category: spec.key
    for spec in SECTION_SPECS.values()
    for category in spec.partial_failure_categories
}

TRUNCATED_KEYS_BY_SECTION: dict[str, tuple[str, ...]] = {
    key: spec.truncation_collections
    for key, spec in SECTION_SPECS.items()
    if spec.truncation_collections
}

TRUNCATION_COLLECTION_SECTION: dict[str, str] = {
    collection: spec.key
    for spec in SECTION_SPECS.values()
    for collection in spec.truncation_collections
}

EMPTY_TRUNCATED_COLLECTIONS: dict[str, bool] = {
    collection: False
    for spec in SECTION_SPECS.values()
    for collection in spec.truncation_collections
}

MISSING_SCOPE_PARTIAL_FAILURES: frozenset[str] = frozenset(
    category
    for spec in SECTION_SPECS.values()
    for category in spec.partial_failure_categories
    if category.endswith("_missing_scope")
)


def module_scopes() -> dict[str, tuple[str, ...]]:
    """Return option_key → scopes for modules that need scopes beyond core Trading."""
    return {
        spec.option_key: spec.scopes
        for spec in SECTION_SPECS.values()
        if spec.option_key and spec.scopes != (CORE_SCOPE,)
    }


def enabled_sections_for_options(options: Mapping[str, Any]) -> set[str]:
    """Return payload sections that should be fetched for the given options."""
    sections: set[str] = set()
    for key, spec in SECTION_SPECS.items():
        if spec.option_key is None or not options.get(spec.option_key):
            continue
        if spec.requires_selling and not options.get(CONF_SELLING_ENABLED):
            continue
        sections.add(key)
    return sections


def section_marketplace_capability(section: str) -> str | None:
    """Return the marketplace capability gate for a section, if any."""
    spec = SECTION_SPECS.get(section)
    return None if spec is None else spec.marketplace_capability
