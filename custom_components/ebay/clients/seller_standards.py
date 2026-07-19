"""Seller standards profile + CSM client mixin."""

from __future__ import annotations

import logging
from typing import Any

from ..marketplace import capability_supported
from ..seller_ops import (
    empty_seller_ops,
    marketplace_id_for_site,
    parse_customer_service_metric,
    parse_seller_standards_profiles,
)
from .errors import EbayPartialFailure

_LOGGER = logging.getLogger(__name__)


class SellerStandardsClientMixin:
    """Seller standards and customer-service metric fetchers."""

    _endpoints: Any
    site_id: str

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
