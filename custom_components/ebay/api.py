"""Read-only eBay API client facade."""

from __future__ import annotations

import asyncio
from datetime import datetime
import logging
from typing import Any

import aiohttp

from .clients.endpoints import endpoints_for
from .clients.errors import (
    EbayApiError,
    EbayAuthError,
    EbayAuthTransientError,
    EbayError,
    EbayParseError,
    EbayPartialFailure,
)
from .clients.http import RestClientMixin
from .clients.rest_apis import RestApisClientMixin
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
)
from .marketplace import accept_language_for_site
from .models import ApiFailure
from .section_fetch import SectionFetchMixin
from .sections import enabled_sections_for_options

__all__ = [
    "EbayApiClient",
    "EbayApiError",
    "EbayAuthError",
    "EbayAuthTransientError",
    "EbayError",
    "EbayParseError",
    "EbayPartialFailure",
]

_LOGGER = logging.getLogger(__name__)


class EbayApiClient(
    RestClientMixin,
    TradingClientMixin,
    RestApisClientMixin,
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
        rate_limit_tracker: Any | None = None,
        retry_policy: Any | None = None,
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
        self._token_refresh_lock: asyncio.Lock | None = None
        self._rate_limit_tracker = rate_limit_tracker
        self._retry_policy = retry_policy
        self._last_retry_count = 0

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
