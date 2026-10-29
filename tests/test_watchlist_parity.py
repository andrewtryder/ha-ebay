"""Unit tests for the Milestone 1 Watchlist XML vs. GraphQL parity harness."""

from __future__ import annotations

import asyncio
import json
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, Mock

import aiohttp
import pytest

import scripts.validate_watchlist_parity as cli_harness
from custom_components.ebay.clients.errors import (
    EbayApiError,
    EbayAuthTransientError,
    EbayGraphQLError,
    EbayParseError,
    sanitize_graphql_errors,
)
from custom_components.ebay.parsers.watchlist_parity import (
    MONITORED_WATCHLIST_FIELDS,
    PARITY_DIFFERENT_SEMANTICS,
    PARITY_EQUIVALENT_NORMALIZED,
    PARITY_EXACT_MATCH,
    PARITY_MISSING_FIELD,
    PARITY_UNVERIFIED,
    WATCHLIST_CONTRACT_PROBES,
    WatchlistContractProbe,
    compare_watchlist_snapshots,
    format_parity_report_markdown,
    merge_graphql_watchlist_pages,
    normalize_graphql_watchlist_payload,
    normalize_listing_type_category,
    normalize_watchlist_item_id,
    parity_status,
    sanitize_diagnostic_token,
    sanitize_parity_report,
    summarize_probe_exception,
    validate_all_contract_probes,
)


def test_all_builtin_contract_probes_are_strictly_read_only() -> None:
    """All built-in contract probes must pass validate_read_only_query."""
    validate_all_contract_probes()
    assert len(WATCHLIST_CONTRACT_PROBES) >= 4
    for probe in WATCHLIST_CONTRACT_PROBES:
        assert "mutation" not in probe.query.lower()
        assert "subscription" not in probe.query.lower()


def test_normalize_watchlist_item_id_variants() -> None:
    """Composite v1|...|... IDs normalize to numeric legacy IDs."""
    assert normalize_watchlist_item_id("123456789012") == (
        "123456789012",
        "legacy_numeric",
    )
    assert normalize_watchlist_item_id("v1|123456789012|0") == (
        "123456789012",
        "composite_v1",
    )
    assert normalize_watchlist_item_id("v1|123456789012|999888") == (
        "123456789012",
        "composite_v1",
    )
    assert normalize_watchlist_item_id("custom-sku-abc") == (
        "custom-sku-abc",
        "opaque_string",
    )
    assert normalize_watchlist_item_id(None) == (None, "missing")
    assert normalize_watchlist_item_id("   ") == (None, "missing")


def test_normalize_listing_type_category_variants() -> None:
    """Auction and fixed-price formats across XML and GraphQL map to canonical categories."""
    assert normalize_listing_type_category("Chinese") == "auction"
    assert normalize_listing_type_category("AUCTION") == "auction"
    assert normalize_listing_type_category(["AUCTION", "BEST_OFFER"]) == "auction"
    assert normalize_listing_type_category("FixedPriceItem") == "fixed_price"
    assert normalize_listing_type_category("FIXED_PRICE") == "fixed_price"
    assert normalize_listing_type_category("StoresFixedPrice") == "fixed_price"
    assert normalize_listing_type_category(["FIXED_PRICE"]) == "fixed_price"
    assert normalize_listing_type_category(["CLASSIFIED_AD"]) == "classified_ad"
    assert normalize_listing_type_category("ClassifiedAd") == "classifiedad"
    assert normalize_listing_type_category([]) is None
    assert normalize_listing_type_category(None) is None
    assert normalize_listing_type_category("  ") is None


def test_normalize_graphql_watchlist_payload_alternate_shapes() -> None:
    """Parser supports items[], watchlistItems[], edges[].node, and count-only payloads."""
    # 1. watchlistItems + watchListCount shape
    alt_payload = {
        "userWatchList": {
            "watchListCount": 1,
            "watchListMaximum": 400,
            "watchlistItems": [
                {
                    "legacyItemId": "999888777666",
                    "title": "Test Item",
                    "currentPrice": {"amount": 25.0, "currencyCode": "gbp"},
                    "buyingOptions": ["FIXED_PRICE"],
                    "endTime": "2026-12-01T00:00:00Z",
                    "itemUrl": "https://www.ebay.co.uk/itm/999888777666",
                    "galleryUrl": "https://i.ebayimg.com/images/g/xyz/s-l140.jpg",
                    "bids": 0,
                }
            ],
        }
    }
    normalized = normalize_graphql_watchlist_payload(alt_payload)
    assert normalized["reported_total"] == 1
    assert normalized["total_field_name"] == "watchListCount"
    assert normalized["items_field_name"] == "watchlistItems"
    assert normalized["items_list_present"] is True
    assert normalized["graphql_complete"] is True
    assert normalized["graphql_truncated"] is False
    assert normalized["pagination_status"] == "complete"
    item = normalized["items_by_id"]["999888777666"]
    assert item["current_price"] == 25.0
    assert item["price_was_string"] is False
    assert item["currency"] == "GBP"
    assert item["listing_type_category"] == "fixed_price"
    assert item["bid_count"] == 0

    # 2. edges[].node shape with duplicate composite IDs and invalid node
    edges_payload = {
        "userWatchList": {
            "totalCount": 3,
            "edges": [
                {"node": {"itemId": "v1|111|0", "title": "First"}},
                {"node": {"itemId": "111", "title": "Duplicate"}},
                {"node": "invalid"},
            ],
        }
    }
    norm_edges = normalize_graphql_watchlist_payload(edges_payload)
    assert norm_edges["items_field_name"] == "edges"
    assert norm_edges["composite_id_count"] == 1
    assert norm_edges["duplicate_id_count"] == 1
    assert norm_edges["missing_id_count"] == 1
    assert norm_edges["graphql_complete"] is False
    assert norm_edges["pagination_status"] == "incomplete_identity"
    assert len(norm_edges["items_by_id"]) == 1

    # 3. Count-only shape
    count_only = normalize_graphql_watchlist_payload({"userWatchList": {"total": 5}})
    assert count_only["reported_total"] == 5
    assert count_only["items_list_present"] is False
    assert count_only["graphql_complete"] is False
    assert count_only["graphql_truncated"] is True
    assert count_only["pagination_status"] == "count_only"
    assert count_only["items_by_id"] == {}

    # 4. Invalid payloads
    with pytest.raises(TypeError, match="must be a mapping"):
        normalize_graphql_watchlist_payload(None)
    with pytest.raises(ValueError, match="missing 'userWatchList'"):
        normalize_graphql_watchlist_payload({})


