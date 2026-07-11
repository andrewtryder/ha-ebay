"""Diagnostics redaction and payload tests."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import Mock

from custom_components.ebay.const import (
    CONF_CLIENT_ID,
    CONF_CLIENT_SECRET,
    CONF_ENVIRONMENT,
    CONF_PINNED_ITEM_IDS,
    CONF_REFRESH_TOKEN,
    CONF_RUNAME,
    CONF_SITE_ID,
    DEFAULT_OPTIONS,
)
from custom_components.ebay.diagnostics import TO_REDACT, async_get_config_entry_diagnostics


def _coordinator(*, data: dict[str, Any] | None = None, error: str | None = None) -> Mock:
    coordinator = Mock()
    coordinator.data = data
    coordinator.options = {
        **DEFAULT_OPTIONS,
        CONF_PINNED_ITEM_IDS: "1,2",
    }
    coordinator.last_error_category = error
    return coordinator


def test_diagnostics_without_coordinator_data() -> None:
    entry = Mock(
        data={
            CONF_ENVIRONMENT: "production",
            CONF_SITE_ID: "0",
            CONF_CLIENT_ID: "secret-client",
            CONF_CLIENT_SECRET: "secret-cert",
            CONF_REFRESH_TOKEN: "secret-refresh",
            CONF_RUNAME: "secret-runame",
            "token": {"access_token": "secret-access", "refresh_token": "secret-refresh"},
        },
        runtime_data=_coordinator(data=None),
    )
    result = asyncio.run(async_get_config_entry_diagnostics(Mock(), entry))
    assert result["counts"] == {"watched": 0, "bidding": 0, "selling": 0}
    assert result["partial_failure_categories"] == []
    assert result["truncated_collections"] == {}
    assert result["api_warnings"] == []
    assert result["last_error_category"] is None
    blob = str(result)
    assert "secret-client" not in blob
    assert "secret-cert" not in blob
    assert "secret-refresh" not in blob
    assert "secret-access" not in blob
    assert set(TO_REDACT).issubset(set(result["redacted"]))


def test_diagnostics_with_coordinator_data() -> None:
    entry = Mock(
        data={
            CONF_ENVIRONMENT: "sandbox",
            CONF_SITE_ID: "0",
        },
        runtime_data=_coordinator(
            data={
                "watched": {"w1": {}},
                "bidding": {"b1": {}, "b2": {}},
                "selling": {"s1": {}},
                "summary": {"last_successful_update": "2026-01-01T00:00:00+00:00"},
                "partial_failures": ["analytics_views"],
                "truncated_collections": {"selling": True},
                "api_warnings": [{"code": "1", "short_message": "warn"}],
            },
            error=None,
        ),
    )
    result = asyncio.run(async_get_config_entry_diagnostics(Mock(), entry))
    assert result["counts"] == {"watched": 1, "bidding": 2, "selling": 1}
    assert result["partial_failure_categories"] == ["analytics_views"]
    assert result["truncated_collections"] == {"selling": True}
    assert result["api_warnings"] == [{"code": "1", "short_message": "warn"}]
    assert result["enabled_features"]["analytics"] is True
    assert result["pinned_item_ids_count"] == 2
