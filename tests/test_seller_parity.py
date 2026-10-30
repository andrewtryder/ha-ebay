"""Unit tests for the Milestone 5 Active Seller Listings XML vs. GraphQL parity harness."""

from __future__ import annotations

import asyncio
import json
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, Mock, patch

import aiohttp
import pytest

import scripts.validate_seller_parity as cli_harness
from custom_components.ebay.api import EbayApiClient
from custom_components.ebay.clients.errors import (
    EbayApiError,
    EbayAuthError,
    EbayAuthTransientError,
    EbayError,
    EbayGraphQLError,
    EbayParseError,
)
from custom_components.ebay.parsers.seller_parity import (
    MONITORED_SELLER_FIELDS,
    PARITY_DIFFERENT_SEMANTICS,
    PARITY_EQUIVALENT_NORMALIZED,
    PARITY_EXACT_MATCH,
    PARITY_MISSING_FIELD,
    PARITY_UNVERIFIED,
    SELLER_CONTRACT_PROBES,
    _extract_money,
    _images_equivalent,
    _to_bool_or_none,
    _urls_equivalent,
    compare_seller_snapshots,
    infer_and_validate_query_operation,
    merge_graphql_seller_pages,
    normalize_graphql_seller_item,
    normalize_graphql_seller_payload,
    normalize_listing_type_category,
    normalize_seller_item_id,
    parse_query_variables,
    rank_seller_probe_candidate,
    render_seller_parity_json,
    render_seller_parity_markdown,
    sanitize_diagnostic_token,
    sanitize_graphql_field_name,
    sanitize_graphql_operation_name,
    sanitize_seller_parity_report,
    sanitize_seller_probe_key,
    summarize_seller_probe_exception,
    validate_all_seller_contract_probes,
)


def test_all_builtin_seller_contract_probes_are_strictly_read_only() -> None:
    """All built-in contract probes must pass validate_read_only_query."""
    validate_all_seller_contract_probes()
    assert len(SELLER_CONTRACT_PROBES) >= 5
    for probe in SELLER_CONTRACT_PROBES:
        assert "mutation" not in probe.query.lower()
        assert "subscription" not in probe.query.lower()
        op_name = infer_and_validate_query_operation(
            probe.query, operation_name=probe.operation_name
        )
        assert op_name == probe.operation_name


def test_infer_and_validate_query_operation_rejects_mutations() -> None:
    """Mutations and subscriptions must be rejected immediately."""
    with pytest.raises(ValueError, match="Mutations are strictly prohibited"):
        infer_and_validate_query_operation(
            'mutation CreateListing { createOrReplaceInventoryItem(sku: "SKU123") { status } }'
        )

    with pytest.raises(ValueError, match="Subscriptions are strictly prohibited"):
        infer_and_validate_query_operation(
            "subscription OnListingUpdated { listingUpdated { id } }"
        )


def test_normalize_seller_item_id_variants() -> None:
    """Item ID normalizer correctly handles composite, numeric, opaque, and missing formats."""
    assert normalize_seller_item_id("111222333444") == (
        "111222333444",
        "legacy_numeric",
    )
    assert normalize_seller_item_id("v1|111222333444|0") == (
        "111222333444",
        "composite_v1",
    )
    assert normalize_seller_item_id("v1|111222333444|999888") == (
        "111222333444",
        "composite_v1",
    )
    assert normalize_seller_item_id("custom-sku-99") == (
        "custom-sku-99",
        "opaque_string",
    )
    assert normalize_seller_item_id(None) == ("", "missing")
    assert normalize_seller_item_id("   ") == ("", "empty")
    assert normalize_seller_item_id(123456789012) == ("123456789012", "legacy_numeric")


def test_normalize_listing_type_category_variants() -> None:
    """Auction, fixed price, and classified formats map to standard categories."""
    assert normalize_listing_type_category("Chinese") == "auction"
    assert normalize_listing_type_category("AUCTION") == "auction"
    assert normalize_listing_type_category(["AUCTION", "BEST_OFFER"]) == "auction"
    assert normalize_listing_type_category("FixedPriceItem") == "fixed_price"
    assert normalize_listing_type_category("StoresFixedPrice") == "fixed_price"
    assert normalize_listing_type_category("FIXED_PRICE") == "fixed_price"
    assert normalize_listing_type_category(["FIXED_PRICE"]) == "fixed_price"
    assert normalize_listing_type_category("ClassifiedAd") == "classifiedad"
    assert normalize_listing_type_category(None) is None
    assert normalize_listing_type_category("   ") is None
    assert normalize_listing_type_category([]) is None


def test_extract_money_and_to_bool_or_none() -> None:
    """Monetary values and booleans extract cleanly from diverse GraphQL shapes."""
    val, cur, was_str = _extract_money({"value": "28.50", "currency": "USD"})
    assert val == 28.50
    assert cur == "USD"
    assert was_str is True

    val2, cur2, was_str2 = _extract_money({"amount": 195.0, "currencyCode": "gbp"})
    assert val2 == 195.0
    assert cur2 == "GBP"
    assert was_str2 is False

    assert _extract_money(None) == (None, None, False)
    assert _extract_money({}) == (None, None, False)

    assert _to_bool_or_none(True) is True
    assert _to_bool_or_none(False) is False
    assert _to_bool_or_none("true") is True
    assert _to_bool_or_none("0") is False
    assert _to_bool_or_none(None) is None


def test_normalize_graphql_seller_payload_items_and_listings_shapes() -> None:
    """Normalization parses both items[] and listings[] containers and composite IDs."""
    # 1. items shape
    payload_items = {
        "sellerListingSearch": {
            "total": 1,
            "items": [
                {
                    "itemId": "v1|123456789012|0",
                    "sku": "SKU-VINTAGE-01",
                    "title": "Ceramic Mug",
                    "currentPrice": {"value": "28.50", "currency": "USD"},
                    "quantity": 10,
                    "quantityAvailable": 7,
                    "quantitySold": 3,
                    "views": 42,
                    "watchers": 5,
                    "questions": 1,
                    "offers": 2,
                    "itemEndDate": "2026-11-25T18:00:00Z",
                    "itemWebUrl": "https://www.ebay.com/itm/123456789012",
                    "image": {
                        "imageUrl": "https://i.ebayimg.com/images/g/mug/s-l500.jpg"
                    },
                    "formatType": "FIXED_PRICE",
                }
            ],
        }
    }
    norm_items = normalize_graphql_seller_payload(payload_items)
    assert norm_items["reported_total"] == 1
    assert norm_items["items_field_name"] == "items"
    assert norm_items["composite_id_count"] == 1
    assert "123456789012" in norm_items["items_by_id"]
    item = norm_items["items_by_id"]["123456789012"]
    assert item["current_price"] == 28.50
    assert item["currency"] == "USD"
    assert item["sku"] == "SKU-VINTAGE-01"
    assert item["quantity"] == 10
    assert item["quantity_available"] == 7
    assert item["quantity_sold"] == 3
    assert item["views"] == 42
    assert item["watchers"] == 5
    assert item["questions"] == 1
    assert item["offers"] == 2

    # 2. listings shape (alternate candidate probe)
    payload_alt = {
        "sellerListings": {
            "totalCount": 1,
            "listings": [
                {
                    "listingId": "987654321098",
                    "title": "Mechanical Watch",
                    "price": {"amount": 195.0, "currencyCode": "USD"},
                    "availableQuantity": 1,
                    "soldQuantity": 0,
                    "hitCount": 120,
                    "watchCount": 14,
                    "questionCount": 0,
                    "bestOfferCount": 0,
                    "endTime": "2026-11-28T12:00:00.000Z",
                    "imageUrl": "https://i.ebayimg.com/images/g/watch/s-l225.jpg",
                }
            ],
        }
    }
    norm_alt = normalize_graphql_seller_payload(payload_alt)
    assert norm_alt["reported_total"] == 1
    assert norm_alt["items_field_name"] == "listings"
    assert "987654321098" in norm_alt["items_by_id"]
    item_b = norm_alt["items_by_id"]["987654321098"]
    assert item_b["quantity_available"] == 1
    assert item_b["views"] == 120
    assert item_b["watchers"] == 14


def test_diagnostic_privacy_allowlist_redacts_item_ids_accounts_and_secrets() -> None:
    """Allowlist strictly preserves documented error codes and redacts arbitrary strings."""
    assert (
        sanitize_diagnostic_token("VALIDATION_ERROR", kind="code") == "VALIDATION_ERROR"
    )
    assert sanitize_diagnostic_token("FORBIDDEN", kind="code") == "FORBIDDEN"
    assert sanitize_diagnostic_token("ITEM_NOT_FOUND", kind="code") == "ITEM_NOT_FOUND"
    assert (
        sanitize_diagnostic_token("LISTING_NOT_FOUND", kind="code")
        == "LISTING_NOT_FOUND"
    )
    assert sanitize_diagnostic_token("25001", kind="code") == "25001"
    assert sanitize_diagnostic_token("REQUEST", kind="category") == "REQUEST"
    assert (
        sanitize_diagnostic_token("SELL_INVENTORY", kind="domain") == "SELL_INVENTORY"
    )

    # Sensitive or unapproved values must be redacted
    assert (
        sanitize_diagnostic_token("user_12345_private_account", kind="code")
        == "UNKNOWN_CODE"
    )
    assert sanitize_diagnostic_token("item_999888777666", kind="code") == "UNKNOWN_CODE"
    assert (
        sanitize_diagnostic_token("secret_token_value", kind="category")
        == "UNKNOWN_CATEGORY"
    )
    assert (
        sanitize_diagnostic_token("custom_domain_private", kind="domain")
        == "UNKNOWN_DOMAIN"
    )


