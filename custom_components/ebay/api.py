"""Read-only eBay API client facade.

Public imports stay at ``custom_components.ebay.api``. Implementation is split
across ``clients/`` (HTTP mixins) and ``parsers/`` (pure helpers).
"""

from __future__ import annotations

from datetime import datetime, timezone
import logging
import time
from typing import Any

import aiohttp

from .clients.account import AccountClientMixin
from .clients.analytics import AnalyticsClientMixin
from .clients.endpoints import (
    EbayEndpoints,
    build_consent_url,
    endpoints_for,
    extract_authorization_code,
    extract_oauth_callback_params,
)
from .clients.errors import (
    API_WARNINGS_STATE_ATTR_MAX,
    EbayApiError,
    EbayAuthError,
    EbayAuthTransientError,
    EbayError,
    EbayParseError,
    EbayPartialFailure,
    _error_text_blob,
    _is_eligibility_or_permission_error,
    _is_unsupported_error,
    _safe_ebay_error_payload,
    build_api_failure,
    build_partial_failure_detail,
    classify_rest_status,
    endpoint_key_from_url,
    map_rest_failure_category,
    merge_partial_failure_details,
    sanitize_ebay_rest_errors,
)
from .clients.feedback import FeedbackClientMixin
from .clients.finances import FinancesClientMixin
from .clients.fulfillment import FulfillmentClientMixin
from .clients.http import TOKEN_REFRESH_MARGIN, RestClientMixin
from .clients.messages import MessagesClientMixin
from .clients.scopes import (
    ALL_OPTIONAL_SCOPES,
    CORE_SCOPE,
    DEFAULT_SCOPE,
    MODULE_SCOPES,
    SCOPE_ACCOUNT_READONLY,
    SCOPE_ANALYTICS_READONLY,
    SCOPE_FEEDBACK,
    SCOPE_FINANCES,
    SCOPE_FULFILLMENT_READONLY,
    SCOPE_MESSAGE,
    SCOPE_PAYMENT_DISPUTE,
    has_core_scope,
    join_scopes,
    missing_scopes_for_options,
    parse_scope_set,
    resolve_granted_scopes,
    scopes_for_options,
)
from .clients.seller_standards import SellerStandardsClientMixin
from .clients.trading import TradingClientMixin
from .const import (
    CONF_ACCOUNT_PRIVILEGES_ENABLED,
    CONF_ANALYTICS_ENABLED,
    CONF_BUYING_ENABLED,
    CONF_FEEDBACK_ENABLED,
    CONF_FINANCES_ENABLED,
    CONF_FULFILLMENT_ENABLED,
    CONF_MESSAGES_ENABLED,
    CONF_SELLER_STANDARDS_ENABLED,
    CONF_SELLING_ENABLED,
    EBAY_XML_NS,
    SECTION_ACCOUNT_PRIVILEGES,
    SECTION_ANALYTICS,
    SECTION_BUYING,
    SECTION_FEEDBACK,
    SECTION_FINANCES,
    SECTION_FULFILLMENT,
    SECTION_MESSAGES,
    SECTION_SELLER_STANDARDS,
    SECTION_SELLING,
)
from .marketplace import accept_language_for_site, capability_supported
from .models import ApiFailure
from .parsers.payload import _dict_by_item_id, summarize_payload
from .parsers.trading_builders import (
    BEST_OFFERS_CALL_NAME,
    BEST_OFFERS_ENTRIES_PER_PAGE,
    BEST_OFFERS_MAX_PAGES,
    GET_USER_CALL_NAME,
    READ_ONLY_CALL_NAME,
    SELLER_LIST_CALL_NAME,
    SELLING_CALL_NAME,
    SOLD_UNSOLD_DURATION_DAYS,
    SOLD_UNSOLD_REFRESH_HOURS,
    _parse_optional_datetime,
    _should_fetch_sold_unsold,
    build_get_best_offers_xml,
    build_get_my_ebay_buying_xml,
    build_get_my_ebay_selling_classification_xml,
    build_get_my_ebay_selling_list_xml,
    build_get_my_ebay_selling_xml,
    build_get_seller_list_xml,
    build_get_user_xml,
    parse_selling_list_item_ids,
    parse_sold_unsold_classification,
)
from .parsers.trading_items import (
    active_offers_by_item_id,
    analytics_views_by_item_id,
    pagination_total_pages,
    parse_buying_item,
    parse_container,
    parse_selling_container,
    parse_selling_item,
    seller_list_views_by_item_id,
)
from .parsers.xml import (
    ANALYTICS_BATCH_SIZE,
    MAX_PAGES,
    _duration_ms,
    _safe_exception_context,
    _to_bool,
    _to_float,
    _to_int,
    _total_pages,
    chunk_item_ids,
    dedupe_trading_warnings,
    ebay_time,
    extract_trading_warnings,
    format_seconds,
    parse_ebay_time,
    seconds_until,
)
from .seller_ops import empty_seller_ops

