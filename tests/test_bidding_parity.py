"""Unit tests for the Milestone 4 Auction and Bidding XML vs. GraphQL parity harness."""

from __future__ import annotations

import asyncio
import json
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, Mock

import aiohttp
import pytest

import scripts.validate_bidding_parity as cli_harness
from custom_components.ebay.clients.errors import (
    EbayApiError,
    EbayAuthError,
    EbayAuthTransientError,
    EbayGraphQLError,
    EbayParseError,
)
from custom_components.ebay.parsers.bidding_parity import (
    BIDDING_CONTRACT_PROBES,
    PARITY_DIFFERENT_SEMANTICS,
    PARITY_EQUIVALENT_NORMALIZED,
    PARITY_EXACT_MATCH,
    PARITY_MISSING_FIELD,
    PARITY_UNVERIFIED,
    _extract_money,
    _to_bool_or_none,
    compare_bidding_snapshots,
    infer_and_validate_query_operation,
    merge_graphql_bidding_pages,
    normalize_bidding_item_id,
    normalize_graphql_bidding_payload,
    normalize_listing_type_category,
    parse_query_variables,
    rank_bidding_probe_candidate,
    render_bidding_parity_json,
    render_bidding_parity_markdown,
    sanitize_bidding_parity_report,
    sanitize_diagnostic_token,
    summarize_bidding_probe_exception,
    validate_all_bidding_contract_probes,
)


def test_all_builtin_bidding_contract_probes_are_strictly_read_only() -> None:
    """All built-in contract probes must pass validate_read_only_query."""
    validate_all_bidding_contract_probes()
    assert len(BIDDING_CONTRACT_PROBES) >= 5
    for probe in BIDDING_CONTRACT_PROBES:
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
            "mutation PlaceBid { placeBid(id: 123) { status } }"
        )

    with pytest.raises(ValueError, match="Subscriptions are strictly prohibited"):
        infer_and_validate_query_operation(
            "subscription OnBidPlaced { bidPlaced { id } }"
        )


def test_normalize_bidding_item_id_variants() -> None:
    """Item ID normalizer correctly handles composite, numeric, opaque, and missing formats."""
    assert normalize_bidding_item_id("111222333444") == (
        "111222333444",
        "legacy_numeric",
    )
    assert normalize_bidding_item_id("v1|111222333444|0") == (
        "111222333444",
        "composite_v1",
    )
    assert normalize_bidding_item_id("v1|111222333444|999888") == (
        "111222333444",
        "composite_v1",
    )
    assert normalize_bidding_item_id("custom-sku-99") == (
        "custom-sku-99",
        "opaque_string",
    )
    assert normalize_bidding_item_id(None) == ("", "missing")
    assert normalize_bidding_item_id("   ") == ("", "empty")
    assert normalize_bidding_item_id(123456789012) == ("123456789012", "legacy_numeric")


def test_normalize_listing_type_category_variants() -> None:
    """Auction, fixed price, and classified formats map to standard categories."""
    assert normalize_listing_type_category("Chinese") == "auction"
    assert normalize_listing_type_category("AUCTION") == "auction"
    assert normalize_listing_type_category(["AUCTION", "BEST_OFFER"]) == "auction"
    assert normalize_listing_type_category("FixedPriceItem") == "fixed_price"
    assert normalize_listing_type_category("FIXED_PRICE") == "fixed_price"
    assert normalize_listing_type_category(["FIXED_PRICE"]) == "fixed_price"
    assert normalize_listing_type_category("ClassifiedAd") == "classifiedad"
    assert normalize_listing_type_category(None) is None
    assert normalize_listing_type_category("   ") is None
    assert normalize_listing_type_category([]) is None