def test_summarize_seller_probe_exception_http_400_and_resolver_forbidden() -> None:
    """HTTP 400 validation error vs resolver FORBIDDEN are distinguished cleanly."""
    # 1. Resolver error: FORBIDDEN
    forbidden_err = EbayAuthError(
        "eBay GraphQL permission error (insufficient scope): Access denied",
        is_expired=False,
        is_permission_error=True,
        errors=[
            {
                "message": "Access is denied to sellerListingSearch",
                "code": "FORBIDDEN",
                "error_code": "FORBIDDEN",
                "category": "REQUEST",
                "domain": "SELL_LISTING",
            }
        ],
        status=200,
    )
    summary_f = summarize_seller_probe_exception(forbidden_err)
    assert summary_f["failure_class"] == "permission_or_scope_error"
    assert summary_f["category"] == "REQUEST"
    assert "FORBIDDEN" in summary_f["error_codes"]
    assert summary_f["http_status"] == 200

    # 2. HTTP 400 schema validation error
    val_err = EbayGraphQLError(
        "eBay GraphQL query validation failed (HTTP 400) for GetSellerListingsTotal",
        errors=[
            {
                "code": "VALIDATION_ERROR",
                "error_code": "VALIDATION_ERROR",
                "category": "REQUEST",
                "domain": None,
            }
        ],
        status=400,
    )
    summary_v = summarize_seller_probe_exception(val_err)
    assert summary_v["failure_class"] == "validation_error"
    assert summary_v["http_status"] == 400
    assert "VALIDATION_ERROR" in summary_v["error_codes"]

    # 3. HTTP 400 with unclassified body
    raw_400 = EbayApiError("Bad Request", status=400)
    summary_raw = summarize_seller_probe_exception(raw_400)
    assert summary_raw["failure_class"] == "bad_request"
    assert summary_raw["http_status"] == 400


def test_completeness_and_pagination_fail_closed() -> None:
    """Truncated, partial, or unreconciled responses fail closed to inconclusive."""
    xml_items = {
        "111": {
            "item_id": "111",
            "title": "Mug",
            "current_price": 25.0,
            "currency": "USD",
            "quantity": 5,
            "quantity_available": 3,
            "quantity_sold": 2,
            "views": 10,
            "watchers": 2,
            "questions": 0,
            "offers": 1,
            "end_time": "2026-11-20T00:00:00Z",
            "listing_type": "fixed_price",
            "url": "https://www.ebay.com/itm/111",
            "image": "https://i.ebayimg.com/images/g/a/s-l140.jpg",
        },
        "222": {
            "item_id": "222",
            "title": "Plate",
            "current_price": 15.0,
            "currency": "USD",
            "quantity": 2,
            "quantity_available": 2,
            "quantity_sold": 0,
            "views": 4,
            "watchers": 1,
            "questions": 0,
            "offers": 0,
            "end_time": "2026-11-21T00:00:00Z",
            "listing_type": "fixed_price",
            "url": "https://www.ebay.com/itm/222",
            "image": "https://i.ebayimg.com/images/g/b/s-l140.jpg",
        },
    }

    # Case A: GraphQL returns only 1 item while XML has 2
    gql_partial = {
        "sellerListingSearch": {
            "total": 2,
            "items": [
                {
                    "itemId": "111",
                    "title": "Mug",
                    "currentPrice": {"value": "25.00", "currency": "USD"},
                    "quantity": 5,
                    "quantityAvailable": 3,
                    "quantitySold": 2,
                    "views": 10,
                    "watchers": 2,
                    "questions": 0,
                    "offers": 1,
                    "itemEndDate": "2026-11-20T00:00:00Z",
                    "itemWebUrl": "https://www.ebay.com/itm/111",
                    "image": {
                        "imageUrl": "https://i.ebayimg.com/images/g/a/s-l140.jpg"
                    },
                    "formatType": "FIXED_PRICE",
                }
            ],
        }
    }
    comp_a = compare_seller_snapshots(xml_items, gql_partial)
    assert comp_a["status"] == "inconclusive"
    assert comp_a["fields"]["selling_count"]["classification"] == PARITY_UNVERIFIED
    assert comp_a["fields"]["views"]["classification"] == PARITY_UNVERIFIED

    # Case B: XML is truncated
    comp_b = compare_seller_snapshots(xml_items, gql_partial, xml_truncated=True)
    assert comp_b["status"] == "inconclusive"
    assert comp_b["fields"]["selling_count"]["classification"] == PARITY_UNVERIFIED

    # Case C: GraphQL has duplicate item IDs
    gql_dupes = {
        "sellerListingSearch": {
            "total": 2,
            "items": [
                {"itemId": "111", "title": "Mug 1"},
                {"itemId": "111", "title": "Mug 2"},
            ],
        }
    }
    comp_c = compare_seller_snapshots(xml_items, gql_dupes)
    assert comp_c["status"] == "inconclusive"
    assert comp_c["duplicate_ids_count"] == 1


def test_compare_seller_snapshots_all_classifications() -> None:
    """Verifies exact match, normalized match, different semantics, and missing fields."""
    xml_selling = {
        "100": {
            "item_id": "100",
            "title": "Antique Clock",
            "current_price": 85.0,
            "currency": "USD",
            "quantity": 1,
            "quantity_available": 1,
            "quantity_sold": 0,
            "views": 50,
            "watchers": 7,
            "questions": 2,
            "offers": 1,
            "end_time": "2026-11-10T15:00:00.000Z",
            "listing_type": "Chinese",
            "url": "https://www.ebay.com/itm/Antique-Clock/100?hash=item1",
            "image": "https://i.ebayimg.com/images/g/abc/s-l140.jpg",
        }
    }

    # Matches after normalization: composite ID, string price, UTC format, listing format
    gql_payload = {
        "sellerListingSearch": {
            "total": 1,
            "items": [
                {
                    "itemId": "v1|100|0",
                    "title": "Antique Clock",
                    "currentPrice": {"value": "85.00", "currency": "USD"},
                    "quantity": 1,
                    "quantityAvailable": 1,
                    "quantitySold": 0,
                    "views": 50,
                    "watchers": 7,
                    "questions": 2,
                    "offers": 1,
                    "itemEndDate": "2026-11-10T15:00:00Z",
                    "formatType": "AUCTION",
                    "itemWebUrl": "https://www.ebay.com/itm/100",
                    "image": {
                        "imageUrl": "https://i.ebayimg.com/images/g/abc/s-l500.jpg"
                    },
                }
            ],
        }
    }

    comp = compare_seller_snapshots(xml_selling, gql_payload)
    assert comp["status"] == "parity_verified"
    fields = comp["fields"]

    assert fields["selling_count"]["classification"] == PARITY_EXACT_MATCH
    assert fields["title"]["classification"] == PARITY_EXACT_MATCH
    assert fields["currency"]["classification"] == PARITY_EXACT_MATCH
    assert fields["quantity"]["classification"] == PARITY_EXACT_MATCH
    assert fields["quantity_available"]["classification"] == PARITY_EXACT_MATCH
    assert fields["quantity_sold"]["classification"] == PARITY_EXACT_MATCH
    assert fields["views"]["classification"] == PARITY_EXACT_MATCH
    assert fields["watchers"]["classification"] == PARITY_EXACT_MATCH
    assert fields["questions"]["classification"] == PARITY_EXACT_MATCH
    assert fields["offers"]["classification"] == PARITY_EXACT_MATCH

    assert fields["item_id"]["classification"] == PARITY_EQUIVALENT_NORMALIZED
    assert fields["current_price"]["classification"] == PARITY_EQUIVALENT_NORMALIZED
    assert fields["end_time"]["classification"] == PARITY_EQUIVALENT_NORMALIZED
    assert fields["listing_type"]["classification"] == PARITY_EQUIVALENT_NORMALIZED
    assert fields["url"]["classification"] == PARITY_EQUIVALENT_NORMALIZED
    assert fields["image"]["classification"] == PARITY_EQUIVALENT_NORMALIZED


def test_compare_seller_snapshots_different_semantics_and_missing_field() -> None:
    """Verifies different semantics on mismatched metrics and missing field detection."""
    xml_selling = {
        "100": {
            "item_id": "100",
            "title": "Antique Clock",
            "current_price": 85.0,
            "currency": "USD",
            "quantity": 1,
            "quantity_available": 1,
            "quantity_sold": 0,
            "views": 50,
            "watchers": 7,
            "questions": 2,
            "offers": 1,
            "end_time": "2026-11-10T15:00:00.000Z",
            "listing_type": "Chinese",
            "url": "https://www.ebay.com/itm/100",
            "image": "https://i.ebayimg.com/images/g/abc/s-l140.jpg",
        }
    }

    # GraphQL has views=20 (mismatch) and omits watchers
    gql_payload = {
        "sellerListingSearch": {
            "total": 1,
            "items": [
                {
                    "itemId": "100",
                    "title": "Antique Clock",
                    "currentPrice": {"value": "85.00", "currency": "USD"},
                    "quantity": 1,
                    "quantityAvailable": 1,
                    "quantitySold": 0,
                    "views": 20,  # Mismatch
                    "questions": 2,
                    "offers": 1,
                    "itemEndDate": "2026-11-10T15:00:00Z",
                    "formatType": "AUCTION",
                    "itemWebUrl": "https://www.ebay.com/itm/100",
                    "image": {
                        "imageUrl": "https://i.ebayimg.com/images/g/abc/s-l140.jpg"
                    },
                }
            ],
        }
    }

    comp = compare_seller_snapshots(xml_selling, gql_payload)
    assert comp["status"] == "semantic_differences"
    fields = comp["fields"]
    assert fields["views"]["classification"] == PARITY_DIFFERENT_SEMANTICS
    assert fields["watchers"]["classification"] == PARITY_MISSING_FIELD


def test_rank_seller_probe_candidate() -> None:
    """Candidates with complete listing collections outrank partial or count-only probes."""
    cand_items_complete = {
        "outcome": "success",
        "pagination_status": "complete",
        "graphql_selling_count": 5,
        "items_list_present": True,
        "raw_items_count": 5,
        "reported_total": 5,
        "duplicate_ids_count": 0,
        "missing_ids_count": 0,
    }
    cand_count_only = {
        "outcome": "success",
        "pagination_status": "metadata_only",
        "graphql_selling_count": 0,
        "items_list_present": False,
        "raw_items_count": 0,
        "reported_total": 5,
        "duplicate_ids_count": 0,
        "missing_ids_count": 0,
    }
    cand_with_dupes = {
        "outcome": "success",
        "pagination_status": "unverified",
        "graphql_selling_count": 4,
        "items_list_present": True,
        "raw_items_count": 5,
        "reported_total": 5,
        "duplicate_ids_count": 1,
        "missing_ids_count": 0,
    }

    rank_complete = rank_seller_probe_candidate(cand_items_complete)
    rank_count = rank_seller_probe_candidate(cand_count_only)
    rank_dupes = rank_seller_probe_candidate(cand_with_dupes)

    assert rank_complete > rank_count
    assert rank_complete > rank_dupes