__all__ = [
    "ALL_OPTIONAL_SCOPES",
    "ANALYTICS_BATCH_SIZE",
    "API_WARNINGS_STATE_ATTR_MAX",
    "BEST_OFFERS_CALL_NAME",
    "BEST_OFFERS_ENTRIES_PER_PAGE",
    "BEST_OFFERS_MAX_PAGES",
    "CONF_ACCOUNT_PRIVILEGES_ENABLED",
    "CONF_ANALYTICS_ENABLED",
    "CONF_BUYING_ENABLED",
    "CONF_FEEDBACK_ENABLED",
    "CONF_FINANCES_ENABLED",
    "CONF_FULFILLMENT_ENABLED",
    "CONF_MESSAGES_ENABLED",
    "CONF_SELLER_STANDARDS_ENABLED",
    "CONF_SELLING_ENABLED",
    "CORE_SCOPE",
    "DEFAULT_SCOPE",
    "EBAY_XML_NS",
    "EbayApiClient",
    "EbayApiError",
    "EbayAuthError",
    "EbayAuthTransientError",
    "EbayEndpoints",
    "EbayError",
    "EbayParseError",
    "EbayPartialFailure",
    "GET_USER_CALL_NAME",
    "MAX_PAGES",
    "MODULE_SCOPES",
    "PARTIAL_FAILURE_CATEGORIES_BY_SECTION",
    "READ_ONLY_CALL_NAME",
    "SCOPE_ACCOUNT_READONLY",
    "SCOPE_ANALYTICS_READONLY",
    "SCOPE_FEEDBACK",
    "SCOPE_FINANCES",
    "SCOPE_FULFILLMENT_READONLY",
    "SCOPE_MESSAGE",
    "SCOPE_PAYMENT_DISPUTE",
    "SECTION_ACCOUNT_PRIVILEGES",
    "SECTION_ANALYTICS",
    "SECTION_BUYING",
    "SECTION_FEEDBACK",
    "SECTION_FINANCES",
    "SECTION_FULFILLMENT",
    "SECTION_MESSAGES",
    "SECTION_SELLER_STANDARDS",
    "SECTION_SELLING",
    "SELLER_LIST_CALL_NAME",
    "SELLING_CALL_NAME",
    "SOLD_UNSOLD_DURATION_DAYS",
    "SOLD_UNSOLD_REFRESH_HOURS",
    "TOKEN_REFRESH_MARGIN",
    "TRUNCATED_KEYS_BY_SECTION",
    "_EMPTY_TRUNCATED_COLLECTIONS",
    "_copy_seller_ops",
    "_dict_by_item_id",
    "_duration_ms",
    "_error_text_blob",
    "_is_eligibility_or_permission_error",
    "_is_unsupported_error",
    "_parse_optional_datetime",
    "_parse_section_dt_map",
    "_payload_shell",
    "_safe_ebay_error_payload",
    "_safe_exception_context",
    "_seller_ops_summary_keys",
    "_should_fetch_sold_unsold",
    "_stamp_section_attempt",
    "_to_bool",
    "_to_float",
    "_to_int",
    "_total_pages",
    "active_offers_by_item_id",
    "analytics_views_by_item_id",
    "build_api_failure",
    "build_consent_url",
    "build_get_best_offers_xml",
    "build_get_my_ebay_buying_xml",
    "build_get_my_ebay_selling_classification_xml",
    "build_get_my_ebay_selling_list_xml",
    "build_get_my_ebay_selling_xml",
    "build_get_seller_list_xml",
    "build_get_user_xml",
    "build_partial_failure_detail",
    "chunk_item_ids",
    "classify_rest_status",
    "dedupe_trading_warnings",
    "ebay_time",
    "enabled_sections_for_options",
    "endpoint_key_from_url",
    "endpoints_for",
    "extract_authorization_code",
    "extract_oauth_callback_params",
    "extract_trading_warnings",
    "format_seconds",
    "has_core_scope",
    "join_scopes",
    "map_rest_failure_category",
    "merge_partial_failure_details",
    "missing_scopes_for_options",
    "pagination_total_pages",
    "parse_buying_item",
    "parse_container",
    "parse_ebay_time",
    "parse_scope_set",
    "parse_selling_container",
    "parse_selling_item",
    "parse_selling_list_item_ids",
    "parse_sold_unsold_classification",
    "resolve_granted_scopes",
    "sanitize_ebay_rest_errors",
    "scopes_for_options",
    "seconds_until",
    "seller_list_views_by_item_id",
    "summarize_payload",
]

_LOGGER = logging.getLogger(__name__)


