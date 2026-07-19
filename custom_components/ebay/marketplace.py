"""Marketplace identifiers, locales, and per-API capability matrix."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# Trading Site ID → Sell marketplace ID. Locale variants share a marketplace
# and select language via Accept-Language (see SITE_ID_TO_ACCEPT_LANGUAGE).
SITE_ID_TO_MARKETPLACE: dict[str, str] = {
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
    "210": "EBAY_CA",  # Canada French → EBAY_CA + fr-CA, not EBAY_CAFR
    "211": "EBAY_PH",
    "212": "EBAY_PL",
    "216": "EBAY_SG",
}

SITE_ID_TO_ACCEPT_LANGUAGE: dict[str, str] = {
    "2": "en-CA",
    "210": "fr-CA",
    "23": "nl-BE",
    "123": "fr-BE",
}


@dataclass(frozen=True)
class MarketplaceCapabilities:
    """Which Sell/Commerce resources are supported for a marketplace."""

    seller_standards: bool = True
    customer_service_metrics: bool = True
    traffic_report: bool = True
    feedback: bool = True
    fulfillment: bool = True
    finances: bool = False
    account_privileges: bool = True


# Defaults assume broad US/EU support; override known gaps.
# Finances often requires digital request signing outside a few marketplaces;
# leave finances=False unless the marketplace is known to work without signing.
_DEFAULT_CAPS = MarketplaceCapabilities()
_WITH_FINANCES = MarketplaceCapabilities(finances=True)
_NO_TRAFFIC = MarketplaceCapabilities(traffic_report=False)
_LIMITED = MarketplaceCapabilities(
    traffic_report=False,
    customer_service_metrics=False,
    finances=False,
)

MARKETPLACE_CAPABILITIES: dict[str, MarketplaceCapabilities] = {
    "EBAY_US": _WITH_FINANCES,
    "EBAY_CA": _NO_TRAFFIC,
    "EBAY_GB": _WITH_FINANCES,
    "EBAY_AU": _WITH_FINANCES,
    "EBAY_AT": _DEFAULT_CAPS,
    "EBAY_BE": _NO_TRAFFIC,
    "EBAY_FR": _DEFAULT_CAPS,
    "EBAY_DE": _WITH_FINANCES,
    "EBAY_IT": _DEFAULT_CAPS,
    "EBAY_NL": _DEFAULT_CAPS,
    "EBAY_ES": _DEFAULT_CAPS,
    "EBAY_CH": _DEFAULT_CAPS,
    "EBAY_HK": _LIMITED,
    "EBAY_IN": _LIMITED,
    "EBAY_IE": _DEFAULT_CAPS,
    "EBAY_MY": _LIMITED,
    "EBAY_PH": _LIMITED,
    "EBAY_PL": _DEFAULT_CAPS,
    "EBAY_SG": _LIMITED,
}


def is_known_site_id(site_id: str) -> bool:
    """Return True when Site ID maps to a known marketplace."""
    return str(site_id) in SITE_ID_TO_MARKETPLACE


def marketplace_id_for_site(site_id: str) -> str | None:
    """Map Trading Site ID to Sell marketplace ID."""
    return SITE_ID_TO_MARKETPLACE.get(str(site_id))


def accept_language_for_site(site_id: str) -> str | None:
    """Return Accept-Language for locale-specific Site IDs, if any."""
    return SITE_ID_TO_ACCEPT_LANGUAGE.get(str(site_id))


def capabilities_for_marketplace(marketplace_id: str | None) -> MarketplaceCapabilities:
    """Return capability flags for a marketplace (conservative unknowns)."""
    if not marketplace_id:
        return MarketplaceCapabilities(
            seller_standards=False,
            customer_service_metrics=False,
            traffic_report=False,
            feedback=True,
            fulfillment=True,
            finances=False,
            account_privileges=False,
        )
    return MARKETPLACE_CAPABILITIES.get(marketplace_id, _LIMITED)


def capabilities_for_site(site_id: str) -> MarketplaceCapabilities:
    """Return capability flags for a Trading Site ID."""
    return capabilities_for_marketplace(marketplace_id_for_site(site_id))


def capability_supported(site_id: str, capability: str) -> bool:
    """Return whether ``capability`` is supported for the site."""
    caps = capabilities_for_site(site_id)
    return bool(getattr(caps, capability, False))


def marketplace_matrix_rows() -> list[dict[str, Any]]:
    """Return Site ID × marketplace × capability rows for tests."""
    rows: list[dict[str, Any]] = []
    for site_id, marketplace in SITE_ID_TO_MARKETPLACE.items():
        caps = capabilities_for_marketplace(marketplace)
        rows.append(
            {
                "site_id": site_id,
                "marketplace": marketplace,
                "accept_language": accept_language_for_site(site_id),
                "seller_standards": caps.seller_standards,
                "customer_service_metrics": caps.customer_service_metrics,
                "traffic_report": caps.traffic_report,
                "finances": caps.finances,
                "account_privileges": caps.account_privileges,
            }
        )
    return rows
