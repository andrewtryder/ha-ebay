"""Scalar/XML helpers shared by Trading parsers and clients."""

from __future__ import annotations

from datetime import datetime, timezone
import time
from typing import Any
import xml.etree.ElementTree as ET

from ..const import NS

MAX_PAGES = 20
ANALYTICS_BATCH_SIZE = 100
_WARNING_MESSAGE_MAX_LEN = 240


def _text(parent: ET.Element | None, path: str) -> str | None:
    if parent is None:
        return None
    value = parent.findtext(path, namespaces=NS)
    return value.strip() if value else None


def _first_text(parent: ET.Element | None, paths: list[str]) -> str | None:
    for path in paths:
        value = _text(parent, path)
        if value:
            return value
    return None


def _to_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _to_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _to_bool(value: Any) -> bool | None:
    if value is None:
        return None
    lowered = str(value).strip().lower()
    if lowered in {"true", "1", "yes"}:
        return True
    if lowered in {"false", "0", "no"}:
        return False
    return None


def _total_pages(root: ET.Element, container_name: str | None = None) -> int:
    """Parse PaginationResult.TotalNumberOfPages, capped for safety."""
    return min(_reported_total_pages(root, container_name), MAX_PAGES)


def _reported_total_pages(root: ET.Element, container_name: str | None = None) -> int:
    """Parse PaginationResult.TotalNumberOfPages without applying local caps."""
    parent = root.find(f"e:{container_name}", namespaces=NS) if container_name else root
    total = _to_int(_text(parent, "e:PaginationResult/e:TotalNumberOfPages"))
    if total is None or total < 1:
        return 1
    return total


def _auth_like_error(root: ET.Element) -> bool:
    """Return whether a Trading API XML failure looks token-related."""
    for error in root.findall("e:Errors", namespaces=NS):
        code = _text(error, "e:ErrorCode")
        fields = [
            code,
            _text(error, "e:ShortMessage"),
            _text(error, "e:LongMessage"),
            _text(error, "e:SeverityCode"),
        ]
        haystack = " ".join(value for value in fields if value).lower()
        if code in {"931", "932", "16110", "17470"}:
            return True
        if any(
            marker in haystack
            for marker in (
                "auth",
                "token",
                "oauth",
                "credential",
                "access denied",
                "not authorized",
            )
        ):
            return True
    return False


def _sanitize_warning_text(value: str | None) -> str | None:
    """Truncate and redact credential-like substrings from Trading API warnings."""
    if not value:
        return None
    sanitized = value
    for marker in ("Bearer ", "token=", "refresh_token", "access_token"):
        if marker.lower() in sanitized.lower():
            sanitized = "[redacted]"
            break
    if len(sanitized) > _WARNING_MESSAGE_MAX_LEN:
        sanitized = f"{sanitized[:_WARNING_MESSAGE_MAX_LEN]}…"
    return sanitized


def extract_trading_warnings(root: ET.Element) -> list[dict[str, str | None]]:
    """Return sanitized Trading API warning entries from an Ack=Warning response."""
    warnings: list[dict[str, str | None]] = []
    for error in root.findall("e:Errors", namespaces=NS):
        severity = (_text(error, "e:SeverityCode") or "").lower()
        if severity and severity != "warning":
            continue
        warnings.append(
            {
                "code": _text(error, "e:ErrorCode"),
                "severity": _text(error, "e:SeverityCode"),
                "short_message": _sanitize_warning_text(_text(error, "e:ShortMessage")),
                "long_message": _sanitize_warning_text(_text(error, "e:LongMessage")),
            }
        )
    return warnings


def dedupe_trading_warnings(
    warnings: list[dict[str, str | None]],
) -> list[dict[str, str | None]]:
    """Deduplicate Trading API warnings by code and message text."""
    deduped: list[dict[str, str | None]] = []
    seen: set[tuple[str | None, str | None, str | None]] = set()
    for warning in warnings:
        key = (
            warning.get("code"),
            warning.get("short_message"),
            warning.get("long_message"),
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(warning)
    return deduped


def chunk_item_ids(
    item_ids: list[str], batch_size: int = ANALYTICS_BATCH_SIZE
) -> list[list[str]]:
    """Split item IDs into bounded Analytics request batches."""
    if batch_size < 1:
        raise ValueError("batch_size must be >= 1")
    return [
        item_ids[index : index + batch_size]
        for index in range(0, len(item_ids), batch_size)
    ]


def _duration_ms(start: float) -> float:
    """Return elapsed monotonic time in milliseconds."""
    return (time.monotonic() - start) * 1000


def _safe_exception_context(exc: BaseException) -> str:
    """Return safe exception class and message context for debug logs."""
    detail = str(exc)
    if not detail:
        return type(exc).__name__
    return f"{type(exc).__name__}: {detail}"


def _page_number_from_xml(xml_body: str) -> int | None:
    """Extract a Trading API page number from generated XML without logging XML."""
    try:
        root = ET.fromstring(xml_body)
    except ET.ParseError:
        return None
    return _to_int(_text(root, ".//e:PageNumber"))


def _money(item: ET.Element, path: str) -> tuple[float | None, str | None]:
    node = item.find(path, namespaces=NS)
    if node is None or node.text is None:
        return None, None
    return _to_float(node.text), node.attrib.get("currencyID")


def ebay_time(value: datetime) -> str:
    """Format a datetime for eBay Trading API XML."""
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def parse_ebay_time(value: str | None) -> datetime | None:
    """Parse an eBay timestamp."""
    if not value:
        return None
    normalized = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def seconds_until(value: datetime | str | None) -> int | None:
    """Return seconds until a datetime, never below zero."""
    end_time = parse_ebay_time(value) if isinstance(value, str) else value
    if not end_time:
        return None
    return max(0, int((end_time - datetime.now(timezone.utc)).total_seconds()))


def format_seconds(seconds: int | None) -> str | None:
    """Return a human readable duration."""
    if seconds is None:
        return None
    if seconds <= 0:
        return "ended"
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _ = divmod(rem, 60)
    if days:
        return f"{days}d {hours}h {minutes}m"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"