class EbayApiClient(
    RestClientMixin,
    TradingClientMixin,
    AnalyticsClientMixin,
    FulfillmentClientMixin,
    SellerStandardsClientMixin,
    FeedbackClientMixin,
    MessagesClientMixin,
    AccountClientMixin,
    FinancesClientMixin,
):
    """Async read-only eBay API client."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        *,
        environment: str,
        client_id: str,
        client_secret: str,
        runame: str,
        refresh_token: str | None,
        site_id: str,
        oauth_session: Any | None = None,
    ) -> None:
        self._session = session
        self._oauth_session = oauth_session
        self.environment = environment
        self.client_id = client_id
        self._client_secret = client_secret
        self.runame = runame
        self.refresh_token = refresh_token
        self.site_id = site_id
        self._endpoints = endpoints_for(environment)
        self._access_token: str | None = None
        self._access_token_expires_at: datetime | None = None
        # Cached authenticated eBay username for Feedback API user_id params.
        self._feedback_user_id: str | None = None
        self._api_warnings: list[dict[str, str | None]] = []
        self._partial_failure_details: list[dict[str, Any]] = []
        self._last_api_failure: ApiFailure | None = None
        self._accept_language = accept_language_for_site(site_id)

    async def async_fetch_data(
        self,
        *,
        buying_enabled: bool,
        selling_enabled: bool,
        analytics_enabled: bool,
        fulfillment_enabled: bool = False,
        seller_standards_enabled: bool = False,
        feedback_enabled: bool = False,
        messages_enabled: bool = False,
        account_privileges_enabled: bool = False,
        finances_enabled: bool = False,
        ending_soon_threshold_seconds: int,
        granted_scopes: str | None = None,
        sections: set[str] | None = None,
        previous: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Fetch and normalize configured read-only telemetry.

        When ``sections`` is None, all enabled sections are fetched (full refresh).
        Otherwise only the requested sections are refreshed and merged into
        ``previous`` (or an empty shell).
        """
        enabled_sections = enabled_sections_for_options(
            buying_enabled=buying_enabled,
            selling_enabled=selling_enabled,
            analytics_enabled=analytics_enabled,
            fulfillment_enabled=fulfillment_enabled,
            seller_standards_enabled=seller_standards_enabled,
            feedback_enabled=feedback_enabled,
            messages_enabled=messages_enabled,
            account_privileges_enabled=account_privileges_enabled,
            finances_enabled=finances_enabled,
        )
        due = enabled_sections if sections is None else (sections & enabled_sections)
        return await self.async_fetch_sections(
            due,
            previous=previous,
            buying_enabled=buying_enabled,
            selling_enabled=selling_enabled,
            analytics_enabled=analytics_enabled,
            fulfillment_enabled=fulfillment_enabled,
            seller_standards_enabled=seller_standards_enabled,
            feedback_enabled=feedback_enabled,
            messages_enabled=messages_enabled,
            account_privileges_enabled=account_privileges_enabled,
            finances_enabled=finances_enabled,
            ending_soon_threshold_seconds=ending_soon_threshold_seconds,
            granted_scopes=granted_scopes,
        )

    async def async_fetch_sections(
        self,
        sections: set[str],
        *,
        previous: dict[str, Any] | None = None,
        buying_enabled: bool,
        selling_enabled: bool,
        analytics_enabled: bool,
        fulfillment_enabled: bool = False,
        seller_standards_enabled: bool = False,
        feedback_enabled: bool = False,
        messages_enabled: bool = False,
        account_privileges_enabled: bool = False,
        finances_enabled: bool = False,
        ending_soon_threshold_seconds: int,
        granted_scopes: str | None = None,
    ) -> dict[str, Any]:
        """Fetch due sections and merge into a central coordinator payload."""
        refresh_start = time.monotonic()
        _LOGGER.debug(
            "eBay API section refresh started sections=%s buying_enabled=%s selling_enabled=%s analytics_enabled=%s fulfillment_enabled=%s seller_standards_enabled=%s feedback_enabled=%s messages_enabled=%s account_privileges_enabled=%s finances_enabled=%s",
            sorted(sections),
            buying_enabled,
            selling_enabled,
            analytics_enabled,
            fulfillment_enabled,
            seller_standards_enabled,
            feedback_enabled,
            messages_enabled,
            account_privileges_enabled,
            finances_enabled,
        )
        base = _payload_shell(previous)
        watched: dict[str, dict[str, Any]] = dict(base["watched"])
        bidding: dict[str, dict[str, Any]] = dict(base["bidding"])
        selling: dict[str, dict[str, Any]] = {
            item_id: dict(item) for item_id, item in base["selling"].items()
        }
        selling_classification: dict[str, str] = dict(base["selling_classification"])
        seller_ops = _copy_seller_ops(base["seller_ops"])
        section_last_fetched: dict[str, datetime] = dict(
            base.get("section_last_fetched") or {}
        )
        section_last_attempt: dict[str, datetime] = dict(
            base.get("section_last_attempt") or {}
        )
        section_last_success: dict[str, datetime] = dict(
            base.get("section_last_success") or section_last_fetched
        )
        partial_failures = [
            category
            for category in base.get("partial_failures") or []
            if not any(
                category in PARTIAL_FAILURE_CATEGORIES_BY_SECTION.get(section, ())
                for section in sections
            )
            and not (
                category == "trading_warning"
                and (SECTION_BUYING in sections or SECTION_SELLING in sections)
            )
        ]
        truncated_collections = dict(base.get("truncated_collections") or {})
        for section in sections:
            for key in TRUNCATED_KEYS_BY_SECTION.get(section, ()):
                truncated_collections[key] = False

        feature_options = {
            CONF_SELLING_ENABLED: selling_enabled,
            CONF_ANALYTICS_ENABLED: analytics_enabled,
            CONF_FULFILLMENT_ENABLED: fulfillment_enabled,
            CONF_SELLER_STANDARDS_ENABLED: seller_standards_enabled,
            CONF_FEEDBACK_ENABLED: feedback_enabled,
            CONF_MESSAGES_ENABLED: messages_enabled,
            CONF_ACCOUNT_PRIVILEGES_ENABLED: account_privileges_enabled,
            CONF_FINANCES_ENABLED: finances_enabled,
        }
        missing_by_feature = missing_scopes_for_options(granted_scopes, feature_options)
        missing_scope_features = sorted(missing_by_feature)

        trading_sections = SECTION_BUYING in sections or SECTION_SELLING in sections
        if trading_sections:
            self._api_warnings = []
        self._partial_failure_details = []
        previous_last_api_failure = base.get("last_api_failure")
        self._last_api_failure = None

        sold_items_count = base["summary"].get("sold_items_count")
        unsold_items_count = base["summary"].get("unsold_items_count")
        sold_unsold_last_fetched = _parse_optional_datetime(
            base.get("sold_unsold_last_fetched")
            or (base.get("summary") or {}).get("sold_unsold_last_fetched")
        )

        if SECTION_BUYING in sections and buying_enabled:
            (
                watched,
                bidding,
                watched_truncated,
                bidding_truncated,
            ) = await self._fetch_buying_pages()
            truncated_collections["watched"] = watched_truncated
            truncated_collections["bidding"] = bidding_truncated
            if watched_truncated or bidding_truncated:
                partial_failures.append("buying_truncated")
            _stamp_section_attempt(
                SECTION_BUYING,
                success=True,
                section_last_attempt=section_last_attempt,
                section_last_success=section_last_success,
                section_last_fetched=section_last_fetched,
            )

        if SECTION_SELLING in sections and selling_enabled:
            previous_selling = dict(base["selling"])
            selling, selling_truncated = await self._fetch_selling_pages()
            truncated_collections["selling"] = selling_truncated
            if selling_truncated:
                partial_failures.append("selling_truncated")
            fetch_now = datetime.now(timezone.utc)
            if _should_fetch_sold_unsold(
                previous_selling,
                selling,
                sold_unsold_last_fetched,
                now=fetch_now,
            ):
                try:
                    (
                        selling_classification,
                        sold_items_count,
                        unsold_items_count,
                        sold_unsold_truncated,
                    ) = await self.async_fetch_sold_unsold_classification()
                    truncated_collections["sold_unsold"] = sold_unsold_truncated
                    if sold_unsold_truncated:
                        partial_failures.append("sold_unsold_truncated")
                    sold_unsold_last_fetched = fetch_now
                except EbayAuthError:
                    raise
                except (EbayError, aiohttp.ClientError, TimeoutError) as exc:
                    _LOGGER.debug(
                        "Optional sold/unsold classification failed error=%s",
                        _safe_exception_context(exc),
                    )
                    partial_failures.append("sold_unsold_classification")
                    selling_classification = {}
                    sold_items_count = None
                    unsold_items_count = None
                    truncated_collections["sold_unsold"] = True
            try:
                (
                    seller_list_views,
                    seller_list_views_truncated,
                ) = await self._fetch_seller_list_views()
                for item_id, views in seller_list_views.items():
                    if item_id in selling:
                        selling[item_id]["views"] = views
                truncated_collections["seller_list_views"] = seller_list_views_truncated
                if seller_list_views_truncated:
                    partial_failures.append("seller_list_views_truncated")
            except (EbayError, aiohttp.ClientError, TimeoutError) as exc:
                _LOGGER.debug(
                    "Optional GetSellerList views failed error=%s",
                    _safe_exception_context(exc),
                )
                partial_failures.append("seller_list_views")

            try:
                page = 1
                total_pages = 1
                active_offer_counts: dict[str, int] = {}
                while page <= min(total_pages, BEST_OFFERS_MAX_PAGES):
                    offers = await self.async_call_trading_api(
                        BEST_OFFERS_CALL_NAME, build_get_best_offers_xml(page=page)
                    )
                    total_pages = pagination_total_pages(offers)
                    page_offers = active_offers_by_item_id(offers)
                    for item_id, count in page_offers.items():
                        if item_id in selling:
                            active_offer_counts[item_id] = (
                                active_offer_counts.get(item_id, 0) + count
                            )
                    _LOGGER.debug(
                        "Fetched active offers page=%s offer_item_count=%s total_pages=%s",
                        page,
                        len(page_offers),
                        total_pages,
                    )
                    page += 1
                for item_id, count in active_offer_counts.items():
                    selling[item_id]["offers"] = count
                active_offers_truncated = total_pages > BEST_OFFERS_MAX_PAGES
                truncated_collections["active_offers"] = active_offers_truncated
                if active_offers_truncated:
                    partial_failures.append("active_offers_truncated")
            except (EbayError, aiohttp.ClientError, TimeoutError) as exc:
                _LOGGER.debug(
                    "Optional GetBestOffers failed error=%s",
                    _safe_exception_context(exc),
                )
                partial_failures.append("active_offers")
            _stamp_section_attempt(
                SECTION_SELLING,
                success=True,
                section_last_attempt=section_last_attempt,
                section_last_success=section_last_success,
                section_last_fetched=section_last_fetched,
            )

        if SECTION_ANALYTICS in sections and analytics_enabled and selling_enabled:
            analytics_ok = True
            if CONF_ANALYTICS_ENABLED in missing_by_feature:
                partial_failures.append("analytics_views_missing_scope")
                analytics_ok = False
            elif not capability_supported(self.site_id, "traffic_report"):
                _LOGGER.debug(
                    "Skipping analytics traffic report; unsupported for site_id=%s",
                    self.site_id,
                )
                partial_failures.append("analytics_views_unsupported")
                analytics_ok = False
            else:
                try:
                    (
                        views_by_id,
                        analytics_partial,
                    ) = await self.async_fetch_analytics_views(list(selling))
                    for item_id, views in views_by_id.items():
                        if item_id in selling:
                            selling[item_id]["analytics_views_30d"] = views
                    if analytics_partial:
                        partial_failures.append("analytics_views")
                        analytics_ok = False
                except EbayAuthError:
                    raise
                except (EbayError, aiohttp.ClientError, TimeoutError) as exc:
                    _LOGGER.debug(
                        "Optional analytics views failed error=%s",
                        _safe_exception_context(exc),
                    )
                    partial_failures.append("analytics_views")
                    analytics_ok = False
            _stamp_section_attempt(
                SECTION_ANALYTICS,
                success=analytics_ok,
                section_last_attempt=section_last_attempt,
                section_last_success=section_last_success,
                section_last_fetched=section_last_fetched,
            )

        if SECTION_FULFILLMENT in sections and fulfillment_enabled:
            fulfillment_ok = True
            if CONF_FULFILLMENT_ENABLED in missing_by_feature:
                partial_failures.append("orders_missing_scope")
                partial_failures.append("payment_disputes_missing_scope")
                fulfillment_ok = False
            else:
                try:
                    orders_summary, orders_truncated = await self.async_fetch_orders()
                    seller_ops["orders"] = orders_summary
                    truncated_collections["orders"] = orders_truncated
                    if orders_truncated:
                        partial_failures.append("orders_truncated")
                except EbayAuthError:
                    raise
                except (EbayError, aiohttp.ClientError, TimeoutError) as exc:
                    _LOGGER.debug(
                        "Optional orders fetch failed error=%s",
                        _safe_exception_context(exc),
                    )
                    partial_failures.append("orders")
                    fulfillment_ok = False
                try:
                    (
                        disputes_summary,
                        disputes_truncated,
                    ) = await self.async_fetch_payment_disputes()
                    seller_ops["disputes"] = disputes_summary
                    truncated_collections["payment_disputes"] = disputes_truncated
                    if disputes_truncated:
                        partial_failures.append("payment_disputes_truncated")
                except EbayAuthError:
                    raise
                except (EbayError, aiohttp.ClientError, TimeoutError) as exc:
                    _LOGGER.debug(
                        "Optional payment disputes fetch failed error=%s",
                        _safe_exception_context(exc),
                    )
                    partial_failures.append("payment_disputes")
                    fulfillment_ok = False
            _stamp_section_attempt(
                SECTION_FULFILLMENT,
                success=fulfillment_ok,
                section_last_attempt=section_last_attempt,
                section_last_success=section_last_success,
                section_last_fetched=section_last_fetched,
            )

        if SECTION_SELLER_STANDARDS in sections and seller_standards_enabled:
            standards_ok = True
            if CONF_SELLER_STANDARDS_ENABLED in missing_by_feature:
                partial_failures.append("seller_standards_missing_scope")
                standards_ok = False
            else:
                try:
                    seller_ops["standards"] = await self.async_fetch_seller_standards()
                except EbayAuthError:
                    raise
                except (EbayError, aiohttp.ClientError, TimeoutError) as exc:
                    _LOGGER.debug(
                        "Optional seller standards fetch failed error=%s",
                        _safe_exception_context(exc),
                    )
                    partial_failures.append("seller_standards")
                    standards_ok = False
            _stamp_section_attempt(
                SECTION_SELLER_STANDARDS,
                success=standards_ok,
                section_last_attempt=section_last_attempt,
                section_last_success=section_last_success,
                section_last_fetched=section_last_fetched,
            )

        if SECTION_FEEDBACK in sections and feedback_enabled:
            feedback_ok = True
            if CONF_FEEDBACK_ENABLED in missing_by_feature:
                partial_failures.append("feedback_missing_scope")
                feedback_ok = False
            else:
                try:
                    seller_ops["feedback"] = await self.async_fetch_feedback()
                except EbayAuthError:
                    raise
                except EbayPartialFailure as exc:
                    failure = exc.category or "feedback"
                    _LOGGER.debug(
                        "Optional feedback fetch failed error=%s",
                        _safe_exception_context(exc),
                    )
                    partial_failures.append(failure)
                    feedback_ok = False
                except (EbayError, aiohttp.ClientError, TimeoutError) as exc:
                    _LOGGER.debug(
                        "Optional feedback fetch failed error=%s",
                        _safe_exception_context(exc),
                    )
                    partial_failures.append("feedback")
                    feedback_ok = False
            _stamp_section_attempt(
                SECTION_FEEDBACK,
                success=feedback_ok,
                section_last_attempt=section_last_attempt,
                section_last_success=section_last_success,
                section_last_fetched=section_last_fetched,
            )

        if SECTION_MESSAGES in sections and messages_enabled:
            messages_ok = True
            if CONF_MESSAGES_ENABLED in missing_by_feature:
                partial_failures.append("messages_missing_scope")
                messages_ok = False
            else:
                try:
                    (
                        messages_summary,
                        messages_truncated,
                    ) = await self.async_fetch_messages()
                    seller_ops["messages"] = messages_summary
                    truncated_collections["messages"] = messages_truncated
                    if messages_truncated:
                        partial_failures.append("messages_truncated")
                except EbayAuthError:
                    raise
                except (EbayError, aiohttp.ClientError, TimeoutError) as exc:
                    _LOGGER.debug(
                        "Optional messages fetch failed error=%s",
                        _safe_exception_context(exc),
                    )
                    partial_failures.append("messages")
                    messages_ok = False
            _stamp_section_attempt(
                SECTION_MESSAGES,
                success=messages_ok,
                section_last_attempt=section_last_attempt,
                section_last_success=section_last_success,
                section_last_fetched=section_last_fetched,
            )

        if SECTION_ACCOUNT_PRIVILEGES in sections and account_privileges_enabled:
            privileges_ok = True
            if CONF_ACCOUNT_PRIVILEGES_ENABLED in missing_by_feature:
                partial_failures.append("account_privileges_missing_scope")
                privileges_ok = False
            elif not capability_supported(self.site_id, "account_privileges"):
                _LOGGER.debug(
                    "Skipping account privileges; unsupported for site_id=%s",
                    self.site_id,
                )
                partial_failures.append("account_privileges_unsupported")
                privileges_ok = False
            else:
                try:
                    seller_ops[
                        "account_privileges"
                    ] = await self.async_fetch_account_privileges()
                except EbayAuthError:
                    raise
                except (EbayError, aiohttp.ClientError, TimeoutError) as exc:
                    _LOGGER.debug(
                        "Optional account privileges fetch failed error=%s",
                        _safe_exception_context(exc),
                    )
                    partial_failures.append("account_privileges")
                    privileges_ok = False
            _stamp_section_attempt(
                SECTION_ACCOUNT_PRIVILEGES,
                success=privileges_ok,
                section_last_attempt=section_last_attempt,
                section_last_success=section_last_success,
                section_last_fetched=section_last_fetched,
            )

        if SECTION_FINANCES in sections and finances_enabled:
            finances_ok = True
            if CONF_FINANCES_ENABLED in missing_by_feature:
                partial_failures.append("finances_missing_scope")
                finances_ok = False
            elif not capability_supported(self.site_id, "finances"):
                # Many marketplaces need digital request signing for Finances;
                # treat those as unsupported rather than attempting unsigned calls.
                _LOGGER.debug(
                    "Skipping finances; unsupported (signing may be required) for site_id=%s",
                    self.site_id,
                )
                partial_failures.append("finances_unsupported")
                finances_ok = False
            else:
                try:
                    seller_ops["finances"] = await self.async_fetch_finances()
                except EbayAuthError:
                    raise
                except (EbayError, aiohttp.ClientError, TimeoutError) as exc:
                    _LOGGER.debug(
                        "Optional finances fetch failed error=%s",
                        _safe_exception_context(exc),
                    )
                    partial_failures.append("finances")
                    finances_ok = False
            _stamp_section_attempt(
                SECTION_FINANCES,
                success=finances_ok,
                section_last_attempt=section_last_attempt,
                section_last_success=section_last_success,
                section_last_fetched=section_last_fetched,
            )

        if trading_sections:
            api_warnings = dedupe_trading_warnings(self._api_warnings)
        else:
            api_warnings = list(base.get("api_warnings") or [])
        if api_warnings and "trading_warning" not in partial_failures:
            partial_failures.append("trading_warning")

        # Keep missing-scope markers accurate even when a section was skipped.
        for feature_key, category in (
            (CONF_ANALYTICS_ENABLED, "analytics_views_missing_scope"),
            (CONF_FULFILLMENT_ENABLED, "orders_missing_scope"),
            (CONF_FULFILLMENT_ENABLED, "payment_disputes_missing_scope"),
            (CONF_SELLER_STANDARDS_ENABLED, "seller_standards_missing_scope"),
            (CONF_FEEDBACK_ENABLED, "feedback_missing_scope"),
            (CONF_MESSAGES_ENABLED, "messages_missing_scope"),
            (CONF_ACCOUNT_PRIVILEGES_ENABLED, "account_privileges_missing_scope"),
            (CONF_FINANCES_ENABLED, "finances_missing_scope"),
        ):
            if feature_key in missing_by_feature:
                if category not in partial_failures:
                    partial_failures.append(category)
            elif category in partial_failures:
                partial_failures = [c for c in partial_failures if c != category]

        partial_failure_details = merge_partial_failure_details(
            base.get("partial_failure_details"),
            self._partial_failure_details,
            partial_failures,
        )
        if self._last_api_failure is not None:
            last_api_failure: dict[str, Any] | None = self._last_api_failure.to_dict()
        elif (
            isinstance(previous_last_api_failure, dict)
            and previous_last_api_failure.get("mapped_category") in partial_failures
        ):
            last_api_failure = previous_last_api_failure
        else:
            last_api_failure = None

        now = datetime.now(timezone.utc)
        summary = summarize_payload(
            watched, bidding, selling, ending_soon_threshold_seconds
        )
        summary.update(_seller_ops_summary_keys(seller_ops))
        summary["sold_items_count"] = sold_items_count
        summary["unsold_items_count"] = unsold_items_count
        summary["sold_unsold_window_days"] = SOLD_UNSOLD_DURATION_DAYS
        summary["sold_unsold_last_fetched"] = (
            sold_unsold_last_fetched.isoformat()
            if isinstance(sold_unsold_last_fetched, datetime)
            else sold_unsold_last_fetched
        )
        summary["missing_scopes"] = missing_by_feature
        summary["missing_scope_features"] = missing_scope_features
        summary["section_last_fetched"] = {
            key: value.isoformat() if isinstance(value, datetime) else value
            for key, value in section_last_fetched.items()
        }
        summary["section_last_attempt"] = {
            key: value.isoformat() if isinstance(value, datetime) else value
            for key, value in section_last_attempt.items()
        }
        summary["section_last_success"] = {
            key: value.isoformat() if isinstance(value, datetime) else value
            for key, value in section_last_success.items()
        }
        sold_item_ids = sorted(
            item_id
            for item_id, status in selling_classification.items()
            if status == "sold"
        )
        unsold_item_ids = sorted(
            item_id
            for item_id, status in selling_classification.items()
            if status == "unsold"
        )
        _LOGGER.debug(
            "eBay API section refresh completed sections=%s watched_count=%s bidding_count=%s selling_count=%s partial_failures=%s truncated_collections=%s duration_ms=%.1f",
            sorted(sections),
            len(watched),
            len(bidding),
            len(selling),
            partial_failures,
            truncated_collections,
            _duration_ms(refresh_start),
        )
        return {
            "watched": watched,
            "bidding": bidding,
            "selling": selling,
            "seller_ops": seller_ops,
            "selling_classification": selling_classification,
            "sold_item_ids": sold_item_ids,
            "unsold_item_ids": unsold_item_ids,
            "summary": summary,
            "last_update": now,
            "last_successful_update": now,
            "partial_failures": partial_failures,
            "truncated_collections": truncated_collections,
            "api_warnings": api_warnings,
            "partial_failure_details": partial_failure_details,
            "last_api_failure": last_api_failure,
            "missing_scopes": missing_by_feature,
            "missing_scope_features": missing_scope_features,
            "section_last_fetched": section_last_fetched,
            "section_last_attempt": section_last_attempt,
            "section_last_success": section_last_success,
            "sold_unsold_last_fetched": sold_unsold_last_fetched,
            "refreshed_sections": sorted(sections),
        }