def test_extract_money_and_to_bool_or_none() -> None:
    """Monetary values and booleans extract cleanly from diverse GraphQL shapes."""
    val, cur, was_str = _extract_money({"value": "149.50", "currency": "USD"})
    assert val == 149.50
    assert cur == "USD"
    assert was_str is True

    val2, cur2, was_str2 = _extract_money({"amount": 75.25, "currencyCode": "gbp"})
    assert val2 == 75.25
    assert cur2 == "GBP"
    assert was_str2 is False

    val3, cur3, was_str3 = _extract_money({"value": "199.99"})
    assert val3 == 199.99
    assert cur3 is None
    assert was_str3 is True

    assert _extract_money(None) == (None, None, False)
    assert _extract_money({}) == (None, None, False)

    assert _to_bool_or_none(True) is True
    assert _to_bool_or_none(False) is False
    assert _to_bool_or_none("true") is True
    assert _to_bool_or_none("True") is True
    assert _to_bool_or_none("false") is False
    assert _to_bool_or_none("1") is True
    assert _to_bool_or_none("0") is False
    assert _to_bool_or_none(1) is True
    assert _to_bool_or_none(0) is False
    assert _to_bool_or_none(None) is None
    assert _to_bool_or_none("unknown") is None


def test_normalize_graphql_bidding_payload_items_and_bids_shapes() -> None:
    """Normalization parses both items[] and bids[] containers and composite IDs."""
    # 1. items shape
    payload_items = {
        "userBiddingActivity": {
            "total": 1,
            "items": [
                {
                    "itemId": "v1|123456789012|0",
                    "title": "Rare Coin",
                    "currentPrice": {"value": "50.00", "currency": "USD"},
                    "maxBid": {"value": "60.00", "currency": "USD"},
                    "winning": True,
                    "bidCount": 5,
                    "itemEndDate": "2026-12-01T12:00:00Z",
                    "itemWebUrl": "https://www.ebay.com/itm/123456789012",
                    "image": {
                        "imageUrl": "https://i.ebayimg.com/images/g/coin/s-l500.jpg"
                    },
                    "formatType": "AUCTION",
                }
            ],
        }
    }
    norm_items = normalize_graphql_bidding_payload(payload_items)
    assert norm_items["reported_total"] == 1
    assert norm_items["items_field_name"] == "items"
    assert norm_items["composite_id_count"] == 1
    assert "123456789012" in norm_items["items_by_id"]
    item = norm_items["items_by_id"]["123456789012"]
    assert item["current_price"] == 50.0
    assert item["currency"] == "USD"
    assert item["max_bid"] == 60.0
    assert item["winning"] is True
    assert item["bid_count"] == 5

    # 2. bids shape (alternate candidate probe)
    payload_bids = {
        "userBiddingActivity": {
            "activeBiddingCount": 1,
            "bids": [
                {
                    "itemId": "987654321098",
                    "title": "Vintage Watch",
                    "currentPrice": {"value": "120.00", "currency": "USD"},
                    "maxBid": {"value": "150.00", "currency": "USD"},
                    "highBidder": False,
                    "endDate": "2026-11-20T10:00:00.000Z",
                    "image": {
                        "imageUrl": "https://i.ebayimg.com/images/g/watch/s-l225.jpg"
                    },
                }
            ],
        }
    }
    norm_bids = normalize_graphql_bidding_payload(payload_bids)
    assert norm_bids["reported_total"] == 1
    assert norm_bids["items_field_name"] == "bids"
    assert "987654321098" in norm_bids["items_by_id"]
    item_b = norm_bids["items_by_id"]["987654321098"]
    assert item_b["winning"] is False
    assert item_b["max_bid"] == 150.0


def test_diagnostic_privacy_allowlist_redacts_item_ids_accounts_and_secrets() -> None:
    """Allowlist strictly preserves documented error codes and redacts arbitrary strings."""
    # Allowlisted error codes
    assert (
        sanitize_diagnostic_token("VALIDATION_ERROR", kind="code") == "VALIDATION_ERROR"
    )
    assert sanitize_diagnostic_token("FORBIDDEN", kind="code") == "FORBIDDEN"
    assert (
        sanitize_diagnostic_token("UNAUTHENTICATED", kind="code") == "UNAUTHENTICATED"
    )
    assert sanitize_diagnostic_token("RATE_LIMITED", kind="code") == "RATE_LIMITED"
    assert sanitize_diagnostic_token("ACCESS_DENIED", kind="code") == "ACCESS_DENIED"

    # Allowlisted category
    assert sanitize_diagnostic_token("REQUEST", kind="category") == "REQUEST"
    assert sanitize_diagnostic_token("APPLICATION", kind="category") == "APPLICATION"

    # Documented numeric codes
    assert sanitize_diagnostic_token("11001", kind="code") == "11001"
    assert sanitize_diagnostic_token(11001, kind="code") == "11001"

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