def test_sanitize_seller_parity_report_privacy() -> None:
    """Sanitizer scrubs forbidden keys, tokens, and caller-specified secrets."""
    dirty_report = {
        "access_token": "v^1.1#secret-oauth-token-12345",
        "refresh_token": "secret-refresh-token",
        "client_secret": "my-secret-key",
        "authorization": "Bearer token999",
        "token_expires_in": 7200,
        "user_secret": "password123",
        "status": "parity_verified",
        "diagnostic_msg": "User authorized with Bearer v^1.1#secret-token",
        "nested": {
            "api_key": "some_token_here",
            "safe_val": 42,
        },
    }

    clean = sanitize_seller_parity_report(
        dirty_report,
        forbidden_values=["custom_secret_phrase"],
    )

    assert "access_token" not in clean
    assert "refresh_token" not in clean
    assert "client_secret" not in clean
    assert "authorization" not in clean
    assert "token_expires_in" not in clean
    assert "user_secret" not in clean
    assert "api_key" not in clean["nested"]
    assert clean["nested"]["safe_val"] == 42
    assert "v^1.1#" not in clean["diagnostic_msg"]
    assert "[REDACTED_TOKEN]" in clean["diagnostic_msg"]


def test_render_seller_parity_markdown_and_json() -> None:
    """Renderers produce properly formatted Markdown and valid JSON without leaking secrets."""
    report = {
        "status": "parity_verified",
        "environment": "production",
        "site_id": "0",
        "xml_available": True,
        "xml_selling_count": 1,
        "xml_truncated": False,
        "graphql_selling_count": 1,
        "graphql_complete": True,
        "pagination_status": "complete",
        "common_items_count": 1,
        "composite_ids_normalized_count": 0,
        "duplicate_ids_count": 0,
        "missing_ids_count": 0,
        "fields": {
            "selling_count": {
                "field": "selling_count",
                "classification": PARITY_EXACT_MATCH,
                "coverage": "full",
                "compared_count": 1,
                "exact_match_count": 1,
                "normalized_match_count": 0,
                "mismatch_count": 0,
                "both_absent_count": 0,
                "notes": "Exact match",
            }
        },
    }

    md = render_seller_parity_markdown(report)
    assert (
        "# eBay Active Seller Listings API Contract and Parity Validation Report" in md
    )
    assert "parity_verified" in md
    assert "selling_count" in md

    json_str = render_seller_parity_json(report)
    parsed = json.loads(json_str)
    assert parsed["status"] == "parity_verified"
    assert parsed["common_items_count"] == 1


def test_cli_refuses_network_without_live_flag() -> None:
    """CLI exits with error code if called without --live and without fixture/demo."""
    with pytest.raises(SystemExit) as exc_info:
        cli_harness.main([])
    assert exc_info.value.code == 2


def test_cli_synthetic_demo_markdown_and_json(tmp_path: Path, capsys: Any) -> None:
    """CLI synthetic demo runs offline and writes both Markdown and JSON files."""
    out_md = tmp_path / "seller_parity.md"
    out_json = tmp_path / "seller_parity.json"

    exit_code = cli_harness.main(
        [
            "--synthetic-demo",
            "--output",
            str(out_md),
            "--json-output",
            str(out_json),
        ]
    )
    assert exit_code == 0
    assert out_md.is_file()
    assert out_json.is_file()

    md_content = out_md.read_text(encoding="utf-8")
    assert (
        "eBay Active Seller Listings API Contract and Parity Validation Report"
        in md_content
    )
    assert "parity_verified" in md_content

    json_content = out_json.read_text(encoding="utf-8")
    parsed = json.loads(json_content)
    assert parsed["status"] == "parity_verified"
    assert parsed["common_items_count"] == 2


def test_cli_offline_fixtures(tmp_path: Path) -> None:
    """CLI correctly executes offline comparison against fixture files."""
    xml_file = tmp_path / "selling.xml"
    gql_file = tmp_path / "seller_listings.json"
    xml_demo, gql_demo = cli_harness._synthetic_demo_fixtures()
    xml_file.write_text(xml_demo, encoding="utf-8")
    gql_file.write_text(gql_demo, encoding="utf-8")

    out_file = tmp_path / "fixture_report.json"
    exit_code = cli_harness.main(
        [
            "--xml-fixture",
            str(xml_file),
            "--graphql-fixture",
            str(gql_file),
            "--format",
            "json",
            "--output",
            str(out_file),
        ]
    )
    assert exit_code == 0
    assert out_file.is_file()
    parsed = json.loads(out_file.read_text(encoding="utf-8"))
    assert parsed["status"] == "parity_verified"
    assert parsed["mode"] == "offline_fixture"


def test_cli_resolve_live_credentials(tmp_path: Path) -> None:
    """resolve_live_credentials loads and parses environment and .env files safely."""
    # 1. From environ mapping with short-lived token
    creds_token = cli_harness.resolve_live_credentials(
        environ={"EBAY_ACCESS_TOKEN": "short_lived_token_123"}
    )
    assert creds_token["access_token"] == "short_lived_token_123"
    assert creds_token["environment"] == "production"

    # 2. From .env file with client credentials
    dotenv_file = tmp_path / ".env.test"
    dotenv_file.write_text(
        "EBAY_CLIENT_ID=client123\n"
        "EBAY_CLIENT_SECRET=secret456\n"
        "EBAY_REFRESH_TOKEN=refresh789\n",
        encoding="utf-8",
    )
    creds_dotenv = cli_harness.resolve_live_credentials(
        env_file=dotenv_file, environ={}
    )
    assert creds_dotenv["client_id"] == "client123"
    assert creds_dotenv["client_secret"] == "secret456"
    assert creds_dotenv["refresh_token"] == "refresh789"


def test_run_live_seller_validation_with_mocked_client() -> None:
    """run_live_seller_validation queries XML ActiveList and GraphQL probes safely."""
    xml_demo, gql_demo = cli_harness._synthetic_demo_fixtures()
    mock_client = Mock()
    mock_client.environment = "production"
    mock_client.site_id = "0"
    mock_client.refresh_token = "mock-refresh"

    mock_client.async_call_trading_api = AsyncMock(return_value=ET.fromstring(xml_demo))
    gql_dict = json.loads(gql_demo)["data"]
    mock_client.async_post_graphql = AsyncMock(return_value=gql_dict)

    report = asyncio.run(cli_harness.run_live_seller_validation(mock_client))
    assert report["mode"] == "live_authenticated"
    assert report["xml_available"] is True
    assert report["status"] == "parity_verified"
    assert report["selected_probe_key"] is not None
    assert len(report["probes"]) >= 5


def test_parse_query_variables_and_extract_declared() -> None:
    """Variable declarations extract types and default values accurately."""
    query = """
    query GetListings($limit: Int, $offset: Int, $status: String!) {
      sellerListingSearch(limit: $limit, offset: $offset) { total }
    }
    """
    vars_parsed = parse_query_variables(query, {"limit": 50, "status": "ACTIVE"})
    assert vars_parsed is not None
    assert vars_parsed["limit"] == 50
    assert vars_parsed["offset"] == 0
    assert vars_parsed["status"] == "ACTIVE"

    vars_default = parse_query_variables(query, {"status": "ACTIVE"})
    assert vars_default is not None
    assert vars_default["limit"] == 100

    assert parse_query_variables("query Simple { __typename }") is None
    assert parse_query_variables("query Simple { __typename }", {"foo": "bar"}) == {
        "foo": "bar"
    }


def test_summarize_seller_probe_exception_diverse_error_types() -> None:
    """Exception summarizer maps auth transient, parse, client, timeout, and api errors."""
    summary_at = summarize_seller_probe_exception(
        EbayAuthTransientError("Token refresh failed")
    )
    assert summary_at["failure_class"] == "auth_transient_error"

    summary_pe = summarize_seller_probe_exception(EbayParseError("Malformed XML"))
    assert summary_pe["failure_class"] == "parse_error"

    summary_ce = summarize_seller_probe_exception(
        aiohttp.ClientError("Connection reset")
    )
    assert summary_ce["failure_class"] == "transport_error"

    summary_to = summarize_seller_probe_exception(TimeoutError("Request timed out"))
    assert summary_to["failure_class"] == "transport_error"

    summary_rl = summarize_seller_probe_exception(
        EbayApiError("Rate limit exceeded 429")
    )
    assert summary_rl["failure_class"] == "rate_limit"
    assert summary_rl["http_status"] == 429

    summary_un = summarize_seller_probe_exception(RuntimeError("Unexpected OS error"))
    assert summary_un["failure_class"] == "unexpected_error"


def test_urls_and_images_equivalent_helpers() -> None:
    """URL and image equivalence helpers compare canonical identifiers."""
    assert _urls_equivalent(
        "https://www.ebay.com/itm/123456789012",
        "https://ebay.com/itm/Item-Title/123456789012?hash=item1",
        "123456789012",
    )
    assert not _urls_equivalent(
        "https://www.ebay.com/itm/123456789012",
        "https://www.ebay.co.uk/itm/123456789012",
        "123456789012",
    )
    assert not _urls_equivalent("invalid-url-1", "invalid-url-2", "123")

    assert _images_equivalent(
        "https://i.ebayimg.com/images/g/abc123XYZ/s-l140.jpg",
        "https://i.ebayimg.com/images/g/abc123XYZ/s-l500.jpg",
    )
    assert not _images_equivalent(
        "https://i.ebayimg.com/images/g/abc123XYZ/s-l140.jpg",
        "https://i.ebayimg.com/images/g/differentAsset/s-l140.jpg",
    )


def test_merge_graphql_seller_pages_edge_cases() -> None:
    """merge_graphql_seller_pages handles empty input, missing containers, and pageInfo."""
    assert merge_graphql_seller_pages([])["page_count"] == 0
    assert merge_graphql_seller_pages(["not-a-map"])["items_by_id"] == {}

    page = {
        "sellerListingSearch": {
            "total": 10,
            "pageInfo": {"hasNextPage": True},
            "items": [{"itemId": "111", "title": "Item 1"}],
        }
    }
    merged = merge_graphql_seller_pages([page])
    assert merged["page_count"] == 1
    assert merged["pagination_status"] == "truncated"

    with pytest.raises(TypeError, match="GraphQL Seller data must be a mapping"):
        normalize_graphql_seller_payload("not-a-dict")