PARTIAL_FAILURE_CATEGORIES_BY_SECTION: dict[str, frozenset[str]] = {
    SECTION_BUYING: frozenset({"buying_truncated"}),
    SECTION_SELLING: frozenset(
        {
            "selling_truncated",
            "sold_unsold_truncated",
            "sold_unsold_classification",
            "seller_list_views_truncated",
            "seller_list_views",
            "active_offers_truncated",
            "active_offers",
        }
    ),
    SECTION_ANALYTICS: frozenset(
        {
            "analytics_views_missing_scope",
            "analytics_views",
            "analytics_views_unsupported",
        }
    ),
    SECTION_FULFILLMENT: frozenset(
        {
            "orders_missing_scope",
            "payment_disputes_missing_scope",
            "orders_truncated",
            "orders",
            "payment_disputes_truncated",
            "payment_disputes",
        }
    ),
    SECTION_SELLER_STANDARDS: frozenset(
        {"seller_standards_missing_scope", "seller_standards"}
    ),
    SECTION_FEEDBACK: frozenset(
        {
            "feedback_missing_scope",
            "feedback",
            "feedback_invalid_request",
            "feedback_user_lookup",
            "feedback_forbidden",
            "feedback_not_found",
        }
    ),
    SECTION_MESSAGES: frozenset(
        {"messages_missing_scope", "messages_truncated", "messages"}
    ),
    SECTION_ACCOUNT_PRIVILEGES: frozenset(
        {
            "account_privileges_missing_scope",
            "account_privileges_unsupported",
            "account_privileges",
        }
    ),
    SECTION_FINANCES: frozenset(
        {
            "finances_missing_scope",
            "finances_unsupported",
            "finances",
        }
    ),
}

