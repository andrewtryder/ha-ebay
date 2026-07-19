"""Diagnostics for the eBay integration."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import (
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
    CONF_SITE_ID,
    CONF_WATCHED_PRICE_CHANGE_MIN_PERCENT,
    CONF_WATCHED_PRICE_DROP_CURRENCY,
    CONF_WATCHED_PRICE_DROP_THRESHOLD,
    DOMAIN,
)
from .coordinator import EbayDataUpdateCoordinator
from .models import ApiFailure
from .options_parse import pinned_ids
from .sections import SECTION_SPECS

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
            key: options.get(spec.option_key) if spec.option_key else None
            for key, spec in SECTION_SPECS.items()
        },
        "poll_interval": options.get(CONF_POLL_INTERVAL),
        "section_last_fetched_ages_minutes": _section_ages_minutes(data),
        "ending_soon_threshold": options.get(CONF_ENDING_SOON_THRESHOLD),
        "entity_mode": options.get(CONF_ENTITY_MODE),
        "per_item_cap": options.get(CONF_PER_ITEM_CAP),
        "pinned_item_ids_count": len(
            pinned_ids(str(options.get(CONF_PINNED_ITEM_IDS, "")))
        ),
        "pinned_item_price_targets_count": len(
            [
                line
                for line in str(
                    options.get(CONF_PINNED_ITEM_PRICE_TARGETS, "")
                ).splitlines()
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
            "orders": len(
                ((data.get("seller_ops") or {}).get("orders") or {}).get("by_id") or {}
            ),
            "open_payment_disputes": (
                ((data.get("seller_ops") or {}).get("disputes") or {}).get("open_count")
                or 0
            ),
            "unread_conversations": (
                ((data.get("seller_ops") or {}).get("messages") or {}).get(
                    "unread_count"
                )
                or 0
            ),
        },
        "seller_ops_summary": _safe_seller_ops_summary(data.get("seller_ops") or {}),
        "last_successful_update": summary.get("last_successful_update"),
        "last_attempt_at": coordinator.last_attempt_at.isoformat()
        if coordinator.last_attempt_at
        else None,
        "last_refresh_duration_seconds": coordinator.last_refresh_duration_seconds,
        "last_refresh_result": coordinator.last_refresh_result,
        "last_error_category": coordinator.last_error_category,
        "partial_failure_categories": data.get("partial_failures", []),
        "partial_failure_details": data.get("partial_failure_details", []),
        "last_api_failure": _last_api_failure_diagnostics(data),
        "missing_scope_features": data.get("missing_scope_features")
        or summary.get("missing_scope_features", []),
        "missing_scopes": data.get("missing_scopes")
        or summary.get("missing_scopes", {}),
        "truncated_collections": data.get("truncated_collections", {}),
        "api_warnings": data.get("api_warnings", []),
        "scheduled_ending_soon_timer_count": coordinator.scheduled_ending_soon_count,
        "redacted": sorted(TO_REDACT),
    }


def _last_api_failure_diagnostics(data: dict[str, Any]) -> dict[str, Any] | None:
    """Return the diagnostics shape for the last structured API failure."""
    failure = ApiFailure.from_dict(data.get("last_api_failure"))
    if failure is None:
        return None
    return failure.to_diagnostics()


def _section_ages_minutes(data: dict[str, Any]) -> dict[str, float | None]:
    """Return minutes since each section was last fetched."""
    now = datetime.now(timezone.utc)
    ages: dict[str, float | None] = {}
    for section, value in (data.get("section_last_fetched") or {}).items():
        if isinstance(value, datetime):
            fetched = value
        elif isinstance(value, str):
            try:
                fetched = datetime.fromisoformat(value)
            except ValueError:
                ages[section] = None
                continue
        else:
            ages[section] = None
            continue
        ages[section] = round(max(0.0, (now - fetched).total_seconds() / 60.0), 1)
    return ages


def _safe_seller_ops_summary(seller_ops: dict[str, Any]) -> dict[str, Any]:
    """Return seller-ops diagnostics without usernames, comments, or message bodies."""
    orders = seller_ops.get("orders") or {}
    disputes = seller_ops.get("disputes") or {}
    standards = seller_ops.get("standards") or {}
    feedback = seller_ops.get("feedback") or {}
    messages = seller_ops.get("messages") or {}
    privileges = seller_ops.get("account_privileges") or {}
    finances = seller_ops.get("finances") or {}
    return {
        "orders": {
            "awaiting_shipment": orders.get("awaiting_shipment"),
            "shipping_today": orders.get("shipping_today"),
            "overdue": orders.get("overdue"),
            "shipped": orders.get("shipped"),
            "partially_fulfilled": orders.get("partially_fulfilled"),
            "order_count": len(orders.get("by_id") or {}),
        },
        "disputes": {"open_count": disputes.get("open_count")},
        "standards": {
            "seller_level": standards.get("seller_level"),
            "at_risk": standards.get("at_risk"),
            "next_evaluation_date": standards.get("next_evaluation_date"),
            "item_not_received_above_benchmark": standards.get(
                "item_not_received_above_benchmark"
            ),
            "item_not_as_described_above_benchmark": standards.get(
                "item_not_as_described_above_benchmark"
            ),
        },
        "feedback": {
            "score": feedback.get("score"),
            "positive_percent": feedback.get("positive_percent"),
            "recent_positive": feedback.get("recent_positive"),
            "recent_neutral": feedback.get("recent_neutral"),
            "recent_negative": feedback.get("recent_negative"),
            "awaiting_count": feedback.get("awaiting_count"),
        },
        "messages": {
            "unread_count": messages.get("unread_count"),
            "buyer_question_count": messages.get("buyer_question_count"),
            "oldest_unanswered_hours": messages.get("oldest_unanswered_hours"),
            "conversation_count": len(messages.get("by_id") or {}),
        },
        "account_privileges": {
            "seller_registration_completed": privileges.get(
                "seller_registration_completed"
            ),
            "amount_remaining": privileges.get("amount_remaining"),
            "amount_used": privileges.get("amount_used"),
            "amount_currency": privileges.get("amount_currency"),
            "quantity_remaining": privileges.get("quantity_remaining"),
            "quantity_used": privileges.get("quantity_used"),
            "near_limit": privileges.get("near_limit"),
        },
        "finances": {
            "available_funds": finances.get("available_funds"),
            "pending_funds": finances.get("pending_funds"),
            "last_payout_amount": finances.get("last_payout_amount"),
            "last_payout_status": finances.get("last_payout_status"),
        },
    }


def _manifest_version() -> str | None:
    """Return the integration version from manifest.json."""
    manifest_path = Path(__file__).with_name("manifest.json")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return manifest.get("version")