def test_summarize_probe_exception_flat_sanitized_and_nested_errors() -> None:
    """Sanitized flat GraphQL errors preserve validated code, error_code, category, and domain."""
    # 1. Flat structure produced by sanitize_graphql_errors() in GraphQL transport
    raw_transport_errors = [
        {
            "message": "Cannot query field 'watchListCount' on type 'WatchList' for user john_doe_secret",
            "extensions": {
                "code": "VALIDATION_ERROR",
                "errorCode": "VALIDATION_ERROR",
                "category": "REQUEST",
                "domain": "BUY_WATCHLIST",
            },
        }
    ]
    flattened = sanitize_graphql_errors(raw_transport_errors)
    exc = EbayGraphQLError(
        "eBay GraphQL query returned errors: Cannot query field 'watchListCount' for user john_doe_secret",
        errors=flattened,
    )
    summary = summarize_probe_exception(exc)
    assert summary["error_type"] == "EbayGraphQLError"
    assert summary["failure_class"] == "validation_error"
    assert summary["is_validation_error"] is True
    assert summary["is_auth_error"] is False
    assert summary["is_scope_or_permission_error"] is False
    assert summary["is_rate_limit"] is False
    assert summary["code"] == "VALIDATION_ERROR"
    assert summary["error_code"] == "VALIDATION_ERROR"
    assert summary["category"] == "REQUEST"
    assert summary["domain"] == "BUY_WATCHLIST"
    assert summary["error_codes"] == ["VALIDATION_ERROR"]
    assert summary["categories"] == ["REQUEST"]
    assert summary["domains"] == ["BUY_WATCHLIST"]
    assert summary["errors"] == [
        {
            "code": "VALIDATION_ERROR",
            "error_code": "VALIDATION_ERROR",
            "error_id": "VALIDATION_ERROR",
            "category": "REQUEST",
            "domain": "BUY_WATCHLIST",
        }
    ]
    assert "john_doe_secret" not in json.dumps(summary)
    assert "Cannot query field" not in json.dumps(summary)

    # 2. Missing scope / permission error in GraphQL errors
    scope_errors = sanitize_graphql_errors(
        [
            {
                "message": "Insufficient scope for user secret_acct",
                "extensions": {
                    "code": "INSUFFICIENT_SCOPE",
                    "category": "REQUEST",
                    "domain": "SECURITY",
                },
            }
        ]
    )
    scope_summary = summarize_probe_exception(
        EbayGraphQLError("Permission error", errors=scope_errors)
    )
    assert scope_summary["failure_class"] == "permission_or_scope_error"
    assert scope_summary["is_scope_or_permission_error"] is True
    assert scope_summary["is_auth_error"] is True
    assert scope_summary["error_codes"] == ["INSUFFICIENT_SCOPE"]

    # 3. Rate limit error in GraphQL errors and HTTP 429 EbayApiError
    rl_errors = sanitize_graphql_errors(
        [
            {
                "message": "Rate limit exceeded",
                "extensions": {"code": "RATE_LIMITED", "category": "REQUEST"},
            }
        ]
    )
    rl_summary = summarize_probe_exception(
        EbayGraphQLError("Rate limited", errors=rl_errors)
    )
    assert rl_summary["failure_class"] == "rate_limit"
    assert rl_summary["is_rate_limit"] is True
    assert rl_summary["error_codes"] == ["RATE_LIMITED"]

    http_429_summary = summarize_probe_exception(
        EbayApiError("eBay GraphQL rate limit exceeded (HTTP 429) for GetUserWatchList")
    )
    assert http_429_summary["failure_class"] == "rate_limit"
    assert http_429_summary["is_rate_limit"] is True
    assert http_429_summary["http_status"] == 429

    # 4. Allowlisted diagnostic tokens vs. unrecognized/sensitive values
    assert sanitize_diagnostic_token("VALIDATION_ERROR") == "VALIDATION_ERROR"
    assert sanitize_diagnostic_token(1001) == "1001"
    assert sanitize_diagnostic_token("REQUEST", kind="category") == "REQUEST"
    assert sanitize_diagnostic_token("BUY_WATCHLIST", kind="domain") == "BUY_WATCHLIST"
    assert sanitize_diagnostic_token("Bearer secret-jwt") == "UNKNOWN_CODE"
    assert sanitize_diagnostic_token("v^1.1#i^1#p^3#token") == "UNKNOWN_CODE"
    assert sanitize_diagnostic_token("Free form message with spaces") == "UNKNOWN_CODE"
    assert sanitize_diagnostic_token(None) is None
    assert sanitize_diagnostic_token(True) is None