TRUNCATED_KEYS_BY_SECTION: dict[str, tuple[str, ...]] = {
    SECTION_BUYING: ("watched", "bidding"),
    SECTION_SELLING: ("selling", "seller_list_views", "active_offers", "sold_unsold"),
    SECTION_FULFILLMENT: ("orders", "payment_disputes"),
    SECTION_MESSAGES: ("messages",),
}

_EMPTY_TRUNCATED_COLLECTIONS = {
    "watched": False,
    "bidding": False,
    "selling": False,
    "seller_list_views": False,
    "active_offers": False,
    "orders": False,
    "payment_disputes": False,
    "messages": False,
    "sold_unsold": False,
}


def enabled_sections_for_options(
    *,
    buying_enabled: bool,
    selling_enabled: bool,
    analytics_enabled: bool,
    fulfillment_enabled: bool,
    seller_standards_enabled: bool,
    feedback_enabled: bool,
    messages_enabled: bool,
    account_privileges_enabled: bool = False,
    finances_enabled: bool = False,
) -> set[str]:
    """Return payload sections that should be fetched for the given options."""
    from .sections import SECTION_SPECS

    option_flags = {
        CONF_BUYING_ENABLED: buying_enabled,
        CONF_SELLING_ENABLED: selling_enabled,
        CONF_ANALYTICS_ENABLED: analytics_enabled,
        CONF_FULFILLMENT_ENABLED: fulfillment_enabled,
        CONF_SELLER_STANDARDS_ENABLED: seller_standards_enabled,
        CONF_FEEDBACK_ENABLED: feedback_enabled,
        CONF_MESSAGES_ENABLED: messages_enabled,
        CONF_ACCOUNT_PRIVILEGES_ENABLED: account_privileges_enabled,
        CONF_FINANCES_ENABLED: finances_enabled,
    }
    sections: set[str] = set()
    for key, spec in SECTION_SPECS.items():
        if spec.option_key is None or not option_flags.get(spec.option_key):
            continue
        # Analytics is only scheduled when Selling is also enabled.
        if key == SECTION_ANALYTICS and not selling_enabled:
            continue
        sections.add(key)
    return sections


