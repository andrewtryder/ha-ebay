"""Constants for the eBay integration."""

from __future__ import annotations

from datetime import timedelta

DOMAIN = "ebay"

CONF_CLIENT_ID = "client_id"
CONF_CLIENT_SECRET = "client_secret"
CONF_ENVIRONMENT = "environment"
CONF_OAUTH_MODE = "oauth_mode"
CONF_REFRESH_TOKEN = "refresh_token"
CONF_RUNAME = "runame"
CONF_SITE_ID = "site_id"

CONF_ANALYTICS_ENABLED = "analytics_enabled"
CONF_BUYING_ENABLED = "buying_enabled"
CONF_ENDING_SOON_THRESHOLD = "ending_soon_threshold"
CONF_ENTITY_MODE = "entity_mode"
CONF_PER_ITEM_CAP = "per_item_cap"
CONF_PINNED_ITEM_IDS = "pinned_item_ids"
CONF_POLL_INTERVAL = "poll_interval"
CONF_SELLING_ENABLED = "selling_enabled"

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
DEFAULT_SITE_ID = "0"

DEFAULT_OPTIONS = {
    CONF_POLL_INTERVAL: DEFAULT_POLL_INTERVAL,
    CONF_ENDING_SOON_THRESHOLD: DEFAULT_ENDING_SOON_THRESHOLD,
    CONF_ENTITY_MODE: DEFAULT_ENTITY_MODE,
    CONF_PER_ITEM_CAP: DEFAULT_PER_ITEM_CAP,
    CONF_PINNED_ITEM_IDS: "",
    CONF_ANALYTICS_ENABLED: True,
    CONF_BUYING_ENABLED: True,
    CONF_SELLING_ENABLED: True,
}

PLATFORMS = ["sensor", "event"]

SCAN_INTERVAL = timedelta(minutes=DEFAULT_POLL_INTERVAL)

EBAY_XML_NS = "urn:ebay:apis:eBLBaseComponents"
NS = {"e": EBAY_XML_NS}
COMPATIBILITY_LEVEL = "1209"

SELLING_EVENT_TYPES = [
    "bid_count_increased",
    "offer_received",
    "question_received",
    "watcher_count_increased",
    "quantity_sold_increased",
    "item_disappeared_unknown",
]

WATCHING_EVENT_TYPES = [
    "watched_item_ending_soon",
    "watched_item_price_changed",
    "watched_item_bid_count_changed",
    "watched_item_ended",
]

BIDDING_EVENT_TYPES = [
    "outbid",
    "winning",
    "bidding_item_ending_soon",
    "bid_item_ended",
]
