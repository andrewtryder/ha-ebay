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
    normalize_pinned_item_ids,
    normalize_price_targets,
    parse_nonnegative_decimal,
    parse_price_targets,
    pinned_ids,
    to_decimal,
    validate_pinned_item_options,
    validate_price_options,
)


def test_parse_price_targets_normalizes() -> None:
    targets = parse_price_targets(" 123456789012=250.00 usd \n987654321098=80.00 EUR\n")
    assert targets == {
        "123456789012": (Decimal("250.00"), "USD"),
        "987654321098": (Decimal("80.00"), "EUR"),
    }


def test_parse_price_targets_from_structured_list() -> None:
    targets = parse_price_targets(
        [
            {"item_id": "1", "amount": "9.50", "currency": "usd"},
            {"item_id": "2", "amount": 12, "currency": "EUR"},
        ]
    )
    assert targets == {
        "1": (Decimal("9.50"), "USD"),
        "2": (Decimal("12"), "EUR"),
    }
    assert normalize_price_targets(
        [{"item_id": "2", "amount": "5", "currency": "GBP"}]
    ) == [{"item_id": "2", "amount": "5", "currency": "GBP"}]


def test_parse_price_targets_rejects_duplicates() -> None:
    with pytest.raises(ValueError, match="duplicate_price_target"):
        parse_price_targets("1=10 USD\n1=20 USD")
    with pytest.raises(ValueError, match="duplicate_price_target"):
        parse_price_targets(
            [
                {"item_id": "1", "amount": "10", "currency": "USD"},
                {"item_id": "1", "amount": "20", "currency": "USD"},
            ]
        )


def test_parse_price_targets_rejects_invalid() -> None:
    with pytest.raises(ValueError, match="invalid_price_target"):
        parse_price_targets("not-a-target")
    with pytest.raises(ValueError, match="invalid_price_target"):
        parse_price_targets("1=-5 USD")
    with pytest.raises(ValueError, match="invalid_price_target"):
        parse_price_targets([{"item_id": "1", "amount": "-1", "currency": "USD"}])
    with pytest.raises(ValueError, match="invalid_price_target"):
        parse_price_targets({"item_id": "1", "amount": "1", "currency": "USD"})


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
    assert data[CONF_PINNED_ITEM_PRICE_TARGETS] == [
        {"item_id": "1", "amount": "9.99", "currency": "USD"}
    ]


def test_validate_price_options_accepts_structured_list() -> None:
    data = {
        CONF_WATCHED_PRICE_CHANGE_MIN_PERCENT: 0,
        CONF_WATCHED_PRICE_DROP_THRESHOLD: "",
        CONF_WATCHED_PRICE_DROP_CURRENCY: "",
        CONF_PINNED_ITEM_IDS: ["1", "2"],
        CONF_PINNED_ITEM_PRICE_TARGETS: [
            {"item_id": "1", "amount": "9.99", "currency": "USD"}
        ],
    }
    assert validate_price_options(data) == {}


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
    data = {CONF_PINNED_ITEM_IDS: "123456789012\n987654321098, abc-1_2"}
    assert validate_pinned_item_options(data) == {}
    assert data[CONF_PINNED_ITEM_IDS] == [
        "123456789012",
        "987654321098",
        "abc-1_2",
    ]
    data = {CONF_PINNED_ITEM_IDS: ["xyz", "abc"]}
    assert validate_pinned_item_options(data) == {}
    assert data[CONF_PINNED_ITEM_IDS] == ["abc", "xyz"]


def test_validate_pinned_item_options_rejects_malformed() -> None:
    errors = validate_pinned_item_options(
        {CONF_PINNED_ITEM_IDS: "123456789012=25.00 USD"}
    )
    assert errors[CONF_PINNED_ITEM_IDS] == "invalid_pinned_item_ids"
    errors = validate_pinned_item_options({CONF_PINNED_ITEM_IDS: "bad id!"})
    assert errors[CONF_PINNED_ITEM_IDS] == "invalid_pinned_item_ids"


def test_parse_nonnegative_decimal_and_to_decimal() -> None:
    assert parse_nonnegative_decimal(None) is None
    assert parse_nonnegative_decimal("") is None
    assert parse_nonnegative_decimal("  ") is None
    assert parse_nonnegative_decimal("1.5") == Decimal("1.5")
    with pytest.raises(ValueError, match="invalid"):
        parse_nonnegative_decimal("nope")
    with pytest.raises(ValueError, match="invalid"):
        parse_nonnegative_decimal("-1")
    assert to_decimal(None) is None
    assert to_decimal("2.5") == Decimal("2.5")
    assert to_decimal("not-a-number") is None


def test_pinned_ids_and_blank_price_target_lines() -> None:
    assert pinned_ids("1\n2, 3") == {"1", "2", "3"}
    assert pinned_ids(["1", "2", " 3 "]) == {"1", "2", "3"}
    assert normalize_pinned_item_ids("2,1") == ["1", "2"]
    assert parse_price_targets("\n\n1=10 USD\n") == {"1": (Decimal("10"), "USD")}


def test_parse_price_targets_rejects_malformed_parts() -> None:
    with pytest.raises(ValueError, match="invalid_price_target"):
        parse_price_targets("=10 USD")
    with pytest.raises(ValueError, match="invalid_price_target"):
        parse_price_targets("1=10")
    with pytest.raises(ValueError, match="invalid_price_target"):
        parse_price_targets("1=nope USD")


def test_validate_price_options_more_error_paths() -> None:
    data = {
        CONF_WATCHED_PRICE_CHANGE_MIN_PERCENT: None,
        CONF_WATCHED_PRICE_DROP_THRESHOLD: "",
        CONF_WATCHED_PRICE_DROP_CURRENCY: "USD",
        CONF_PINNED_ITEM_IDS: "",
        CONF_PINNED_ITEM_PRICE_TARGETS: "",
    }
    errors = validate_price_options(data)
    assert errors[CONF_WATCHED_PRICE_CHANGE_MIN_PERCENT] == "invalid"
    assert errors[CONF_WATCHED_PRICE_DROP_THRESHOLD] == "invalid"

    data = {
        CONF_WATCHED_PRICE_CHANGE_MIN_PERCENT: "nope",
        CONF_WATCHED_PRICE_DROP_THRESHOLD: "nope",
        CONF_WATCHED_PRICE_DROP_CURRENCY: "USD",
        CONF_PINNED_ITEM_IDS: "1",
        CONF_PINNED_ITEM_PRICE_TARGETS: "1=10",
    }
    errors = validate_price_options(data)
    assert errors[CONF_WATCHED_PRICE_CHANGE_MIN_PERCENT] == "invalid"
    assert errors[CONF_WATCHED_PRICE_DROP_THRESHOLD] == "invalid"
    assert errors[CONF_PINNED_ITEM_PRICE_TARGETS] == "invalid_price_target"

    data = {
        CONF_WATCHED_PRICE_CHANGE_MIN_PERCENT: 0,
        CONF_WATCHED_PRICE_DROP_THRESHOLD: "",
        CONF_WATCHED_PRICE_DROP_CURRENCY: "",
        CONF_PINNED_ITEM_IDS: "1",
        CONF_PINNED_ITEM_PRICE_TARGETS: "1=10 USD\n1=11 USD",
    }
    errors = validate_price_options(data)
    assert errors[CONF_PINNED_ITEM_PRICE_TARGETS] == "duplicate_price_target"