def test_render_seller_parity_markdown_with_operational_warnings_and_probes() -> None:
    """Markdown renderer formats operational warnings and probe results tables."""
    report = {
        "status": "inconclusive",
        "environment": "production",
        "site_id": "0",
        "operational_warnings": ["Test warning: short-lived token without refresh."],
        "probes": [
            {
                "key": "typename_handshake",
                "outcome": "success",
                "summary": "Root __typename handshake succeeded",
            },
            {
                "key": "seller_listings_total",
                "outcome": "error",
                "summary": "EbayGraphQLError (failure_class=permission_or_scope_error)",
            },
        ],
        "xml_available": True,
        "xml_selling_count": 0,
        "xml_truncated": False,
        "graphql_selling_count": 0,
        "graphql_complete": False,
        "pagination_status": "empty",
        "common_items_count": 0,
        "composite_ids_normalized_count": 0,
        "duplicate_ids_count": 0,
        "missing_ids_count": 0,
        "fields": {},
    }
    md = render_seller_parity_markdown(report)
    assert "Operational Warnings" in md
    assert "Test warning: short-lived token without refresh." in md
    assert "Candidate Contract Probes" in md
    assert "typename_handshake" in md
    assert "seller_listings_total" in md


def test_edges_graphql_connection_and_variable_defaults() -> None:
    """GraphQL Relay connection edges and query variable default values parse properly."""
    payload_edges = {
        "sellerListingSearch": {
            "totalCount": 1,
            "edges": [
                {
                    "node": {
                        "itemId": "12345",
                        "title": "Edge Item",
                        "currentPrice": {"amount": 20.0, "currencyCode": "usd"},
                    }
                }
            ],
        }
    }
    norm_edges = normalize_graphql_seller_payload(payload_edges)
    assert norm_edges["items_field_name"] == "edges"
    assert "12345" in norm_edges["items_by_id"]

    query = "query Test($flag: Boolean = true, $opt: String) { sellerListingSearch { total } }"
    parsed = parse_query_variables(query)
    assert parsed is not None
    assert parsed["flag"] is True
    assert "opt" not in parsed


def test_summarize_seller_probe_exception_http_500() -> None:
    """HTTP 500 error maps to graphql_error failure class."""
    summary_500 = summarize_seller_probe_exception(
        EbayGraphQLError("Server error", status=500)
    )
    assert summary_500["failure_class"] == "graphql_error"
    assert summary_500["http_status"] == 500


def test_compare_seller_snapshots_exact_types_and_selected_probe_markdown() -> None:
    """Test float matching, datetime objects, and selected_probe markdown output."""
    xml_item = {
        "999": {
            "item_id": "999",
            "title": "Exact Title",
            "current_price": 50.0,
            "currency": "usd",
            "quantity": 2,
            "quantity_available": 2,
            "quantity_sold": 0,
            "views": 10,
            "watchers": 1,
            "questions": 0,
            "offers": 0,
            "end_time": datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc),
            "listing_type": "fixed_price",
            "url": "https://www.ebay.com/itm/999",
            "image": "https://i.ebayimg.com/images/g/xyz/s-l140.jpg",
        }
    }
    gql_item = {
        "sellerListingSearch": {
            "total": 1,
            "items": [
                {
                    "itemId": "999",
                    "title": "Exact Title",
                    "currentPrice": {"amount": 50.0, "currencyCode": "USD"},
                    "quantity": 2,
                    "quantityAvailable": 2,
                    "quantitySold": 0,
                    "views": 10,
                    "watchers": 1,
                    "questions": 0,
                    "offers": 0,
                    "itemEndDate": "2026-11-01T12:00:00Z",
                    "formatType": "FIXED_PRICE",
                    "itemWebUrl": "https://www.ebay.com/itm/999",
                    "image": {
                        "imageUrl": "https://i.ebayimg.com/images/g/xyz/s-l140.jpg"
                    },
                }
            ],
        }
    }
    comp = compare_seller_snapshots(xml_item, gql_item)
    assert comp["status"] == "parity_verified"

    report = {
        "status": "parity_verified",
        "selected_probe": {
            "key": "seller_listings_paginated",
            "operation_name": "GetSellerListingsPaginated",
            "outcome": "success",
            "items_field": "items",
            "total_field": "total",
            "reported_total": 5,
            "pagination_status": "complete",
        },
        "fields": {},
    }
    md = render_seller_parity_markdown(report)
    assert "Selected Contract Probe" in md
    assert "seller_listings_paginated" in md


def test_field_mismatches_and_edge_coverage() -> None:
    """Covers field-level mismatches, base EbayError, and ranking edge cases."""
    summary_base = summarize_seller_probe_exception(EbayError("Base error"))
    assert summary_base["failure_class"] == "api_error"

    assert rank_seller_probe_candidate({"outcome": "empty"})[0] == 2
    assert rank_seller_probe_candidate({"outcome": "unsupported_shape"})[0] == 1
    assert rank_seller_probe_candidate({"outcome": "other"})[0] == 0

    xml_mismatch = {
        "101": {
            "item_id": "101",
            "title": "Title A",
            "current_price": 50.0,
            "currency": "USD",
            "quantity": 5,
            "quantity_available": 3,
            "quantity_sold": 2,
            "views": 10,
            "watchers": 2,
            "questions": 1,
            "offers": 0,
            "end_time": "2026-11-01T12:00:00Z",
            "listing_type": "FixedPriceItem",
            "url": "https://www.ebay.com/itm/101",
            "image": "https://i.ebayimg.com/images/g/a/s-l140.jpg",
        }
    }
    gql_mismatch = {
        "sellerListingSearch": {
            "total": 1,
            "items": [
                {
                    "itemId": "101",
                    "title": "Title B",  # mismatch
                    "currentPrice": {
                        "amount": 75.0,
                        "currencyCode": "EUR",
                    },  # mismatch price & currency
                    "quantity": 99,  # mismatch
                    "quantityAvailable": 90,  # mismatch
                    "quantitySold": 9,  # mismatch
                    "views": 100,  # mismatch
                    "watchers": 20,  # mismatch
                    "questions": 5,  # mismatch
                    "offers": 3,  # mismatch
                    "itemEndDate": "2027-01-01T00:00:00Z",  # mismatch
                    "formatType": "AUCTION",  # mismatch
                    "itemWebUrl": "https://www.ebay.com/itm/completely-different/999",  # mismatch
                    "image": {
                        "imageUrl": "https://i.ebayimg.com/images/g/completelyDifferent/s-l140.jpg"
                    },  # mismatch
                }
            ],
        }
    }
    comp_mm = compare_seller_snapshots(xml_mismatch, gql_mismatch)
    assert comp_mm["status"] == "semantic_differences"
    f = comp_mm["fields"]
    assert f["current_price"]["mismatch_count"] == 1
    assert f["currency"]["mismatch_count"] == 1
    assert f["quantity"]["mismatch_count"] == 1
    assert f["quantity_available"]["mismatch_count"] == 1
    assert f["quantity_sold"]["mismatch_count"] == 1
    assert f["views"]["mismatch_count"] == 1
    assert f["watchers"]["mismatch_count"] == 1
    assert f["questions"]["mismatch_count"] == 1
    assert f["offers"]["mismatch_count"] == 1
    assert f["end_time"]["mismatch_count"] == 1
    assert f["listing_type"]["mismatch_count"] == 1
    assert f["url"]["mismatch_count"] == 1
    assert f["image"]["mismatch_count"] == 1

    gql_total_diff = {
        "sellerListingSearch": {
            "total": 99,
            "items": [
                {
                    "itemId": "101",
                    "title": "Title A",
                    "currentPrice": {"value": "50.00", "currency": "USD"},
                    "quantity": 5,
                    "quantityAvailable": 3,
                    "quantitySold": 2,
                    "views": 10,
                    "watchers": 2,
                    "questions": 1,
                    "offers": 0,
                    "itemEndDate": "2026-11-01T12:00:00Z",
                    "formatType": "FIXED_PRICE",
                    "itemWebUrl": "https://www.ebay.com/itm/101",
                    "image": {
                        "imageUrl": "https://i.ebayimg.com/images/g/a/s-l140.jpg"
                    },
                }
            ],
        }
    }
    comp_tot = compare_seller_snapshots(xml_mismatch, gql_total_diff)
    assert comp_tot["fields"]["selling_count"]["classification"] == PARITY_UNVERIFIED
    assert comp_tot["status"] == "inconclusive"


