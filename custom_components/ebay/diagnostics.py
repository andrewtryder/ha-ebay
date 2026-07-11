"""Diagnostics for the eBay integration."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import (
    CONF_ANALYTICS_ENABLED,
    CONF_BUYING_ENABLED,
    CONF_CLIENT_ID,
    CONF_CLIENT_SECRET,
    CONF_ENDING_SOON_THRESHOLD,
    CONF_ENTITY_MODE,
    CONF_ENVIRONMENT,
    CONF_PER_ITEM_CAP,
    CONF_PINNED_ITEM_IDS,
    CONF_PINNED_ITEM_PRICE_TARGETS,
    CONF_POLL_INTERVAL,
    CONF_REFRESH_TOKEN,
    CONF_RUNAME,
    CONF_SELLING_ENABLED,
    CONF_SITE_ID,
    CONF_WATCHED_PRICE_CHANGE_MIN_PERCENT,
    CONF_WATCHED_PRICE_DROP_CURRENCY,
    CONF_WATCHED_PRICE_DROP_THRESHOLD,
    DOMAIN,
)
from .coordinator import EbayDataUpdateCoordinator
from .options_parse import pinned_ids

TO_REDACT = {
    CONF_CLIENT_ID,
    CONF_CLIENT_SECRET,
    CONF_REFRESH_TOKEN,
    CONF_RUNAME,
    "authorization",
    "authorization_code",
    "code",
    "access_token",
    "refresh_token",
    "token",
}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return redacted diagnostics."""
    coordinator: EbayDataUpdateCoordinator = entry.runtime_data
    data = coordinator.data or {}
    summary = data.get("summary", {})
    options = coordinator.options
    return {
        "integration_version": _manifest_version(),
        "domain": DOMAIN,
        "environment": entry.data.get(CONF_ENVIRONMENT),
        "site_id": entry.data.get(CONF_SITE_ID),
        "enabled_features": {
            "buying": options.get(CONF_BUYING_ENABLED),
            "selling": options.get(CONF_SELLING_ENABLED),
            "analytics": options.get(CONF_ANALYTICS_ENABLED),
        },
        "poll_interval": options.get(CONF_POLL_INTERVAL),
        "ending_soon_threshold": options.get(CONF_ENDING_SOON_THRESHOLD),
        "entity_mode": options.get(CONF_ENTITY_MODE),
        "per_item_cap": options.get(CONF_PER_ITEM_CAP),
        "pinned_item_ids_count": len(
            pinned_ids(str(options.get(CONF_PINNED_ITEM_IDS, "")))
        ),
        "pinned_item_price_targets_count": len(
            [
                line
                for line in str(options.get(CONF_PINNED_ITEM_PRICE_TARGETS, "")).splitlines()
                if line.strip()
            ]
        ),
        "watched_price_change_min_percent": options.get(
            CONF_WATCHED_PRICE_CHANGE_MIN_PERCENT
        ),
        "watched_price_drop_configured": bool(
            str(options.get(CONF_WATCHED_PRICE_DROP_THRESHOLD, "")).strip()
            and str(options.get(CONF_WATCHED_PRICE_DROP_CURRENCY, "")).strip()
        ),
        "counts": {
            "watched": len(data.get("watched", {})),
            "bidding": len(data.get("bidding", {})),
            "selling": len(data.get("selling", {})),
        },
        "last_successful_update": summary.get("last_successful_update"),
        "last_attempt_at": coordinator.last_attempt_at.isoformat()
        if coordinator.last_attempt_at
        else None,
        "last_refresh_duration_seconds": coordinator.last_refresh_duration_seconds,
        "last_refresh_result": coordinator.last_refresh_result,
        "last_error_category": coordinator.last_error_category,
        "partial_failure_categories": data.get("partial_failures", []),
        "truncated_collections": data.get("truncated_collections", {}),
        "api_warnings": data.get("api_warnings", []),
        "scheduled_ending_soon_timer_count": coordinator.scheduled_ending_soon_count,
        "redacted": sorted(TO_REDACT),
    }


def _manifest_version() -> str | None:
    """Return the integration version from manifest.json."""
    manifest_path = Path(__file__).with_name("manifest.json")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return manifest.get("version")
