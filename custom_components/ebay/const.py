"""Constants for the eBay integration."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

DOMAIN = "ebay"

CONF_CLIENT_ID = "client_id"
CONF_CLIENT_SECRET = "client_secret"
CONF_ENVIRONMENT = "environment"
CONF_OAUTH_MODE = "oauth_mode"
CONF_REFRESH_TOKEN = "refresh_token"
CONF_RUNAME = "runame"
CONF_SITE_ID = "site_id"

CONF_ACCOUNT_PRIVILEGES_ENABLED = "account_privileges_enabled"
CONF_ANALYTICS_ENABLED = "analytics_enabled"
CONF_BUYING_ENABLED = "buying_enabled"
CONF_ENDING_SOON_THRESHOLD = "ending_soon_threshold"
CONF_ENTITY_MODE = "entity_mode"
CONF_FEEDBACK_ENABLED = "feedback_enabled"
CONF_FINANCES_ENABLED = "finances_enabled"
CONF_FULFILLMENT_ENABLED = "fulfillment_enabled"
CONF_MESSAGES_ENABLED = "messages_enabled"
CONF_OAUTH_SCOPES = "oauth_scopes"
CONF_PENDING_OPTIONS = "pending_options"
CONF_ITEM_ENTITY_GRACE_DAYS = "item_entity_grace_days"
CONF_PER_ITEM_CAP = "per_item_cap"
CONF_PINNED_ITEM_IDS = "pinned_item_ids"
CONF_PINNED_ITEM_PRICE_TARGETS = "pinned_item_price_targets"
CONF_POLL_INTERVAL = "poll_interval"
CONF_SELLING_ENABLED = "selling_enabled"
CONF_SELLER_STANDARDS_ENABLED = "seller_standards_enabled"
CONF_WATCHED_PRICE_CHANGE_MIN_PERCENT = "watched_price_change_min_percent"
CONF_WATCHED_PRICE_DROP_CURRENCY = "watched_price_drop_currency"
CONF_WATCHED_PRICE_DROP_THRESHOLD = "watched_price_drop_threshold"

ENV_PRODUCTION = "production"
ENV_SANDBOX = "sandbox"
ENVIRONMENTS = [ENV_PRODUCTION, ENV_SANDBOX]

OAUTH_MODE_MANUAL = "manual"
OAUTH_MODE_CALLBACK = "callback"
OAUTH_MODES = [OAUTH_MODE_MANUAL, OAUTH_MODE_CALLBACK]

ENTITY_MODE_MINIMAL = "minimal"
ENTITY_MODE_BALANCED = "balanced"
ENTITY_MODE_DETAILED = "detailed"
ENTITY_MODES = [ENTITY_MODE_MINIMAL, ENTITY_MODE_BALANCED, ENTITY_MODE_DETAILED]

DEFAULT_POLL_INTERVAL = 30
DEFAULT_ENDING_SOON_THRESHOLD = 60
DEFAULT_ENTITY_MODE = ENTITY_MODE_BALANCED
DEFAULT_PER_ITEM_CAP = 25
ALLOWED_PER_ITEM_CAPS = [25, 50, 100, 250]
DEFAULT_ITEM_ENTITY_GRACE_DAYS = 7
DEFAULT_SITE_ID = "0"
DEFAULT_WATCHED_PRICE_CHANGE_MIN_PERCENT = 0
RECENT_EVENTS_MAX = 20
TITLE_HISTORY_MAX_LEN = 120
CALENDAR_MARKER_MINUTES = 5
MANUAL_REFRESH_COOLDOWN_SECONDS = 60

DEFAULT_OPTIONS = {
    CONF_POLL_INTERVAL: DEFAULT_POLL_INTERVAL,
    CONF_ENDING_SOON_THRESHOLD: DEFAULT_ENDING_SOON_THRESHOLD,
    CONF_ENTITY_MODE: DEFAULT_ENTITY_MODE,
    CONF_PER_ITEM_CAP: DEFAULT_PER_ITEM_CAP,
    CONF_ITEM_ENTITY_GRACE_DAYS: DEFAULT_ITEM_ENTITY_GRACE_DAYS,
    CONF_PINNED_ITEM_IDS: [],
    CONF_PINNED_ITEM_PRICE_TARGETS: [],
    CONF_WATCHED_PRICE_CHANGE_MIN_PERCENT: DEFAULT_WATCHED_PRICE_CHANGE_MIN_PERCENT,
    CONF_WATCHED_PRICE_DROP_THRESHOLD: "",
    CONF_WATCHED_PRICE_DROP_CURRENCY: "",
    CONF_ANALYTICS_ENABLED: False,
    CONF_BUYING_ENABLED: True,
    CONF_SELLING_ENABLED: True,
    CONF_FULFILLMENT_ENABLED: False,
    CONF_SELLER_STANDARDS_ENABLED: False,
    CONF_FEEDBACK_ENABLED: False,
    CONF_MESSAGES_ENABLED: False,
    CONF_ACCOUNT_PRIVILEGES_ENABLED: False,
    CONF_FINANCES_ENABLED: False,
}

PLATFORMS = ["sensor", "binary_sensor", "event", "calendar", "button"]

SCAN_INTERVAL = timedelta(minutes=DEFAULT_POLL_INTERVAL)

EBAY_XML_NS = "urn:ebay:apis:eBLBaseComponents"
NS = {"e": EBAY_XML_NS}
COMPATIBILITY_LEVEL = "1209"

SELLING_EVENT_TYPES = [
    "selling_item_added",
    "bid_count_increased",
    "offer_received",
    "question_received",
    "watcher_count_increased",
    "quantity_sold_increased",
    "item_sold",
    "item_ended_unsold",
    "item_sale_completed",
    "item_disappeared_unknown",
]

SELLER_OPS_EVENT_TYPES = [
    "order_received",
    "order_paid",
    "shipment_due_soon",
    "shipment_overdue",
    "order_marked_shipped",
    "tracking_added",
    "payment_dispute_opened",
    "seller_level_changed",
    "seller_standard_at_risk",
    "service_metric_above_peer_benchmark",
    "feedback_received",
    "negative_feedback_received",
    "feedback_rating_changed",
    "new_buyer_question",
    "new_message_received",
]

WATCHING_EVENT_TYPES = [
    "watched_item_added",
    "watched_item_ending_soon",
    "watched_item_price_changed",
    "watched_item_price_increased",
    "watched_item_price_decreased",
    "watched_item_price_dropped_below",
    "watched_item_bid_count_changed",
    "watched_item_bid_count_increased",
    "watched_item_ended",
    "watched_item_disappeared_unknown",
]

BIDDING_EVENT_TYPES = [
    "bid_item_added",
    "outbid",
    "winning",
    "bidding_item_ending_soon",
    "bid_item_ended",
    "bid_item_disappeared_unknown",
]

# Legacy compatibility aliases are emitted for automations but are not
# recorded in recent-activity history.
LEGACY_COMPATIBILITY_EVENT_TYPES = frozenset(
    {
        "watched_item_price_changed",
        "watched_item_bid_count_changed",
    }
)

REFRESH_RESULT_UNKNOWN = "unknown"
REFRESH_RESULT_SUCCESS = "success"
REFRESH_RESULT_PARTIAL = "partial"
REFRESH_RESULT_ERROR = "error"
REFRESH_RESULT_AUTH_ERROR = "auth_error"

# Coordinator payload sections with independent refresh cadences.
SECTION_BUYING = "buying"
SECTION_SELLING = "selling"
SECTION_FULFILLMENT = "fulfillment"
SECTION_MESSAGES = "messages"
SECTION_FEEDBACK = "feedback"
SECTION_ANALYTICS = "analytics"
SECTION_SELLER_STANDARDS = "seller_standards"
SECTION_ACCOUNT_PRIVILEGES = "account_privileges"
SECTION_FINANCES = "finances"

SECTION_FEEDBACK_INTERVAL_MINUTES = 60
SECTION_ANALYTICS_INTERVAL_MINUTES = 360
SECTION_SELLER_STANDARDS_INTERVAL_MINUTES = 1440
SECTION_ACCOUNT_PRIVILEGES_INTERVAL_MINUTES = 360
SECTION_FINANCES_INTERVAL_MINUTES = 60

# Soft-fail retry cadence (minutes). Missing-scope failures do not use these.
SECTION_FEEDBACK_RETRY_MINUTES = 60
SECTION_SELLER_STANDARDS_RETRY_MINUTES = 60
SECTION_ANALYTICS_RETRY_MINUTES = 60
SECTION_ACCOUNT_PRIVILEGES_RETRY_MINUTES = 60
SECTION_FINANCES_RETRY_MINUTES = 60

# Soft-fail / truncation conditions must be seen this many consecutive section
# attempts before a Repairs issue is created.
REPAIR_STREAK_THRESHOLD = 3


def __getattr__(name: str) -> Any:
    """Lazy re-exports owned by the SectionSpec registry."""
    if name in {
        "ALL_SECTIONS",
        "PARTIAL_FAILURE_SECTION",
        "TRUNCATION_COLLECTION_SECTION",
        "MISSING_SCOPE_PARTIAL_FAILURES",
    }:
        from . import sections

        return getattr(sections, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def section_interval_minutes(section: str, poll_interval: int) -> int:
    """Return the refresh interval in minutes for a payload section."""
    # Lazy import avoids a circular dependency (sections → const).
    from .sections import MESSAGES_POLL_MULTIPLIER, SECTION_SPECS

    poll = max(poll_interval, 1)
    if section == SECTION_MESSAGES:
        return poll * MESSAGES_POLL_MULTIPLIER
    spec = SECTION_SPECS.get(section)
    if spec is not None and spec.interval_minutes is not None:
        return spec.interval_minutes
    return poll


def section_retry_minutes(section: str, poll_interval: int) -> int:
    """Return the soft-fail retry delay in minutes for a payload section."""
    from .sections import MESSAGES_POLL_MULTIPLIER, SECTION_SPECS

    poll = max(poll_interval, 1)
    if section == SECTION_MESSAGES:
        return poll * MESSAGES_POLL_MULTIPLIER
    spec = SECTION_SPECS.get(section)
    if spec is not None and spec.retry_minutes is not None:
        return spec.retry_minutes
    return poll
