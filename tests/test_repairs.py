"""Tests for Home Assistant Repairs issue sync."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import Mock

import pytest

from custom_components.ebay.const import (
    CONF_FEEDBACK_ENABLED,
    CONF_SITE_ID,
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
    return registry


def _entry(*, site_id: str = "0", entry_id: str = "entry-1") -> Mock:
    entry = Mock()
    entry.entry_id = entry_id
    entry.data = {CONF_SITE_ID: site_id}
    return entry


def _coordinator() -> Mock:
    coordinator = Mock()
    coordinator._repair_streaks = {}
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

    cleared = {
        "missing_scope_features": [],
        "missing_scopes": {},
        "partial_failures": [],
        "truncated_collections": {},
    }
    asyncio.run(async_sync_repair_issues(Mock(), entry, coordinator, cleared))
    assert (DOMAIN, issue_id) not in issue_registry.issues


def test_truncated_requires_streak(issue_registry: _FakeIssueRegistry) -> None:
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