def test_summarize_bidding_probe_exception_http_400_and_resolver_forbidden() -> None:
    """HTTP 400 validation error vs resolver FORBIDDEN are distinguished cleanly."""
    # 1. Resolver error: FORBIDDEN
    forbidden_err = EbayAuthError(
        "eBay GraphQL permission error (insufficient scope): Access denied",
        is_expired=False,
        is_permission_error=True,
        errors=[
            {
                "message": "Access is denied to userBiddingActivity",
                "code": "FORBIDDEN",
                "error_code": "FORBIDDEN",
                "category": "REQUEST",
                "domain": "BUY_AUCTION",
            }
        ],
        status=200,
    )
    summary_f = summarize_bidding_probe_exception(forbidden_err)
    assert summary_f["failure_class"] == "permission_or_scope_error"
    assert summary_f["category"] == "REQUEST"
    assert "FORBIDDEN" in summary_f["error_codes"]
    assert summary_f["http_status"] == 200

    # 2. HTTP 400 schema validation error
    val_err = EbayGraphQLError(
        "eBay GraphQL query validation failed (HTTP 400) for GetBiddingTotal",
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
    summary_v = summarize_bidding_probe_exception(val_err)
    assert summary_v["failure_class"] == "validation_error"
    assert summary_v["http_status"] == 400
    assert "VALIDATION_ERROR" in summary_v["error_codes"]

    # 3. HTTP 400 with unclassified body
    raw_400 = EbayApiError("Bad Request", status=400)
    summary_raw = summarize_bidding_probe_exception(raw_400)
    assert summary_raw["failure_class"] == "bad_request"
    assert summary_raw["http_status"] == 400


def test_completeness_and_pagination_fail_closed() -> None:
    """Truncated, partial, or unreconciled responses fail closed to inconclusive."""
    xml_items = {
        "111": {
            "item_id": "111",
            "title": "Camera",
            "current_price": 100.0,
            "currency": "USD",
            "max_bid": 120.0,
            "winning": True,
            "bid_count": 3,
            "end_time": "2026-11-15T00:00:00Z",
            "listing_type": "auction",
            "url": "https://www.ebay.com/itm/111",
            "image": "https://i.ebayimg.com/images/g/a/s-l140.jpg",
        },
        "222": {
            "item_id": "222",
            "title": "Lens",
            "current_price": 50.0,
            "currency": "USD",
            "max_bid": 60.0,
            "winning": False,
            "bid_count": 1,
            "end_time": "2026-11-16T00:00:00Z",
            "listing_type": "auction",
            "url": "https://www.ebay.com/itm/222",
            "image": "https://i.ebayimg.com/images/g/b/s-l140.jpg",
        },
    }

    # Case A: GraphQL returns only 1 item while XML has 2
    gql_partial = {
        "userBiddingActivity": {
            "total": 2,
            "items": [
                {
                    "itemId": "111",
                    "title": "Camera",
                    "currentPrice": {"value": "100.00", "currency": "USD"},
                    "maxBid": {"value": "120.00", "currency": "USD"},
                    "winning": True,
                    "bidCount": 3,
                    "itemEndDate": "2026-11-15T00:00:00Z",
                    "itemWebUrl": "https://www.ebay.com/itm/111",
                    "image": {
                        "imageUrl": "https://i.ebayimg.com/images/g/a/s-l140.jpg"
                    },
                    "formatType": "AUCTION",
                }
            ],
        }
    }
    comp_a = compare_bidding_snapshots(xml_items, gql_partial)
    assert comp_a["status"] == "inconclusive"
    assert comp_a["fields"]["bidding_count"]["classification"] == PARITY_UNVERIFIED
    assert comp_a["fields"]["max_bid"]["classification"] == PARITY_UNVERIFIED
    assert comp_a["fields"]["winning"]["classification"] == PARITY_UNVERIFIED

    # Case B: XML is truncated
    comp_b = compare_bidding_snapshots(xml_items, gql_partial, xml_truncated=True)
    assert comp_b["status"] == "inconclusive"
    assert comp_b["fields"]["bidding_count"]["classification"] == PARITY_UNVERIFIED

    # Case C: GraphQL has duplicate item IDs
    gql_dupes = {
        "userBiddingActivity": {
            "total": 2,
            "items": [
                {"itemId": "111", "title": "Camera 1"},
                {"itemId": "111", "title": "Camera 2"},
            ],
        }
    }
    comp_c = compare_bidding_snapshots(xml_items, gql_dupes)
    assert comp_c["status"] == "inconclusive"
    assert comp_c["duplicate_ids_count"] == 1


def test_compare_bidding_snapshots_all_classifications() -> None:
    """Verifies exact match, normalized match, different semantics, and missing fields."""
    xml_bidding = {
        "100": {
            "item_id": "100",
            "title": "Antique Clock",
            "current_price": 85.0,
            "currency": "USD",
            "max_bid": 110.0,
            "winning": True,
            "bid_count": 5,
            "end_time": "2026-11-10T15:00:00.000Z",
            "listing_type": "Chinese",
            "url": "https://www.ebay.com/itm/Antique-Clock/100?hash=item1",
            "image": "https://i.ebayimg.com/images/g/abc/s-l140.jpg",
        }
    }

    # Matches after normalization: composite ID, string price/max_bid, UTC format, listing format
    gql_payload = {
        "userBiddingActivity": {
            "total": 1,
            "items": [
                {
                    "itemId": "v1|100|0",
                    "title": "Antique Clock",
                    "currentPrice": {"value": "85.00", "currency": "USD"},
                    "maxBid": {"value": "110.00", "currency": "USD"},
                    "winning": True,
                    "bidCount": 5,
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

    comp = compare_bidding_snapshots(xml_bidding, gql_payload)
    assert comp["status"] == "parity_verified"
    fields = comp["fields"]

    assert fields["bidding_count"]["classification"] == PARITY_EXACT_MATCH
    assert fields["title"]["classification"] == PARITY_EXACT_MATCH
    assert fields["currency"]["classification"] == PARITY_EXACT_MATCH
    assert fields["winning"]["classification"] == PARITY_EXACT_MATCH
    assert fields["bid_count"]["classification"] == PARITY_EXACT_MATCH

    assert fields["item_id"]["classification"] == PARITY_EQUIVALENT_NORMALIZED
    assert fields["current_price"]["classification"] == PARITY_EQUIVALENT_NORMALIZED
    assert fields["max_bid"]["classification"] == PARITY_EQUIVALENT_NORMALIZED
    assert fields["end_time"]["classification"] == PARITY_EQUIVALENT_NORMALIZED
    assert fields["listing_type"]["classification"] == PARITY_EQUIVALENT_NORMALIZED
    assert fields["url"]["classification"] == PARITY_EQUIVALENT_NORMALIZED
    assert fields["image"]["classification"] == PARITY_EQUIVALENT_NORMALIZED


def test_compare_bidding_snapshots_different_semantics_and_missing_field() -> None:
    """Verifies different semantics on mismatched values and missing field detection."""
    xml_bidding = {
        "100": {
            "item_id": "100",
            "title": "Antique Clock",
            "current_price": 85.0,
            "currency": "USD",
            "max_bid": 110.0,
            "winning": True,
            "bid_count": 5,
            "end_time": "2026-11-10T15:00:00.000Z",
            "listing_type": "Chinese",
            "url": "https://www.ebay.com/itm/100",
            "image": "https://i.ebayimg.com/images/g/abc/s-l140.jpg",
        }
    }

    # GraphQL has winning=False (mismatch) and omits maxBid
    gql_payload = {
        "userBiddingActivity": {
            "total": 1,
            "items": [
                {
                    "itemId": "100",
                    "title": "Antique Clock",
                    "currentPrice": {"value": "85.00", "currency": "USD"},
                    "winning": False,  # Mismatch
                    "bidCount": 5,
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

    comp = compare_bidding_snapshots(xml_bidding, gql_payload)
    assert comp["status"] == "semantic_differences"
    fields = comp["fields"]
    assert fields["winning"]["classification"] == PARITY_DIFFERENT_SEMANTICS
    assert fields["max_bid"]["classification"] == PARITY_MISSING_FIELD


def test_rank_bidding_probe_candidate() -> None:
    """Candidates with complete item collections outrank partial or count-only probes."""
    cand_items_complete = {
        "outcome": "success",
        "pagination_status": "complete",
        "graphql_bidding_count": 5,
        "items_list_present": True,
        "raw_items_count": 5,
        "reported_total": 5,
        "duplicate_ids_count": 0,
        "missing_ids_count": 0,
    }
    cand_count_only = {
        "outcome": "success",
        "pagination_status": "metadata_only",
        "graphql_bidding_count": 0,
        "items_list_present": False,
        "raw_items_count": 0,
        "reported_total": 5,
        "duplicate_ids_count": 0,
        "missing_ids_count": 0,
    }
    cand_with_dupes = {
        "outcome": "success",
        "pagination_status": "unverified",
        "graphql_bidding_count": 4,
        "items_list_present": True,
        "raw_items_count": 5,
        "reported_total": 5,
        "duplicate_ids_count": 1,
        "missing_ids_count": 0,
    }

    rank_complete = rank_bidding_probe_candidate(cand_items_complete)
    rank_count = rank_bidding_probe_candidate(cand_count_only)
    rank_dupes = rank_bidding_probe_candidate(cand_with_dupes)

    assert rank_complete > rank_count
    assert rank_complete > rank_dupes


def test_sanitize_bidding_parity_report_privacy() -> None:
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

    clean = sanitize_bidding_parity_report(
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


def test_render_bidding_parity_markdown_and_json() -> None:
    """Renderers produce properly formatted Markdown and valid JSON without leaking secrets."""
    report = {
        "status": "parity_verified",
        "environment": "production",
        "site_id": "0",
        "xml_available": True,
        "xml_bidding_count": 1,
        "xml_truncated": False,
        "graphql_bidding_count": 1,
        "graphql_complete": True,
        "pagination_status": "complete",
        "common_items_count": 1,
        "composite_ids_normalized_count": 0,
        "duplicate_ids_count": 0,
        "missing_ids_count": 0,
        "fields": {
            "bidding_count": {
                "field": "bidding_count",
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

    md = render_bidding_parity_markdown(report)
    assert "# eBay Auction and Bidding API Contract and Parity Validation Report" in md
    assert "parity_verified" in md
    assert "bidding_count" in md

    json_str = render_bidding_parity_json(report)
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
    out_md = tmp_path / "bidding_parity.md"
    out_json = tmp_path / "bidding_parity.json"

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
        "eBay Auction and Bidding API Contract and Parity Validation Report"
        in md_content
    )
    assert "parity_verified" in md_content

    json_content = out_json.read_text(encoding="utf-8")
    parsed = json.loads(json_content)
    assert parsed["status"] == "parity_verified"
    assert parsed["common_items_count"] == 2


def test_cli_offline_fixtures(tmp_path: Path) -> None:
    """CLI correctly executes offline comparison against fixture files."""
    xml_file = tmp_path / "bidding.xml"
    gql_file = tmp_path / "bidding.json"
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
        "EBAY_REFRESH_TOKEN=refresh789\n"
        "EBAY_SITE_ID=3\n"
    )
    creds_dotenv = cli_harness.resolve_live_credentials(
        env_file=dotenv_file,
        environ={},
    )
    assert creds_dotenv["client_id"] == "client123"
    assert creds_dotenv["client_secret"] == "secret456"
    assert creds_dotenv["refresh_token"] == "refresh789"
    assert creds_dotenv["site_id"] == "3"

    # 3. Missing credentials raises ValueError
    with pytest.raises(ValueError, match="Missing required eBay live credentials"):
        cli_harness.resolve_live_credentials(
            env_file=tmp_path / "empty.env", environ={}
        )


def test_run_live_bidding_validation_with_mocked_client() -> None:
    """run_live_bidding_validation queries XML BidList and GraphQL probes safely."""
    xml_demo, gql_demo = cli_harness._synthetic_demo_fixtures()
    mock_client = Mock()
    mock_client.environment = "production"
    mock_client.site_id = "0"
    mock_client.refresh_token = "mock-refresh"

    # Async mock for Trading API
    mock_client.async_call_trading_api = AsyncMock(return_value=ET.fromstring(xml_demo))

    # Async mock for GraphQL API
    gql_dict = json.loads(gql_demo)["data"]
    mock_client.async_post_graphql = AsyncMock(return_value=gql_dict)

    report = asyncio.run(cli_harness.run_live_bidding_validation(mock_client))
    assert report["mode"] == "live_authenticated"
    assert report["xml_available"] is True
    assert report["status"] == "parity_verified"
    assert report["selected_probe_key"] is not None
    assert len(report["probes"]) >= 5


def test_parse_query_variables_and_extract_declared() -> None:
    """Variable declarations extract types and default values accurately."""
    query = """
    query GetBids($limit: Int, $offset: Int, $filter: String!) {
      userBiddingActivity(limit: $limit, offset: $offset) { total }
    }
    """
    vars_parsed = parse_query_variables(query, {"limit": 50, "filter": "ACTIVE"})
    assert vars_parsed is not None
    assert vars_parsed["limit"] == 50
    assert vars_parsed["offset"] == 0
    assert vars_parsed["filter"] == "ACTIVE"

    vars_default = parse_query_variables(query, {"filter": "ACTIVE"})
    assert vars_default is not None
    assert vars_default["limit"] == 100

    # Query with no variables declared
    assert parse_query_variables("query Simple { __typename }") is None
    assert parse_query_variables("query Simple { __typename }", {"foo": "bar"}) == {
        "foo": "bar"
    }


def test_summarize_bidding_probe_exception_diverse_error_types() -> None:
    """Exception summarizer maps auth transient, parse, client, timeout, and api errors."""
    # Auth transient
    summary_at = summarize_bidding_probe_exception(
        EbayAuthTransientError("Token refresh failed")
    )
    assert summary_at["failure_class"] == "auth_transient_error"

    # Parse error
    summary_pe = summarize_bidding_probe_exception(EbayParseError("Malformed XML"))
    assert summary_pe["failure_class"] == "parse_error"

    # Client error
    summary_ce = summarize_bidding_probe_exception(
        aiohttp.ClientError("Connection reset")
    )
    assert summary_ce["failure_class"] == "transport_error"

    # Timeout error
    summary_to = summarize_bidding_probe_exception(TimeoutError("Request timed out"))
    assert summary_to["failure_class"] == "transport_error"

    # 429 in message
    summary_rl = summarize_bidding_probe_exception(
        EbayApiError("Rate limit exceeded 429")
    )
    assert summary_rl["failure_class"] == "rate_limit"
    assert summary_rl["http_status"] == 429

    # Unexpected error
    summary_un = summarize_bidding_probe_exception(RuntimeError("Unexpected OS error"))
    assert summary_un["failure_class"] == "unexpected_error"


def test_urls_and_images_equivalent_helpers() -> None:
    """URL and image equivalence helpers compare canonical identifiers."""
    from custom_components.ebay.parsers.bidding_parity import (
        _images_equivalent,
        _urls_equivalent,
    )

    # URLs
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
    assert not _urls_equivalent(
        "https://www.ebay.com/itm/123456789012",
        "https://www.ebay.com/itm/999999999999",
        "123456789012",
    )
    assert not _urls_equivalent("invalid-url-1", "invalid-url-2", "123")

    # Images
    assert _images_equivalent(
        "https://i.ebayimg.com/images/g/abc123XYZ/s-l140.jpg",
        "https://i.ebayimg.com/images/g/abc123XYZ/s-l500.jpg",
    )
    assert not _images_equivalent(
        "https://i.ebayimg.com/images/g/abc123XYZ/s-l140.jpg",
        "https://i.ebayimg.com/images/g/differentAsset/s-l140.jpg",
    )


def test_merge_graphql_bidding_pages_edge_cases() -> None:
    """merge_graphql_bidding_pages handles empty input, missing containers, and pageInfo."""
    # Empty
    assert merge_graphql_bidding_pages([])["page_count"] == 0

    # Non-mapping
    assert merge_graphql_bidding_pages(["not-a-map"])["items_by_id"] == {}

    # With pageInfo hasNextPage
    page = {
        "userBiddingActivity": {
            "total": 10,
            "pageInfo": {"hasNextPage": True},
            "items": [{"itemId": "111", "title": "Item 1"}],
        }
    }
    merged = merge_graphql_bidding_pages([page])
    assert merged["page_count"] == 1
    assert merged["pagination_status"] == "truncated"

    # Single item with no items list raises / flags invalid payload
    with pytest.raises(TypeError, match="GraphQL Bidding data must be a mapping"):
        normalize_graphql_bidding_payload("not-a-dict")


def test_render_bidding_parity_markdown_with_operational_warnings_and_probes() -> None:
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
                "key": "user_bidding_activity_total",
                "outcome": "error",
                "summary": "EbayGraphQLError (failure_class=permission_or_scope_error)",
            },
        ],
        "xml_available": True,
        "xml_bidding_count": 0,
        "xml_truncated": False,
        "graphql_bidding_count": 0,
        "graphql_complete": False,
        "pagination_status": "empty",
        "common_items_count": 0,
        "composite_ids_normalized_count": 0,
        "duplicate_ids_count": 0,
        "missing_ids_count": 0,
        "fields": {},
    }
    md = render_bidding_parity_markdown(report)
    assert "Operational Warnings" in md
    assert "Test warning: short-lived token without refresh." in md
    assert "Candidate Contract Probes" in md
    assert "typename_handshake" in md
    assert "user_bidding_activity_total" in md


def test_edges_graphql_connection_and_variable_defaults() -> None:
    """GraphQL Relay connection edges and query variable default values parse properly."""
    payload_edges = {
        "userBiddingActivity": {
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
    norm_edges = normalize_graphql_bidding_payload(payload_edges)
    assert norm_edges["items_field_name"] == "edges"
    assert "12345" in norm_edges["items_by_id"]

    query = "query Test($flag: Boolean = true, $opt: String) { userBiddingActivity { total } }"
    parsed = parse_query_variables(query)
    assert parsed is not None
    assert parsed["flag"] is True
    assert "opt" not in parsed


def test_summarize_bidding_probe_exception_http_500() -> None:
    """HTTP 500 error maps to graphql_error failure class."""
    summary_500 = summarize_bidding_probe_exception(
        EbayGraphQLError("Server error", status=500)
    )
    assert summary_500["failure_class"] == "graphql_error"
    assert summary_500["http_status"] == 500


def test_compare_bidding_snapshots_exact_types_and_selected_probe_markdown() -> None:
    """Test float matching, datetime objects, and selected_probe markdown output."""
    xml_item = {
        "999": {
            "item_id": "999",
            "title": "Exact Title",
            "current_price": 50.0,
            "currency": "usd",
            "max_bid": 60.0,
            "winning": True,
            "bid_count": 2,
            "end_time": datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc),
            "listing_type": "auction",
            "url": "https://www.ebay.com/itm/999",
            "image": "https://i.ebayimg.com/images/g/xyz/s-l140.jpg",
        }
    }
    gql_item = {
        "userBiddingActivity": {
            "total": 1,
            "items": [
                {
                    "itemId": "999",
                    "title": "Exact Title",
                    "currentPrice": {"amount": 50.0, "currencyCode": "USD"},
                    "maxBid": {"amount": 60.0, "currencyCode": "USD"},
                    "winning": True,
                    "bidCount": 2,
                    "itemEndDate": "2026-11-01T12:00:00Z",
                    "formatType": "AUCTION",
                    "itemWebUrl": "https://www.ebay.com/itm/999",
                    "image": {
                        "imageUrl": "https://i.ebayimg.com/images/g/xyz/s-l140.jpg"
                    },
                }
            ],
        }
    }
    comp = compare_bidding_snapshots(xml_item, gql_item)
    assert comp["status"] == "parity_verified"

    report = {
        "status": "parity_verified",
        "selected_probe": {
            "key": "user_bidding_activity_paginated",
            "operation_name": "GetUserBiddingActivity",
            "outcome": "success",
            "items_field": "items",
            "total_field": "total",
            "reported_total": 5,
            "pagination_status": "complete",
        },
        "fields": {},
    }
    md = render_bidding_parity_markdown(report)
    assert "Selected Contract Probe" in md
    assert "user_bidding_activity_paginated" in md


def test_field_mismatches_and_edge_coverage() -> None:
    """Covers field-level mismatches, base EbayError, and ranking edge cases."""
    from custom_components.ebay.clients.errors import EbayError

    # Base EbayError mapping
    summary_base = summarize_bidding_probe_exception(EbayError("Base error"))
    assert summary_base["failure_class"] == "api_error"

    # Outcome ranking edge cases
    assert rank_bidding_probe_candidate({"outcome": "empty"})[0] == 2
    assert rank_bidding_probe_candidate({"outcome": "unsupported_shape"})[0] == 1
    assert rank_bidding_probe_candidate({"outcome": "other"})[0] == 0

    # Field-level mismatches on common item
    xml_mismatch = {
        "101": {
            "item_id": "101",
            "title": "Title A",
            "current_price": 50.0,
            "currency": "USD",
            "max_bid": 60.0,
            "winning": True,
            "bid_count": 2,
            "end_time": "2026-11-01T12:00:00Z",
            "listing_type": "Chinese",
            "url": "https://www.ebay.com/itm/101",
            "image": "https://i.ebayimg.com/images/g/a/s-l140.jpg",
        }
    }
    gql_mismatch = {
        "userBiddingActivity": {
            "total": 1,
            "items": [
                {
                    "itemId": "101",
                    "title": "Title B",  # mismatch
                    "currentPrice": {
                        "amount": 75.0,
                        "currencyCode": "EUR",
                    },  # mismatch price & currency
                    "maxBid": {"amount": 80.0, "currencyCode": "EUR"},  # mismatch
                    "winning": False,  # mismatch
                    "bidCount": 4,  # mismatch
                    "itemEndDate": "2027-01-01T00:00:00Z",  # mismatch
                    "formatType": "FIXED_PRICE",  # mismatch
                    "itemWebUrl": "https://www.ebay.com/itm/completely-different/999",  # mismatch
                    "image": {
                        "imageUrl": "https://i.ebayimg.com/images/g/completelyDifferent/s-l140.jpg"
                    },  # mismatch
                }
            ],
        }
    }
    comp_mm = compare_bidding_snapshots(xml_mismatch, gql_mismatch)
    assert comp_mm["status"] == "semantic_differences"
    f = comp_mm["fields"]
    assert f["current_price"]["mismatch_count"] == 1
    assert f["currency"]["mismatch_count"] == 1
    assert f["max_bid"]["mismatch_count"] == 1
    assert f["winning"]["mismatch_count"] == 1
    assert f["bid_count"]["mismatch_count"] == 1
    assert f["end_time"]["mismatch_count"] == 1
    assert f["listing_type"]["mismatch_count"] == 1
    assert f["url"]["mismatch_count"] == 1
    assert f["image"]["mismatch_count"] == 1

    # Reported total differs from retrieved count when both items matched
    gql_total_diff = {
        "userBiddingActivity": {
            "total": 99,  # reported total differs
            "items": [
                {
                    "itemId": "101",
                    "title": "Title A",
                    "currentPrice": {"amount": 50.0, "currencyCode": "USD"},
                    "maxBid": {"amount": 60.0, "currencyCode": "USD"},
                    "winning": True,
                    "bidCount": 2,
                    "itemEndDate": "2026-11-01T12:00:00Z",
                    "formatType": "AUCTION",
                    "itemWebUrl": "https://www.ebay.com/itm/101",
                    "image": {
                        "imageUrl": "https://i.ebayimg.com/images/g/a/s-l140.jpg"
                    },
                }
            ],
        }
    }
    # When reported total exceeds retrieved count, collection is truncated/incomplete
    comp_tot = compare_bidding_snapshots(xml_mismatch, gql_total_diff)
    assert comp_tot["fields"]["bidding_count"]["classification"] == PARITY_UNVERIFIED
    assert comp_tot["status"] == "inconclusive"