def test_offline_fixture_partial_error_fails_closed() -> None:
    """Verify partial-error GraphQL fixtures produce sanitized diagnostics and fail closed."""
    xml_fixture, _ = cli_harness._synthetic_demo_fixtures()

    partial_err_gql = json.dumps(
        {
            "data": {
                "sellerListingSearch": {
                    "total": 2,
                    "items": [
                        {
                            "itemId": "v1|111222333444|0",
                            "title": "Synthetic Handcrafted Ceramic Mug",
                            "formatType": "FIXED_PRICE",
                            "itemEndDate": "2026-11-25T18:00:00Z",
                            "itemWebUrl": "https://www.ebay.com/itm/111222333444",
                            "currentPrice": {"value": "28.50", "currency": "USD"},
                            "quantity": 10,
                            "quantityAvailable": 7,
                            "quantitySold": 3,
                            "views": 42,
                            "watchers": 5,
                            "questions": 1,
                            "offers": 2,
                            "image": {
                                "imageUrl": "https://i.ebayimg.com/images/g/mug123XYZ/s-l500.jpg"
                            },
                        },
                        {
                            "itemId": "555666777888",
                            "title": "Synthetic Vintage Mechanical Watch",
                            "formatType": "AUCTION",
                            "itemEndDate": "2026-11-28T12:00:00.000Z",
                            "itemWebUrl": "https://www.ebay.com/itm/555666777888",
                            "currentPrice": {"value": "195.00", "currency": "USD"},
                            "quantity": 1,
                            "quantityAvailable": 1,
                            "quantitySold": 0,
                            "views": 120,
                            "watchers": 14,
                            "questions": 0,
                            "offers": 0,
                            "image": {
                                "imageUrl": "https://i.ebayimg.com/images/g/watch456UVW/s-l225.jpg"
                            },
                        },
                    ],
                }
            },
            "errors": [
                {
                    "message": "Not authorized to access seller listings",
                    "extensions": {"code": "FORBIDDEN", "category": "APPLICATION"},
                }
            ],
        }
    )

    report = cli_harness.run_offline_fixture_comparison(xml_fixture, partial_err_gql)
    assert report["status"] == "inconclusive"
    assert report["graphql_complete"] is False
    assert report["graphql_available"] == "unauthorized"
    assert report["graphql_error_summary"] is not None
    assert report["graphql_error_summary"]["code"] == "FORBIDDEN"
    assert report["graphql_error_summary"]["is_scope_or_permission_error"] is True
    assert report["fields"]["selling_count"]["classification"] == PARITY_UNVERIFIED
    assert report["fields"]["title"]["classification"] == PARITY_UNVERIFIED
    assert report["fields"]["current_price"]["classification"] == PARITY_UNVERIFIED

    multi_page_with_err = json.dumps(
        [
            {
                "data": {
                    "sellerListingSearch": {
                        "total": 2,
                        "items": [
                            {
                                "itemId": "111222333444",
                                "title": "Ceramic Mug",
                                "currentPrice": {"value": 28.50, "currency": "USD"},
                            }
                        ],
                    }
                }
            },
            {
                "errors": [
                    {
                        "message": "Validation failed on offset parameter",
                        "extensions": {"code": "VALIDATION_ERROR"},
                    }
                ]
            },
        ]
    )
    report_multi = cli_harness.run_offline_fixture_comparison(
        xml_fixture, multi_page_with_err
    )
    assert report_multi["status"] == "inconclusive"
    assert report_multi["graphql_complete"] is False
    assert report_multi["graphql_available"] is False
    assert report_multi["graphql_error_summary"]["is_validation_error"] is True

    err_only_gql = json.dumps(
        {
            "errors": [
                {
                    "message": "Field 'sellerListingSearch' is not defined",
                    "extensions": {"code": "FIELD_UNDEFINED"},
                }
            ]
        }
    )
    report_err_only = cli_harness.run_offline_fixture_comparison(
        xml_fixture, err_only_gql
    )
    assert report_err_only["status"] == "unverified"
    assert report_err_only["graphql_complete"] is False
    assert report_err_only["graphql_available"] is False
    assert report_err_only["graphql_selling_count"] is None
    assert report_err_only["graphql_error_summary"]["is_validation_error"] is True

    malformed_err_gql = json.dumps(
        {
            "data": {"sellerListingSearch": {"total": 0, "items": []}},
            "errors": [
                "Raw string error without dictionary structure",
                {"code": "TOTALLY_UNKNOWN_ERROR_CODE_XYZ"},
            ],
        }
    )
    report_malformed = cli_harness.run_offline_fixture_comparison(
        xml_fixture, malformed_err_gql
    )
    assert report_malformed["status"] == "inconclusive"
    assert report_malformed["graphql_complete"] is False
    assert report_malformed["graphql_error_summary"]["unrecognized_code_count"] >= 1
    assert "MALFORMED_ERROR" in report_malformed["graphql_error_summary"]["error_codes"]


def test_graphql_aliases_and_private_keys_redacted() -> None:
    """Verify caller aliases and non-canonical property names are replaced and never leaked."""
    assert (
        sanitize_graphql_field_name("sellerListingSearch", kind="top_level")
        == "sellerListingSearch"
    )
    assert sanitize_graphql_field_name("__typename", kind="top_level") == "__typename"
    assert (
        sanitize_graphql_field_name("my_secret_account_alias", kind="top_level")
        == "[ALIASED_TOP_LEVEL_FIELD]"
    )
    assert (
        sanitize_graphql_field_name("private_seller_container", kind="container")
        == "[ALIASED_CONTAINER]"
    )
    assert (
        sanitize_graphql_field_name("secret_listing_field", kind="field")
        == "[ALIASED_FIELD]"
    )
    assert sanitize_graphql_field_name("currentPrice", kind="field") == "currentPrice"

    aliased_page = {
        "private_account_alias_12345": {
            "total": 1,
            "items": [
                {
                    "itemId": "123456789",
                    "title": "Aliased Listing",
                }
            ],
        }
    }
    merged = merge_graphql_seller_pages([aliased_page])
    assert "private_account_alias_12345" not in merged["container_keys"]
    assert "[ALIASED_CONTAINER]" in merged["container_keys"]

    dirty_report = {
        "status": "inconclusive",
        "top_level_keys": ["__typename", "private_account_handle_xyz"],
        "graphql_schema_metadata": {
            "container_keys": ["sellerListingSearch", "private_alias_987"],
            "total_field_name": "private_total_calc",
            "items_field_name": "private_items_array",
        },
        "probes": [
            {
                "key": "custom_probe",
                "top_level_keys": ["private_top_key"],
                "summary": "Probe ran for account user_john_doe",
            }
        ],
    }
    clean = sanitize_seller_parity_report(
        dirty_report,
        forbidden_values=["user_john_doe", "private_alias_987"],
    )
    assert clean["top_level_keys"] == ["__typename", "[ALIASED_TOP_LEVEL_FIELD]"]
    assert clean["graphql_schema_metadata"]["container_keys"] == [
        "sellerListingSearch",
        "[ALIASED_CONTAINER]",
    ]
    assert clean["graphql_schema_metadata"]["total_field_name"] == "[ALIASED_FIELD]"
    assert clean["graphql_schema_metadata"]["items_field_name"] == "[ALIASED_FIELD]"
    assert "user_john_doe" not in clean["probes"][0]["summary"]
    assert "[REDACTED]" in clean["probes"][0]["summary"]

    rendered_json = render_seller_parity_json(clean)
    assert "private_account_handle_xyz" not in rendered_json
    assert "private_alias_987" not in rendered_json
    assert "user_john_doe" not in rendered_json

    rendered_md = render_seller_parity_markdown(clean)
    assert "private_account_handle_xyz" not in rendered_md
    assert "private_alias_987" not in rendered_md
    assert "user_john_doe" not in rendered_md


def test_evidence_based_probe_ranking_prioritizes_items_over_counts() -> None:
    """Verify candidate probe ranking strictly prioritizes item-bearing responses over count-only."""
    count_only_candidate = {
        "outcome": "success",
        "pagination_status": "metadata_only",
        "graphql_selling_count": 0,
        "items_list_present": False,
        "raw_items_count": 0,
        "reported_total": 10,
        "duplicate_ids_count": 0,
        "missing_ids_count": 0,
        "is_custom": False,
    }
    item_bearing_unverified = {
        "outcome": "success",
        "pagination_status": "unverified",
        "graphql_selling_count": 5,
        "items_list_present": True,
        "raw_items_count": 5,
        "reported_total": None,
        "duplicate_ids_count": 0,
        "missing_ids_count": 0,
        "is_custom": False,
    }
    item_bearing_truncated = {
        "outcome": "success",
        "pagination_status": "truncated",
        "graphql_selling_count": 5,
        "items_list_present": True,
        "raw_items_count": 5,
        "reported_total": 10,
        "duplicate_ids_count": 0,
        "missing_ids_count": 0,
        "is_custom": False,
    }
    item_bearing_complete = {
        "outcome": "success",
        "pagination_status": "complete",
        "graphql_selling_count": 5,
        "items_list_present": True,
        "raw_items_count": 5,
        "reported_total": 5,
        "duplicate_ids_count": 0,
        "missing_ids_count": 0,
        "is_custom": False,
    }

    rank_count_only = rank_seller_probe_candidate(count_only_candidate)
    rank_items_unverified = rank_seller_probe_candidate(item_bearing_unverified)
    rank_items_truncated = rank_seller_probe_candidate(item_bearing_truncated)
    rank_items_complete = rank_seller_probe_candidate(item_bearing_complete)

    assert rank_items_unverified > rank_count_only
    assert rank_items_truncated > rank_count_only
    assert rank_items_complete > rank_items_unverified
    assert rank_items_complete > rank_items_truncated

    with_dups = dict(item_bearing_unverified)
    with_dups["duplicate_ids_count"] = 2
    rank_with_dups = rank_seller_probe_candidate(with_dups)
    assert rank_items_unverified > rank_with_dups

    custom_cand = dict(item_bearing_complete)
    custom_cand["is_custom"] = True
    rank_custom = rank_seller_probe_candidate(custom_cand)
    assert rank_custom > rank_items_complete


def test_report_metadata_and_markdown_formatting_no_misleading_none() -> None:
    """Verify selected_probe, graphql_available, and table formatting eliminate misleading None."""
    xml_fixture, gql_fixture = cli_harness._synthetic_demo_fixtures()
    report = cli_harness.run_offline_fixture_comparison(xml_fixture, gql_fixture)

    assert "selected_probe" in report
    assert report["selected_probe"] is not None
    assert report["selected_probe"]["key"] == "offline_fixture"
    assert report["selected_probe"]["operation_name"] == "OfflineFixture"
    assert report["selected_probe"]["outcome"] == "success"
    assert report["selected_probe"]["graphql_selling_count"] == 2
    assert report["graphql_available"] is True

    md = render_seller_parity_markdown(report)
    assert "## Selected Contract Probe" in md
    assert "- **Probe Key:** `offline_fixture`" in md
    assert "- **Operation Name:** `OfflineFixture`" in md
    assert "- **GraphQL Seller Listings Available:** `True`" in md
    assert "`None`" not in md

    comp_unauth = compare_seller_snapshots(
        {},
        None,
        graphql_error_summary={
            "is_scope_or_permission_error": True,
            "http_status": 403,
        },
    )
    assert comp_unauth["graphql_available"] == "unauthorized"

    comp_count_only = compare_seller_snapshots(
        {},
        {"sellerListingSearch": {"total": 5}},
    )
    assert comp_count_only["graphql_available"] == "count_only"

    comp_unavailable = compare_seller_snapshots(
        {},
        None,
        graphql_error_summary={
            "is_scope_or_permission_error": False,
            "http_status": 500,
        },
    )
    assert comp_unavailable["graphql_available"] is False


def test_contradictory_pagination_fails_closed() -> None:
    """Verify empty/zero-count pages claiming hasNextPage fail closed to truncated/inconsistent."""
    contradictory_page = {
        "sellerListingSearch": {
            "total": 0,
            "items": [],
            "pageInfo": {"hasNextPage": True},
        }
    }
    merged_1 = merge_graphql_seller_pages([contradictory_page])
    assert merged_1["graphql_complete"] is False
    assert merged_1["graphql_truncated"] is True
    assert merged_1["pagination_status"] == "inconsistent_total"

    contradictory_page_no_total = {
        "sellerListingSearch": {
            "items": [],
            "pageInfo": {"hasNextPage": True},
        }
    }
    merged_2 = merge_graphql_seller_pages([contradictory_page_no_total])
    assert merged_2["graphql_complete"] is False
    assert merged_2["graphql_truncated"] is True
    assert merged_2["pagination_status"] == "truncated"

    valid_empty_page = {
        "sellerListingSearch": {
            "total": 0,
            "items": [],
            "pageInfo": {"hasNextPage": False},
        }
    }
    merged_3 = merge_graphql_seller_pages([valid_empty_page])
    assert merged_3["graphql_complete"] is True
    assert merged_3["graphql_truncated"] is False
    assert merged_3["pagination_status"] == "complete"


