"""Domain REST API client mixin (Analytics, Fulfillment, Standards, Feedback, Messages, Account, Finances)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import logging
from typing import Any
from zoneinfo import ZoneInfo

import aiohttp

from ..marketplace import capability_supported
from ..models import ApiFailure
from ..parsers.trading_builders import GET_USER_CALL_NAME, build_get_user_xml
from ..parsers.trading_items import analytics_views_by_item_id
from ..parsers.xml import (
    _WARNING_MESSAGE_MAX_LEN,
    _safe_exception_context,
    _sanitize_warning_text,
    _text,
    chunk_item_ids,
)
from ..seller_ops import (
    CONVERSATIONS_MAX_PAGES,
    CONVERSATIONS_PAGE_LIMIT,
    DISPUTES_MAX_PAGES,
    DISPUTES_PAGE_LIMIT,
    FEEDBACK_ITEMS_MAX_PAGES,
    FEEDBACK_ITEMS_PAGE_LIMIT,
    FEEDBACK_RECENT_IDS_MAX,
    ORDERS_LOOKBACK_DAYS,
    ORDERS_MAX_PAGES,
    ORDERS_PAGE_LIMIT,
    empty_seller_ops,
    encode_creation_date_filter,
    encode_fulfillment_status_filter,
    finances_date_range_filter,
    marketplace_id_for_site,
    merge_failed_payout_stats,
    normalize_conversation,
    normalize_dispute,
    normalize_order,
    parse_account_privileges,
    parse_awaiting_feedback_count,
    parse_customer_service_metric,
    parse_feedback_items,
    parse_feedback_summary,
    parse_last_payout,
    parse_payout_status_page,
    parse_payout_summary,
    parse_seller_funds_summary,
    parse_seller_standards_profiles,
    parse_transaction_summary,
    summarize_disputes,
    summarize_messages,
    summarize_orders,
    utc_day_bounds,
    utc_month_bounds,
    utc_rolling_days_bounds,
)
from .errors import EbayAuthError, EbayError, EbayPartialFailure

_LOGGER = logging.getLogger(__name__)

_ANALYTICS_TZ = ZoneInfo("America/Los_Angeles")

_FAILED_PAYOUT_STATUS_FILTERS = (
    ("RETRYABLE_FAILED", "failed"),
    ("TERMINAL_FAILED", "failed"),
    ("REVERSED", "returned"),
)


def analytics_date_range_filter(*, now: datetime | None = None, days: int = 30) -> str:
    """Build a Pacific calendar date_range filter for traffic_report."""
    end = (now or datetime.now(_ANALYTICS_TZ)).astimezone(_ANALYTICS_TZ)
    start = end - timedelta(days=days)
    return f"date_range:[{start:%Y%m%d}..{end:%Y%m%d}]"


class RestApisClientMixin:
    """Domain REST API fetchers for Analytics, Fulfillment, Standards, Feedback, Messages, Account, and Finances."""

    _session: aiohttp.ClientSession
    _endpoints: Any
    site_id: str
    _feedback_user_id: str | None

    # ------------------------------------------------------------------
    # Sell Analytics
    # ------------------------------------------------------------------

    async def async_get_traffic_report(self, item_ids: list[str]) -> dict[str, Any]:
        """Fetch optional Sell Analytics traffic report for one ID batch."""
        if not item_ids:
            return {}
        listing_ids = "{" + "|".join(item_ids) + "}"
        return await self._async_rest_get(
            self._endpoints.analytics,
            params={
                "dimension": "LISTING",
                "filter": f"listing_ids:{listing_ids},{analytics_date_range_filter()}",
                "metric": "LISTING_VIEWS_TOTAL",
                "sort": "LISTING_VIEWS_TOTAL",
            },
            partial_category="analytics_views",
            operation="analytics.traffic_report",
        )

    async def async_fetch_analytics_views(
        self, item_ids: list[str]
    ) -> tuple[dict[str, int], bool]:
        """Fetch 30-day Analytics views in batches; merge successes on partial failure."""
        if not item_ids:
            return {}, False
        merged: dict[str, int] = {}
        partial_failure = False
        batches = chunk_item_ids(item_ids)
        for batch_number, batch in enumerate(batches, start=1):
            _LOGGER.debug(
                "Fetching Sell Analytics batch batch=%s item_count=%s",
                batch_number,
                len(batch),
            )
            try:
                payload = await self.async_get_traffic_report(batch)
            except EbayAuthError:
                raise
            except (EbayError, aiohttp.ClientError, TimeoutError) as exc:
                _LOGGER.debug(
                    "Sell Analytics batch failed batch=%s item_count=%s error=%s",
                    batch_number,
                    len(batch),
                    _safe_exception_context(exc),
                )
                partial_failure = True
                continue
            batch_views = analytics_views_by_item_id(payload)
            merged.update(batch_views)
            _LOGGER.debug(
                "Sell Analytics batch completed batch=%s item_count=%s view_count=%s",
                batch_number,
                len(batch),
                len(batch_views),
            )
        return merged, partial_failure

    # ------------------------------------------------------------------
    # Sell Fulfillment (Orders & Payment Disputes)
    # ------------------------------------------------------------------

    async def async_fetch_orders(
        self,
    ) -> tuple[dict[str, Any], bool]:
        """Fetch open and recent orders; return summary and truncation flag."""
        now = datetime.now(timezone.utc)
        start = now - timedelta(days=ORDERS_LOOKBACK_DAYS)
        filters = [
            encode_fulfillment_status_filter(["NOT_STARTED", "IN_PROGRESS"]),
            encode_creation_date_filter(start, now),
        ]
        merged: dict[str, dict[str, Any]] = {}
        truncated = False
        for filter_value in filters:
            offset = 0
            page = 0
            while page < ORDERS_MAX_PAGES:
                page += 1
                payload = await self._async_rest_get(
                    f"{self._endpoints.fulfillment}/order",
                    params={
                        "filter": filter_value,
                        "limit": str(ORDERS_PAGE_LIMIT),
                        "offset": str(offset),
                    },
                    partial_category="orders",
                )
                orders = payload.get("orders") or []
                for raw in orders:
                    normalized = normalize_order(raw, now=now)
                    if normalized.get("order_id"):
                        merged[normalized["order_id"]] = normalized
                total = payload.get("total")
                offset += ORDERS_PAGE_LIMIT
                if not orders:
                    break
                if total is not None:
                    try:
                        if offset >= int(total):
                            break
                    except (TypeError, ValueError):
                        pass
                elif len(orders) < ORDERS_PAGE_LIMIT:
                    break
            else:
                truncated = True
        return summarize_orders(list(merged.values())), truncated

    async def async_fetch_payment_disputes(self) -> tuple[dict[str, Any], bool]:
        """Fetch open payment dispute summaries."""
        merged: dict[str, dict[str, Any]] = {}
        truncated = False
        offset = 0
        for page in range(DISPUTES_MAX_PAGES):
            payload = await self._async_rest_get(
                f"{self._endpoints.payment_disputes}/payment_dispute_summary",
                params=[
                    ("limit", str(DISPUTES_PAGE_LIMIT)),
                    ("offset", str(offset)),
                    ("payment_dispute_status", "OPEN"),
                    ("payment_dispute_status", "ACTION_NEEDED"),
                ],
                partial_category="payment_disputes",
            )
            summaries = payload.get("paymentDisputeSummaries") or []
            for raw in summaries:
                normalized = normalize_dispute(raw)
                if normalized.get("dispute_id"):
                    status = str(normalized.get("status") or "").upper()
                    if status in {"OPEN", "ACTION_NEEDED", "WAITING_SELLER"}:
                        merged[normalized["dispute_id"]] = normalized
            offset += DISPUTES_PAGE_LIMIT
            total = payload.get("total")
            if not summaries:
                break
            if total is not None:
                try:
                    if offset >= int(total):
                        break
                except (TypeError, ValueError):
                    pass
            elif len(summaries) < DISPUTES_PAGE_LIMIT:
                break
            if page == DISPUTES_MAX_PAGES - 1:
                truncated = True
        return summarize_disputes(list(merged.values())), truncated

    # ------------------------------------------------------------------
    # Seller Standards & Customer Service Metrics
    # ------------------------------------------------------------------

    async def async_fetch_seller_standards(self) -> dict[str, Any]:
        """Fetch seller standards profile plus INR/INAD customer-service metrics."""
        standards = empty_seller_ops()["standards"]
        profiles = await self._async_rest_get(
            f"{self._endpoints.analytics_base}/seller_standards_profile",
            partial_category="seller_standards",
            allow_404=True,
        )
        parsed = parse_seller_standards_profiles(profiles)
        standards.update(parsed)

        marketplace = marketplace_id_for_site(self.site_id)
        if marketplace is None or not capability_supported(
            self.site_id, "customer_service_metrics"
        ):
            _LOGGER.debug(
                "Skipping seller standards marketplace metrics for site_id=%s",
                self.site_id,
            )
            return standards
        for metric_type, rating_key, flag_key in (
            (
                "ITEM_NOT_RECEIVED",
                "item_not_received_rating",
                "item_not_received_above_benchmark",
            ),
            (
                "ITEM_NOT_AS_DESCRIBED",
                "item_not_as_described_rating",
                "item_not_as_described_above_benchmark",
            ),
        ):
            try:
                metric_payload = await self._async_rest_get(
                    (
                        f"{self._endpoints.analytics_base}/customer_service_metric/"
                        f"{metric_type}/CURRENT"
                    ),
                    params={"evaluation_marketplace_id": marketplace},
                    partial_category="seller_standards",
                    allow_404=True,
                )
            except EbayPartialFailure:
                continue
            metric = parse_customer_service_metric(metric_payload)
            standards[rating_key] = metric.get("rating")
            standards[flag_key] = bool(metric.get("above_peer_benchmark"))
            if metric.get("above_peer_benchmark"):
                standards["at_risk"] = True
        return standards

    # ------------------------------------------------------------------
    # Commerce Feedback
    # ------------------------------------------------------------------

    async def async_get_feedback_user_id(self) -> str:
        """Return the authenticated account username for Feedback API user_id."""
        if self._feedback_user_id:
            return self._feedback_user_id
        try:
            root = await self.async_call_trading_api(
                GET_USER_CALL_NAME, build_get_user_xml()
            )
        except EbayAuthError:
            raise
        except (EbayError, aiohttp.ClientError, TimeoutError) as exc:
            failure = ApiFailure(
                operation="trading.get_user",
                endpoint_key=GET_USER_CALL_NAME,
                http_status=None,
                ebay_error_id=None,
                ebay_domain=None,
                ebay_category=None,
                message=_sanitize_warning_text(str(exc)[:_WARNING_MESSAGE_MAX_LEN]),
                classification=(
                    "transient"
                    if isinstance(exc, (aiohttp.ClientError, TimeoutError))
                    else "unknown"
                ),
                mapped_category="feedback_user_lookup",
            )
            self._raise_partial_failure("feedback_user_lookup", failure=failure)
        # Feedback requires the public eBay username; Trading UserID is that value.
        user_id = _text(root, "e:User/e:UserID")
        if not user_id:
            failure = ApiFailure(
                operation="trading.get_user",
                endpoint_key=GET_USER_CALL_NAME,
                http_status=None,
                ebay_error_id=None,
                ebay_domain=None,
                ebay_category=None,
                message="missing_user_id",
                classification="malformed_request",
                mapped_category="feedback_user_lookup",
            )
            self._raise_partial_failure("feedback_user_lookup", failure=failure)
        self._feedback_user_id = user_id
        return self._feedback_user_id

    async def async_fetch_feedback(self) -> dict[str, Any]:
        """Fetch feedback summary, recent ids for events, and awaiting count.

        Rating-summary distribution remains authoritative for recent_* counts.
        Item pages are paginated only to collect bounded feedback ids for
        event detection; item page counts never overwrite summary totals.
        """
        feedback = empty_seller_ops()["feedback"]
        ebay_user_id = await self.async_get_feedback_user_id()
        summary_payload = await self._async_rest_get(
            f"{self._endpoints.feedback}/feedback_rating_summary",
            params={
                "user_id": ebay_user_id,
                "filter": "ratingType:OVERALL_EXPERIENCE,lookbackPeriodInDays:30",
            },
            partial_category="feedback",
            allow_404=True,
        )
        feedback.update(parse_feedback_summary(summary_payload))

        try:
            recent_ids: list[str] = []
            negative_ids: list[str] = []
            items_truncated = False
            offset = 0
            for page in range(FEEDBACK_ITEMS_MAX_PAGES):
                items_payload = await self._async_rest_get(
                    f"{self._endpoints.feedback}/feedback",
                    params={
                        "user_id": ebay_user_id,
                        "feedback_type": "FEEDBACK_RECEIVED",
                        "filter": "period:30,role:SELLER",
                        "limit": str(FEEDBACK_ITEMS_PAGE_LIMIT),
                        "offset": str(offset),
                    },
                    partial_category="feedback",
                    allow_404=True,
                )
                parsed = parse_feedback_items(items_payload)
                recent_ids.extend(parsed["ids"])
                negative_ids.extend(parsed["negative_ids"])
                items = (
                    items_payload.get("feedbackEntries")
                    or items_payload.get("feedbackItems")
                    or items_payload.get("feedback")
                    or []
                )
                offset += FEEDBACK_ITEMS_PAGE_LIMIT
                total = items_payload.get("total")
                if not items:
                    break
                if total is not None:
                    try:
                        if offset >= int(total):
                            break
                    except (TypeError, ValueError):
                        pass
                elif len(items) < FEEDBACK_ITEMS_PAGE_LIMIT:
                    break
                if page == FEEDBACK_ITEMS_MAX_PAGES - 1:
                    items_truncated = True
            if len(recent_ids) > FEEDBACK_RECENT_IDS_MAX:
                items_truncated = True
            bounded_ids = recent_ids[:FEEDBACK_RECENT_IDS_MAX]
            bounded_id_set = set(bounded_ids)
            feedback["recent_feedback_ids"] = bounded_ids
            feedback["recent_negative_feedback_ids"] = [
                feedback_id
                for feedback_id in negative_ids
                if feedback_id in bounded_id_set
            ][:FEEDBACK_RECENT_IDS_MAX]
            feedback["items_truncated"] = items_truncated
        except EbayPartialFailure:
            pass

        try:
            # Awaiting feedback is scoped to the OAuth user; no user_id param.
            awaiting_payload = await self._async_rest_get(
                f"{self._endpoints.feedback}/awaiting_feedback",
                params={"limit": "50", "offset": "0"},
                partial_category="feedback",
                allow_404=True,
            )
            feedback["awaiting_count"] = parse_awaiting_feedback_count(awaiting_payload)
        except EbayPartialFailure:
            pass
        return feedback

    # ------------------------------------------------------------------
    # Commerce Messages
    # ------------------------------------------------------------------

    async def async_fetch_messages(self) -> tuple[dict[str, Any], bool]:
        """Fetch member conversation metadata aggregates."""
        merged: dict[str, dict[str, Any]] = {}
        truncated = False
        now = datetime.now(timezone.utc)
        offset = 0
        for page in range(CONVERSATIONS_MAX_PAGES):
            payload = await self._async_rest_get(
                f"{self._endpoints.messages}/conversation",
                params={
                    "conversation_type": "FROM_MEMBERS",
                    "limit": str(CONVERSATIONS_PAGE_LIMIT),
                    "offset": str(offset),
                },
                partial_category="messages",
            )
            conversations = payload.get("conversations") or []
            for raw in conversations:
                normalized = normalize_conversation(raw)
                if normalized.get("conversation_id"):
                    merged[normalized["conversation_id"]] = normalized
            offset += CONVERSATIONS_PAGE_LIMIT
            total = payload.get("total")
            if not conversations:
                break
            if total is not None:
                try:
                    if offset >= int(total):
                        break
                except (TypeError, ValueError):
                    pass
            elif len(conversations) < CONVERSATIONS_PAGE_LIMIT:
                break
            if page == CONVERSATIONS_MAX_PAGES - 1:
                truncated = True
        return summarize_messages(list(merged.values()), now=now), truncated

    # ------------------------------------------------------------------
    # Sell Account Privileges
    # ------------------------------------------------------------------

    async def async_fetch_account_privileges(self) -> dict[str, Any]:
        """Fetch Sell Account privileges (selling limits / registration)."""
        privileges = empty_seller_ops()["account_privileges"]
        payload = await self._async_rest_get(
            f"{self._endpoints.account}/privilege",
            partial_category="account_privileges",
            allow_404=True,
        )
        privileges.update(parse_account_privileges(payload))
        return privileges

    # ------------------------------------------------------------------
    # Sell Finances
    # ------------------------------------------------------------------

    async def async_fetch_finances(self) -> dict[str, Any]:
        """Fetch seller funds, payout health, and period summaries.

        Some marketplaces require digital request signing for Finances; those
        are capability-gated before this method is called. Subcalls soft-fail
        so funds summary remains useful when payout/transaction endpoints fail.
        """
        finances = empty_seller_ops()["finances"]
        funds_payload = await self._async_rest_get(
            f"{self._endpoints.finances}/seller_funds_summary",
            partial_category="finances",
            allow_404=True,
        )
        finances.update(parse_seller_funds_summary(funds_payload))

        try:
            payout_payload = await self._async_rest_get(
                f"{self._endpoints.finances}/payout",
                params={"limit": "1"},
                partial_category="finances",
                allow_404=True,
            )
            finances.update(parse_last_payout(payout_payload))
        except EbayPartialFailure:
            pass

        await self._async_fetch_failed_payouts(finances)
        await self._async_fetch_period_summaries(finances)
        return finances

    async def _async_fetch_failed_payouts(self, finances: dict[str, Any]) -> None:
        """Soft-fetch failed/returned payout counts and newest failure date."""
        parts: list[dict[str, Any]] = []
        for status, kind in _FAILED_PAYOUT_STATUS_FILTERS:
            try:
                payload = await self._async_rest_get(
                    f"{self._endpoints.finances}/payout",
                    params={
                        "limit": "20",
                        "filter": f"payoutStatus:{{{status}}}",
                    },
                    partial_category="finances",
                    allow_404=True,
                )
                parts.append(parse_payout_status_page(payload, status_kind=kind))
            except EbayPartialFailure:
                continue
        if parts:
            finances.update(merge_failed_payout_stats(*parts))

    async def _async_fetch_period_summaries(self, finances: dict[str, Any]) -> None:
        """Soft-fetch rolling/day/month transaction and payout summaries."""
        now = datetime.now(timezone.utc)
        windows = (
            ("recent", *utc_rolling_days_bounds(30, now)),
            ("daily", *utc_day_bounds(now)),
            ("monthly", *utc_month_bounds(now)),
        )
        for prefix, start, end in windows:
            tx_filter = "transactionStatus:{PAYOUT}," + finances_date_range_filter(
                "transactionDate", start, end
            )
            try:
                payload = await self._async_rest_get(
                    f"{self._endpoints.finances}/transaction_summary",
                    params={"filter": tx_filter},
                    partial_category="finances",
                    allow_404=True,
                )
                parsed = parse_transaction_summary(payload)
                finances[f"{prefix}_fees"] = parsed.get("fees")
                finances[f"{prefix}_refunds"] = parsed.get("refunds")
                finances[f"{prefix}_net"] = parsed.get("net")
                if finances.get("funds_currency") is None and parsed.get("currency"):
                    finances["funds_currency"] = parsed["currency"]
            except EbayPartialFailure:
                pass

            if prefix == "recent":
                continue
            payout_filter = finances_date_range_filter("payoutDate", start, end)
            try:
                payload = await self._async_rest_get(
                    f"{self._endpoints.finances}/payout_summary",
                    params={"filter": payout_filter},
                    partial_category="finances",
                    allow_404=True,
                )
                parsed = parse_payout_summary(payload)
                finances[f"{prefix}_payout_total"] = parsed.get("payout_total")
                if finances.get("funds_currency") is None and parsed.get("currency"):
                    finances["funds_currency"] = parsed["currency"]
            except EbayPartialFailure:
                pass
