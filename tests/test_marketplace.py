"""Marketplace ID, locale, and capability matrix tests."""

from __future__ import annotations

from custom_components.ebay.marketplace import (
    SITE_ID_TO_MARKETPLACE,
    accept_language_for_site,
    capabilities_for_site,
    capability_supported,
    is_known_site_id,
    marketplace_id_for_site,
    marketplace_matrix_rows,
)
from custom_components.ebay.seller_ops import (
    SITE_ID_TO_MARKETPLACE as SELLER_OPS_SITE_MAP,
)
from custom_components.ebay.seller_ops import (
    is_known_site_id as seller_ops_is_known,
)
from custom_components.ebay.seller_ops import (
    marketplace_id_for_site as seller_ops_marketplace,
)


def test_canada_french_maps_to_ebay_ca_not_cafr() -> None:
    assert marketplace_id_for_site("210") == "EBAY_CA"
    assert SITE_ID_TO_MARKETPLACE["210"] == "EBAY_CA"
    assert accept_language_for_site("210") == "fr-CA"
    assert accept_language_for_site("2") == "en-CA"


def test_belgian_locales_share_ebay_be() -> None:
    assert marketplace_id_for_site("23") == "EBAY_BE"
    assert marketplace_id_for_site("123") == "EBAY_BE"
    assert accept_language_for_site("23") == "nl-BE"
    assert accept_language_for_site("123") == "fr-BE"


def test_seller_ops_reexports_marketplace_map() -> None:
    assert SELLER_OPS_SITE_MAP is SITE_ID_TO_MARKETPLACE
    assert seller_ops_marketplace("210") == "EBAY_CA"
    assert seller_ops_is_known("210") is True
    assert seller_ops_is_known("999") is False


def test_marketplace_matrix_covers_every_site_id() -> None:
    rows = marketplace_matrix_rows()
    site_ids = {row["site_id"] for row in rows}
    assert site_ids == set(SITE_ID_TO_MARKETPLACE)
    for row in rows:
        assert row["marketplace"] == SITE_ID_TO_MARKETPLACE[row["site_id"]]
        assert isinstance(row["seller_standards"], bool)
        assert isinstance(row["customer_service_metrics"], bool)
        assert isinstance(row["traffic_report"], bool)


def test_canada_traffic_report_not_supported() -> None:
    assert capability_supported("0", "traffic_report") is True
    assert capability_supported("2", "traffic_report") is False
    assert capabilities_for_site("2").traffic_report is False


def test_traffic_report_allowlisted_marketplaces_only() -> None:
    """eBay documents traffic_report for AU/FR/DE/GB/IT/ES/US only."""
    supported_sites = {
        "0",  # US
        "3",  # GB
        "15",  # AU
        "71",  # FR
        "77",  # DE
        "101",  # IT
        "186",  # ES
    }
    unsupported_sites = {
        "2",  # CA
        "16",  # AT
        "23",  # BE
        "146",  # NL
        "193",  # CH
        "205",  # IE
        "212",  # PL
        "201",  # HK
    }
    for site_id in supported_sites:
        assert capability_supported(site_id, "traffic_report") is True, site_id
    for site_id in unsupported_sites:
        assert capability_supported(site_id, "traffic_report") is False, site_id


def test_unknown_site_id() -> None:
    assert is_known_site_id("999") is False
    assert marketplace_id_for_site("999") is None
    caps = capabilities_for_site("999")
    assert caps.seller_standards is False
    assert caps.traffic_report is False
    assert caps.feedback is True
    assert caps.finances is False
    assert caps.account_privileges is False


def test_finances_enabled_for_core_marketplaces() -> None:
    assert capability_supported("0", "finances") is True  # US
    assert capability_supported("3", "finances") is True  # GB
    assert capability_supported("77", "finances") is True  # DE
    assert capability_supported("15", "finances") is True  # AU
    assert capability_supported("71", "finances") is False  # FR (signing)
    assert capability_supported("2", "finances") is False  # CA


def test_account_privileges_default_supported() -> None:
    assert capability_supported("0", "account_privileges") is True
    assert capability_supported("71", "account_privileges") is True
    assert capability_supported("999", "account_privileges") is False