@pytest.mark.asyncio
async def test_probe_error_isolation_live_harness() -> None:
    """Verify failed candidate probes do not contaminate a successful selected probe."""
    client = Mock()
    client.environment = "production"
    client.site_id = "0"
    client.refresh_token = "token123"

    sample_xml_item = {
        "111": {
            "item_id": "111",
            "title": "Item 1",
            "current_price": 10.0,
            "currency": "USD",
            "quantity": 1,
            "quantity_available": 1,
            "quantity_sold": 0,
            "views": 5,
            "watchers": 1,
            "questions": 0,
            "offers": 0,
            "end_time": datetime(2026, 11, 1, 12, 0, 0, tzinfo=timezone.utc),
            "listing_type": "FixedPriceItem",
            "url": "https://www.ebay.com/itm/111",
            "image": "https://i.ebayimg.com/images/g/a/s-l140.jpg",
        }
    }
    successful_gql_page = {
        "sellerListingSearch": {
            "total": 1,
            "items": [
                {
                    "itemId": "111",
                    "title": "Item 1",
                    "currentPrice": {"value": 10.0, "currency": "USD"},
                    "quantity": 1,
                    "quantityAvailable": 1,
                    "quantitySold": 0,
                    "views": 5,
                    "watchers": 1,
                    "questions": 0,
                    "offers": 0,
                    "itemEndDate": "2026-11-01T12:00:00Z",
                    "formatType": "FIXED_PRICE",
                    "itemWebUrl": "https://www.ebay.com/itm/111",
                    "image": {
                        "imageUrl": "https://i.ebayimg.com/images/g/a/s-l140.jpg"
                    },
                }
            ],
        }
    }

    # Case A: Probe 1 succeeds with complete items; Probe 2 fails with FORBIDDEN.
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            cli_harness,
            "_fetch_xml_selling_only",
            AsyncMock(return_value=(sample_xml_item, False)),
        )

        async def _mock_fetch_pages_a(c, probe, paginate=False):
            if probe.key == "typename_handshake":
                return [{"__typename": "Query"}]
            if probe.key == "seller_listings_paginated":
                return [successful_gql_page]
            raise EbayGraphQLError(
                "Resolver FORBIDDEN",
                status=403,
                errors=[{"message": "forbidden", "extensions": {"code": "FORBIDDEN"}}],
            )

        mp.setattr(
            cli_harness,
            "async_fetch_bounded_graphql_pages",
            _mock_fetch_pages_a,
        )

        report = await cli_harness.run_live_seller_validation(client)

        assert report["selected_probe_key"] == "seller_listings_paginated"
        assert report["selected_probe"]["outcome"] == "success"
        assert report["graphql_error_summary"] is None
        assert report["status"] == "parity_verified"
        assert report["graphql_complete"] is True
        assert report["graphql_available"] is True

        failed_probes = [p for p in report["probes"] if p["outcome"] == "error"]
        assert len(failed_probes) > 0
        for fp in failed_probes:
            assert fp["error"]["code"] == "FORBIDDEN"
            assert fp["error"]["is_scope_or_permission_error"] is True

    # Case B: Probe 1 fails with VALIDATION_ERROR; Probe 2 succeeds.
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            cli_harness,
            "_fetch_xml_selling_only",
            AsyncMock(return_value=(sample_xml_item, False)),
        )

        async def _mock_fetch_pages_b(c, probe, paginate=False):
            if probe.key == "typename_handshake":
                raise EbayGraphQLError(
                    "Validation failed",
                    status=400,
                    errors=[
                        {
                            "message": "Validation error",
                            "extensions": {"code": "VALIDATION_ERROR"},
                        }
                    ],
                )
            if probe.key == "seller_listings_paginated":
                return [successful_gql_page]
            return [{"sellerListingSearch": {"total": 1}}]

        mp.setattr(
            cli_harness,
            "async_fetch_bounded_graphql_pages",
            _mock_fetch_pages_b,
        )

        report_b = await cli_harness.run_live_seller_validation(client)
        assert report_b["selected_probe_key"] == "seller_listings_paginated"
        assert report_b["graphql_error_summary"] is None
        assert report_b["status"] == "parity_verified"
        assert report_b["probes"][0]["outcome"] == "error"
        assert report_b["probes"][0]["error"]["is_validation_error"] is True

    # Case C: All candidate probes fail.
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            cli_harness,
            "_fetch_xml_selling_only",
            AsyncMock(return_value=(sample_xml_item, False)),
        )

        async def _mock_fetch_pages_c(c, probe, paginate=False):
            raise EbayGraphQLError(
                "Access Denied",
                status=403,
                errors=[
                    {"message": "access denied", "extensions": {"code": "FORBIDDEN"}}
                ],
            )

        mp.setattr(
            cli_harness,
            "async_fetch_bounded_graphql_pages",
            _mock_fetch_pages_c,
        )

        report_c = await cli_harness.run_live_seller_validation(client)
        assert report_c["selected_probe"] is None
        assert report_c["status"] == "unverified"
        assert report_c["graphql_available"] == "unauthorized"
        assert report_c["graphql_error_summary"] is not None
        assert report_c["graphql_error_summary"]["code"] == "FORBIDDEN"

    # Case D: Selected probe returns partial data + errors.
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            cli_harness,
            "_fetch_xml_selling_only",
            AsyncMock(return_value=(sample_xml_item, False)),
        )

        partial_data_with_err = {
            "data": successful_gql_page,
            "errors": [
                {
                    "message": "partial error",
                    "extensions": {"code": "FORBIDDEN"},
                }
            ],
            "sellerListingSearch": successful_gql_page["sellerListingSearch"],
        }

        async def _mock_fetch_pages_d(c, probe, paginate=False):
            if probe.key == "typename_handshake":
                return [{"__typename": "Query"}]
            if probe.key == "seller_listings_paginated":
                return [partial_data_with_err]
            raise EbayGraphQLError(
                "Resolver FORBIDDEN",
                status=403,
                errors=[{"message": "forbidden", "extensions": {"code": "FORBIDDEN"}}],
            )

        mp.setattr(
            cli_harness,
            "async_fetch_bounded_graphql_pages",
            _mock_fetch_pages_d,
        )

        report_d = await cli_harness.run_live_seller_validation(client)
        assert report_d["selected_probe_key"] == "seller_listings_paginated"
        assert report_d["graphql_error_summary"] is not None
        assert report_d["status"] == "inconclusive"
        assert report_d["graphql_complete"] is False
        assert report_d["graphql_available"] == "unauthorized"

    # Case E: Successful probe with unverified pagination.
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            cli_harness,
            "_fetch_xml_selling_only",
            AsyncMock(return_value=(sample_xml_item, False)),
        )

        unverified_pag_page = {
            "sellerListingSearch": {
                "items": successful_gql_page["sellerListingSearch"]["items"],
            }
        }

        async def _mock_fetch_pages_e(c, probe, paginate=False):
            if probe.key == "typename_handshake":
                return [{"__typename": "Query"}]
            if probe.key == "seller_listings_paginated":
                return [unverified_pag_page]
            raise EbayGraphQLError(
                "Resolver FORBIDDEN",
                status=403,
                errors=[{"message": "forbidden", "extensions": {"code": "FORBIDDEN"}}],
            )

        mp.setattr(
            cli_harness,
            "async_fetch_bounded_graphql_pages",
            _mock_fetch_pages_e,
        )

        report_e = await cli_harness.run_live_seller_validation(client)
        assert report_e["selected_probe_key"] == "seller_listings_paginated"
        assert report_e["graphql_error_summary"] is None
        assert report_e["status"] == "inconclusive"
        assert report_e["graphql_complete"] is False
        assert report_e["graphql_available"] is True


@pytest.mark.asyncio
async def test_custom_operation_name_privacy_gap(tmp_path: Path) -> None:
    """Verify custom GraphQL operation names containing private handles are replaced with [CUSTOM_OPERATION]."""
    private_handle = "private_seller_handle_jane_doe_888"
    custom_query_content = f"""
    query {private_handle} {{
        sellerListingSearch {{
            total
        }}
    }}
    """
    query_file = tmp_path / "custom_seller_test.graphql"
    query_file.write_text(custom_query_content, encoding="utf-8")

    custom_probe = cli_harness.build_custom_probe(query_file)
    assert custom_probe.operation_name == private_handle
    assert sanitize_graphql_operation_name(private_handle) == "[CUSTOM_OPERATION]"
    assert (
        sanitize_graphql_operation_name("GetSellerListingsPaginated")
        == "GetSellerListingsPaginated"
    )
    assert sanitize_graphql_operation_name("[CUSTOM_OPERATION]") == "[CUSTOM_OPERATION]"
    assert sanitize_graphql_operation_name(None) is None

    assert sanitize_seller_probe_key("custom_query_file") == "custom_query_file"
    assert sanitize_seller_probe_key("unknown_private_probe_123") == "[CUSTOM_PROBE]"
    assert sanitize_seller_probe_key(None) == "[CUSTOM_PROBE]"

    client = Mock()
    client.environment = "production"
    client.site_id = "0"
    client.refresh_token = "valid_token"

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            cli_harness,
            "_fetch_xml_selling_only",
            AsyncMock(return_value=({}, False)),
        )

        async def _mock_fetch(c, probe, paginate=False):
            if probe.key == "custom_query_file":
                return [{"sellerListingSearch": {"total": 0, "items": []}}]
            return [{"__typename": "Query"}]

        mp.setattr(
            cli_harness,
            "async_fetch_bounded_graphql_pages",
            _mock_fetch,
        )

        report = await cli_harness.run_live_seller_validation(
            client, custom_probe=custom_probe
        )

        custom_probe_result = next(
            p for p in report["probes"] if p["key"] == "custom_query_file"
        )
        assert custom_probe_result["operation_name"] == "[CUSTOM_OPERATION]"
        assert report["selected_probe"]["operation_name"] == "[CUSTOM_OPERATION]"

        rendered_md = render_seller_parity_markdown(report)
        rendered_json = render_seller_parity_json(report)

        assert private_handle not in rendered_md
        assert private_handle not in rendered_json
        assert "[CUSTOM_OPERATION]" in rendered_md
        assert "[CUSTOM_OPERATION]" in rendered_json


