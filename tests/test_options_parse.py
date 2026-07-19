"""Tests for watched-price option parsing and validation."""

from __future__ import annotations

from decimal import Decimal

import pytest

from custom_components.ebay.const import (
    CONF_PINNED_ITEM_IDS,
    CONF_PINNED_ITEM_PRICE_TARGETS,
    CONF_WATCHED_PRICE_CHANGE_MIN_PERCENT,
    CONF_WATCHED_PRICE_DROP_CURRENCY,
    CONF_WATCHED_PRICE_DROP_THRESHOLD,
)
from custom_components.ebay.options_parse import (
    parse_price_targets,
    validate_pinned_item_options,
    validate_price_options,
)


def test_parse_price_targets_normalizes() -> None:
    targets = parse_price_targets(" 123456789012=250.00 usd \n987654321098=80.00 EUR\n")
    assert targets == {
        "123456789012": (Decimal("250.00"), "USD"),
        "987654321098": (Decimal("80.00"), "EUR"),
    }


def test_parse_price_targets_rejects_duplicates() -> None:
    with pytest.raises(ValueError, match="duplicate_price_target"):
        parse_price_targets("1=10 USD\n1=20 USD")


def test_parse_price_targets_rejects_invalid() -> None:
    with pytest.raises(ValueError, match="invalid_price_target"):
        parse_price_targets("not-a-target")
    with pytest.raises(ValueError, match="invalid_price_target"):
        parse_price_targets("1=-5 USD")


def test_validate_price_options_accepts_valid() -> None:
    data = {
        CONF_WATCHED_PRICE_CHANGE_MIN_PERCENT: 1.5,
        CONF_WATCHED_PRICE_DROP_THRESHOLD: "10",
        CONF_WATCHED_PRICE_DROP_CURRENCY: "usd",
        CONF_PINNED_ITEM_IDS: "1,2",
        CONF_PINNED_ITEM_PRICE_TARGETS: "1=9.99 USD",
    }
    assert validate_price_options(data) == {}
    assert data[CONF_WATCHED_PRICE_DROP_CURRENCY] == "USD"


def test_validate_price_options_rejects_incomplete_threshold() -> None:
    data = {
        CONF_WATCHED_PRICE_CHANGE_MIN_PERCENT: 0,
        CONF_WATCHED_PRICE_DROP_THRESHOLD: "10",
        CONF_WATCHED_PRICE_DROP_CURRENCY: "",
        CONF_PINNED_ITEM_IDS: "",
        CONF_PINNED_ITEM_PRICE_TARGETS: "",
    }
    errors = validate_price_options(data)
    assert errors[CONF_WATCHED_PRICE_DROP_CURRENCY] == "required"


def test_validate_price_options_rejects_unpinned_target() -> None:
    data = {
        CONF_WATCHED_PRICE_CHANGE_MIN_PERCENT: 0,
        CONF_WATCHED_PRICE_DROP_THRESHOLD: "",
        CONF_WATCHED_PRICE_DROP_CURRENCY: "",
        CONF_PINNED_ITEM_IDS: "1",
        CONF_PINNED_ITEM_PRICE_TARGETS: "2=10 USD",
    }
    errors = validate_price_options(data)
    assert errors[CONF_PINNED_ITEM_PRICE_TARGETS] == "target_not_pinned"


def test_validate_price_options_rejects_negative_percent() -> None:
    data = {
        CONF_WATCHED_PRICE_CHANGE_MIN_PERCENT: -1,
        CONF_WATCHED_PRICE_DROP_THRESHOLD: "",
        CONF_WATCHED_PRICE_DROP_CURRENCY: "",
        CONF_PINNED_ITEM_IDS: "",
        CONF_PINNED_ITEM_PRICE_TARGETS: "",
    }
    errors = validate_price_options(data)
    assert errors[CONF_WATCHED_PRICE_CHANGE_MIN_PERCENT] == "invalid"


def test_validate_pinned_item_options_accepts_valid() -> None:
    assert (
        validate_pinned_item_options(
            {CONF_PINNED_ITEM_IDS: "123456789012\n987654321098, abc-1_2"}
        )
        == {}
    )


def test_validate_pinned_item_options_rejects_malformed() -> None:
    errors = validate_pinned_item_options(
        {CONF_PINNED_ITEM_IDS: "123456789012=25.00 USD"}
    )
    assert errors[CONF_PINNED_ITEM_IDS] == "invalid_pinned_item_ids"
    errors = validate_pinned_item_options({CONF_PINNED_ITEM_IDS: "bad id!"})
    assert errors[CONF_PINNED_ITEM_IDS] == "invalid_pinned_item_ids"
