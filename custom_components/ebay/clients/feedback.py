"""Feedback API client mixin."""

from __future__ import annotations

from typing import Any

import aiohttp

from ..models import ApiFailure
from ..parsers.trading_builders import GET_USER_CALL_NAME, build_get_user_xml
from ..parsers.xml import _WARNING_MESSAGE_MAX_LEN, _sanitize_warning_text, _text
from ..seller_ops import (
    FEEDBACK_ITEMS_MAX_PAGES,
    FEEDBACK_ITEMS_PAGE_LIMIT,
    FEEDBACK_RECENT_IDS_MAX,
    empty_seller_ops,
    parse_awaiting_feedback_count,
    parse_feedback_items,
    parse_feedback_summary,
)
from .errors import EbayAuthError, EbayError, EbayPartialFailure


class FeedbackClientMixin:
    """Feedback summary/items/awaiting fetchers."""

    _endpoints: Any
    _feedback_user_id: str | None

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