def test_field_level_fail_closed_on_incomplete_or_errored_collection() -> None:
    """Verify that when collection completeness cannot be established, every field is unverified."""
    xml_items = {
        "101": {
            "item_id": "101",
            "title": "XML Ceramic Mug",
            "current_price": 28.50,
            "currency": "USD",
            "quantity": 10,
            "quantity_available": 7,
            "quantity_sold": 3,
            "views": 42,
            "watchers": 5,
            "questions": 1,
            "offers": 2,
            "end_time": datetime(2026, 11, 25, 18, 0, 0, tzinfo=timezone.utc),
            "listing_type": "FixedPriceItem",
            "url": "https://www.ebay.com/itm/101",
            "image": "https://i.ebayimg.com/images/g/a/s-l140.jpg",
        }
    }
    gql_partial = {
        "sellerListingSearch": {
            "total": 1,
            "items": [
                {
                    "itemId": "101",
                    "title": "Completely Different Title",
                    "currentPrice": {
                        "amount": 999.0,
                        "currencyCode": "EUR",
                    },
                    "quantity": 999,
                    "quantityAvailable": 900,
                    "quantitySold": 99,
                    "views": 9999,
                    "watchers": 888,
                    "questions": 77,
                    "offers": 66,
                    "itemEndDate": "2029-01-01T00:00:00Z",
                    "formatType": "AUCTION",
                    "itemWebUrl": "https://www.ebay.com/itm/other",
                    "image": None,
                }
            ],
        }
    }

    comp_with_errors = compare_seller_snapshots(
        xml_items,
        gql_partial,
        graphql_error_summary={"error_count": 1, "code": "FORBIDDEN"},
    )
    assert comp_with_errors["status"] == "inconclusive"
    assert comp_with_errors["graphql_complete"] is False

    fields = comp_with_errors["fields"]
    for field_name in (
        "selling_count",
        "item_id",
        "title",
        "current_price",
        "currency",
        "quantity",
        "quantity_available",
        "quantity_sold",
        "views",
        "watchers",
        "questions",
        "offers",
        "end_time",
        "listing_type",
        "url",
        "image",
    ):
        f = fields[field_name]
        assert f["classification"] == PARITY_UNVERIFIED, (
            f"Field {field_name} was {f['classification']}"
        )
        assert f["classification_label"] == "Unverified"

    assert fields["title"]["mismatch_count"] == 1
    assert fields["current_price"]["mismatch_count"] == 1
    assert fields["quantity"]["mismatch_count"] == 1
    assert fields["views"]["mismatch_count"] == 1
    assert fields["image"]["xml_present_count"] == 1
    assert fields["image"]["graphql_present_count"] == 0

    comp_xml_trunc = compare_seller_snapshots(
        xml_items,
        gql_partial,
        xml_truncated=True,
    )
    assert comp_xml_trunc["status"] == "inconclusive"
    for field_name in fields:
        assert (
            comp_xml_trunc["fields"][field_name]["classification"] == PARITY_UNVERIFIED
        )


def test_inventory_api_cannot_claim_complete_census_without_proof() -> None:
    """Verify that inventory items or SKU probes without total or completeness validation fail closed."""
    xml_items = {
        "101": {
            "item_id": "101",
            "title": "Mug",
            "current_price": 28.50,
            "currency": "USD",
            "quantity": 10,
            "quantity_available": 7,
            "quantity_sold": 3,
            "views": 42,
            "watchers": 5,
            "questions": 1,
            "offers": 2,
            "end_time": "2026-11-25T18:00:00Z",
            "listing_type": "fixed_price",
            "url": "https://www.ebay.com/itm/101",
            "image": "https://i.ebayimg.com/images/g/a/s-l140.jpg",
        }
    }

    # Case 1: Inventory probe returns SKU record but no listing ID or total -> incomplete_identity
    inv_probe_payload = {
        "sellerInventory": {
            "inventoryItems": [
                {
                    "sku": "SKU-MUG-001",
                    # Missing listingId / itemId
                    "title": "Mug",
                    "price": {"value": 28.50, "currency": "USD"},
                    "availableQuantity": 7,
                }
            ]
        }
    }
    merged_inv = merge_graphql_seller_pages([inv_probe_payload])
    assert merged_inv["missing_id_count"] == 1
    assert merged_inv["graphql_complete"] is False
    assert merged_inv["pagination_status"] in ("incomplete_identity", "unverified")

    # When compared against XML, must fail closed
    comp_inv = compare_seller_snapshots(xml_items, inv_probe_payload)
    assert comp_inv["status"] == "inconclusive"
    assert comp_inv["graphql_complete"] is False
    assert comp_inv["fields"]["selling_count"]["classification"] == PARITY_UNVERIFIED

    # Case 2: Listing ID preserved with SKU variation
    inv_with_listing_id = {
        "sellerInventory": {
            "total": 1,
            "inventoryItems": [
                {
                    "listingId": "101",
                    "sku": "SKU-MUG-001",
                    "title": "Mug",
                    "price": {"value": 28.50, "currency": "USD"},
                    "availableQuantity": 7,
                }
            ],
        }
    }
    merged_with_id = merge_graphql_seller_pages([inv_with_listing_id])
    assert merged_with_id["missing_id_count"] == 0
    assert "101" in merged_with_id["items_by_id"]
    item_norm = merged_with_id["items_by_id"]["101"]
    assert item_norm["sku"] == "SKU-MUG-001"
    assert item_norm["item_id"] == "101"


def test_seller_parity_extended_coverage_helpers() -> None:
    """Test sanitizers, edge error summaries, and normalization helpers."""
    from custom_components.ebay.parsers.seller_parity import (
        sanitize_diagnostic_token,
        sanitize_graphql_field_name,
        sanitize_graphql_operation_name,
        sanitize_seller_probe_key,
    )

    assert sanitize_graphql_operation_name(None) is None
    assert sanitize_graphql_operation_name("custom_something") == "[CUSTOM_OPERATION]"
    assert sanitize_seller_probe_key(None) == "[CUSTOM_PROBE]"
    assert sanitize_seller_probe_key("unknown_key_123") == "[CUSTOM_PROBE]"

    assert sanitize_graphql_field_name(None) == "[UNKNOWN]"
    assert (
        sanitize_graphql_field_name("custom_alias", kind="field") == "[ALIASED_FIELD]"
    )
    assert (
        sanitize_graphql_field_name("custom_top", kind="top_level")
        == "[ALIASED_TOP_LEVEL_FIELD]"
    )
    assert (
        sanitize_graphql_field_name("custom_container", kind="container")
        == "[ALIASED_CONTAINER]"
    )
    assert (
        sanitize_graphql_field_name("sellerListingSearch", kind="top_level")
        == "sellerListingSearch"
    )

    assert sanitize_diagnostic_token("TOTALLY_UNKNOWN_CODE_XYZ") == "UNKNOWN_CODE"
    assert sanitize_diagnostic_token(None) is None
    assert (
        sanitize_diagnostic_token("ACCESS_DENIED", kind="category")
        == "UNKNOWN_CATEGORY"
    )
    assert sanitize_diagnostic_token("random_domain", kind="domain") == "UNKNOWN_DOMAIN"

    # Exception with scalar or dict raw_errors
    custom_err = EbayAuthError(
        "auth failed",
        status=401,
        errors=[{"message": "secret info", "code": "99999"}],
    )

    summary = summarize_seller_probe_exception(custom_err)
    assert summary["http_status"] == 401
    assert summary["failure_class"] == "auth_error"
    assert summary["unrecognized_code_count"] >= 1

    # Exception with status 403
    forbidden_err = EbayAuthError(
        "Forbidden",
        status=403,
        errors=[{"message": "Forbidden", "error_code": "FORBIDDEN"}],
    )

    summary_403 = summarize_seller_probe_exception(forbidden_err)
    assert summary_403["http_status"] == 403
    assert summary_403["is_scope_or_permission_error"] is True

    # Raw listing_type as list of strings
    item = normalize_graphql_seller_item(
        {"itemId": "123", "listingType": ["FixedPriceItem", "StoreInventory"]}
    )
    assert item["listing_type"] == "FixedPriceItem, StoreInventory"


def test_compare_seller_snapshots_edge_branches() -> None:
    """Test compare_seller_snapshots when XML is None and for status branches."""
    # When XML is None/unavailable
    gql_payload = {
        "sellerListingSearch": {
            "total": 1,
            "items": [
                {
                    "itemId": "101",
                    "title": "Widget",
                    "currentPrice": {"value": 15.0, "currency": "USD"},
                }
            ],
        }
    }
    res_no_xml = compare_seller_snapshots(
        xml_selling=None,
        graphql_payload=gql_payload,
        xml_available=False,
    )
    assert res_no_xml["status"] == "unverified"
    assert res_no_xml["xml_available"] is False
    assert res_no_xml["graphql_available"] is True

    # When GraphQL is unauthorized
    res_unauth = compare_seller_snapshots(
        xml_selling=None,
        graphql_payload=gql_payload,
        xml_available=False,
        graphql_error_summary={
            "http_status": 403,
            "is_scope_or_permission_error": True,
        },
    )
    assert res_unauth["graphql_available"] == "unauthorized"

    # When XML has questions and views but GraphQL has none -> missing_fields status
    xml_items = {
        "101": {
            "item_id": "101",
            "title": "Item 101",
            "current_price": 25.0,
            "currency": "USD",
            "quantity": 1,
            "quantity_available": 1,
            "quantity_sold": 0,
            "views": 50,
            "watchers": 5,
            "questions": 2,
            "offers": 0,
            "end_time": "2026-11-01T00:00:00Z",
            "listing_type": "FixedPriceItem",
            "url": "https://www.ebay.com/itm/101",
            "image": "https://img.ebay.com/101.jpg",
        }
    }
    gql_items_missing_fields = {
        "sellerListingSearch": {
            "total": 1,
            "items": [
                {
                    "itemId": "101",
                    "title": "Item 101",
                    "currentPrice": {"value": 25.0, "currency": "USD"},
                    "quantity": 1,
                    "quantityAvailable": 1,
                    "quantitySold": 0,
                    # views, watchers, questions, offers omitted -> missing_field
                    "endAt": "2026-11-01T00:00:00Z",
                    "format": "FIXED_PRICE",
                    "itemWebUrl": "https://www.ebay.com/itm/101",
                    "image": {"imageUrl": "https://img.ebay.com/101.jpg"},
                }
            ],
        }
    }
    res_missing = compare_seller_snapshots(xml_items, gql_items_missing_fields)
    assert res_missing["status"] == "missing_fields"
    assert res_missing["fields"]["views"]["classification"] == PARITY_MISSING_FIELD

    # When items count matches but reported_total differs
    gql_reported_diff = {
        "sellerListingSearch": {
            "total": 99,
            "items": [
                {
                    "itemId": "101",
                    "title": "Item 101",
                    "currentPrice": {"value": 25.0, "currency": "USD"},
                    "quantity": 1,
                    "availableQuantity": 1,
                    "quantitySold": 0,
                    "watchers": 5,
                    "views": 50,
                    "questions": 2,
                    "bestOfferCount": 0,
                    "listingDetails": {"endTime": "2026-11-01T00:00:00Z"},
                    "listingType": "FixedPriceItem",
                    "viewItemUrl": "https://www.ebay.com/itm/101",
                    "pictureDetails": {"galleryUrl": "https://img.ebay.com/101.jpg"},
                }
            ],
        }
    }
    res_diff_total = compare_seller_snapshots(xml_items, gql_reported_diff)
    assert (
        res_diff_total["fields"]["selling_count"]["classification"] == PARITY_UNVERIFIED
    )