def _copy_seller_ops(seller_ops: dict[str, Any]) -> dict[str, Any]:
    """Shallow-copy seller-ops sections so merges do not mutate the previous payload."""
    empty = empty_seller_ops()
    copied: dict[str, Any] = {}
    for key, default in empty.items():
        value = seller_ops.get(key)
        if not isinstance(value, dict):
            copied[key] = dict(default)
            continue
        section = dict(value)
        if "by_id" in default:
            section["by_id"] = dict(value.get("by_id") or {})
        copied[key] = section
    return copied


def _stamp_section_attempt(
    section: str,
    *,
    success: bool,
    section_last_attempt: dict[str, datetime],
    section_last_success: dict[str, datetime],
    section_last_fetched: dict[str, datetime],
    when: datetime | None = None,
) -> None:
    """Record a section attempt; stamp success/fetched only when successful."""
    stamp = when or datetime.now(timezone.utc)
    section_last_attempt[section] = stamp
    if success:
        section_last_success[section] = stamp
        section_last_fetched[section] = stamp


def _parse_section_dt_map(raw: dict[str, Any] | None) -> dict[str, datetime]:
    """Parse a section→datetime map from previous payloads."""
    result: dict[str, datetime] = {}
    for key, value in (raw or {}).items():
        if isinstance(value, datetime):
            result[key] = value
        elif isinstance(value, str):
            try:
                result[key] = datetime.fromisoformat(value)
            except ValueError:
                continue
    return result


