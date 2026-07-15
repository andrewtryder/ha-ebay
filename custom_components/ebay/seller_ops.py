"""Parsers and aggregates for read-only seller-ops APIs."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote

SITE_ID_TO_MARKETPLACE = {
    "0": "EBAY_US",
    "2": "EBAY_CA",
    "3": "EBAY_GB",
    "15": "EBAY_AU",
    "16": "EBAY_AT",
    "23": "EBAY_BE",
    "71": "EBAY_FR",
    "77": "EBAY_DE",
    "101": "EBAY_IT",
    "123": "EBAY_BE",
    "146": "EBAY_NL",
    "186": "EBAY_ES",
    "193": "EBAY_CH",
    "201": "EBAY_HK",
    "203": "EBAY_IN",
    "205": "EBAY_IE",
    "207": "EBAY_MY",
    "210": "EBAY_CAFR",
    "211": "EBAY_PH",
    "212": "EBAY_PL",
    "216": "EBAY_SG",
}

SHIPMENT_DUE_SOON_HOURS = 24
ORDERS_LOOKBACK_DAYS = 30
ORDERS_PAGE_LIMIT = 50
ORDERS_MAX_PAGES = 10
DISPUTES_PAGE_LIMIT = 50
DISPUTES_MAX_PAGES = 5
CONVERSATIONS_PAGE_LIMIT = 25
CONVERSATIONS_MAX_PAGES = 5
FEEDBACK_RECENT_LOOKBACK_DAYS = 30


def is_known_site_id(site_id: str) -> bool:
    """Return True when Site ID maps to a known Sell Analytics marketplace."""
    return str(site_id) in SITE_ID_TO_MARKETPLACE


def marketplace_id_for_site(site_id: str) -> str | None:
    """Map Trading Site ID to Sell Analytics marketplace ID.

    Returns None when the Site ID is unknown so callers can soft-fail instead of
    silently querying the wrong marketplace.
    """
    return SITE_ID_TO_MARKETPLACE.get(str(site_id))


def empty_seller_ops() -> dict[str, Any]:
    """Return an empty seller-ops payload section.

    Count fields use None until a successful fetch so disabled/unfetched
    features are not confused with genuine zeros.
    """
    return {
        "orders": {
            "awaiting_shipment": None,
            "shipping_today": None,
            "overdue": None,
            "shipped": None,
            "partially_fulfilled": None,
            "by_id": {},
        },
        "disputes": {"open_count": None, "by_id": {}},
        "standards": {
            "seller_level": None,
            "program": None,
            "transaction_defect_rate": None,
            "late_shipment_rate": None,
            "cases_closed_without_seller_resolution": None,
            "item_not_received_rating": None,
            "item_not_as_described_rating": None,
            "item_not_received_above_benchmark": None,
            "item_not_as_described_above_benchmark": None,
            "next_evaluation_date": None,
            "at_risk": None,
        },
        "feedback": {
            "score": None,
            "positive_percent": None,
            "recent_positive": None,
            "recent_neutral": None,
            "recent_negative": None,
            "awaiting_count": None,
        },
        "messages": {
            "unread_count": None,
            "buyer_question_count": None,
            "oldest_unanswered_hours": None,
            "by_id": {},
        },
    }


def _parse_ebay_datetime(value: Any) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _date_only_utc(value: datetime) -> datetime:
    return value.astimezone(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    )


def encode_fulfillment_status_filter(statuses: list[str]) -> str:
    """Build a percent-encoded orderfulfillmentstatus filter value."""
    joined = "|".join(statuses)
    return f"orderfulfillmentstatus:{quote('{' + joined + '}', safe='')}"


def encode_creation_date_filter(start: datetime, end: datetime) -> str:
    """Build a creationdate range filter."""
    start_text = start.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    end_text = end.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    return f"creationdate:[{start_text}..{end_text}]"


def normalize_order(
    raw: dict[str, Any], *, now: datetime | None = None
) -> dict[str, Any]:
    """Normalize one Fulfillment API order into a safe summary record."""
    now = now or datetime.now(timezone.utc)
    order_id = str(raw.get("orderId") or "")
    payment_status = str(raw.get("orderPaymentStatus") or "")
    fulfillment_status = str(raw.get("orderFulfillmentStatus") or "")
    line_items = raw.get("lineItems") or []
    first_item = line_items[0] if line_items else {}
    listing_id = str(
        first_item.get("legacyItemId")
        or first_item.get("listingId")
        or first_item.get("sku")
        or ""
    )
    title = first_item.get("title")
    if isinstance(title, str) and len(title) > 120:
        title = title[:119] + "…"

    ship_by: datetime | None = None
    for line in line_items:
        instructions = line.get("lineItemFulfillmentInstructions") or {}
        candidate = _parse_ebay_datetime(instructions.get("shipByDate"))
        if candidate and (ship_by is None or candidate < ship_by):
            ship_by = candidate

    has_tracking = False
    for fulfillment in raw.get("shippingFulfillments") or []:
        if fulfillment.get("shipmentTrackingNumber"):
            has_tracking = True
            break
        for line in fulfillment.get("lineItems") or []:
            if line.get("shipmentTrackingNumber"):
                has_tracking = True
                break
        if has_tracking:
            break

    shipped = fulfillment_status == "FULFILLED"
    partial = fulfillment_status == "IN_PROGRESS"
    awaiting = payment_status == "PAID" and fulfillment_status in {
        "NOT_STARTED",
        "IN_PROGRESS",
    }
    overdue = False
    shipping_today = False
    due_soon = False
    if ship_by and not shipped:
        ship_day = _date_only_utc(ship_by)
        today = _date_only_utc(now)
        shipping_today = ship_day == today
        overdue = ship_day < today
        due_soon = not overdue and timedelta(0) <= (ship_by - now) <= timedelta(
            hours=SHIPMENT_DUE_SOON_HOURS
        )

    return {
        "order_id": order_id,
        "item_id": listing_id or None,
        "listing_id": listing_id or None,
        "title": title,
        "payment_status": payment_status,
        "fulfillment_status": fulfillment_status,
        "creation_date": _parse_ebay_datetime(raw.get("creationDate")),
        "ship_by": ship_by,
        "has_tracking": has_tracking,
        "awaiting_shipment": awaiting,
        "shipping_today": shipping_today,
        "overdue": overdue,
        "due_soon": due_soon,
        "partially_fulfilled": partial,
        "shipped": shipped,
        "paid": payment_status == "PAID",
    }


def summarize_orders(orders: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate normalized orders into counts and an id map."""
    by_id = {order["order_id"]: order for order in orders if order.get("order_id")}
    return {
        "awaiting_shipment": sum(
            1 for o in by_id.values() if o.get("awaiting_shipment")
        ),
        "shipping_today": sum(1 for o in by_id.values() if o.get("shipping_today")),
        "overdue": sum(1 for o in by_id.values() if o.get("overdue")),
        "shipped": sum(1 for o in by_id.values() if o.get("shipped")),
        "partially_fulfilled": sum(
            1 for o in by_id.values() if o.get("partially_fulfilled")
        ),
        "by_id": by_id,
    }