def test_diagnostic_privacy_allowlist_redacts_item_ids_accounts_and_secrets() -> None:
    """Account names, 12-digit numeric item IDs, and 64-char secrets in error codes/domains are replaced with generic labels."""
    secret_64 = "a1b2c3d4e5f60718293a4b5c6d7e8f90a1b2c3d4e5f60718293a4b5c6d7e8f90"
    item_id_code = "123456789012"
    account_domain = "user_private_account"
    account_code = "seller_handle_99"

    assert sanitize_diagnostic_token(item_id_code, kind="code") == "UNKNOWN_CODE"
    assert sanitize_diagnostic_token(123456789012, kind="code") == "UNKNOWN_CODE"
    assert sanitize_diagnostic_token(account_code, kind="code") == "UNKNOWN_CODE"
    assert sanitize_diagnostic_token(secret_64, kind="code") == "UNKNOWN_CODE"
    assert sanitize_diagnostic_token(account_domain, kind="domain") == "UNKNOWN_DOMAIN"
    assert sanitize_diagnostic_token(secret_64, kind="domain") == "UNKNOWN_DOMAIN"
    assert (
        sanitize_diagnostic_token("private_category", kind="category")
        == "UNKNOWN_CATEGORY"
    )

    flattened_errors = sanitize_graphql_errors(
        [
            {
                "message": "Private account failure for seller_handle_99",
                "extensions": {
                    "code": item_id_code,
                    "errorCode": account_code,
                    "category": "private_category",
                    "domain": account_domain,
                },
            },
            {
                "message": "Token leak attempt",
                "extensions": {
                    "code": secret_64,
                    "category": "REQUEST",
                    "domain": "BUY_WATCHLIST",
                },
            },
        ]
    )
    exc = EbayGraphQLError("Synthetic error", errors=flattened_errors)
    summary = summarize_probe_exception(exc)

    assert summary["error_count"] == 2
    assert summary["unrecognized_code_count"] == 2
    assert summary["unrecognized_category_count"] == 1
    assert summary["unrecognized_domain_count"] == 1
    assert summary["error_codes"] == ["UNKNOWN_CODE"]
    assert summary["categories"] == ["UNKNOWN_CATEGORY", "REQUEST"]
    assert summary["domains"] == ["UNKNOWN_DOMAIN", "BUY_WATCHLIST"]

    report = sanitize_parity_report(
        {
            "status": "unverified",
            "graphql_error_summary": summary,
            "manual_error": {
                "code": item_id_code,
                "error_code": account_code,
                "domain": account_domain,
            },
        }
    )
    serialized = json.dumps(report)
    assert item_id_code not in serialized
    assert account_domain not in serialized
    assert account_code not in serialized
    assert secret_64 not in serialized
    assert report["manual_error"]["code"] == "UNKNOWN_CODE"
    assert report["manual_error"]["error_code"] == "UNKNOWN_CODE"
    assert report["manual_error"]["domain"] == "UNKNOWN_DOMAIN"


def test_completeness_and_pagination_fail_closed_and_bounded_multi_page() -> None:
    """Partial pages, >100 items, duplicate/missing IDs, and XML truncation are handled safely."""
    dt = datetime(2026, 11, 15, 18, 30, 0, tzinfo=timezone.utc)
    # Build a 150-item XML watchlist
    xml_150: dict[str, dict[str, Any]] = {}
    for idx in range(1, 151):
        item_id = f"{100000000000 + idx}"
        xml_150[item_id] = {
            "item_id": item_id,
            "title": f"Item {idx}",
            "current_price": 10.0 + idx,
            "currency": "USD",
            "listing_type": "FixedPriceItem",
            "end_time": dt,
            "url": f"https://www.ebay.com/itm/{item_id}",
            "image": f"https://i.ebayimg.com/images/g/asset{idx}/s-l140.jpg",
            "bid_count": 0,
        }

    def _make_gql_item(idx: int) -> dict[str, Any]:
        item_id = f"{100000000000 + idx}"
        return {
            "itemId": f"v1|{item_id}|0",
            "title": f"Item {idx}",
            "price": {"value": f"{10.0 + idx:.2f}", "currency": "USD"},
            "formatType": "FIXED_PRICE",
            "itemEndDate": "2026-11-15T18:30:00Z",
            "itemWebUrl": f"https://www.ebay.com/itm/{item_id}",
            "image": {
                "imageUrl": f"https://i.ebayimg.com/images/g/asset{idx}/s-l500.jpg"
            },
            "bidCount": 0,
        }

    page_1_only = {
        "userWatchList": {
            "total": 150,
            "items": [_make_gql_item(i) for i in range(1, 101)],
        }
    }
    page_2 = {
        "userWatchList": {
            "total": 150,
            "items": [_make_gql_item(i) for i in range(101, 151)],
        }
    }

    # 1. Single-page partial response (100 of 150 items) MUST fail closed
    partial_report = compare_watchlist_snapshots(xml_150, page_1_only)
    assert partial_report["status"] == "inconclusive"
    assert partial_report["graphql_complete"] is False
    assert partial_report["graphql_truncated"] is True
    assert partial_report["pagination_status"] == "truncated"
    assert partial_report["graphql_watched_count"] == 100
    assert partial_report["graphql_reported_total"] == 150
    assert (
        partial_report["fields"]["watched_count"]["classification"] == PARITY_UNVERIFIED
    )
    # Sample-matched fields must also be downgraded to unverified on a partial collection
    for field in MONITORED_WATCHLIST_FIELDS:
        assert partial_report["fields"][field]["classification"] == PARITY_UNVERIFIED

    # 2. Complete 2-page response (100 + 50 = 150 items) reconciles completely
    complete_report = compare_watchlist_snapshots(xml_150, [page_1_only, page_2])
    assert complete_report["status"] == "compared"
    assert complete_report["graphql_complete"] is True
    assert complete_report["graphql_truncated"] is False
    assert complete_report["pagination_status"] == "complete"
    assert complete_report["graphql_watched_count"] == 150
    assert complete_report["common_items_count"] == 150
    assert (
        complete_report["fields"]["watched_count"]["classification"]
        == PARITY_EXACT_MATCH
    )
    assert (
        complete_report["fields"]["item_id"]["classification"]
        == PARITY_EQUIVALENT_NORMALIZED
    )
    assert complete_report["fields"]["title"]["classification"] == PARITY_EXACT_MATCH

    # 3. xml_truncated=True blocks definitive parity even when GraphQL page matches
    xml_trunc_report = compare_watchlist_snapshots(
        xml_150,
        [page_1_only, page_2],
        xml_truncated=True,
    )
    assert xml_trunc_report["status"] == "inconclusive"
    assert (
        xml_trunc_report["fields"]["watched_count"]["classification"]
        == PARITY_UNVERIFIED
    )
    assert xml_trunc_report["fields"]["title"]["classification"] == PARITY_UNVERIFIED

    # 4. Duplicate IDs or missing IDs mark pagination incomplete_identity
    dup_payload = {
        "userWatchList": {
            "total": 2,
            "items": [_make_gql_item(1), _make_gql_item(1)],
        }
    }
    dup_report = compare_watchlist_snapshots(
        {"100000000001": xml_150["100000000001"]},
        dup_payload,
    )
    assert dup_report["status"] == "inconclusive"
    assert dup_report["pagination_status"] == "incomplete_identity"
    assert dup_report["duplicate_ids_count"] == 1
    assert dup_report["fields"]["watched_count"]["classification"] == PARITY_UNVERIFIED

    # 5. Count-only response with matching total is still unverified (no items list)
    count_only_report = compare_watchlist_snapshots(
        {"100000000001": xml_150["100000000001"]},
        {"userWatchList": {"total": 1}},
    )
    assert count_only_report["status"] == "unverified"
    assert count_only_report["pagination_status"] == "count_only"
    assert (
        count_only_report["fields"]["watched_count"]["classification"]
        == PARITY_UNVERIFIED
    )


