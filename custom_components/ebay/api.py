"""Read-only eBay API client facade.

Public imports stay at ``custom_components.ebay.api``. Implementation is split
across ``clients/`` (HTTP mixins) and ``parsers/`` (pure helpers).
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

import aiohttp

from .clients.account import AccountClientMixin
from .clients.analytics import AnalyticsClientMixin, analytics_date_range_filter
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
from .marketplace import accept_language_for_site
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
from .section_fetch import (
    SectionFetchMixin,
    _copy_seller_ops,
    _parse_section_dt_map,
    _payload_shell,
    _seller_ops_summary_keys,
    _stamp_section_attempt,
)
from .sections import (
    EMPTY_TRUNCATED_COLLECTIONS as _EMPTY_TRUNCATED_COLLECTIONS,
)
from .sections import (
    PARTIAL_FAILURE_CATEGORIES_BY_SECTION,
    TRUNCATED_KEYS_BY_SECTION,
    enabled_sections_for_options,
)

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
    "EbayApiClient",
    "EbayApiError",
    "EbayAuthError",
    "EbayAuthTransientError",
    "EbayEndpoints",
    "EbayError",
    "EbayParseError",
    "EbayPartialFailure",
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
    "analytics_date_range_filter",
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
    SectionFetchMixin,
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
            {
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
