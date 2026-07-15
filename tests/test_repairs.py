"""Tests for Home Assistant Repairs issue sync."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest

from custom_components.ebay.const import (
    CONF_ANALYTICS_ENABLED,
    CONF_FEEDBACK_ENABLED,
    CONF_MESSAGES_ENABLED,
    CONF_SELLING_ENABLED,
    CONF_SITE_ID,
    DEFAULT_OPTIONS,
    DOMAIN,
    REPAIR_STREAK_THRESHOLD,
)
from custom_components.ebay.repairs import (
    async_sync_reauth_issue,
    async_sync_repair_issues,
    issue_id_for,
)


class _FakeIssue:
    def __init__(self, domain: str, issue_id: str) -> None:
        self.domain = domain
        self.issue_id = issue_id


class _FakeIssueRegistry:
    def __init__(self) -> None:
        self.issues: dict[tuple[str, str], _FakeIssue] = {}
        self.created: list[dict[str, Any]] = []
        self.deleted: list[tuple[str, str]] = []

    def create(self, hass: Any, domain: str, issue_id: str, **kwargs: Any) -> None:
        self.issues[(domain, issue_id)] = _FakeIssue(domain, issue_id)
        self.created.append({"domain": domain, "issue_id": issue_id, **kwargs})

    def delete(self, hass: Any, domain: str, issue_id: str) -> None:
        self.issues.pop((domain, issue_id), None)
        self.deleted.append((domain, issue_id))


@pytest.fixture
def issue_registry(monkeypatch: pytest.MonkeyPatch) -> _FakeIssueRegistry:
    registry = _FakeIssueRegistry()
    monkeypatch.setattr(
        "custom_components.ebay.repairs.ir.async_get",
        lambda hass: registry,
    )
    monkeypatch.setattr(
        "custom_components.ebay.repairs.ir.async_create_issue",
        registry.create,
    )
    monkeypatch.setattr(
        "custom_components.ebay.repairs.ir.async_delete_issue",
        registry.delete,
    )
    monkeypatch.setattr(
        "custom_components.ebay.repairs.async_save_repair_streaks",
        AsyncMock(),
    )
    return registry


def _entry(*, site_id: str = "0", entry_id: str = "entry-1") -> Mock:
    entry = Mock()
    entry.entry_id = entry_id
    entry.data = {CONF_SITE_ID: site_id}
    return entry


def _coordinator() -> Mock:
    coordinator = Mock()
    coordinator._repair_streaks = {}
    coordinator._repair_streaks_dirty = False
    return coordinator


def test_missing_scope_creates_issue_immediately(
    issue_registry: _FakeIssueRegistry,
) -> None:
    entry = _entry()
    coordinator = _coordinator()
    payload = {
        "missing_scope_features": [CONF_FEEDBACK_ENABLED],
        "missing_scopes": {CONF_FEEDBACK_ENABLED: ["https://scope/feedback"]},
        "partial_failures": ["feedback_missing_scope"],
        "truncated_collections": {},
    }

    asyncio.run(async_sync_repair_issues(Mock(), entry, coordinator, payload))

    issue_id = issue_id_for(entry.entry_id, f"missing_scope_{CONF_FEEDBACK_ENABLED}")
    assert (DOMAIN, issue_id) in issue_registry.issues


def test_partial_failure_requires_streak(
    issue_registry: _FakeIssueRegistry,
) -> None:
    entry = _entry()
    coordinator = _coordinator()
    payload = {
        "missing_scope_features": [],
        "missing_scopes": {},
        "partial_failures": ["analytics_views"],
        "truncated_collections": {},
    }

    for _ in range(REPAIR_STREAK_THRESHOLD - 1):
        asyncio.run(async_sync_repair_issues(Mock(), entry, coordinator, payload))
    issue_id = issue_id_for(entry.entry_id, "partial_failure_analytics_views")
    assert (DOMAIN, issue_id) not in issue_registry.issues

    asyncio.run(async_sync_repair_issues(Mock(), entry, coordinator, payload))
    assert (DOMAIN, issue_id) in issue_registry.issues
    created = next(
        item for item in issue_registry.created if item["issue_id"] == issue_id
    )
    assert created["is_persistent"] is True

    cleared = {
        "missing_scope_features": [],
        "missing_scopes": {},
        "partial_failures": [],
        "truncated_collections": {},
    }
    asyncio.run(async_sync_repair_issues(Mock(), entry, coordinator, cleared))
    assert (DOMAIN, issue_id) not in issue_registry.issues


def test_truncation_requires_streak(issue_registry: _FakeIssueRegistry) -> None:
    entry = _entry()
    coordinator = _coordinator()
    payload = {
        "missing_scope_features": [],
        "missing_scopes": {},
        "partial_failures": ["buying_truncated"],
        "truncated_collections": {"watched": True, "bidding": False},
    }

    for _ in range(REPAIR_STREAK_THRESHOLD - 1):
        asyncio.run(async_sync_repair_issues(Mock(), entry, coordinator, payload))
    issue_id = issue_id_for(entry.entry_id, "truncated_watched")
    assert (DOMAIN, issue_id) not in issue_registry.issues

    asyncio.run(async_sync_repair_issues(Mock(), entry, coordinator, payload))
    assert (DOMAIN, issue_id) in issue_registry.issues
    created = next(
        item for item in issue_registry.created if item["issue_id"] == issue_id
    )
    assert created["is_persistent"] is True


def test_invalid_site_id_creates_fixable_issue(
    issue_registry: _FakeIssueRegistry,
) -> None:
    entry = _entry(site_id="999")
    coordinator = _coordinator()
    payload = {
        "missing_scope_features": [],
        "missing_scopes": {},
        "partial_failures": [],
        "truncated_collections": {},
    }

    asyncio.run(async_sync_repair_issues(Mock(), entry, coordinator, payload))

    issue_id = issue_id_for(entry.entry_id, "invalid_site_id")
    assert (DOMAIN, issue_id) in issue_registry.issues
    created = next(
        item for item in issue_registry.created if item["issue_id"] == issue_id
    )
    assert created["is_fixable"] is True


def test_reauth_issue_create_and_clear(issue_registry: _FakeIssueRegistry) -> None:
    entry = _entry()
    asyncio.run(async_sync_reauth_issue(Mock(), entry, active=True))
    issue_id = issue_id_for(entry.entry_id, "reauthorization_required")
    assert (DOMAIN, issue_id) in issue_registry.issues

    asyncio.run(async_sync_reauth_issue(Mock(), entry, active=False))
    assert (DOMAIN, issue_id) not in issue_registry.issues


def test_seller_ops_unavailable_after_streak(
    issue_registry: _FakeIssueRegistry,
) -> None:
    entry = _entry()
    coordinator = _coordinator()
    payload = {
        "missing_scope_features": [],
        "missing_scopes": {},
        "partial_failures": ["seller_standards"],
        "truncated_collections": {},
    }

    for _ in range(REPAIR_STREAK_THRESHOLD):
        asyncio.run(
            async_sync_repair_issues(
                Mock(),
                entry,
                coordinator,
                payload,
                refreshed_sections={"seller_standards"},
            )
        )

    issue_id = issue_id_for(entry.entry_id, "seller_ops_unavailable_seller_standards")
    assert (DOMAIN, issue_id) in issue_registry.issues
    created = next(
        item for item in issue_registry.created if item["issue_id"] == issue_id
    )
    assert created["is_persistent"] is True
    # Should not also create a generic partial_failure issue for the same category.
    assert (
        DOMAIN,
        issue_id_for(entry.entry_id, "partial_failure_seller_standards"),
    ) not in issue_registry.issues


def test_unrelated_section_refresh_does_not_advance_streak(
    issue_registry: _FakeIssueRegistry,
) -> None:
    entry = _entry()
    coordinator = _coordinator()
    payload = {
        "missing_scope_features": [],
        "missing_scopes": {},
        "partial_failures": ["seller_standards"],
        "truncated_collections": {},
    }

    for _ in range(REPAIR_STREAK_THRESHOLD):
        asyncio.run(
            async_sync_repair_issues(
                Mock(),
                entry,
                coordinator,
                payload,
                refreshed_sections={"buying"},
            )
        )

    assert (
        coordinator._repair_streaks.get("seller_ops_unavailable:seller_standards", 0)
        == 0
    )
    issue_id = issue_id_for(entry.entry_id, "seller_ops_unavailable_seller_standards")
    assert (DOMAIN, issue_id) not in issue_registry.issues


@pytest.mark.parametrize(
    (
        "option_key",
        "enabled_options",
        "disabled_options",
        "payload",
        "refreshed",
        "streak_key",
        "issue_suffix",
    ),
    [
        (
            "feedback",
            {
                CONF_FEEDBACK_ENABLED: True,
            },
            {
                CONF_FEEDBACK_ENABLED: False,
            },
            {
                "missing_scope_features": [],
                "missing_scopes": {},
                "partial_failures": ["feedback"],
                "truncated_collections": {},
            },
            {"feedback"},
            "seller_ops_unavailable:feedback",
            "seller_ops_unavailable_feedback",
        ),
        (
            "messages",
            {
                CONF_MESSAGES_ENABLED: True,
            },
            {
                CONF_MESSAGES_ENABLED: False,
            },
            {
                "missing_scope_features": [],
                "missing_scopes": {},
                "partial_failures": ["messages_truncated"],
                "truncated_collections": {"messages": True},
            },
            {"messages"},
            "truncated:messages",
            "truncated_messages",
        ),
        (
            "analytics",
            {
                CONF_SELLING_ENABLED: True,
                CONF_ANALYTICS_ENABLED: True,
            },
            {
                CONF_SELLING_ENABLED: True,
                CONF_ANALYTICS_ENABLED: False,
            },
            {
                "missing_scope_features": [],
                "missing_scopes": {},
                "partial_failures": ["analytics_views"],
                "truncated_collections": {},
            },
            {"analytics"},
            "partial_failure:analytics_views",
            "partial_failure_analytics_views",
        ),
    ],
)
def test_disabling_optional_module_clears_repair_and_streak(
    issue_registry: _FakeIssueRegistry,
    option_key: str,
    enabled_options: dict[str, bool],
    disabled_options: dict[str, bool],
    payload: dict[str, Any],
    refreshed: set[str],
    streak_key: str,
    issue_suffix: str,
) -> None:
    """Disabled optional sections drop persisted repairs instead of preserving them."""
    entry = _entry()
    coordinator = _coordinator()
    coordinator.options = {**DEFAULT_OPTIONS, **enabled_options}

    for _ in range(REPAIR_STREAK_THRESHOLD):
        asyncio.run(
            async_sync_repair_issues(
                Mock(),
                entry,
                coordinator,
                payload,
                refreshed_sections=refreshed,
            )
        )

    issue_id = issue_id_for(entry.entry_id, issue_suffix)
    assert (DOMAIN, issue_id) in issue_registry.issues
    assert coordinator._repair_streaks.get(streak_key, 0) >= REPAIR_STREAK_THRESHOLD

    # Feature disabled and section not refreshed — historically this preserved the issue.
    coordinator.options = {**DEFAULT_OPTIONS, **disabled_options}
    stale_payload = {
        "missing_scope_features": [],
        "missing_scopes": {},
        "partial_failures": list(payload["partial_failures"]),
        "truncated_collections": dict(payload["truncated_collections"]),
    }
    asyncio.run(
        async_sync_repair_issues(
            Mock(),
            entry,
            coordinator,
            stale_payload,
            refreshed_sections={"buying"},
        )
    )

    assert (DOMAIN, issue_id) not in issue_registry.issues
    assert streak_key not in coordinator._repair_streaks


def test_enabled_unobserved_section_still_preserves_repair(
    issue_registry: _FakeIssueRegistry,
) -> None:
    """Unobserved enabled sections keep streak-gated issues until rechecked."""
    entry = _entry()
    coordinator = _coordinator()
    coordinator.options = {**DEFAULT_OPTIONS, CONF_FEEDBACK_ENABLED: True}
    payload = {
        "missing_scope_features": [],
        "missing_scopes": {},
        "partial_failures": ["feedback"],
        "truncated_collections": {},
    }
    for _ in range(REPAIR_STREAK_THRESHOLD):
        asyncio.run(
            async_sync_repair_issues(
                Mock(),
                entry,
                coordinator,
                payload,
                refreshed_sections={"feedback"},
            )
        )
    issue_id = issue_id_for(entry.entry_id, "seller_ops_unavailable_feedback")
    assert (DOMAIN, issue_id) in issue_registry.issues

    asyncio.run(
        async_sync_repair_issues(
            Mock(),
            entry,
            coordinator,
            payload,
            refreshed_sections={"buying"},
        )
    )
    assert (DOMAIN, issue_id) in issue_registry.issues
    assert (
        coordinator._repair_streaks.get("seller_ops_unavailable:feedback", 0)
        >= REPAIR_STREAK_THRESHOLD
    )


def test_feedback_invalid_request_uses_partial_failure_not_unavailable(
    issue_registry: _FakeIssueRegistry,
) -> None:
    """HTTP 400-style feedback_invalid_request must not claim API unavailability."""
    entry = _entry()
    coordinator = _coordinator()
    coordinator.options = {**DEFAULT_OPTIONS, CONF_FEEDBACK_ENABLED: True}
    payload = {
        "missing_scope_features": [],
        "missing_scopes": {},
        "partial_failures": ["feedback_invalid_request"],
        "truncated_collections": {},
    }
    for _ in range(REPAIR_STREAK_THRESHOLD):
        asyncio.run(
            async_sync_repair_issues(
                Mock(),
                entry,
                coordinator,
                payload,
                refreshed_sections={"feedback"},
            )
        )

    unavailable_id = issue_id_for(entry.entry_id, "seller_ops_unavailable_feedback")
    partial_id = issue_id_for(
        entry.entry_id, "partial_failure_feedback_invalid_request"
    )
    assert (DOMAIN, unavailable_id) not in issue_registry.issues
    assert (DOMAIN, partial_id) in issue_registry.issues
    assert (
        coordinator._repair_streaks.get("partial_failure:feedback_invalid_request", 0)
        >= REPAIR_STREAK_THRESHOLD
    )


def test_persisted_streaks_recreate_issue_immediately(
    issue_registry: _FakeIssueRegistry,
) -> None:
    """Loaded streaks at threshold recreate the issue on the next observed failure."""
    entry = _entry()
    coordinator = _coordinator()
    coordinator._repair_streaks = {
        "seller_ops_unavailable:seller_standards": REPAIR_STREAK_THRESHOLD - 1
    }
    payload = {
        "missing_scope_features": [],
        "missing_scopes": {},
        "partial_failures": ["seller_standards"],
        "truncated_collections": {},
    }

    asyncio.run(
        async_sync_repair_issues(
            Mock(),
            entry,
            coordinator,
            payload,
            refreshed_sections={"seller_standards"},
        )
    )

    issue_id = issue_id_for(entry.entry_id, "seller_ops_unavailable_seller_standards")
    assert (DOMAIN, issue_id) in issue_registry.issues
    assert (
        coordinator._repair_streaks["seller_ops_unavailable:seller_standards"]
        == REPAIR_STREAK_THRESHOLD
    )


def test_sync_saves_streaks_when_dirty(
    issue_registry: _FakeIssueRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    save = AsyncMock()
    monkeypatch.setattr(
        "custom_components.ebay.repairs.async_save_repair_streaks",
        save,
    )
    entry = _entry()
    coordinator = _coordinator()
    payload = {
        "missing_scope_features": [],
        "missing_scopes": {},
        "partial_failures": ["analytics_views"],
        "truncated_collections": {},
    }

    asyncio.run(async_sync_repair_issues(Mock(), entry, coordinator, payload))

    save.assert_awaited_once()
    assert coordinator._repair_streaks_dirty is False
    assert coordinator._repair_streaks["partial_failure:analytics_views"] == 1


def test_delete_entry_issues(issue_registry: _FakeIssueRegistry) -> None:
    from custom_components.ebay.repairs import async_delete_entry_issues

    entry = _entry()
    other = _entry(entry_id="entry-2")
    issue_registry.create(
        Mock(), DOMAIN, issue_id_for(entry.entry_id, "invalid_site_id")
    )
    issue_registry.create(
        Mock(), DOMAIN, issue_id_for(other.entry_id, "invalid_site_id")
    )
    async_delete_entry_issues(Mock(), entry)
    assert (
        DOMAIN,
        issue_id_for(entry.entry_id, "invalid_site_id"),
    ) not in issue_registry.issues
    assert (
        DOMAIN,
        issue_id_for(other.entry_id, "invalid_site_id"),
    ) in issue_registry.issues


def test_known_site_id_helpers() -> None:
    from custom_components.ebay.seller_ops import (
        is_known_site_id,
        marketplace_id_for_site,
    )

    assert is_known_site_id("0") is True
    assert marketplace_id_for_site("0") == "EBAY_US"
    assert is_known_site_id("999") is False
    assert marketplace_id_for_site("999") is None


def test_repair_streaks_store_round_trip(monkeypatch: pytest.MonkeyPatch) -> None:
    from custom_components.ebay.repairs import (
        async_load_repair_streaks,
        async_remove_repair_streaks,
        async_save_repair_streaks,
        repair_streaks_store,
    )

    hass = Mock()
    stores: dict[str, dict[str, Any]] = {}

    class _MemStore:
        def __init__(
            self, _hass: Any, _version: int, key: str, *args: Any, **kwargs: Any
        ) -> None:
            self.key = key

        async def async_load(self) -> dict[str, Any] | None:
            return stores.get(self.key)

        async def async_save(self, data: dict[str, Any]) -> None:
            stores[self.key] = data

        async def async_remove(self) -> None:
            stores.pop(self.key, None)

    monkeypatch.setattr(
        "custom_components.ebay.repairs.Store",
        _MemStore,
    )

    entry_id = "entry-1"
    asyncio.run(
        async_save_repair_streaks(
            hass, entry_id, {"truncation:watched": 3, "partial_failure:x": 1}
        )
    )
    loaded = asyncio.run(async_load_repair_streaks(hass, entry_id))
    assert loaded == {"truncation:watched": 3, "partial_failure:x": 1}
    assert repair_streaks_store(hass, entry_id).key.endswith("repair_streaks")

    asyncio.run(async_remove_repair_streaks(hass, entry_id))
    assert asyncio.run(async_load_repair_streaks(hass, entry_id)) == {}