def test_missing_field_and_null_coverage_evidence() -> None:
    """Missing/null values on both sides are tracked separately and never inflate exact_match."""
    dt = datetime(2026, 11, 15, 18, 30, 0, tzinfo=timezone.utc)
    xml_watched = {
        "111": {
            "item_id": "111",
            "title": "Item One",
            "current_price": 10.0,
            "currency": "USD",
            "listing_type": "FixedPriceItem",
            "end_time": dt,
            "url": "https://www.ebay.com/itm/111",
            "image": "https://i.ebayimg.com/images/g/one/s-l140.jpg",
            "bid_count": None,  # Null in XML on item 111
        },
        "222": {
            "item_id": "222",
            "title": "Item Two",
            "current_price": 20.0,
            "currency": "USD",
            "listing_type": "Chinese",
            "end_time": None,  # Null in XML on both items test below
            "url": None,  # Null on item 222 in both XML and GraphQL (partial coverage)
            "image": "https://i.ebayimg.com/images/g/two/s-l140.jpg",
            "bid_count": None,  # Null in XML on item 222 too
        },
    }
    xml_watched["111"]["end_time"] = None

    graphql_payload = {
        "userWatchList": {
            "total": 2,
            "items": [
                {
                    "itemId": "111",
                    "title": "Item One",
                    "price": {"value": 10.0, "currency": "USD"},
                    "formatType": "FixedPriceItem",
                    "itemWebUrl": "https://www.ebay.com/itm/111",
                    "image": "https://i.ebayimg.com/images/g/one/s-l140.jpg",
                },
                {
                    "itemId": "222",
                    "title": "Item Two",
                    "price": {"value": 20.0, "currency": "USD"},
                    "formatType": "Chinese",
                    "image": "https://i.ebayimg.com/images/g/two/s-l140.jpg",
                },
            ],
        }
    }

    report = compare_watchlist_snapshots(xml_watched, graphql_payload)
    fields = report["fields"]

    # 1. end_time and bid_count are null on both sides for all 2 items:
    # exact_match_count MUST be 0, both_absent_count MUST be 2, coverage == "none"
    for field_name in ("end_time", "bid_count"):
        f_row = fields[field_name]
        assert f_row["classification"] == PARITY_UNVERIFIED
        assert f_row["coverage"] == "none"
        assert f_row["exact_match_count"] == 0
        assert f_row["both_present_count"] == 0
        assert f_row["both_absent_count"] == 2

    # 2. url is present and identical on item 111, but null on both sides on item 222:
    # coverage is "partial", exact_match_count == 1, both_absent_count == 1,
    # classification MUST be downgraded to PARITY_UNVERIFIED (not PARITY_EXACT_MATCH)
    url_row = fields["url"]
    assert url_row["coverage"] == "partial"
    assert url_row["exact_match_count"] == 1
    assert url_row["both_present_count"] == 1
    assert url_row["both_absent_count"] == 1
    assert url_row["classification"] == PARITY_UNVERIFIED

    # 3. title has non-null values on all 2 items and matches identically:
    title_row = fields["title"]
    assert title_row["coverage"] == "complete"
    assert title_row["exact_match_count"] == 2
    assert title_row["both_present_count"] == 2
    assert title_row["both_absent_count"] == 0
    assert title_row["classification"] == PARITY_EXACT_MATCH


def test_compare_watchlist_snapshots_classifications() -> None:
    """Verify Exact match, Equivalent after normalization, Missing field, and Unverified."""
    dt = datetime(2026, 11, 15, 18, 30, 0, tzinfo=timezone.utc)
    xml_watched = {
        "111222333444": {
            "item_id": "111222333444",
            "title": "Synthetic Camera",
            "current_price": 149.5,
            "currency": "USD",
            "listing_type": "Chinese",
            "end_time": dt,
            "url": "https://www.ebay.com/itm/Synthetic-Camera/111222333444?hash=abc",
            "image": "https://i.ebayimg.com/images/g/abc123XYZ/s-l140.jpg",
            "bid_count": 4,
        }
    }
    graphql_payload = {
        "userWatchList": {
            "total": 1,
            "items": [
                {
                    "itemId": "v1|111222333444|0",
                    "title": "Synthetic Camera",
                    "price": {"value": "149.50", "currency": "USD"},
                    "formatType": "AUCTION",
                    "itemEndDate": "2026-11-15T18:30:00Z",
                    "itemWebUrl": "https://www.ebay.com/itm/111222333444",
                    "image": {
                        "imageUrl": "https://i.ebayimg.com/images/g/abc123XYZ/s-l500.jpg"
                    },
                    # bidCount intentionally omitted to verify missing_field detection
                }
            ],
        }
    }

    report = compare_watchlist_snapshots(xml_watched, graphql_payload)
    assert report["status"] == "compared"
    assert report["common_items_count"] == 1
    assert report["composite_ids_normalized_count"] == 1

    fields = report["fields"]
    assert fields["watched_count"]["classification"] == PARITY_EXACT_MATCH
    assert fields["item_id"]["classification"] == PARITY_EQUIVALENT_NORMALIZED
    assert fields["title"]["classification"] == PARITY_EXACT_MATCH
    assert fields["current_price"]["classification"] == PARITY_EQUIVALENT_NORMALIZED
    assert fields["currency"]["classification"] == PARITY_EXACT_MATCH
    assert fields["listing_type"]["classification"] == PARITY_EQUIVALENT_NORMALIZED
    assert fields["end_time"]["classification"] == PARITY_EQUIVALENT_NORMALIZED
    assert fields["url"]["classification"] == PARITY_EQUIVALENT_NORMALIZED
    assert fields["image"]["classification"] == PARITY_EQUIVALENT_NORMALIZED
    assert fields["bid_count"]["classification"] == PARITY_MISSING_FIELD


