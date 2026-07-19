"""Parse and validate eBay integration options."""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Any

from .const import (
    CONF_PINNED_ITEM_IDS,
    CONF_PINNED_ITEM_PRICE_TARGETS,
    CONF_WATCHED_PRICE_CHANGE_MIN_PERCENT,
    CONF_WATCHED_PRICE_DROP_CURRENCY,
    CONF_WATCHED_PRICE_DROP_THRESHOLD,
)

# eBay item IDs are typically numeric; allow common safe ID characters.
_PINNED_ITEM_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def pinned_ids(value: str) -> set[str]:
    """Parse comma- or newline-separated pinned item IDs."""
    return {
        part.strip() for part in value.replace("\n", ",").split(",") if part.strip()
    }


def validate_pinned_item_ids(value: str) -> None:
    """Validate pinned item ID tokens.

    Raises ValueError with key ``invalid_pinned_item_ids`` when any non-empty
    token is malformed (for example price-target syntax or punctuation).
    """
    for part in str(value or "").replace("\n", ",").split(","):
        token = part.strip()
        if not token:
            continue
        if "=" in token or not _PINNED_ITEM_ID_RE.fullmatch(token):
            raise ValueError("invalid_pinned_item_ids")


def parse_nonnegative_decimal(value: Any) -> Decimal | None:
    """Parse a nonnegative Decimal, or return None for blank values."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        amount = Decimal(text)
    except (InvalidOperation, ValueError):
        raise ValueError("invalid") from None
    if amount < 0:
        raise ValueError("invalid")
    return amount


def parse_price_targets(value: str) -> dict[str, tuple[Decimal, str]]:
    """Parse multiline item_id=amount CURRENCY targets.

    Raises ValueError with a short error key on invalid input.
    """
    targets: dict[str, tuple[Decimal, str]] = {}
    for raw_line in str(value or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if "=" not in line:
            raise ValueError("invalid_price_target")
        item_id, remainder = line.split("=", 1)
        item_id = item_id.strip()
        parts = remainder.strip().split()
        if not item_id or len(parts) != 2:
            raise ValueError("invalid_price_target")
        amount_text, currency = parts
        currency = currency.strip().upper()
        if not currency:
            raise ValueError("invalid_price_target")
        try:
            amount = Decimal(amount_text)
        except (InvalidOperation, ValueError):
            raise ValueError("invalid_price_target") from None
        if amount < 0:
            raise ValueError("invalid_price_target")
        if item_id in targets:
            raise ValueError("duplicate_price_target")
        targets[item_id] = (amount, currency)
    return targets


def validate_pinned_item_options(user_input: dict[str, Any]) -> dict[str, str]:
    """Return field errors for pinned item ID formatting."""
    errors: dict[str, str] = {}
    try:
        validate_pinned_item_ids(str(user_input.get(CONF_PINNED_ITEM_IDS, "") or ""))
    except ValueError as exc:
        errors[CONF_PINNED_ITEM_IDS] = str(exc)
    return errors


def validate_price_options(user_input: dict[str, Any]) -> dict[str, str]:
    """Return field errors for watched-price option values."""
    errors: dict[str, str] = {}
    raw_percent = user_input.get(CONF_WATCHED_PRICE_CHANGE_MIN_PERCENT, 0)
    try:
        percent = parse_nonnegative_decimal(raw_percent)
        if percent is None:
            errors[CONF_WATCHED_PRICE_CHANGE_MIN_PERCENT] = "invalid"
        else:
            user_input[CONF_WATCHED_PRICE_CHANGE_MIN_PERCENT] = float(percent)
    except ValueError:
        errors[CONF_WATCHED_PRICE_CHANGE_MIN_PERCENT] = "invalid"

    threshold_raw = user_input.get(CONF_WATCHED_PRICE_DROP_THRESHOLD, "")
    currency_raw = user_input.get(CONF_WATCHED_PRICE_DROP_CURRENCY, "")
    threshold_text = "" if threshold_raw is None else str(threshold_raw).strip()
    currency_text = "" if currency_raw is None else str(currency_raw).strip().upper()
    user_input[CONF_WATCHED_PRICE_DROP_CURRENCY] = currency_text

    if threshold_text:
        try:
            amount = parse_nonnegative_decimal(threshold_text)
            if amount is None:
                errors[CONF_WATCHED_PRICE_DROP_THRESHOLD] = "invalid"
            else:
                user_input[CONF_WATCHED_PRICE_DROP_THRESHOLD] = str(amount)
        except ValueError:
            errors[CONF_WATCHED_PRICE_DROP_THRESHOLD] = "invalid"
        if not currency_text:
            errors[CONF_WATCHED_PRICE_DROP_CURRENCY] = "required"
    else:
        user_input[CONF_WATCHED_PRICE_DROP_THRESHOLD] = ""
        if currency_text:
            errors[CONF_WATCHED_PRICE_DROP_THRESHOLD] = "invalid"

    targets_raw = user_input.get(CONF_PINNED_ITEM_PRICE_TARGETS, "")
    pinned = pinned_ids(str(user_input.get(CONF_PINNED_ITEM_IDS, "") or ""))
    try:
        targets = parse_price_targets(str(targets_raw or ""))
        for item_id in targets:
            if item_id not in pinned:
                errors[CONF_PINNED_ITEM_PRICE_TARGETS] = "target_not_pinned"
                break
        user_input[CONF_PINNED_ITEM_PRICE_TARGETS] = str(targets_raw or "")
    except ValueError as exc:
        key = (
            str(exc)
            if str(exc) in {"invalid_price_target", "duplicate_price_target"}
            else "invalid_price_target"
        )
        errors[CONF_PINNED_ITEM_PRICE_TARGETS] = key

    return errors


def to_decimal(value: Any) -> Decimal | None:
    """Convert a price-like value to Decimal, or None if unavailable."""
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
