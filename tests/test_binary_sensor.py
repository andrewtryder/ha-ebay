"""Binary sensor tests for seller health conditions."""

from __future__ import annotations

import asyncio
from unittest.mock import Mock

from custom_components.ebay.binary_sensor import (
    EbayBinarySensor,
    binary_sensors_for_options,
    async_setup_entry,
)
from custom_components.ebay.const import (
    CONF_FEEDBACK_ENABLED,
    CONF_FULFILLMENT_ENABLED,
    CONF_MESSAGES_ENABLED,
    CONF_SELLER_STANDARDS_ENABLED,
    DEFAULT_OPTIONS,
)


def _entity_by_key(entities: list, key: str) -> EbayBinarySensor:
    for entity in entities:
        if entity.entity_description.key == key:
            return entity
    raise AssertionError(f"missing binary sensor {key}")


def test_binary_sensors_for_options_gates_features() -> None:
    defaults = binary_sensors_for_options(DEFAULT_OPTIONS)
    assert {d.key for d in defaults} == {
        "account_data_partially_unavailable",
        "api_data_truncated",
    }

    enabled = binary_sensors_for_options(
        {
            **DEFAULT_OPTIONS,
            CONF_FULFILLMENT_ENABLED: True,
            CONF_MESSAGES_ENABLED: True,
            CONF_FEEDBACK_ENABLED: True,
            CONF_SELLER_STANDARDS_ENABLED: True,
        }
    )
    assert {d.key for d in enabled} == {
        "seller_standard_at_risk",
        "orders_overdue",
        "orders_due_today",
        "open_payment_dispute",
        "unread_buyer_messages",
        "negative_feedback_received_recently",
        "account_data_partially_unavailable",
        "api_data_truncated",
    }


def test_binary_sensor_setup_and_seller_risk_state() -> None:
    coordinator = Mock()
    coordinator.options = {**DEFAULT_OPTIONS, CONF_SELLER_STANDARDS_ENABLED: True}
    coordinator.data = {
        "summary": {"seller_standard_at_risk": True},
        "seller_ops": {"standards": {"at_risk": False}},
        "partial_failures": [],
        "truncated_collections": {},
    }
    entry = Mock(entry_id="entry-1", runtime_data=coordinator)
    added: list = []
    asyncio.run(
        async_setup_entry(Mock(), entry, lambda entities: added.append(list(entities)))
    )
    entities = added[0]
    assert len(entities) == 3
    entity = _entity_by_key(entities, "seller_standard_at_risk")
    assert entity.is_on is True

    coordinator.data = {
        "summary": {},
        "seller_ops": {"standards": {"at_risk": False}},
        "partial_failures": [],
        "truncated_collections": {},
    }
    assert entity.is_on is False

    coordinator.data = {
        "summary": {},
        "seller_ops": {"standards": {}},
        "partial_failures": [],
        "truncated_collections": {},
    }
    assert entity.is_on is None


def test_actionable_count_binary_sensors() -> None:
    coordinator = Mock()
    coordinator.options = {
        **DEFAULT_OPTIONS,
        CONF_FULFILLMENT_ENABLED: True,
        CONF_MESSAGES_ENABLED: True,
        CONF_FEEDBACK_ENABLED: True,
    }
    coordinator.data = {
        "summary": {
            "orders_overdue": 2,
            "orders_shipping_today": 0,
            "open_payment_disputes": 1,
            "unread_conversations": 3,
            "recent_negative_feedback": 0,
        },
        "partial_failures": [],
        "truncated_collections": {},
    }
    entry = Mock(entry_id="entry-1", runtime_data=coordinator)
    added: list = []
    asyncio.run(
        async_setup_entry(Mock(), entry, lambda entities: added.append(list(entities)))
    )
    entities = added[0]

    assert _entity_by_key(entities, "orders_overdue").is_on is True
    assert _entity_by_key(entities, "orders_due_today").is_on is False
    assert _entity_by_key(entities, "open_payment_dispute").is_on is True
    assert _entity_by_key(entities, "unread_buyer_messages").is_on is True
    assert (
        _entity_by_key(entities, "negative_feedback_received_recently").is_on is False
    )

    coordinator.data = {
        "summary": {
            "orders_overdue": None,
            "orders_shipping_today": None,
            "open_payment_disputes": None,
            "unread_conversations": None,
            "recent_negative_feedback": None,
        },
        "partial_failures": [],
        "truncated_collections": {},
    }
    assert _entity_by_key(entities, "orders_overdue").is_on is None
    assert _entity_by_key(entities, "orders_due_today").is_on is None


def test_diagnostic_health_binary_sensors() -> None:
    coordinator = Mock()
    coordinator.options = {**DEFAULT_OPTIONS}
    coordinator.data = {
        "summary": {"partial_failures": ["orders"]},
        "partial_failures": ["orders"],
        "truncated_collections": {"watched": True, "selling": False},
    }
    entry = Mock(entry_id="entry-1", runtime_data=coordinator)
    added: list = []
    asyncio.run(
        async_setup_entry(Mock(), entry, lambda entities: added.append(list(entities)))
    )
    entities = added[0]
    assert len(entities) == 2

    partial = _entity_by_key(entities, "account_data_partially_unavailable")
    truncated = _entity_by_key(entities, "api_data_truncated")
    assert partial.is_on is True
    assert partial.extra_state_attributes == {"partial_failure_categories": ["orders"]}
    assert truncated.is_on is True
    assert truncated.extra_state_attributes == {"truncated_collections": ["watched"]}

    coordinator.data = {
        "summary": {},
        "partial_failures": [],
        "truncated_collections": {"watched": False},
    }
    assert partial.is_on is False
    assert truncated.is_on is False

    coordinator.data = None
    assert partial.is_on is None
    assert truncated.is_on is None


def test_actionable_binary_sensors_disabled_by_default() -> None:
    for description in binary_sensors_for_options(
        {
            **DEFAULT_OPTIONS,
            CONF_FULFILLMENT_ENABLED: True,
            CONF_MESSAGES_ENABLED: True,
            CONF_FEEDBACK_ENABLED: True,
            CONF_SELLER_STANDARDS_ENABLED: True,
        }
    ):
        if description.key == "seller_standard_at_risk":
            assert description.entity_registry_enabled_default is not False
        else:
            assert description.entity_registry_enabled_default is False


def test_summary_count_handles_invalid_values() -> None:
    coordinator = Mock()
    coordinator.options = {**DEFAULT_OPTIONS, CONF_FULFILLMENT_ENABLED: True}
    coordinator.data = {
        "summary": {"orders_overdue": "not-a-number"},
        "partial_failures": [],
        "truncated_collections": {},
    }
    entry = Mock(entry_id="entry-1", runtime_data=coordinator)
    added: list = []
    asyncio.run(
        async_setup_entry(Mock(), entry, lambda entities: added.append(list(entities)))
    )
    overdue = _entity_by_key(added[0], "orders_overdue")
    assert overdue.is_on is True
    assert overdue.extra_state_attributes is None