def test_compare_watchlist_snapshots_different_semantics_and_unverified() -> None:
    """Mismatched values classify as different_semantics; missing GraphQL payload is unverified."""
    dt_xml = datetime(2026, 11, 15, 18, 30, 0, tzinfo=timezone.utc)
    xml_watched = {
        "111": {
            "item_id": "111",
            "title": "XML Title",
            "current_price": 10.0,
            "currency": "USD",
            "listing_type": "Chinese",
            "end_time": dt_xml,
            "url": "https://www.ebay.com/itm/111",
            "image": "https://i.ebayimg.com/images/g/one/s-l140.jpg",
            "bid_count": 2,
        }
    }

    # 1. None GraphQL payload -> all fields unverified
    unverified_report = compare_watchlist_snapshots(
        xml_watched,
        None,
        graphql_error_summary={"error_type": "EbayAuthError", "http_status": 403},
    )
    assert parity_status(unverified_report) == "unverified"
    for field in MONITORED_WATCHLIST_FIELDS:
        assert unverified_report["fields"][field]["classification"] == PARITY_UNVERIFIED

    # 2. Mismatched values on reconciled 1-item set -> different_semantics
    mismatched_gql = {
        "userWatchList": {
            "total": 1,
            "items": [
                {
                    "itemId": "111",
                    "title": "Different GraphQL Title",
                    "price": {"value": 99.99, "currency": "EUR"},
                    "formatType": "FIXED_PRICE",
                    "itemEndDate": "2026-11-16T18:30:00Z",
                    "itemWebUrl": "https://www.ebay.de/itm/999",
                    "image": "https://i.ebayimg.com/images/g/two/s-l140.jpg",
                    "bidCount": 7,
                }
            ],
        }
    }
    diff_report = compare_watchlist_snapshots(xml_watched, mismatched_gql)
    fields = diff_report["fields"]
    assert fields["watched_count"]["classification"] == PARITY_EXACT_MATCH
    assert fields["item_id"]["classification"] == PARITY_EXACT_MATCH
    assert fields["title"]["classification"] == PARITY_DIFFERENT_SEMANTICS
    assert fields["current_price"]["classification"] == PARITY_DIFFERENT_SEMANTICS
    assert fields["currency"]["classification"] == PARITY_DIFFERENT_SEMANTICS
    assert fields["listing_type"]["classification"] == PARITY_DIFFERENT_SEMANTICS
    assert fields["end_time"]["classification"] == PARITY_DIFFERENT_SEMANTICS
    assert fields["url"]["classification"] == PARITY_DIFFERENT_SEMANTICS
    assert fields["image"]["classification"] == PARITY_DIFFERENT_SEMANTICS
    assert fields["bid_count"]["classification"] == PARITY_DIFFERENT_SEMANTICS


def test_custom_query_and_variables_loading_and_validation(tmp_path: Path) -> None:
    """Custom Explorer queries infer operationName, load JSON variables, and validate before network."""
    query_file = tmp_path / "explorer.local.graphql"
    vars_file = tmp_path / "vars.local.json"

    query_file.write_text(
        "query GetWatchList($limit: Int!, $offset: Int = 0) {\n"
        "  userWatchList(limit: $limit, offset: $offset) {\n"
        "    total\n"
        "    items { itemId title }\n"
        "  }\n"
        "}\n",
        encoding="utf-8",
    )
    vars_file.write_text(json.dumps({"limit": 25, "offset": 0}), encoding="utf-8")

    # 1. Infers operation_name="GetWatchList" and loads variables when --operation-name is omitted
    probe = cli_harness.build_custom_probe(
        query_file,
        operation_name=None,
        variables_file=vars_file,
    )
    assert probe.operation_name == "GetWatchList"
    assert probe.variables == {"limit": 25, "offset": 0}

    # 2. Missing required non-null variable $limit: Int! fails before network access
    with pytest.raises(ValueError, match=r"requires non-null variable '\$limit'"):
        cli_harness.build_custom_probe(
            query_file, operation_name=None, variables_file=None
        )

    # 3. Non-object JSON in variables file is rejected
    bad_vars = tmp_path / "bad_vars.local.json"
    bad_vars.write_text("[1, 2, 3]", encoding="utf-8")
    with pytest.raises(ValueError, match="must contain a JSON object"):
        cli_harness.load_variables_file(bad_vars)

    # 4. Anonymous read-only query resolves operation_name=None
    anon_file = tmp_path / "anon.local.graphql"
    anon_file.write_text("{ userWatchList { total } }", encoding="utf-8")
    anon_probe = cli_harness.build_custom_probe(anon_file)
    assert anon_probe.operation_name is None

    # 5. Mutations and subscriptions are blocked
    mut_file = tmp_path / "mut.local.graphql"
    mut_file.write_text(
        "mutation DeleteItem { deleteWatchItem(id: 1) }", encoding="utf-8"
    )
    with pytest.raises(ValueError, match="Mutations are strictly prohibited"):
        cli_harness.build_custom_probe(mut_file)


