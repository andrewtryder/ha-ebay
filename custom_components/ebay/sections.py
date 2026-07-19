"""SectionSpec registry for coordinator payload sections.

Pragmatic metadata for refresh cadence, OAuth scopes, marketplace gates, and
partial-failure categories. Fetch orchestration remains in ``api.py`` for now;
a future pass may move per-section clients under ``clients/``.
"""

from __future__ import annotations

from dataclasses import dataclass

from .api import (
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
    PARTIAL_FAILURE_SECTION,
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


def _partial_categories(section: str) -> tuple[str, ...]:
    """Return partial-failure category keys owned by ``section``."""
    return tuple(
        category
        for category, owner in PARTIAL_FAILURE_SECTION.items()
        if owner == section
    )


SECTION_SPECS: dict[str, SectionSpec] = {
    SECTION_BUYING: SectionSpec(
        key=SECTION_BUYING,
        option_key=CONF_BUYING_ENABLED,
        scopes=(CORE_SCOPE,),
        interval_minutes=None,
        retry_minutes=None,
        marketplace_capability=None,
        fetcher_name="_fetch_buying_pages",
        partial_failure_categories=_partial_categories(SECTION_BUYING),
    ),
    SECTION_SELLING: SectionSpec(
        key=SECTION_SELLING,
        option_key=CONF_SELLING_ENABLED,
        scopes=(CORE_SCOPE,),
        interval_minutes=None,
        retry_minutes=None,
        marketplace_capability=None,
        fetcher_name="_fetch_selling_pages",
        partial_failure_categories=_partial_categories(SECTION_SELLING),
    ),
    SECTION_ANALYTICS: SectionSpec(
        key=SECTION_ANALYTICS,
        option_key=CONF_ANALYTICS_ENABLED,
        scopes=(SCOPE_ANALYTICS_READONLY,),
        interval_minutes=SECTION_ANALYTICS_INTERVAL_MINUTES,
        retry_minutes=SECTION_ANALYTICS_RETRY_MINUTES,
        marketplace_capability="traffic_report",
        fetcher_name="async_fetch_analytics_views",
        partial_failure_categories=_partial_categories(SECTION_ANALYTICS),
    ),
    SECTION_FULFILLMENT: SectionSpec(
        key=SECTION_FULFILLMENT,
        option_key=CONF_FULFILLMENT_ENABLED,
        scopes=(SCOPE_FULFILLMENT_READONLY, SCOPE_PAYMENT_DISPUTE),
        interval_minutes=None,
        retry_minutes=None,
        marketplace_capability="fulfillment",
        fetcher_name="async_fetch_orders",
        partial_failure_categories=_partial_categories(SECTION_FULFILLMENT),
    ),
    SECTION_SELLER_STANDARDS: SectionSpec(
        key=SECTION_SELLER_STANDARDS,
        option_key=CONF_SELLER_STANDARDS_ENABLED,
        scopes=(SCOPE_ANALYTICS_READONLY,),
        interval_minutes=SECTION_SELLER_STANDARDS_INTERVAL_MINUTES,
        retry_minutes=SECTION_SELLER_STANDARDS_RETRY_MINUTES,
        marketplace_capability="seller_standards",
        fetcher_name="async_fetch_seller_standards",
        partial_failure_categories=_partial_categories(SECTION_SELLER_STANDARDS),
    ),
    SECTION_FEEDBACK: SectionSpec(
        key=SECTION_FEEDBACK,
        option_key=CONF_FEEDBACK_ENABLED,
        scopes=(SCOPE_FEEDBACK,),
        interval_minutes=SECTION_FEEDBACK_INTERVAL_MINUTES,
        retry_minutes=SECTION_FEEDBACK_RETRY_MINUTES,
        marketplace_capability="feedback",
        fetcher_name="async_fetch_feedback",
        partial_failure_categories=_partial_categories(SECTION_FEEDBACK),
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
        partial_failure_categories=_partial_categories(SECTION_MESSAGES),
    ),
    SECTION_ACCOUNT_PRIVILEGES: SectionSpec(
        key=SECTION_ACCOUNT_PRIVILEGES,
        option_key=CONF_ACCOUNT_PRIVILEGES_ENABLED,
        scopes=(SCOPE_ACCOUNT_READONLY,),
        interval_minutes=SECTION_ACCOUNT_PRIVILEGES_INTERVAL_MINUTES,
        retry_minutes=SECTION_ACCOUNT_PRIVILEGES_RETRY_MINUTES,
        marketplace_capability="account_privileges",
        fetcher_name="async_fetch_account_privileges",
        partial_failure_categories=_partial_categories(SECTION_ACCOUNT_PRIVILEGES),
    ),
    SECTION_FINANCES: SectionSpec(
        key=SECTION_FINANCES,
        option_key=CONF_FINANCES_ENABLED,
        scopes=(SCOPE_FINANCES,),
        interval_minutes=SECTION_FINANCES_INTERVAL_MINUTES,
        retry_minutes=SECTION_FINANCES_RETRY_MINUTES,
        marketplace_capability="finances",
        fetcher_name="async_fetch_finances",
        partial_failure_categories=_partial_categories(SECTION_FINANCES),
    ),
}