def test_cli_live_with_real_client_constructor_and_mocked_http(tmp_path: Path) -> None:
    """CLI --live executes with real EbayApiClient constructor and real async_post_graphql interface."""
    env_file = tmp_path / ".env.local"
    env_file.write_text(
        "EBAY_ACCESS_TOKEN=short-lived-test-token-xyz\n"
        "EBAY_SITE_ID=0\n"
        "EBAY_ENVIRONMENT=production\n",
        encoding="utf-8",
    )
    out_md = tmp_path / "seller_parity.local.md"

    xml_demo, gql_demo = cli_harness._synthetic_demo_fixtures()
    gql_data = json.loads(gql_demo)["data"]

    with (
        patch.object(
            EbayApiClient,
            "async_call_trading_api",
            new_callable=AsyncMock,
            return_value=ET.fromstring(xml_demo),
        ) as mock_trading,
        patch.object(
            EbayApiClient,
            "async_post_graphql",
            new_callable=AsyncMock,
            return_value=gql_data,
        ) as mock_graphql,
    ):
        exit_code = asyncio.run(
            cli_harness._async_main(
                [
                    "--live",
                    "--env-file",
                    str(env_file),
                    "--output",
                    str(out_md),
                ]
            )
        )

    assert exit_code == 0
    assert mock_trading.called
    assert mock_graphql.called
    assert out_md.exists()
    content = out_md.read_text(encoding="utf-8")
    assert (
        "# eBay Active Seller Listings API Contract and Parity Validation Report"
        in content
    )
    assert "short-lived-test-token-xyz" not in content


def test_multipage_conflicting_totals_fail_closed() -> None:
    """Conflicting reported totals across pages (e.g. 2 vs 3) fail closed to inconsistent_total and unverified."""
    page1 = {
        "sellerListingSearch": {
            "total": 2,
            "items": [
                {
                    "itemId": "101",
                    "title": "Widget One",
                    "currentPrice": {"value": 10.0, "currency": "USD"},
                    "quantity": 1,
                    "quantityAvailable": 1,
                    "quantitySold": 0,
                    "watchers": 2,
                    "views": 20,
                    "questions": 0,
                    "bestOfferCount": 0,
                    "listingDetails": {"endTime": "2026-11-01T00:00:00Z"},
                    "listingType": "FixedPriceItem",
                    "viewItemUrl": "https://www.ebay.com/itm/101",
                    "pictureDetails": {"galleryUrl": "https://img.ebay.com/101.jpg"},
                }
            ],
            "pageInfo": {"hasNextPage": True},
        }
    }
    page2 = {
        "sellerListingSearch": {
            "total": 3,
            "items": [
                {
                    "itemId": "102",
                    "title": "Widget Two",
                    "currentPrice": {"value": 20.0, "currency": "USD"},
                    "quantity": 1,
                    "quantityAvailable": 1,
                    "quantitySold": 0,
                    "watchers": 5,
                    "views": 50,
                    "questions": 1,
                    "bestOfferCount": 0,
                    "listingDetails": {"endTime": "2026-11-02T00:00:00Z"},
                    "listingType": "FixedPriceItem",
                    "viewItemUrl": "https://www.ebay.com/itm/102",
                    "pictureDetails": {"galleryUrl": "https://img.ebay.com/102.jpg"},
                }
            ],
            "pageInfo": {"hasNextPage": False},
        }
    }

    # 1. merge_graphql_seller_pages fails closed on contradictory totals
    merged = merge_graphql_seller_pages([page1, page2])
    assert merged["graphql_complete"] is False
    assert merged["pagination_status"] == "inconsistent_total"
    assert merged["raw_item_count"] == 2
    assert len(merged["items_by_id"]) == 2

    # 2. compare_seller_snapshots with matching 2 items in XML must fail closed
    xml_items = {
        "101": {
            "item_id": "101",
            "title": "Widget One",
            "current_price": 10.0,
            "currency": "USD",
            "quantity": 1,
            "quantity_available": 1,
            "quantity_sold": 0,
            "views": 20,
            "watchers": 2,
            "questions": 0,
            "offers": 0,
            "end_time": "2026-11-01T00:00:00Z",
            "listing_type": "FixedPriceItem",
            "url": "https://www.ebay.com/itm/101",
            "image": "https://img.ebay.com/101.jpg",
        },
        "102": {
            "item_id": "102",
            "title": "Widget Two",
            "current_price": 20.0,
            "currency": "USD",
            "quantity": 1,
            "quantity_available": 1,
            "quantity_sold": 0,
            "views": 50,
            "watchers": 5,
            "questions": 1,
            "offers": 0,
            "end_time": "2026-11-02T00:00:00Z",
            "listing_type": "FixedPriceItem",
            "url": "https://www.ebay.com/itm/102",
            "image": "https://img.ebay.com/102.jpg",
        },
    }

    comparison = compare_seller_snapshots(xml_items, [page1, page2])
    assert comparison["status"] == "inconclusive"
    assert comparison["graphql_complete"] is False
    assert comparison["pagination_status"] == "inconsistent_total"

    # All 16 monitored fields MUST be classified unverified
    for field_name in MONITORED_SELLER_FIELDS:
        field_result = comparison["fields"][field_name]
        assert field_result["classification"] == PARITY_UNVERIFIED, (
            f"Field {field_name} was {field_result['classification']}, expected {PARITY_UNVERIFIED}"
        )


def test_multipage_continuation_and_consistent_totals() -> None:
    """Test conflicting continuation indicators and fully consistent multi-page collections."""
    page_no_next = {
        "sellerListingSearch": {
            "total": 2,
            "items": [{"itemId": "101", "title": "Item 1"}],
            "pageInfo": {"hasNextPage": False},
        }
    }
    page_extra = {
        "sellerListingSearch": {
            "total": 2,
            "items": [{"itemId": "102", "title": "Item 2"}],
            "pageInfo": {"hasNextPage": False},
        }
    }

    # Prior page claimed hasNextPage: False, but subsequent page followed -> conflicting continuation
    merged_conflict = merge_graphql_seller_pages([page_no_next, page_extra])
    assert merged_conflict["graphql_complete"] is False
    assert merged_conflict["pagination_status"] == "conflicting_continuation"

    # Multi-page consistent complete retrieval
    page_consistent_1 = {
        "sellerListingSearch": {
            "total": 2,
            "items": [
                {
                    "itemId": "101",
                    "title": "Item 1",
                    "currentPrice": {"value": 10.0, "currency": "USD"},
                    "quantity": 1,
                    "quantityAvailable": 1,
                    "quantitySold": 0,
                    "views": 10,
                    "watchers": 1,
                    "questions": 0,
                    "bestOfferCount": 0,
                    "listingDetails": {"endTime": "2026-11-01T00:00:00Z"},
                    "listingType": "FixedPriceItem",
                    "viewItemUrl": "https://www.ebay.com/itm/101",
                    "pictureDetails": {"galleryUrl": "https://img.ebay.com/101.jpg"},
                }
            ],
            "pageInfo": {"hasNextPage": True},
        }
    }
    page_consistent_2 = {
        "sellerListingSearch": {
            "total": 2,
            "items": [
                {
                    "itemId": "102",
                    "title": "Item 2",
                    "currentPrice": {"value": 20.0, "currency": "USD"},
                    "quantity": 1,
                    "quantityAvailable": 1,
                    "quantitySold": 0,
                    "views": 20,
                    "watchers": 2,
                    "questions": 0,
                    "bestOfferCount": 0,
                    "listingDetails": {"endTime": "2026-11-02T00:00:00Z"},
                    "listingType": "FixedPriceItem",
                    "viewItemUrl": "https://www.ebay.com/itm/102",
                    "pictureDetails": {"galleryUrl": "https://img.ebay.com/102.jpg"},
                }
            ],
            "pageInfo": {"hasNextPage": False},
        }
    }

    merged_consistent = merge_graphql_seller_pages(
        [page_consistent_1, page_consistent_2]
    )
    assert merged_consistent["graphql_complete"] is True
    assert merged_consistent["pagination_status"] == "complete"
    assert merged_consistent["reported_total"] == 2
    assert len(merged_consistent["items_by_id"]) == 2

    # Incomplete pagination (total reported 3, but only 2 retrieved)
    page_inc_1 = {
        "sellerListingSearch": {
            "total": 3,
            "items": [{"itemId": "101", "title": "Item 1"}],
            "pageInfo": {"hasNextPage": True},
        }
    }
    page_inc_2 = {
        "sellerListingSearch": {
            "total": 3,
            "items": [{"itemId": "102", "title": "Item 2"}],
            "pageInfo": {"hasNextPage": False},
        }
    }
    merged_inc = merge_graphql_seller_pages([page_inc_1, page_inc_2])
    assert merged_inc["graphql_complete"] is False
    assert merged_inc["pagination_status"] == "truncated"