def test_resilient_failure_handling_for_xml_and_graphql_errors() -> None:
    """Harness produces a sanitized diagnostic report even when XML or GraphQL probes fail."""
    gql_demo = json.loads(cli_harness._synthetic_demo_fixtures()[1])["data"]

    client = Mock()
    client.environment = "production"
    client.site_id = "0"
    client.client_id = "secret-client-id"
    client._client_secret = "secret-client-secret"
    client.runame = "secret-runame"
    client.refresh_token = "secret-refresh-token"
    client._access_token = "secret-access-token"

    # Simulate Trading XML failure containing sensitive text
    client.async_call_trading_api = AsyncMock(
        side_effect=EbayApiError("Trading XML failed for account secret-client-id")
    )

    async def _gql_side_effect(
        query: str,
        variables: dict[str, Any] | None = None,
        *,
        operation_name: str | None = None,
    ) -> dict[str, Any]:
        if operation_name == "Handshake":
            raise EbayParseError("Malformed JSON near secret-access-token")
        if operation_name == "GetWatchListTotal":
            raise EbayAuthTransientError("Temporary OAuth failure")
        if operation_name == "GetWatchListCountAlt":
            raise aiohttp.ClientConnectionError("Connection reset by peer")
        if operation_name == "GetUserWatchListAlt":
            return {"aliasedWatchList": {"total": 2}}
        return gql_data_with_items

    gql_data_with_items = gql_demo
    client.async_post_graphql = AsyncMock(side_effect=_gql_side_effect)

    report = asyncio.run(cli_harness.run_live_watchlist_validation(client))
    serialized = json.dumps(report)

    # XML is marked unavailable without inventing parity results
    assert report["xml_available"] is False
    assert report["xml_watched_count"] is None
    assert report["status"] == "unverified"
    assert report["xml_error_summary"]["error_type"] == "EbayApiError"
    assert report["xml_error_summary"]["failure_class"] == "api_error"
    for field in MONITORED_WATCHLIST_FIELDS:
        assert report["fields"][field]["classification"] == PARITY_UNVERIFIED

    # GraphQL probes still ran and recorded their outcomes
    outcomes_by_key = {p["key"]: p for p in report["probes"]}
    assert outcomes_by_key["typename_handshake"]["outcome"] == "error"
    assert (
        outcomes_by_key["typename_handshake"]["error"]["failure_class"] == "parse_error"
    )
    assert (
        outcomes_by_key["user_watchlist_total"]["error"]["failure_class"]
        == "auth_transient_error"
    )
    assert (
        outcomes_by_key["user_watchlist_count_alt"]["error"]["failure_class"]
        == "transport_error"
    )
    assert (
        outcomes_by_key["user_watchlist_watchlist_items_alt"]["outcome"]
        == "unsupported_shape"
    )
    assert outcomes_by_key["user_watchlist_items_paginated"]["outcome"] == "success"
    assert report["graphql_watched_count"] == 2

    # Zero secrets or private strings leaked
    assert "secret-client-id" not in serialized
    assert "secret-access-token" not in serialized
    assert "Synthetic Vintage Camera Lens" not in serialized


def test_bounded_graphql_pagination_helper_stops_on_duplicate_or_complete() -> None:
    """async_fetch_bounded_graphql_pages fetches multiple pages up to reported_total and stops on duplicates."""
    probe = WatchlistContractProbe(
        key="custom_query_file",
        description="Paginated custom probe",
        query=(
            "query GetWatchList($limit: Int!, $offset: Int!) { "
            "userWatchList(limit: $limit, offset: $offset) { total items { itemId title } } }"
        ),
        operation_name="GetWatchList",
        variables={"limit": 2, "offset": 0},
    )

    client = Mock()
    page_responses = [
        {
            "userWatchList": {
                "total": 3,
                "items": [
                    {"itemId": "101", "title": "One"},
                    {"itemId": "102", "title": "Two"},
                ],
            }
        },
        {
            "userWatchList": {
                "total": 3,
                "items": [
                    {"itemId": "103", "title": "Three"},
                ],
            }
        },
    ]
    client.async_post_graphql = AsyncMock(side_effect=page_responses)

    pages = asyncio.run(
        cli_harness.async_fetch_bounded_graphql_pages(client, probe, paginate=True)
    )
    assert len(pages) == 2
    merged = merge_graphql_watchlist_pages(pages)
    assert merged["graphql_complete"] is True
    assert len(merged["items_by_id"]) == 3

    # If server ignores offset and repeats page 1 IDs, pagination stops immediately
    repeating_client = Mock()
    repeating_client.async_post_graphql = AsyncMock(
        side_effect=[
            page_responses[0],
            page_responses[0],
            page_responses[0],
        ]
    )
    dup_pages = asyncio.run(
        cli_harness.async_fetch_bounded_graphql_pages(
            repeating_client, probe, paginate=True
        )
    )
    assert len(dup_pages) == 2
    dup_merged = merge_graphql_watchlist_pages(dup_pages)
    assert dup_merged["graphql_complete"] is False
    assert dup_merged["pagination_status"] == "incomplete_identity"


def test_sanitize_parity_report_redacts_secrets_and_private_values() -> None:
    """sanitize_parity_report strips forbidden keys, Bearer/v^1.1 tokens, and private strings."""
    raw = {
        "mode": "live_authenticated",
        "access_token": "v^1.1#i^1#p^3#secret-token",
        "code": "VALIDATION_ERROR",
        "authorization_code": "v^1.1#auth-code",
        "nested": {
            "refresh_token": "v^1.1#i^1#r^1#secret-refresh",
            "note": "Header had Bearer super-secret-jwt and item Private Listing Title",
            "items": ("111222333444", "safe-text"),
        },
    }
    sanitized = sanitize_parity_report(
        raw,
        forbidden_values=["Private Listing Title", "111222333444", "my-client-secret"],
    )
    serialized = json.dumps(sanitized)
    assert "v^1.1#" not in serialized
    assert "super-secret-jwt" not in serialized
    assert "Private Listing Title" not in serialized
    assert "111222333444" not in serialized
    assert sanitized["access_token"] == "[REDACTED]"
    assert sanitized["authorization_code"] == "[REDACTED]"
    # Validated GraphQL error code is preserved
    assert sanitized["code"] == "VALIDATION_ERROR"
    assert sanitized["nested"]["refresh_token"] == "[REDACTED]"