def normalize_dispute(raw: dict[str, Any]) -> dict[str, Any]:
    """Normalize a payment dispute summary without buyer username."""
    return {
        "dispute_id": str(raw.get("paymentDisputeId") or ""),
        "order_id": str(raw.get("orderId") or "") or None,
        "status": str(raw.get("paymentDisputeStatus") or ""),
        "reason": str(raw.get("reason") or "") or None,
        "open_date": _parse_ebay_datetime(raw.get("openDate")),
        "respond_by": _parse_ebay_datetime(
            (raw.get("amount") or {}).get("respondByDate")
            if isinstance(raw.get("amount"), dict)
            else raw.get("respondByDate")
        ),
    }


def summarize_disputes(disputes: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate payment disputes."""
    by_id = {
        dispute["dispute_id"]: dispute
        for dispute in disputes
        if dispute.get("dispute_id")
    }
    return {"open_count": len(by_id), "by_id": by_id}


def _metric_value(metrics: list[dict[str, Any]], key: str) -> float | None:
    for metric in metrics:
        if metric.get("metricKey") != key:
            continue
        value = metric.get("value")
        if isinstance(value, dict):
            raw = value.get("value")
        else:
            raw = value
        if raw is None:
            return None
        try:
            return float(raw)
        except (TypeError, ValueError):
            return None
    return None


def _metric_level(metrics: list[dict[str, Any]], key: str) -> str | None:
    for metric in metrics:
        if metric.get("metricKey") == key:
            level = metric.get("level") or metric.get("metricLevel")
            return str(level) if level else None
    return None


def parse_seller_standards_profiles(payload: dict[str, Any]) -> dict[str, Any]:
    """Pick the CURRENT default/program profile and extract key rates."""
    profiles = payload.get("standardsProfiles") or []
    if not profiles:
        return {}
    preferred = None
    for profile in profiles:
        cycle = profile.get("cycle") or {}
        if cycle.get("cycleType") == "CURRENT" and profile.get("defaultProgram"):
            preferred = profile
            break
    if preferred is None:
        for profile in profiles:
            cycle = profile.get("cycle") or {}
            if cycle.get("cycleType") == "CURRENT":
                preferred = profile
                break
    if preferred is None:
        preferred = profiles[0]

    metrics = preferred.get("metrics") or []
    level = preferred.get("standardsLevel") or preferred.get("standardslevel")
    cycle = preferred.get("cycle") or {}
    next_eval = _parse_ebay_datetime(cycle.get("evaluationDate"))
    defect = _metric_value(metrics, "TRANSACTION_DEFECT_RATE")
    late = _metric_value(metrics, "SHIPPING_MISS_RATE")
    if late is None:
        late = _metric_value(metrics, "LATE_SHIPMENT_RATE")
    cases = _metric_value(metrics, "CASES_CLOSED_WITHOUT_SELLER_RESOLUTION")
    levels = [
        _metric_level(metrics, "TRANSACTION_DEFECT_RATE"),
        _metric_level(metrics, "SHIPPING_MISS_RATE"),
        _metric_level(metrics, "LATE_SHIPMENT_RATE"),
        _metric_level(metrics, "CASES_CLOSED_WITHOUT_SELLER_RESOLUTION"),
        str(level) if level else None,
    ]
    at_risk = any(value == "BELOW_STANDARD" for value in levels if value)
    return {
        "seller_level": str(level) if level else None,
        "program": preferred.get("program"),
        "transaction_defect_rate": defect,
        "late_shipment_rate": late,
        "cases_closed_without_seller_resolution": cases,
        "next_evaluation_date": next_eval.isoformat() if next_eval else None,
        "at_risk": at_risk,
    }


_CS_RATING_ENUMS = frozenset(
    {
        "VERY_HIGH",
        "HIGH",
        "AVERAGE",
        "LOW",
        "VERY_LOW",
        "ABOVE_STANDARD",
        "BELOW_STANDARD",
    }
)


def parse_customer_service_metric(payload: dict[str, Any]) -> dict[str, Any]:
    """Extract rating and peer-benchmark flag from a CS metric response."""
    dimension_metrics = payload.get("dimensionMetrics") or []
    rating = None
    above_benchmark = False
    if dimension_metrics:
        metric = dimension_metrics[0]
        metrics = metric.get("metrics") or []
        if metrics:
            entry = metrics[0]
            raw_score = entry.get("avgRatingOrScore") or entry.get("rating")
            if raw_score is None:
                raw_score = (
                    (entry.get("value") or {}).get("value")
                    if isinstance(entry.get("value"), dict)
                    else entry.get("value")
                )
            benchmark = entry.get("benchmark") or {}
            rating_enum = benchmark.get("rating") or entry.get("metricRating")
            # Prefer a declared enum over a numeric score for sensor state.
            if isinstance(rating_enum, str) and rating_enum in _CS_RATING_ENUMS:
                rating = rating_enum
            elif isinstance(raw_score, str) and raw_score in _CS_RATING_ENUMS:
                rating = raw_score
            elif raw_score is not None:
                rating = raw_score
            if rating_enum in {"LOW", "VERY_LOW", "BELOW_STANDARD"}:
                above_benchmark = True
            # "Above peer benchmark" means worse than peers for defect-style metrics.
            comparison = benchmark.get("adjustment") or benchmark.get("rating")
            if comparison in {"ABOVE", "HIGH", "VERY_HIGH"}:
                above_benchmark = True
    evaluation = payload.get("evaluationCycle") or {}
    return {
        "rating": str(rating) if rating is not None else None,
        "above_peer_benchmark": above_benchmark,
        "evaluation_date": evaluation.get("evaluationDate"),
    }


def _as_list(value: Any) -> list[Any]:
    """Coerce a single object or list into a list."""
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _feedback_metric_value(metrics: Any, *names: str) -> Any:
    """Return the first matching feedbackMetrics value by metric name/key."""
    if not isinstance(metrics, list):
        return None
    wanted = {name.upper() for name in names}
    for metric in metrics:
        if not isinstance(metric, dict):
            continue
        key = str(
            metric.get("metricName")
            or metric.get("metricKey")
            or metric.get("name")
            or ""
        ).upper()
        if key in wanted:
            return metric.get("value")
    return None


def _coerce_int(value: Any) -> int | None:
    try:
        if value is None or value == "":
            return None
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _coerce_float(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _period_days(period: Any) -> int | str | None:
    """Extract lookback days from a period object or scalar."""
    if isinstance(period, dict):
        days = period.get("value")
        unit = str(period.get("unit") or period.get("periodUnit") or "DAY").upper()
        if days is not None and unit in {"DAY", "DAYS", ""}:
            return days
        return (
            period.get("periodInDays")
            or period.get("lookbackPeriodInDays")
            or period.get("value")
        )
    return period


def parse_feedback_summary(payload: dict[str, Any]) -> dict[str, Any]:
    """Parse Commerce feedback rating summary without usernames or comments."""
    score = None
    positive_percent = None
    recent_positive = 0
    recent_neutral = 0
    recent_negative = 0

    # Documented shape:
    # feedbackRatingSummary -> ratingSummaryByRatingType ->
    #   feedbackMetrics / feedbackRatingValueDistribution
    documented_summaries = _as_list(
        payload.get("feedbackRatingSummary")
        or payload.get("feedbackRatingSummaries")
        or payload.get("summary")
    )
    for summary in documented_summaries:
        if not isinstance(summary, dict):
            continue
        by_type = _as_list(
            summary.get("ratingSummaryByRatingType")
            or summary.get("overallExperience")
            or summary.get("ratingSummary")
            or summary
        )
        for rating_summary in by_type:
            if not isinstance(rating_summary, dict):
                continue
            rating_type = str(
                rating_summary.get("ratingType")
                or rating_summary.get("rating_type")
                or "OVERALL_EXPERIENCE"
            ).upper()
            if rating_type not in {"OVERALL_EXPERIENCE", ""}:
                continue

            metrics = rating_summary.get("feedbackMetrics") or []
            if score is None:
                score = _coerce_int(
                    _feedback_metric_value(metrics, "FEEDBACK_SCORE", "SCORE")
                    or rating_summary.get("feedbackScore")
                )
            if positive_percent is None:
                positive_percent = _coerce_float(
                    _feedback_metric_value(
                        metrics,
                        "POSITIVE_FEEDBACK_PERCENTAGE",
                        "POSITIVE_FEEDBACK_PERCENT",
                        "POSITIVE_PERCENTAGE",
                    )
                    or rating_summary.get("positiveFeedbackPercent")
                )

            distribution = (
                rating_summary.get("feedbackRatingValueDistribution")
                or rating_summary.get("ratingValueDistribution")
                or []
            )
            period_days = _period_days(
                rating_summary.get("period")
                or rating_summary.get("lookbackPeriod")
                or rating_summary.get("periodInDays")
            )
            if period_days not in {30, "30", None}:
                if recent_positive or recent_neutral or recent_negative:
                    continue
            if isinstance(distribution, list) and distribution:
                pos = neu = neg = 0
                for entry in distribution:
                    if not isinstance(entry, dict):
                        continue
                    label = str(
                        entry.get("ratingValue")
                        or entry.get("value")
                        or entry.get("feedbackRating")
                        or ""
                    ).upper()
                    count = (
                        _coerce_int(entry.get("count") or entry.get("valueCount")) or 0
                    )
                    if label in {"POSITIVE", "FEEDBACK_POSITIVE"}:
                        pos += count
                    elif label in {"NEUTRAL", "FEEDBACK_NEUTRAL"}:
                        neu += count
                    elif label in {"NEGATIVE", "FEEDBACK_NEGATIVE"}:
                        neg += count
                if pos or neu or neg:
                    recent_positive = pos
                    recent_neutral = neu
                    recent_negative = neg
                    continue

            # Legacy periodSummaries / feedbackPeriods containers.
            periods = (
                rating_summary.get("periodSummaries")
                or rating_summary.get("feedbackPeriods")
                or summary.get("periodSummaries")
                or summary.get("feedbackPeriods")
                or []
            )
            for period in periods:
                if not isinstance(period, dict):
                    continue
                period_days = period.get("periodInDays") or period.get(
                    "lookbackPeriodInDays"
                )
                if period_days not in {30, "30", None}:
                    if recent_positive or recent_neutral or recent_negative:
                        continue
                counts = period.get("counts") or period
                try:
                    recent_positive = int(
                        counts.get("positive")
                        or counts.get("positiveFeedbackCount")
                        or recent_positive
                        or 0
                    )
                    recent_neutral = int(
                        counts.get("neutral")
                        or counts.get("neutralFeedbackCount")
                        or recent_neutral
                        or 0
                    )
                    recent_negative = int(
                        counts.get("negative")
                        or counts.get("negativeFeedbackCount")
                        or recent_negative
                        or 0
                    )
                except (TypeError, ValueError):
                    continue

    seller_metrics = payload.get("sellerMetrics") or {}
    if score is None and seller_metrics.get("feedbackScore") is not None:
        score = _coerce_int(seller_metrics["feedbackScore"])
    if (
        positive_percent is None
        and seller_metrics.get("positiveFeedbackPercent") is not None
    ):
        positive_percent = _coerce_float(seller_metrics["positiveFeedbackPercent"])

    return {
        "score": score,
        "positive_percent": positive_percent,
        "recent_positive": recent_positive,
        "recent_neutral": recent_neutral,
        "recent_negative": recent_negative,
    }


def parse_feedback_items(payload: dict[str, Any]) -> dict[str, int]:
    """Count recent feedback by comment type without retaining comments/usernames."""
    items = (
        payload.get("feedbackEntries")
        or payload.get("feedbackItems")
        or payload.get("feedback")
        or []
    )
    counts = {"recent_positive": 0, "recent_neutral": 0, "recent_negative": 0}
    for item in items:
        if not isinstance(item, dict):
            continue
        comment_type = str(
            item.get("commentType")
            or item.get("feedbackType")
            or item.get("rating")
            or ""
        ).upper()
        if comment_type in {"POSITIVE", "FEEDBACK_POSITIVE"}:
            counts["recent_positive"] += 1
        elif comment_type in {"NEUTRAL", "FEEDBACK_NEUTRAL"}:
            counts["recent_neutral"] += 1
        elif comment_type in {"NEGATIVE", "FEEDBACK_NEGATIVE"}:
            counts["recent_negative"] += 1
    return counts


def parse_awaiting_feedback_count(payload: dict[str, Any]) -> int:
    """Return count of line items awaiting feedback."""
    items = (
        payload.get("itemsAwaitingFeedback")
        or payload.get("awaitingFeedbackItems")
        or payload.get("lineItems")
        or payload.get("items")
        or []
    )
    total = payload.get("total")
    if total is not None:
        try:
            return int(total)
        except (TypeError, ValueError):
            pass
    return len(items)


def normalize_conversation(raw: dict[str, Any]) -> dict[str, Any]:
    """Normalize a conversation for diffs without message bodies or usernames."""
    conversation_id = str(raw.get("conversationId") or "")
    latest = raw.get("latestMessage") or {}
    subject = (
        raw.get("conversationTitle")
        or raw.get("subject")
        or latest.get("subject")
        or None
    )
    if isinstance(subject, str) and len(subject) > 120:
        subject = subject[:119] + "…"
    reference = raw.get("reference") or {}
    listing_id = (
        str(
            reference.get("referenceId")
            or raw.get("listingId")
            or raw.get("itemId")
            or ""
        )
        or None
    )
    status = str(raw.get("conversationStatus") or raw.get("status") or "").upper()
    unread = status == "UNREAD" or bool(raw.get("unreadMessageCount"))
    message_type = str(
        latest.get("messageType")
        or raw.get("conversationType")
        or raw.get("messageType")
        or ""
    ).upper()
    is_buyer_question = "QUESTION" in message_type or bool(raw.get("isBuyerQuestion"))
    timestamp = _parse_ebay_datetime(
        latest.get("creationDate")
        or raw.get("lastModifiedDate")
        or raw.get("creationDate")
    )
    waiting_on_seller = unread or bool(raw.get("awaitingSellerResponse"))
    return {
        "conversation_id": conversation_id,
        "item_id": listing_id,
        "listing_id": listing_id,
        "title": subject,
        "subject": subject,
        "unread": unread,
        "is_buyer_question": is_buyer_question,
        "waiting_on_seller": waiting_on_seller,
        "timestamp": timestamp,
    }


def summarize_messages(
    conversations: list[dict[str, Any]], *, now: datetime | None = None
) -> dict[str, Any]:
    """Aggregate conversation metadata into counts."""
    now = now or datetime.now(timezone.utc)
    by_id = {
        convo["conversation_id"]: convo
        for convo in conversations
        if convo.get("conversation_id")
    }
    unread = sum(1 for c in by_id.values() if c.get("unread"))
    questions = sum(
        1
        for c in by_id.values()
        if c.get("is_buyer_question") and c.get("waiting_on_seller")
    )
    oldest_hours = None
    for convo in by_id.values():
        if not convo.get("waiting_on_seller"):
            continue
        stamp = convo.get("timestamp")
        if not isinstance(stamp, datetime):
            continue
        hours = max(0.0, (now - stamp).total_seconds() / 3600.0)
        if oldest_hours is None or hours > oldest_hours:
            oldest_hours = round(hours, 1)
    return {
        "unread_count": unread,
        "buyer_question_count": questions,
        "oldest_unanswered_hours": oldest_hours,
        "by_id": by_id,
    }


def orders_for_summary_attrs(orders_section: dict[str, Any]) -> dict[str, Any]:
    """Return order counts plus sorted IDs for diagnostics-safe attributes."""
    return {
        **{k: v for k, v in orders_section.items() if k != "by_id"},
        "order_ids": sorted((orders_section.get("by_id") or {})),
    }