def _payload_shell(previous: dict[str, Any] | None) -> dict[str, Any]:
    """Return a mutable payload shell seeded from a previous coordinator payload."""
    if not previous:
        return {
            "watched": {},
            "bidding": {},
            "selling": {},
            "seller_ops": empty_seller_ops(),
            "selling_classification": {},
            "sold_item_ids": [],
            "unsold_item_ids": [],
            "summary": {},
            "partial_failures": [],
            "truncated_collections": dict(_EMPTY_TRUNCATED_COLLECTIONS),
            "api_warnings": [],
            "partial_failure_details": [],
            "last_api_failure": None,
            "missing_scopes": {},
            "missing_scope_features": [],
            "section_last_fetched": {},
            "section_last_attempt": {},
            "section_last_success": {},
            "sold_unsold_last_fetched": None,
        }
    section_last_fetched = _parse_section_dt_map(previous.get("section_last_fetched"))
    section_last_attempt = _parse_section_dt_map(previous.get("section_last_attempt"))
    section_last_success = _parse_section_dt_map(previous.get("section_last_success"))
    if not section_last_success and section_last_fetched:
        section_last_success = dict(section_last_fetched)
    if not section_last_attempt and section_last_fetched:
        section_last_attempt = dict(section_last_fetched)
    truncated = dict(_EMPTY_TRUNCATED_COLLECTIONS)
    truncated.update(previous.get("truncated_collections") or {})
    return {
        "watched": previous.get("watched") or {},
        "bidding": previous.get("bidding") or {},
        "selling": previous.get("selling") or {},
        "seller_ops": previous.get("seller_ops") or empty_seller_ops(),
        "selling_classification": previous.get("selling_classification") or {},
        "sold_item_ids": previous.get("sold_item_ids") or [],
        "unsold_item_ids": previous.get("unsold_item_ids") or [],
        "summary": previous.get("summary") or {},
        "partial_failures": list(previous.get("partial_failures") or []),
        "truncated_collections": truncated,
        "api_warnings": list(previous.get("api_warnings") or []),
        "partial_failure_details": list(previous.get("partial_failure_details") or []),
        "last_api_failure": previous.get("last_api_failure"),
        "missing_scopes": previous.get("missing_scopes") or {},
        "missing_scope_features": list(previous.get("missing_scope_features") or []),
        "section_last_fetched": section_last_fetched,
        "section_last_attempt": section_last_attempt,
        "section_last_success": section_last_success,
        "sold_unsold_last_fetched": previous.get("sold_unsold_last_fetched")
        or (previous.get("summary") or {}).get("sold_unsold_last_fetched"),
    }