def test_cli_refuses_network_without_live_flag(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Running the CLI without --live or offline fixture flags exits with code 2."""
    with pytest.raises(SystemExit) as exc_info:
        cli_harness.main([])
    assert exc_info.value.code == 2
    captured = capsys.readouterr()
    assert (
        "Refusing to make network calls without explicit --live opt-in" in captured.err
    )


def test_cli_synthetic_demo_markdown_and_json_output(tmp_path: Path) -> None:
    """--synthetic-demo produces sanitized markdown and JSON reports without network calls."""
    md_out = tmp_path / "report.local.md"
    json_out = tmp_path / "report.local.json"

    assert (
        cli_harness.main(
            ["--synthetic-demo", "--format", "markdown", "--output", str(md_out)]
        )
        == 0
    )
    md_text = md_out.read_text(encoding="utf-8")
    assert "# eBay Watchlist XML vs. GraphQL Parity Report" in md_text
    assert "`watched_count`" in md_text
    assert "`bid_count`" in md_text
    assert "111222333444" not in md_text
    assert "Synthetic Vintage Camera Lens" not in md_text

    assert (
        cli_harness.main(
            ["--synthetic-demo", "--format", "json", "--output", str(json_out)]
        )
        == 0
    )
    payload = json.loads(json_out.read_text(encoding="utf-8"))
    assert payload["mode"] == "offline_fixture"
    assert payload["status"] == "compared"
    assert payload["graphql_complete"] is True
    assert payload["common_items_count"] == 2
    assert (
        payload["fields"]["item_id"]["classification"] == PARITY_EQUIVALENT_NORMALIZED
    )


def test_cli_offline_fixture_files(tmp_path: Path) -> None:
    """--xml-fixture and --graphql-fixture compare local files offline."""
    xml_demo, gql_demo = cli_harness._synthetic_demo_fixtures()
    xml_path = tmp_path / "buying.xml"
    gql_path = tmp_path / "watchlist.json"
    out_path = tmp_path / "out.json"
    xml_path.write_text(xml_demo, encoding="utf-8")
    gql_path.write_text(gql_demo, encoding="utf-8")

    rc = cli_harness.main(
        [
            "--xml-fixture",
            str(xml_path),
            "--graphql-fixture",
            str(gql_path),
            "--format",
            "json",
            "--output",
            str(out_path),
        ]
    )
    assert rc == 0
    report = json.loads(out_path.read_text(encoding="utf-8"))
    assert report["xml_watched_count"] == 2
    assert report["graphql_watched_count"] == 2


def test_resolve_live_credentials_from_env_dotenv_and_ha_storage(
    tmp_path: Path,
) -> None:
    """Credentials resolve from .env, short-lived access token, or HA storage with entry selection."""
    dotenv = tmp_path / ".env"
    dotenv.write_text(
        "# comment\n"
        'export EBAY_CLIENT_ID="dotenv-client"\n'
        "EBAY_CLIENT_SECRET='dotenv-secret'\n"
        "EBAY_REFRESH_TOKEN=dotenv-refresh\n"
        "EBAY_SITE_ID=3\n",
        encoding="utf-8",
    )
    creds = cli_harness.resolve_live_credentials(env_file=dotenv, environ={})
    assert creds["client_id"] == "dotenv-client"
    assert creds["client_secret"] == "dotenv-secret"
    assert creds["refresh_token"] == "dotenv-refresh"
    assert creds["site_id"] == "3"

    # Short-lived EBAY_ACCESS_TOKEN works without requiring refresh_token
    token_only_creds = cli_harness.resolve_live_credentials(
        env_file=tmp_path / "missing.env",
        environ={"EBAY_ACCESS_TOKEN": "short-lived-access-token"},
    )
    assert token_only_creds["access_token"] == "short-lived-access-token"

    # HA storage file (read-only) with multiple entries requires --ha-entry-id
    storage_file = tmp_path / "core.config_entries"
    future_expiry = time.time() + 3600
    original_storage = json.dumps(
        {
            "data": {
                "entries": [
                    {"domain": "other", "data": {}},
                    {
                        "entry_id": "entry-uk",
                        "title": "eBay UK",
                        "domain": "ebay",
                        "data": {
                            "environment": "production",
                            "client_id": "ha-client-1",
                            "client_secret": "ha-secret-1",
                            "runame": "ha-runame-1",
                            "site_id": "3",
                            "token": {
                                "refresh_token": "ha-refresh-1",
                                "access_token": "cached-valid-access-token",
                                "expires_at": future_expiry,
                            },
                        },
                    },
                    {
                        "entry_id": "entry-de",
                        "title": "eBay DE",
                        "domain": "ebay",
                        "data": {
                            "environment": "production",
                            "client_id": "ha-client-2",
                            "client_secret": "ha-secret-2",
                            "runame": "ha-runame-2",
                            "site_id": "77",
                            "token": {"refresh_token": "ha-refresh-2"},
                        },
                    },
                ]
            }
        }
    )
    storage_file.write_text(original_storage, encoding="utf-8")

    with pytest.raises(ValueError, match="Multiple 'ebay' config entries found"):
        cli_harness.resolve_live_credentials(ha_config_entries=storage_file)

    ha_creds = cli_harness.resolve_live_credentials(
        ha_config_entries=storage_file,
        ha_entry_id="entry-uk",
    )
    assert ha_creds["client_id"] == "ha-client-1"
    assert ha_creds["refresh_token"] == "ha-refresh-1"
    assert ha_creds["access_token"] == "cached-valid-access-token"
    assert ha_creds["site_id"] == "3"
    assert storage_file.read_text(encoding="utf-8") == original_storage

    with pytest.raises(ValueError, match="Missing required eBay live credentials"):
        cli_harness.resolve_live_credentials(
            env_file=tmp_path / "missing.env", environ={}
        )


def test_run_live_watchlist_validation_with_mocked_client() -> None:
    """Live validation harness runs read-only XML and GraphQL probes and preserves flat error codes."""
    xml_demo, gql_demo = cli_harness._synthetic_demo_fixtures()
    root = ET.fromstring(xml_demo)
    gql_data = json.loads(gql_demo)["data"]

    client = Mock()
    client.environment = "production"
    client.site_id = "0"
    client.client_id = "secret-client-id"
    client._client_secret = "secret-client-secret"
    client.runame = "secret-runame"
    client.refresh_token = "secret-refresh-token"
    client._access_token = "secret-access-token"
    client.async_call_trading_api = AsyncMock(return_value=root)

    async def _side_effect(
        query: str,
        variables: dict[str, Any] | None = None,
        *,
        operation_name: str | None = None,
    ) -> dict[str, Any]:
        if operation_name == "Handshake":
            return {"__typename": "Query"}
        if operation_name == "GetWatchListCountAlt":
            raise EbayGraphQLError(
                "Field watchListCount undefined for user secret-client-id",
                errors=sanitize_graphql_errors(
                    [
                        {
                            "message": "Field watchListCount undefined for user secret-client-id",
                            "extensions": {
                                "code": "VALIDATION_ERROR",
                                "category": "REQUEST",
                                "domain": "BUY_WATCHLIST",
                            },
                        }
                    ]
                ),
            )
        return gql_data

    client.async_post_graphql = AsyncMock(side_effect=_side_effect)

    report = asyncio.run(cli_harness.run_live_watchlist_validation(client))
    serialized = json.dumps(report)

    assert report["mode"] == "live_authenticated"
    assert report["status"] == "compared"
    assert report["common_items_count"] == 2
    probes_by_key = {p["key"]: p for p in report["probes"]}
    assert probes_by_key["typename_handshake"]["outcome"] == "success"
    alt_err = probes_by_key["user_watchlist_count_alt"]["error"]
    assert alt_err["code"] == "VALIDATION_ERROR"
    assert alt_err["error_code"] == "VALIDATION_ERROR"
    assert alt_err["category"] == "REQUEST"
    assert alt_err["domain"] == "BUY_WATCHLIST"
    assert alt_err["error_codes"] == ["VALIDATION_ERROR"]
    assert alt_err["is_validation_error"] is True

    assert "secret-client-id" not in serialized
    assert "secret-client-secret" not in serialized
    assert "secret-refresh-token" not in serialized
    assert "secret-access-token" not in serialized
    assert "Synthetic Vintage Camera Lens" not in serialized
    assert "111222333444" not in serialized

    md = format_parity_report_markdown(report)
    assert "## Contract Probes" in md
    assert "## Field-Level Parity Matrix" in md
    assert "VALIDATION_ERROR" in md


def test_probe_selection_does_not_override_item_probe_with_custom_count_only_probe() -> (
    None
):
    """A custom count-only probe must not replace a built-in probe that returned usable items."""
    xml_demo, gql_demo = cli_harness._synthetic_demo_fixtures()
    root = ET.fromstring(xml_demo)
    gql_data = json.loads(gql_demo)["data"]

    client = Mock()
    client.environment = "production"
    client.site_id = "0"
    client.client_id = "secret-client-id"
    client._client_secret = "secret-client-secret"
    client.runame = "secret-runame"
    client.refresh_token = None
    client._refresh_token = None
    client._access_token = "short-lived-access-token"
    client.async_call_trading_api = AsyncMock(return_value=root)

    custom_count_only_probe = WatchlistContractProbe(
        key="custom_query_file",
        description="Custom count-only probe",
        query="query CustomCountOnly { userWatchList { total } }",
        operation_name="CustomCountOnly",
    )

    async def _side_effect(
        query: str,
        variables: dict[str, Any] | None = None,
        *,
        operation_name: str | None = None,
    ) -> dict[str, Any]:
        if operation_name == "Handshake":
            return {"__typename": "Query"}
        if operation_name in ("GetWatchListTotal", "CustomCountOnly"):
            return {"userWatchList": {"total": 2}}
        if operation_name == "GetUserWatchList":
            return gql_data
        raise EbayGraphQLError(
            "Validation error",
            errors=sanitize_graphql_errors(
                [{"extensions": {"code": "VALIDATION_ERROR", "category": "REQUEST"}}]
            ),
        )

    client.async_post_graphql = AsyncMock(side_effect=_side_effect)

    report = asyncio.run(
        cli_harness.run_live_watchlist_validation(
            client,
            custom_probe=custom_count_only_probe,
        )
    )
    # 1. Built-in item-bearing probe is selected over custom count-only probe
    assert report["selected_probe_key"] == "user_watchlist_items_paginated"
    assert report["status"] == "compared"
    assert report["graphql_watched_count"] == 2
    assert report["common_items_count"] == 2

    # 2. All probes (including custom_query_file) are preserved in report["probes"]
    probes_by_key = {p["key"]: p for p in report["probes"]}
    assert probes_by_key["user_watchlist_items_paginated"]["outcome"] == "success"
    assert probes_by_key["custom_query_file"]["outcome"] == "success"
    assert probes_by_key["custom_query_file"]["pagination_status"] == "count_only"

    # 3. Access-token-only mode emits an explicit operational warning
    assert len(report["operational_warnings"]) == 1
    assert "no refresh token" in report["operational_warnings"][0]
    md = format_parity_report_markdown(report)
    assert "## Operational Warnings" in md
    assert "Selected Parity Probe" in md

    # 4. When custom probe has comparable complete item evidence, custom probe wins tie-breaker
    custom_full_probe = WatchlistContractProbe(
        key="custom_query_file",
        description="Custom full item probe",
        query=(
            "query CustomFull($limit: Int, $offset: Int) { "
            "userWatchList(limit: $limit, offset: $offset) { total items { itemId title } } }"
        ),
        operation_name="CustomFull",
        variables={"limit": 100, "offset": 0},
    )

    async def _tie_side_effect(
        query: str,
        variables: dict[str, Any] | None = None,
        *,
        operation_name: str | None = None,
    ) -> dict[str, Any]:
        if operation_name == "Handshake":
            return {"__typename": "Query"}
        return gql_data

    client.async_post_graphql = AsyncMock(side_effect=_tie_side_effect)
    tie_report = asyncio.run(
        cli_harness.run_live_watchlist_validation(
            client,
            custom_probe=custom_full_probe,
        )
    )
    assert tie_report["selected_probe_key"] == "custom_query_file"
    assert tie_report["status"] == "compared"
