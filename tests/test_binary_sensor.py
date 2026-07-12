"""Binary sensor tests for seller standards at-risk."""

from __future__ import annotations

import asyncio
from unittest.mock import Mock

from custom_components.ebay.binary_sensor import (
    EbaySellerAtRiskBinarySensor,
    async_setup_entry,
)


def test_binary_sensor_setup_and_state() -> None:
    coordinator = Mock()
    coordinator.data = {
        "summary": {"seller_standard_at_risk": True},
        "seller_ops": {"standards": {"at_risk": False}},
    }
    entry = Mock(entry_id="entry-1", runtime_data=coordinator)
    added: list = []
    asyncio.run(
        async_setup_entry(Mock(), entry, lambda entities: added.append(list(entities)))
    )
    assert len(added[0]) == 1
    entity = added[0][0]
    assert isinstance(entity, EbaySellerAtRiskBinarySensor)
    assert entity.is_on is True

    coordinator.data = {
        "summary": {},
        "seller_ops": {"standards": {"at_risk": False}},
    }
    assert entity.is_on is False

    coordinator.data = {"summary": {}, "seller_ops": {"standards": {}}}
    assert entity.is_on is None