def _seller_ops_summary_keys(seller_ops: dict[str, Any]) -> dict[str, Any]:
    """Flatten seller-ops aggregates into summary sensor keys."""
    orders = seller_ops.get("orders") or {}
    disputes = seller_ops.get("disputes") or {}
    standards = seller_ops.get("standards") or {}
    feedback = seller_ops.get("feedback") or {}
    messages = seller_ops.get("messages") or {}
    privileges = seller_ops.get("account_privileges") or {}
    finances = seller_ops.get("finances") or {}
    return {
        "orders_awaiting_shipment": orders.get("awaiting_shipment"),
        "orders_shipping_today": orders.get("shipping_today"),
        "orders_overdue": orders.get("overdue"),
        "orders_shipped": orders.get("shipped"),
        "orders_partially_fulfilled": orders.get("partially_fulfilled"),
        "open_payment_disputes": disputes.get("open_count"),
        "seller_level": standards.get("seller_level"),
        "transaction_defect_rate": standards.get("transaction_defect_rate"),
        "late_shipment_rate": standards.get("late_shipment_rate"),
        "cases_closed_without_seller_resolution": standards.get(
            "cases_closed_without_seller_resolution"
        ),
        "item_not_received_metric": standards.get("item_not_received_rating"),
        "item_not_as_described_metric": standards.get("item_not_as_described_rating"),
        "seller_next_evaluation_date": standards.get("next_evaluation_date"),
        "seller_standard_at_risk": standards.get("at_risk"),
        "feedback_score": feedback.get("score"),
        "positive_feedback_percent": feedback.get("positive_percent"),
        "recent_positive_feedback": feedback.get("recent_positive"),
        "recent_neutral_feedback": feedback.get("recent_neutral"),
        "recent_negative_feedback": feedback.get("recent_negative"),
        "items_awaiting_feedback": feedback.get("awaiting_count"),
        "unread_conversations": messages.get("unread_count"),
        "buyer_question_conversations": messages.get("buyer_question_count"),
        "oldest_unanswered_message_hours": messages.get("oldest_unanswered_hours"),
        "selling_registered": privileges.get("seller_registration_completed"),
        "listing_amount_remaining": privileges.get("amount_remaining"),
        "listing_quantity_remaining": privileges.get("quantity_remaining"),
        "selling_near_limit": privileges.get("near_limit"),
        "available_funds": finances.get("available_funds"),
        "pending_funds": finances.get("pending_funds"),
        "last_payout_amount": finances.get("last_payout_amount"),
        "last_payout_status": finances.get("last_payout_status"),
    }
