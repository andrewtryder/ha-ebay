"""Comprehensive unit tests for Milestone 7: Sold & Unsold Listing Classification Modernization."""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import scripts.validate_sold_unsold_parity as cli_harness
from custom_components.ebay.parsers.trading_builders import SOLD_UNSOLD_DURATION_DAYS
from custom_components.ebay.migration_policy import (
    FeatureKey,
    MigrationPolicy,
    ProviderType,
)
from custom_components.ebay.parsers.sold_unsold_parity import (
    FORBIDDEN_REPORT_KEYS,
    MONITORED_CLASSIFICATION_FIELDS,
    PARITY_DIFFERENT_SEMANTICS,
    PARITY_EXACT_MATCH,
    PARITY_MISSING_FIELD,
    PARITY_UNSUPPORTED,
    PARITY_UNVERIFIED,
    SOLD_UNSOLD_CONTRACT_PROBES,
    ClassificationEvidence,
    ClassificationSource,
    ListingLifecycleState,
    NormalizedListingClassification,
    ProviderVerificationEvidence,
    SaleClassification,
    SoldUnsoldCollectionResult,
    _compare_single_field,
    _is_safe_item_pseudonym,
    _parse_iso_datetime,
    _resolve_safe_field_comparison_note,
    compare_sold_unsold_snapshots,
    merge_composite_modern_sold_unsold,
    merge_rest_fulfillment_orders_pages,
    merge_trading_xml_combined_responses,
    merge_trading_xml_sold_unsold_pages,
    normalize_graphql_seller_listing_search,
    normalize_rest_fulfillment_orders,
    normalize_trading_xml_sold_unsold,
    pseudonymize_identifier,
    render_sold_unsold_parity_json,
    render_sold_unsold_parity_markdown,
    sanitize_diagnostic_string,
    sanitize_diagnostic_token,
    sanitize_sold_unsold_parity_report,
    validate_all_sold_unsold_contract_probes,
    validate_max_pages,
)


# ---------------------------------------------------------------------------
# Test Phase 1 & 2: Probes, Helpers & Bounds
# ---------------------------------------------------------------------------
def test_all_builtin_sold_unsold_contract_probes_are_strictly_read_only() -> None:
    """All candidate sold/unsold GraphQL probes must be read-only query operations."""
    validate_all_sold_unsold_contract_probes()
    for probe in SOLD_UNSOLD_CONTRACT_PROBES:
        assert probe.query.strip().startswith("query ")
        assert "mutation" not in probe.query.lower()


def test_validate_max_pages_bounds() -> None:
    """validate_max_pages strictly enforces integer 1..25."""
    assert validate_max_pages(1) == 1
    assert validate_max_pages(25) == 25
    assert validate_max_pages(10) == 10

    for invalid in (0, -1, 26, 500):
        with pytest.raises(ValueError, match="must be between 1 and 25"):
            validate_max_pages(invalid)

    for bad_type in ("10", 5.5, True, False, None):
        with pytest.raises(ValueError, match="must be an integer"):
            validate_max_pages(bad_type)  # type: ignore[arg-type]


def test_pseudonymize_identifier_and_is_safe_item_pseudonym() -> None:
    """Test deterministic pseudonymization and strict pseudonym format verification."""
    pseudo = pseudonymize_identifier("110000000001", prefix="item")
    assert pseudo.startswith("item_")
    assert len(pseudo) == 17  # 'item_' (5) + 12 hex chars (12)
    assert _is_safe_item_pseudonym(pseudo) is True

    # Determinism
    assert pseudonymize_identifier("110000000001", prefix="item") == pseudo
    assert pseudonymize_identifier("110000000002", prefix="item") != pseudo
    assert pseudonymize_identifier("") == ""

    # Raw numbers or spoofed prefixes must be rejected
    assert _is_safe_item_pseudonym("110000000001") is False
    assert _is_safe_item_pseudonym("item_110000000001") is False  # decimal only
    assert _is_safe_item_pseudonym("item_private_seller_xyz") is False
    assert _is_safe_item_pseudonym("item_secret123") is False
    assert _is_safe_item_pseudonym("item_123") is False


def test_sanitize_diagnostic_string_and_tokens() -> None:
    """Sanitize strings against tokens, emails, prices, and long numbers."""
    raw = "User dev@example.com paid $85.37 for item 110000000001 with refresh_token=secret123"
    clean = sanitize_diagnostic_string(raw)
    assert "dev@example.com" not in clean
    assert "85.37" not in clean
    assert "110000000001" not in clean
    assert "secret123" not in clean
    assert "[REDACTED_EMAIL]" in clean
    assert "[REDACTED_AMOUNT]" in clean
    assert "[REDACTED_ID]" in clean
    assert "[REDACTED_TOKEN]" in clean

    assert sanitize_diagnostic_token("XML_ACK_FAILURE") == "XML_ACK_FAILURE"
    assert sanitize_diagnostic_token("1001") == "1001"
    assert sanitize_diagnostic_token("NON_EXISTENT_CODE") == "UNKNOWN_ERROR"
    assert sanitize_diagnostic_token(None) == "UNKNOWN_ERROR"


def test_parse_iso_datetime_helper() -> None:
    """Test datetime parser handles ISO 8601, eBay format, and invalid values."""
    dt1 = _parse_iso_datetime("2026-10-30T10:00:00.000Z")
    assert isinstance(dt1, datetime)
    assert dt1.tzinfo == timezone.utc

    assert _parse_iso_datetime(None) is None
    assert _parse_iso_datetime("") is None
    assert _parse_iso_datetime("invalid-date-string") is None


# ---------------------------------------------------------------------------
# Test Phase 5: Legacy Trading XML Normalization
# ---------------------------------------------------------------------------
def test_normalize_trading_xml_sold_unsold_basic() -> None:
    """Parse synthetic Trading XML GetMyeBaySelling response with SoldList and UnsoldList."""
    xml_str = cli_harness.get_synthetic_sold_unsold_xml()
    res = normalize_trading_xml_sold_unsold(xml_str)

    assert res.is_complete is True
    assert res.is_truncated is False
    assert res.sold_items_count == 1
    assert res.unsold_items_count == 2
    assert "110000000001" in res.sold_item_ids
    assert "110000000002" in res.unsold_item_ids
    assert "110000000003" in res.unsold_item_ids
    assert res.selling_classification["110000000001"] == "sold"
    assert res.selling_classification["110000000002"] == "unsold"
    assert res.selling_classification["110000000003"] == "unsold"


def test_normalize_trading_xml_sold_precedence() -> None:
    """If an ItemID appears in both UnsoldList and SoldList, SoldList takes precedence."""
    xml_str = """<?xml version="1.0" encoding="utf-8"?>
<GetMyeBaySellingResponse xmlns="urn:ebay:apis:eBLBaseComponents">
  <Ack>Success</Ack>
  <SoldList>
    <PaginationResult>
      <TotalNumberOfPages>1</TotalNumberOfPages>
      <TotalNumberOfEntries>1</TotalNumberOfEntries>
    </PaginationResult>
    <ItemArray>
      <Item><ItemID>110000000099</ItemID></Item>
    </ItemArray>
  </SoldList>
  <UnsoldList>
    <PaginationResult>
      <TotalNumberOfPages>1</TotalNumberOfPages>
      <TotalNumberOfEntries>1</TotalNumberOfEntries>
    </PaginationResult>
    <ItemArray>
      <Item><ItemID>110000000099</ItemID></Item>
    </ItemArray>
  </UnsoldList>
</GetMyeBaySellingResponse>"""
    res = normalize_trading_xml_sold_unsold(xml_str)
    assert res.is_complete is True
    assert res.selling_classification["110000000099"] == "sold"


def test_normalize_trading_xml_sold_unsold_duplicate_ids() -> None:
    """Duplicate item IDs within an XML container trigger fail-closed DUPLICATE_ITEM_ID."""
    xml_str = """<?xml version="1.0" encoding="utf-8"?>
<GetMyeBaySellingResponse xmlns="urn:ebay:apis:eBLBaseComponents">
  <Ack>Success</Ack>
  <SoldList>
    <ItemArray>
      <Item><ItemID>110000000001</ItemID></Item>
      <Item><ItemID>110000000001</ItemID></Item>
    </ItemArray>
  </SoldList>
</GetMyeBaySellingResponse>"""
    res = normalize_trading_xml_sold_unsold(xml_str)
    assert res.is_complete is False
    assert any(e["code"] == "DUPLICATE_ITEM_ID" for e in res.errors)


def test_normalize_trading_xml_sold_unsold_empty_and_malformed() -> None:
    """Empty, non-XML, and unexpected root responses fail closed."""
    res_empty = normalize_trading_xml_sold_unsold("")
    assert res_empty.is_complete is False
    assert any(e["code"] == "EMPTY_XML_PAYLOAD" for e in res_empty.errors)

    res_parse = normalize_trading_xml_sold_unsold("<invalid xml")
    assert res_parse.is_complete is False
    assert any(e["code"] == "XML_PARSE_ERROR" for e in res_parse.errors)

    res_root = normalize_trading_xml_sold_unsold(
        "<WrongRoot xmlns='urn:ebay:apis:eBLBaseComponents'/>"
    )
    assert res_root.is_complete is False
    assert any(e["code"] == "INVALID_XML_ROOT" for e in res_root.errors)


def test_merge_trading_xml_sold_unsold_pages_multi_page() -> None:
    """Multi-page Trading XML SoldList pages merge sequentially and verify counts."""
    p1 = """<?xml version="1.0" encoding="utf-8"?>
<GetMyeBaySellingResponse xmlns="urn:ebay:apis:eBLBaseComponents">
  <Ack>Success</Ack>
  <SoldList>
    <PaginationResult>
      <TotalNumberOfPages>2</TotalNumberOfPages>
      <TotalNumberOfEntries>2</TotalNumberOfEntries>
    </PaginationResult>
    <ItemArray>
      <Item><ItemID>110000000001</ItemID></Item>
    </ItemArray>
  </SoldList>
</GetMyeBaySellingResponse>"""
    p2 = """<?xml version="1.0" encoding="utf-8"?>
<GetMyeBaySellingResponse xmlns="urn:ebay:apis:eBLBaseComponents">
  <Ack>Success</Ack>
  <SoldList>
    <PaginationResult>
      <TotalNumberOfPages>2</TotalNumberOfPages>
      <TotalNumberOfEntries>2</TotalNumberOfEntries>
    </PaginationResult>
    <ItemArray>
      <Item><ItemID>110000000002</ItemID></Item>
    </ItemArray>
  </SoldList>
</GetMyeBaySellingResponse>"""
    merged = merge_trading_xml_sold_unsold_pages([p1, p2], list_name="SoldList")
    assert merged.is_complete is True
    assert merged.sold_items_count == 2
    assert "110000000001" in merged.sold_item_ids
    assert "110000000002" in merged.sold_item_ids


def test_merge_trading_xml_sold_unsold_pages_contradictory_totals() -> None:
    """Divergent TotalNumberOfPages between pages triggers CONTRADICTORY_PAGINATION_TOTALS."""
    p1 = """<?xml version="1.0" encoding="utf-8"?>
<GetMyeBaySellingResponse xmlns="urn:ebay:apis:eBLBaseComponents">
  <Ack>Success</Ack>
  <SoldList>
    <PaginationResult>
      <TotalNumberOfPages>2</TotalNumberOfPages>
      <TotalNumberOfEntries>2</TotalNumberOfEntries>
    </PaginationResult>
    <ItemArray><Item><ItemID>110000000001</ItemID></Item></ItemArray>
  </SoldList>
</GetMyeBaySellingResponse>"""
    p2 = """<?xml version="1.0" encoding="utf-8"?>
<GetMyeBaySellingResponse xmlns="urn:ebay:apis:eBLBaseComponents">
  <Ack>Success</Ack>
  <SoldList>
    <PaginationResult>
      <TotalNumberOfPages>5</TotalNumberOfPages>
      <TotalNumberOfEntries>2</TotalNumberOfEntries>
    </PaginationResult>
    <ItemArray><Item><ItemID>110000000002</ItemID></Item></ItemArray>
  </SoldList>
</GetMyeBaySellingResponse>"""
    merged = merge_trading_xml_sold_unsold_pages([p1, p2], list_name="SoldList")
    assert merged.is_complete is False
    assert any(e["code"] == "CONTRADICTORY_PAGINATION_TOTALS" for e in merged.errors)


def test_merge_trading_xml_sold_unsold_pages_cycle_detection() -> None:
    """Duplicate payload across pages flags PAGINATION_CYCLE_DETECTED."""
    p1 = """<?xml version="1.0" encoding="utf-8"?>
<GetMyeBaySellingResponse xmlns="urn:ebay:apis:eBLBaseComponents">
  <Ack>Success</Ack>
  <SoldList>
    <PaginationResult>
      <TotalNumberOfPages>2</TotalNumberOfPages>
      <TotalNumberOfEntries>2</TotalNumberOfEntries>
    </PaginationResult>
    <ItemArray><Item><ItemID>110000000001</ItemID></Item></ItemArray>
  </SoldList>
</GetMyeBaySellingResponse>"""
    merged = merge_trading_xml_sold_unsold_pages([p1, p1], list_name="SoldList")
    assert merged.is_complete is False
    assert any(e["code"] == "PAGINATION_CYCLE_DETECTED" for e in merged.errors)


# ---------------------------------------------------------------------------
# Test Phase 4: Modern API Normalization (GraphQL & REST Orders)
# ---------------------------------------------------------------------------
def test_normalize_graphql_seller_listing_search_basic() -> None:
    """Normalize candidate GraphQL sellerListingSearch ended listings payload."""
    payload = cli_harness.get_synthetic_graphql_ended_listings()
    res = normalize_graphql_seller_listing_search(payload)

    assert res.is_complete is True
    assert res.sold_items_count == 1
    assert res.unsold_items_count == 2
    assert "110000000001" in res.sold_item_ids
    assert "110000000002" in res.unsold_item_ids
    assert "110000000003" in res.unsold_item_ids


def test_normalize_graphql_seller_listing_search_ended_listings_distinction() -> None:
    """Ended listings with unknown quantitySold must be UNKNOWN, not inferred as unsold."""
    payload = {
        "data": {
            "sellerListingSearch": {
                "total": 3,
                "items": [
                    {
                        "itemId": "110000000001",
                        "status": "ENDED",
                        "quantitySold": 2,  # Confirmed sold
                    },
                    {
                        "itemId": "110000000002",
                        "status": "ENDED",
                        "quantitySold": 0,  # Confirmed unsold
                    },
                    {
                        "itemId": "110000000003",
                        "status": "ENDED",
                        "quantitySold": None,  # Unknown sale outcome
                    },
                ],
            }
        }
    }
    res = normalize_graphql_seller_listing_search(payload)
    assert res.is_complete is True
    assert res.selling_classification["110000000001"] == "sold"
    assert res.selling_classification["110000000002"] == "unsold"
    assert res.selling_classification["110000000003"] == "unknown"


def test_normalize_graphql_seller_listing_search_handshake_typename() -> None:
    """__typename handshake only is marked incomplete with HANDSHAKE_ONLY."""
    payload = {"data": {"__typename": "Query"}}
    res = normalize_graphql_seller_listing_search(payload)
    assert res.is_complete is False
    assert any(e["code"] == "HANDSHAKE_ONLY" for e in res.errors)


def test_normalize_graphql_seller_listing_search_count_only() -> None:
    """Count-only query is marked incomplete with COUNT_ONLY_QUERY."""
    payload = {
        "data": {
            "sellerListingSearch": {
                "total": 42,
            }
        }
    }
    res = normalize_graphql_seller_listing_search(payload)
    assert res.is_complete is False
    assert any(e["code"] == "COUNT_ONLY_QUERY" for e in res.errors)


def test_normalize_rest_fulfillment_orders_basic() -> None:
    """Normalize REST Sell Fulfillment getOrders response."""
    payload = cli_harness.get_synthetic_rest_fulfillment_orders()
    res = normalize_rest_fulfillment_orders(payload)

    assert res.is_complete is True
    assert res.sold_items_count == 1
    assert res.unsold_items_count == 0  # Fulfillment orders cannot confirm unsold
    assert "110000000001" in res.sold_item_ids
    assert res.selling_classification["110000000001"] == "sold"


def test_normalize_rest_fulfillment_orders_multi_line_items_and_multi_qty() -> None:
    """Orders with multiple line items and multi-quantity track unique item IDs correctly."""
    payload = {
        "total": 1,
        "limit": 50,
        "offset": 0,
        "orders": [
            {
                "orderId": "12-34567-89012",
                "lineItems": [
                    {"legacyItemId": "110000000001", "quantity": 3},
                    {"legacyItemId": "110000000002", "quantity": 1},
                ],
            }
        ],
    }
    res = normalize_rest_fulfillment_orders(payload)
    assert res.is_complete is True
    # 1 order, but 2 distinct sold items
    assert res.sold_items_count == 2
    assert "110000000001" in res.sold_item_ids
    assert "110000000002" in res.sold_item_ids


def test_merge_composite_modern_sold_unsold() -> None:
    """Composite modern merger reconciles GraphQL ended listings and REST orders."""
    gql_payload = cli_harness.get_synthetic_graphql_ended_listings()
    gql_res = normalize_graphql_seller_listing_search(gql_payload)

    orders_payload = cli_harness.get_synthetic_rest_fulfillment_orders()
    orders_res = normalize_rest_fulfillment_orders(orders_payload)

    composite = merge_composite_modern_sold_unsold(gql_res, orders_res)
    assert composite.is_complete is True
    assert composite.sold_items_count == 1
    assert composite.unsold_items_count == 2
    assert composite.selling_classification["110000000001"] == "sold"
    assert composite.selling_classification["110000000002"] == "unsold"
    assert composite.selling_classification["110000000003"] == "unsold"


# ---------------------------------------------------------------------------
# Test Phase 6: Parity Comparison Engine
# ---------------------------------------------------------------------------
def test_compare_sold_unsold_snapshots_exact_match() -> None:
    """Baseline XML and Composite candidate with matching classifications match exactly."""
    xml_res = normalize_trading_xml_sold_unsold(
        cli_harness.get_synthetic_sold_unsold_xml()
    )
    comp_res = merge_composite_modern_sold_unsold(
        normalize_graphql_seller_listing_search(
            cli_harness.get_synthetic_graphql_ended_listings()
        ),
        normalize_rest_fulfillment_orders(
            cli_harness.get_synthetic_rest_fulfillment_orders()
        ),
    )

    comparison = compare_sold_unsold_snapshots(xml_res, comp_res)
    assert (
        comparison["field_comparisons"]["sold_items_count"]["status"]
        == PARITY_EXACT_MATCH
    )
    assert (
        comparison["field_comparisons"]["unsold_items_count"]["status"]
        == PARITY_EXACT_MATCH
    )
    assert (
        comparison["field_comparisons"]["selling_classification"]["status"]
        == PARITY_EXACT_MATCH
    )


def test_compare_sold_unsold_snapshots_inconclusive_on_errors() -> None:
    """Snapshot with errors fails closed as inconclusive across all monitored fields."""
    xml_err = SoldUnsoldCollectionResult(
        listings=(),
        sold_item_ids=frozenset(),
        unsold_item_ids=frozenset(),
        selling_classification={},
        is_complete=False,
        errors=({"code": "XML_PARSE_ERROR"},),
    )
    cand = normalize_graphql_seller_listing_search(
        cli_harness.get_synthetic_graphql_ended_listings()
    )

    comparison = compare_sold_unsold_snapshots(xml_err, cand)
    assert comparison["overall_status"] == "inconclusive"
    assert comparison["is_inconclusive"] is True
    for fld in MONITORED_CLASSIFICATION_FIELDS:
        assert comparison["field_comparisons"][fld]["status"] == PARITY_UNVERIFIED


def test_compare_sold_unsold_snapshots_empty_collection_anti_false_positive() -> None:
    """Empty collections match counts but evaluate record fields as unverified and inconclusive."""
    empty1 = SoldUnsoldCollectionResult(
        listings=(),
        sold_item_ids=frozenset(),
        unsold_item_ids=frozenset(),
        selling_classification={},
        sold_items_count=0,
        unsold_items_count=0,
        is_complete=True,
    )
    empty2 = SoldUnsoldCollectionResult(
        listings=(),
        sold_item_ids=frozenset(),
        unsold_item_ids=frozenset(),
        selling_classification={},
        sold_items_count=0,
        unsold_items_count=0,
        is_complete=True,
    )
    comparison = compare_sold_unsold_snapshots(empty1, empty2)
    assert comparison["overall_status"] == "inconclusive"
    assert comparison["is_inconclusive"] is True
    assert (
        comparison["field_comparisons"]["sold_items_count"]["status"]
        == PARITY_EXACT_MATCH
    )
    assert comparison["field_comparisons"]["item_id"]["status"] == PARITY_UNVERIFIED


def test_compare_sold_unsold_snapshots_mismatches() -> None:
    """Mismatches in sold item count or set flag different_semantics."""
    snap1 = SoldUnsoldCollectionResult(
        listings=(
            NormalizedListingClassification(
                item_id="110000000001",
                lifecycle_state=ListingLifecycleState.ENDED,
                sale_classification=SaleClassification.SOLD,
                source=ClassificationSource.TRADING_XML,
            ),
        ),
        sold_item_ids=frozenset({"110000000001"}),
        unsold_item_ids=frozenset(),
        selling_classification={"110000000001": "sold"},
        sold_items_count=1,
        unsold_items_count=0,
        is_complete=True,
    )
    snap2 = SoldUnsoldCollectionResult(
        listings=(
            NormalizedListingClassification(
                item_id="110000000001",
                lifecycle_state=ListingLifecycleState.ENDED,
                sale_classification=SaleClassification.UNSOLD,
                source=ClassificationSource.GRAPHQL,
            ),
        ),
        sold_item_ids=frozenset(),
        unsold_item_ids=frozenset({"110000000001"}),
        selling_classification={"110000000001": "unsold"},
        sold_items_count=0,
        unsold_items_count=1,
        is_complete=True,
    )
    comparison = compare_sold_unsold_snapshots(snap1, snap2)
    assert comparison["overall_status"] == "fail"
    assert (
        comparison["field_comparisons"]["sold_items_count"]["status"]
        == PARITY_DIFFERENT_SEMANTICS
    )
    assert (
        comparison["field_comparisons"]["sale_classification"]["status"]
        == PARITY_DIFFERENT_SEMANTICS
    )


# ---------------------------------------------------------------------------
# Test Phase 8 & 9: Privacy Protection & Default-Deny Sanitization
# ---------------------------------------------------------------------------
def test_sanitize_sold_unsold_parity_report_removes_sensitive_keys() -> None:
    """Report sanitization strips credentials, secrets, and raw listing IDs."""
    dirty = {
        "overall_status": "pass",
        "access_token": "Bearer secret_tok",
        "client_secret": "my_secret",
        "password": "secret_password",
        "buyer_username": "buyer_123",
        "sold_item_ids": ["110000000001", "item_private_seller_xyz"],
        "field_comparisons": {
            "sold_items_count": {
                "status": "exact_match",
                "note": "A note containing client_secret=very_secret_key and access_token=Bearer tok123",
            }
        },
        "errors": [
            {
                "code": "FORBIDDEN",
                "diagnostic": "Failed with token Bearer v^1.1#tok456",
            }
        ],
    }
    sanitized = sanitize_sold_unsold_parity_report(dirty)

    for forbidden in FORBIDDEN_REPORT_KEYS:
        assert forbidden not in sanitized

    # Verify ID pseudonymization
    for i_id in sanitized["sold_item_ids"]:
        assert i_id.startswith("item_")
        assert "110000000001" not in i_id
        assert "private_seller_xyz" not in i_id

    # Verify generic note and error mapping
    assert (
        sanitized["field_comparisons"]["sold_items_count"]["note"]
        == "Exact match between baseline and candidate"
    )
    assert (
        sanitized["errors"][0]["diagnostic"]
        == "Access forbidden; insufficient permissions or scope"
    )


def test_privacy_adversarial_handles_word_amounts_and_malicious_item_keys() -> None:
    """Ensure opaque handles, word amounts, and malicious item keys never leak into JSON or Markdown."""
    dirty = {
        "overall_status": "fail",
        "sold_item_ids": [
            "item_private_seller_xyz",
            "item_secret123",
            "item_110000000001",  # Spoofed decimal suffix
            "110000000002",
            "item_0b0f5cc1cb42",  # Legitimate precomputed pseudonym
        ],
        "selling_classification": {
            "item_private_seller_xyz": "sold",
            "110000000002": "unsold",
        },
        "field_comparisons": {
            "sale_classification": {
                "status": "different_semantics",
                "note": "buyer private_seller_xyz bought for eighty dollars and forty five bucks",
            },
            "item_id": {
                "status": "exact_match",
                "note": "All 5 item IDs matched exactly",  # Safe template
            },
        },
        "errors": [
            {
                "code": "XML_ACK_MISSING",
                "diagnostic": "Missing or unrecognized Ack: MaliciousAckValue",
            },
            {
                "code": "1001",
                "diagnostic": "Sensitive DB Error: table users column email",
            },
        ],
    }

    sanitized = sanitize_sold_unsold_parity_report(dirty)

    item_keys = sanitized["sold_item_ids"]
    assert "item_private_seller_xyz" not in item_keys
    assert "item_secret123" not in item_keys
    assert "item_110000000001" not in item_keys
    assert "110000000002" not in item_keys
    assert "item_0b0f5cc1cb42" in item_keys

    # Check safe template preservation
    assert (
        sanitized["field_comparisons"]["sale_classification"]["note"]
        == "Value or semantic difference between baseline and candidate"
    )
    assert (
        sanitized["field_comparisons"]["item_id"]["note"]
        == "All 5 item IDs matched exactly"
    )

    # Render Markdown & JSON
    md = render_sold_unsold_parity_markdown(dirty)
    js = render_sold_unsold_parity_json(dirty)

    for output in (md, js):
        assert "private_seller_xyz" not in output
        assert "eighty dollars" not in output
        assert "forty five bucks" not in output
        assert "item_private_seller_xyz" not in output
        assert "item_secret123" not in output
        assert "item_110000000001" not in output
        assert "MaliciousAckValue" not in output
        assert "Sensitive DB Error" not in output


def test_render_sold_unsold_parity_markdown_and_json() -> None:
    """Render functions produce valid Markdown and JSON structures."""
    report = cli_harness.run_synthetic_parity_verification()
    md = render_sold_unsold_parity_markdown(report)
    js = render_sold_unsold_parity_json(report)

    assert "# Sold & Unsold Listing Classification Parity Report" in md
    assert "**Overall Status:**" in md
    parsed = json.loads(js)
    assert "overall_status" in parsed
    assert "field_counts" in parsed


# ---------------------------------------------------------------------------
# Test Phase 7 & 1: Migration Policy & Home Assistant Preservation
# ---------------------------------------------------------------------------
def test_migration_policy_sold_unsold_baseline_provider() -> None:
    """MigrationPolicy maps FeatureKey.SOLD_UNSOLD to Trading XML baseline."""
    baseline = MigrationPolicy.get_default_provider(FeatureKey.SOLD_UNSOLD)
    assert baseline == ProviderType.TRADING_XML


def test_sold_unsold_home_assistant_contract_preservation() -> None:
    """Verify that HA entity keys and duration constants are preserved."""
    assert SOLD_UNSOLD_DURATION_DAYS == 60
    assert "sold_items_count" in MONITORED_CLASSIFICATION_FIELDS
    assert "unsold_items_count" in MONITORED_CLASSIFICATION_FIELDS


# ---------------------------------------------------------------------------
# Test CLI Runner Functions
# ---------------------------------------------------------------------------
def test_cli_run_offline_synthetic_demo() -> None:
    """CLI run_synthetic_parity_verification executes offline without network."""
    res = cli_harness.run_synthetic_parity_verification()
    assert isinstance(res, dict)
    assert "field_counts" in res
    assert "field_comparisons" in res


def test_cli_run_offline_custom_fixtures(tmp_path: Path) -> None:
    """CLI execution with local XML and GraphQL fixture files."""
    xml_file = tmp_path / "test_sold.xml"
    xml_file.write_text(cli_harness.get_synthetic_sold_unsold_xml(), encoding="utf-8")

    gql_file = tmp_path / "test_ended.json"
    gql_file.write_text(
        json.dumps(cli_harness.get_synthetic_graphql_ended_listings()), encoding="utf-8"
    )

    orders_file = tmp_path / "test_orders.json"
    orders_file.write_text(
        json.dumps(cli_harness.get_synthetic_rest_fulfillment_orders()),
        encoding="utf-8",
    )

    parser = cli_harness.build_parser()
    args = parser.parse_args(
        [
            "--xml-fixture",
            str(xml_file),
            "--graphql-fixture",
            str(gql_file),
            "--orders-fixture",
            str(orders_file),
            "--format",
            "json",
        ]
    )
    assert args.xml_fixture == xml_file
    assert args.graphql_fixture == gql_file
    assert args.orders_fixture == orders_file


@pytest.mark.asyncio
async def test_async_run_live_validation_fails_closed_without_auth() -> None:
    """Live validation helper reports NOT_IMPLEMENTED because GraphQL schema entitlement is unverified."""
    res = await cli_harness.async_run_live_validation(None, max_pages=10)
    assert res["overall_status"] == "inconclusive"
    assert any(e["code"] == "NOT_IMPLEMENTED" for e in res["errors"])


def test_cli_validate_max_pages_parser() -> None:
    """CLI argument validator rejects invalid page bounds."""
    assert cli_harness._validate_cli_max_pages("1") == 1
    assert cli_harness._validate_cli_max_pages("25") == 25

    with pytest.raises(Exception):
        cli_harness._validate_cli_max_pages("0")
    with pytest.raises(Exception):
        cli_harness._validate_cli_max_pages("26")
    with pytest.raises(Exception):
        cli_harness._validate_cli_max_pages("not-an-int")


# ---------------------------------------------------------------------------
# Deep Coverage & Adversarial Edge Cases for sold_unsold_parity.py
# ---------------------------------------------------------------------------
def test_sanitize_diagnostic_string_non_string() -> None:
    """sanitize_diagnostic_string handles non-string inputs safely."""
    assert sanitize_diagnostic_string(12345) == "12345"  # type: ignore[arg-type]
    assert sanitize_diagnostic_string(None) == "None"  # type: ignore[arg-type]


def test_sanitize_diagnostic_tokens_kinds() -> None:
    """sanitize_diagnostic_token covers kind='category', kind='domain', and kind='unknown'."""
    assert sanitize_diagnostic_token("", kind="error_code") == "UNKNOWN_ERROR"
    assert sanitize_diagnostic_token("", kind="category") == "UNKNOWN"
    assert sanitize_diagnostic_token("BUSINESS", kind="category") == "BUSINESS"
    assert sanitize_diagnostic_token("invalid_cat", kind="category") == "SYSTEM"
    assert sanitize_diagnostic_token("ORDER", kind="domain") == "ORDER"
    assert sanitize_diagnostic_token("invalid_dom", kind="domain") == "COMMERCE"
    assert sanitize_diagnostic_token("anything", kind="unrecognized") == "UNKNOWN"


def test_resolve_safe_field_comparison_note_allowlisted_and_patterns() -> None:
    """_resolve_safe_field_comparison_note resolves exact allowlist, regex patterns, and generic fallback."""
    exact = "Snapshot is incomplete, truncated, or contains errors; fail-closed"
    assert _resolve_safe_field_comparison_note(PARITY_UNVERIFIED, exact) == exact

    pat_sold = "All 12 sold item IDs matched exactly"
    assert _resolve_safe_field_comparison_note(PARITY_EXACT_MATCH, pat_sold) == pat_sold

    pat_unsold = "All 7 unsold item IDs matched exactly"
    assert (
        _resolve_safe_field_comparison_note(PARITY_EXACT_MATCH, pat_unsold)
        == pat_unsold
    )

    pat_none = "No matching item IDs between XML (3) and candidate (5)"
    assert (
        _resolve_safe_field_comparison_note(PARITY_MISSING_FIELD, pat_none) == pat_none
    )

    pat_miss = "Identity mismatch: 2 missing in candidate"
    assert (
        _resolve_safe_field_comparison_note(PARITY_DIFFERENT_SEMANTICS, pat_miss)
        == pat_miss
    )

    pat_extra = "Identity mismatch: 1 extra in candidate"
    assert (
        _resolve_safe_field_comparison_note(PARITY_DIFFERENT_SEMANTICS, pat_extra)
        == pat_extra
    )

    pat_both = "Identity mismatch: 1 missing in candidate; 2 extra in candidate"
    assert (
        _resolve_safe_field_comparison_note(PARITY_DIFFERENT_SEMANTICS, pat_both)
        == pat_both
    )

    fallback = _resolve_safe_field_comparison_note(
        PARITY_EXACT_MATCH, "Unexpected private text"
    )
    assert fallback == "Exact match between baseline and candidate"


def test_parse_iso_datetime_datetime_input() -> None:
    """_parse_iso_datetime handles datetime objects with and without tzinfo."""
    now_utc = datetime.now(timezone.utc)
    assert _parse_iso_datetime(now_utc) == now_utc

    now_naive = datetime(2026, 10, 30, 12, 0, 0)
    parsed = _parse_iso_datetime(now_naive)
    assert parsed is not None
    assert parsed.tzinfo == timezone.utc

    # Standard ISO format with +00:00 or -04:00
    iso_str = "2026-10-30T14:30:00-04:00"
    dt_iso = _parse_iso_datetime(iso_str)
    assert dt_iso is not None
    assert dt_iso.tzinfo == timezone.utc


def test_normalize_trading_xml_element_input_and_acks() -> None:
    """normalize_trading_xml_sold_unsold handles ET.Element input, Ack failure, and errors."""
    xml_ack_fail = """<?xml version="1.0" encoding="utf-8"?>
<GetMyeBaySellingResponse xmlns="urn:ebay:apis:eBLBaseComponents">
  <Ack>Failure</Ack>
  <Errors>
    <ErrorCode>1001</ErrorCode>
    <SeverityCode>Error</SeverityCode>
  </Errors>
  <SoldList>
    <ItemArray>
      <Item>
        <ItemID></ItemID>
      </Item>
      <Item>
        <ItemID>110000000001</ItemID>
      </Item>
    </ItemArray>
  </SoldList>
</GetMyeBaySellingResponse>"""
    root = ET.fromstring(xml_ack_fail)
    res = normalize_trading_xml_sold_unsold(root)
    assert res.is_complete is False
    codes = [e["code"] for e in res.errors]
    assert "XML_ACK_FAILURE" in codes
    assert "1001" in codes
    assert "MISSING_ITEM_ID" in codes


def test_normalize_trading_xml_partial_failure_and_unknown_ack() -> None:
    """normalize_trading_xml_sold_unsold handles PartialFailure and unknown Ack."""
    xml_partial = """<?xml version="1.0" encoding="utf-8"?>
<GetMyeBaySellingResponse xmlns="urn:ebay:apis:eBLBaseComponents">
  <Ack>PartialFailure</Ack>
</GetMyeBaySellingResponse>"""
    res_part = normalize_trading_xml_sold_unsold(xml_partial)
    assert any(e["code"] == "XML_ACK_PARTIAL_FAILURE" for e in res_part.errors)
    assert any(e["code"] == "MISSING_REQUIRED_CONTAINER" for e in res_part.errors)

    xml_unknown_ack = """<?xml version="1.0" encoding="utf-8"?>
<GetMyeBaySellingResponse xmlns="urn:ebay:apis:eBLBaseComponents">
  <Ack>CustomUnknown</Ack>
</GetMyeBaySellingResponse>"""
    res_unk = normalize_trading_xml_sold_unsold(xml_unknown_ack)
    assert any(e["code"] == "XML_ACK_MISSING" for e in res_unk.errors)


def test_merge_trading_xml_sold_unsold_pages_edge_cases() -> None:
    """merge_trading_xml_sold_unsold_pages handles empty list, missing pagination, contradictory totals, and exceeded pages."""
    # Empty sequence
    res_empty = merge_trading_xml_sold_unsold_pages([])
    assert res_empty.is_complete is False
    assert any(e["code"] == "NO_PAGES_PROVIDED" for e in res_empty.errors)

    # Page missing pagination metadata
    page_no_pag = """<?xml version="1.0" encoding="utf-8"?>
<GetMyeBaySellingResponse xmlns="urn:ebay:apis:eBLBaseComponents">
  <Ack>Success</Ack>
  <SoldList>
    <ItemArray>
      <Item><ItemID>110000000001</ItemID></Item>
    </ItemArray>
  </SoldList>
</GetMyeBaySellingResponse>"""
    res_no_pag = merge_trading_xml_sold_unsold_pages([page_no_pag])
    assert any(e["code"] == "MISSING_PAGINATION_METADATA" for e in res_no_pag.errors)
    assert any(e["code"] == "UNVERIFIED_COLLECTION_CENSUS" for e in res_no_pag.errors)

    # Contradictory entries & pages after end declared
    p1 = """<?xml version="1.0" encoding="utf-8"?>
<GetMyeBaySellingResponse xmlns="urn:ebay:apis:eBLBaseComponents">
  <Ack>Success</Ack>
  <SoldList>
    <PaginationResult>
      <TotalNumberOfPages>1</TotalNumberOfPages>
      <TotalNumberOfEntries>10</TotalNumberOfEntries>
    </PaginationResult>
    <ItemArray>
      <Item><ItemID>110000000001</ItemID></Item>
    </ItemArray>
  </SoldList>
</GetMyeBaySellingResponse>"""
    p2 = """<?xml version="1.0" encoding="utf-8"?>
<GetMyeBaySellingResponse xmlns="urn:ebay:apis:eBLBaseComponents">
  <Ack>Success</Ack>
  <SoldList>
    <PaginationResult>
      <TotalNumberOfPages>1</TotalNumberOfPages>
      <TotalNumberOfEntries>5</TotalNumberOfEntries>
    </PaginationResult>
    <ItemArray>
      <Item><ItemID>110000000002</ItemID></Item>
    </ItemArray>
  </SoldList>
</GetMyeBaySellingResponse>"""
    res_contra = merge_trading_xml_sold_unsold_pages([p1, p2])
    codes_contra = [e["code"] for e in res_contra.errors]
    assert "CONTRADICTORY_PAGINATION_TOTALS" in codes_contra
    assert "PAGES_AFTER_END_DECLARED" in codes_contra
    assert "TOTAL_RECORDS_MISMATCH" in codes_contra

    # Pending continuation (retrieved 1 of 2 declared pages)
    p_pend = """<?xml version="1.0" encoding="utf-8"?>
<GetMyeBaySellingResponse xmlns="urn:ebay:apis:eBLBaseComponents">
  <Ack>Success</Ack>
  <SoldList>
    <PaginationResult>
      <TotalNumberOfPages>2</TotalNumberOfPages>
      <TotalNumberOfEntries>2</TotalNumberOfEntries>
    </PaginationResult>
    <ItemArray>
      <Item><ItemID>110000000001</ItemID></Item>
    </ItemArray>
  </SoldList>
</GetMyeBaySellingResponse>"""
    res_pend = merge_trading_xml_sold_unsold_pages([p_pend])
    assert res_pend.is_truncated is True
    assert any(e["code"] == "PENDING_CONTINUATION" for e in res_pend.errors)

    # Max pages exceeded
    pages_3 = [p1, p1, p1]  # 3 pages with max_pages=2
    res_max = merge_trading_xml_sold_unsold_pages(pages_3, max_pages=2)
    assert res_max.is_truncated is True
    assert any(e["code"] == "MAX_PAGES_EXCEEDED" for e in res_max.errors)


def test_normalize_graphql_seller_listing_search_edge_cases() -> None:
    """normalize_graphql_seller_listing_search covers malformed, missing data, errors, and count-only."""
    # Non-mapping
    assert normalize_graphql_seller_listing_search(None).is_complete is False
    assert normalize_graphql_seller_listing_search("not-a-dict").is_complete is False  # type: ignore[arg-type]

    # Top-level errors with code & extensions
    gql_with_errors = {
        "errors": [
            {"message": "Rate limit exceeded", "extensions": {"code": "RATE_LIMITED"}},
            {"message": "General", "code": "FORBIDDEN"},
        ],
        "data": None,
    }
    res_err = normalize_graphql_seller_listing_search(gql_with_errors)
    assert res_err.is_complete is False
    assert any(e["code"] == "RATE_LIMITED" for e in res_err.errors)
    assert any(e["code"] == "FORBIDDEN" for e in res_err.errors)

    # Missing data without errors
    res_no_data = normalize_graphql_seller_listing_search({})
    assert any(e["code"] == "MISSING_DATA" for e in res_no_data.errors)

    # No items container
    res_no_cnt = normalize_graphql_seller_listing_search(
        {"data": {"otherContainer": {}}}
    )
    assert any(e["code"] == "NO_ITEMS_CONTAINER" for e in res_no_cnt.errors)

    # Malformed / empty node in items and status varieties
    gql_items = {
        "data": {
            "sellerListingSearch": {
                "total": 10,
                "items": [
                    "not-a-mapping-node",
                    {"itemId": "", "status": "SCHEDULED"},
                    {
                        "itemId": "110000000001",
                        "status": "SCHEDULED",
                        "quantitySold": 0,
                    },
                    {
                        "itemId": "110000000001",
                        "status": "ACTIVE",
                        "quantitySold": 1,
                    },  # duplicate
                    {
                        "itemId": "110000000002",
                        "status": "ACTIVE",
                        "quantitySold": None,
                    },  # unknown
                ],
            }
        }
    }
    res_items = normalize_graphql_seller_listing_search(gql_items)
    assert any(e["code"] == "MISSING_ITEM_ID" for e in res_items.errors)
    assert any(e["code"] == "DUPLICATE_ITEM_ID" for e in res_items.errors)
    assert any(e["code"] == "TOTAL_RECORDS_MISMATCH" for e in res_items.errors)
    # Check scheduled & active status mapping
    l_sched = next(
        item for item in res_items.listings if item.item_id == "110000000001"
    )
    assert l_sched.lifecycle_state in (
        ListingLifecycleState.SCHEDULED,
        ListingLifecycleState.ACTIVE,
    )
    l_unk = next(item for item in res_items.listings if item.item_id == "110000000002")
    assert l_unk.sale_classification == SaleClassification.UNKNOWN


def test_normalize_rest_fulfillment_orders_edge_cases() -> None:
    """normalize_rest_fulfillment_orders covers non-mapping, error arrays, and malformed items."""
    assert normalize_rest_fulfillment_orders(None).is_complete is False
    assert normalize_rest_fulfillment_orders([1, 2, 3]).is_complete is False  # type: ignore[arg-type]

    # Error array
    payload_err = {
        "errors": [{"errorId": 25001, "message": "Fulfillment service error"}],
        "orders": [],
    }
    res_err = normalize_rest_fulfillment_orders(payload_err)
    assert any(e["code"] == "25001" for e in res_err.errors)

    # Malformed order and line item structure
    payload_malformed = {
        "orders": [
            "not-an-order-dict",
            {
                "orderId": "ORD-1",
                "lineItems": [
                    "not-a-line-dict",
                    {"legacyItemId": ""},  # empty ID
                    {"legacyItemId": "110000000001", "quantity": "invalid-int"},
                ],
            },
        ],
        "total": 1,
    }
    res_mal = normalize_rest_fulfillment_orders(payload_malformed)
    assert "110000000001" in res_mal.sold_item_ids


def test_composite_merger_with_unknown_items() -> None:
    """merge_composite_modern_sold_unsold handles listings with unknown outcome."""
    gql_res = SoldUnsoldCollectionResult(
        listings=(
            NormalizedListingClassification(
                item_id="110000000099",
                lifecycle_state=ListingLifecycleState.ENDED,
                sale_classification=SaleClassification.UNKNOWN,
                source=ClassificationSource.GRAPHQL,
                evidence_level=ClassificationEvidence.LOW,
            ),
        ),
        sold_item_ids=frozenset(),
        unsold_item_ids=frozenset(),
        selling_classification={"110000000099": "unknown"},
        sold_items_count=0,
        unsold_items_count=0,
        is_complete=True,
        is_truncated=False,
    )
    orders_res = SoldUnsoldCollectionResult(
        listings=(),
        sold_item_ids=frozenset(),
        unsold_item_ids=frozenset(),
        selling_classification={},
        sold_items_count=0,
        unsold_items_count=0,
        is_complete=True,
        is_truncated=False,
    )
    comp = merge_composite_modern_sold_unsold(gql_res, orders_res)
    assert comp.selling_classification["110000000099"] == "unknown"


def test_compare_sold_unsold_snapshots_unmatched_and_missing() -> None:
    """compare_sold_unsold_snapshots handles non-overlapping item sets."""
    xml_res = SoldUnsoldCollectionResult(
        listings=(
            NormalizedListingClassification(
                item_id="110000000001",
                lifecycle_state=ListingLifecycleState.ENDED,
                sale_classification=SaleClassification.SOLD,
                source=ClassificationSource.TRADING_XML,
                evidence_level=ClassificationEvidence.HIGH,
            ),
        ),
        sold_item_ids=frozenset(["110000000001"]),
        unsold_item_ids=frozenset(),
        selling_classification={"110000000001": "sold"},
        sold_items_count=1,
        unsold_items_count=0,
        is_complete=True,
        is_truncated=False,
    )
    cand_res = SoldUnsoldCollectionResult(
        listings=(
            NormalizedListingClassification(
                item_id="110000000002",
                lifecycle_state=ListingLifecycleState.ENDED,
                sale_classification=SaleClassification.SOLD,
                source=ClassificationSource.GRAPHQL,
                evidence_level=ClassificationEvidence.HIGH,
            ),
        ),
        sold_item_ids=frozenset(["110000000002"]),
        unsold_item_ids=frozenset(),
        selling_classification={"110000000002": "sold"},
        sold_items_count=1,
        unsold_items_count=0,
        is_complete=True,
        is_truncated=False,
    )
    diff = compare_sold_unsold_snapshots(xml_res, cand_res)
    assert diff["overall_status"] == "fail"
    assert diff["field_comparisons"]["item_id"]["status"] == PARITY_DIFFERENT_SEMANTICS
    assert (
        diff["field_comparisons"]["sale_classification"]["status"]
        == PARITY_MISSING_FIELD
    )


def test_sanitize_sold_unsold_parity_report_malformed_inputs() -> None:
    """sanitize_sold_unsold_parity_report handles non-dict, non-list errors, and empty IDs."""
    assert (
        sanitize_sold_unsold_parity_report("not-a-dict")["overall_status"]
        == "inconclusive"
    )  # type: ignore[arg-type]

    dirty = {
        "overall_status": "pass",
        "sold_item_ids": ["", "   ", "item_0b0f5cc1cb42"],
        "selling_classification": {
            "": "sold",
            "   ": "unsold",
            "110000000001": "invalid_outcome",
        },
        "errors": [
            "not-a-dict-error",
            {"code": "25001", "severity": "Warning"},
            {"code": "UNKNOWN_CUSTOM_CODE"},
        ],
    }
    san = sanitize_sold_unsold_parity_report(dirty)
    assert "" not in san["sold_item_ids"]
    assert any(e["code"] == "MALFORMED_ERROR" for e in san["errors"])
    assert any(e.get("severity") == "Warning" for e in san["errors"])


def test_cli_main_synthetic_demo_json_and_markdown(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Test CLI main() entrypoint with --synthetic-demo for markdown and json formats."""
    # Markdown output
    cli_harness.main(["--synthetic-demo", "--format", "markdown"])
    captured_md = capsys.readouterr().out
    assert "# Sold & Unsold Listing Classification Parity Report" in captured_md

    # JSON output
    cli_harness.main(["--synthetic-demo", "--format", "json"])
    captured_js = capsys.readouterr().out
    parsed = json.loads(captured_js)
    assert "overall_status" in parsed


def test_cli_main_output_file(tmp_path: Path) -> None:
    """Test CLI main() with --output argument writing to file."""
    out_file = tmp_path / "report.md"
    cli_harness.main(
        ["--synthetic-demo", "--format", "markdown", "--output", str(out_file)]
    )
    assert out_file.exists()
    content = out_file.read_text(encoding="utf-8")
    assert "# Sold & Unsold Listing Classification Parity Report" in content


# ---------------------------------------------------------------------------
# Adversarial Regression Tests for Review 5720203171 Hardening
# ---------------------------------------------------------------------------


def test_graphql_grouped_buckets_and_strict_lifecycle() -> None:
    """GraphQL sellerListingSearch grouped buckets must parse correctly with strict lifecycle mapping."""
    payload = {
        "data": {
            "sellerListingSearch": {
                "activeListings": {
                    "total": 1,
                    "items": [
                        {
                            "itemId": "110000000010",
                            "status": "ACTIVE",
                            "quantity": 5,
                            "quantitySold": 0,  # Active listing with 0 sold is NOT unsold!
                        }
                    ],
                },
                "scheduledListings": {
                    "total": 1,
                    "items": [
                        {
                            "itemId": "110000000011",
                            "status": "SCHEDULED",
                            "quantity": 1,
                            "quantitySold": 0,  # Scheduled listing with 0 sold is NOT unsold!
                        }
                    ],
                },
                "endedListings": {
                    "total": 2,
                    "items": [
                        {
                            "itemId": "110000000012",
                            "status": "ENDED",
                            "quantity": 1,
                            "quantitySold": 1,  # Sold
                        },
                        {
                            "itemId": "110000000013",
                            "status": "ENDED",
                            "quantity": 1,
                            "quantitySold": 0,  # Unsold
                        },
                    ],
                },
            }
        }
    }
    res = normalize_graphql_seller_listing_search(payload)
    assert res.is_complete is True
    # Verify active and scheduled are NOT classified as unsold
    assert res.selling_classification["110000000010"] == "unknown"
    assert res.selling_classification["110000000011"] == "unknown"
    assert res.selling_classification["110000000012"] == "sold"
    assert res.selling_classification["110000000013"] == "unsold"
    assert res.sold_items_count == 1
    assert res.unsold_items_count == 1


def test_graphql_census_completeness_and_malformed_page_info() -> None:
    """GraphQL collection must fail-closed if census metadata or pageInfo is missing/malformed."""
    # Missing total and missing pageInfo
    payload_no_census = {
        "data": {
            "sellerListingSearch": {
                "items": [
                    {
                        "itemId": "110000000001",
                        "status": "ENDED",
                        "quantitySold": 0,
                    }
                ],
            }
        }
    }
    res = normalize_graphql_seller_listing_search(payload_no_census)
    assert res.is_complete is False
    assert any(e["code"] == "UNVERIFIED_COLLECTION_CENSUS" for e in res.errors)

    # hasNextPage is True -> incomplete pending continuation
    payload_pending = {
        "data": {
            "sellerListingSearch": {
                "pageInfo": {"hasNextPage": True},
                "items": [
                    {
                        "itemId": "110000000001",
                        "status": "ENDED",
                        "quantitySold": 0,
                    }
                ],
            }
        }
    }
    res_pending = normalize_graphql_seller_listing_search(payload_pending)
    assert res_pending.is_complete is False
    assert any(e["code"] == "PENDING_CONTINUATION" for e in res_pending.errors)

    # hasNextPage is non-boolean
    payload_malformed_pageinfo = {
        "data": {
            "sellerListingSearch": {
                "pageInfo": {"hasNextPage": "true"},
                "items": [
                    {
                        "itemId": "110000000001",
                        "status": "ENDED",
                        "quantitySold": 0,
                    }
                ],
            }
        }
    }
    res_malformed = normalize_graphql_seller_listing_search(payload_malformed_pageinfo)
    assert res_malformed.is_complete is False
    assert any(e["code"] == "MALFORMED_PAGE_INFO" for e in res_malformed.errors)


def test_rest_fulfillment_orders_census_cancellation_and_invalid_quantity() -> None:
    """REST fulfillment orders must check census completeness, ignore canceled orders, and reject invalid quantity."""
    # total declares 10 orders, but payload only provides 1
    payload_pending = {
        "total": 10,
        "limit": 50,
        "offset": 0,
        "orders": [
            {
                "orderId": "ord-1",
                "lineItems": [
                    {
                        "lineItemId": "li-1",
                        "legacyItemId": "110000000001",
                        "quantity": 1,
                    }
                ],
            }
        ],
    }
    res_pending = normalize_rest_fulfillment_orders(payload_pending)
    assert res_pending.is_complete is False
    assert any(e["code"] == "PENDING_CONTINUATION" for e in res_pending.errors)

    # total > 0 but orders container missing
    payload_missing_orders = {"total": 5, "limit": 50, "offset": 0}
    res_missing = normalize_rest_fulfillment_orders(payload_missing_orders)
    assert res_missing.is_complete is False
    assert any(e["code"] == "MISSING_REQUIRED_CONTAINER" for e in res_missing.errors)

    # Canceled orders must not count as confirmed sales
    payload_canceled = {
        "total": 1,
        "limit": 50,
        "offset": 0,
        "orders": [
            {
                "orderId": "ord-canceled",
                "cancelStatus": {"cancelState": "CANCELED"},
                "lineItems": [
                    {
                        "lineItemId": "li-1",
                        "legacyItemId": "110000000001",
                        "quantity": 1,
                    }
                ],
            }
        ],
    }
    res_canceled = normalize_rest_fulfillment_orders(payload_canceled)
    assert res_canceled.sold_items_count == 0
    assert len(res_canceled.listings) == 0

    # Duplicate order IDs
    payload_dup = {
        "total": 2,
        "limit": 50,
        "offset": 0,
        "orders": [
            {
                "orderId": "dup-ord",
                "lineItems": [
                    {
                        "lineItemId": "li-1",
                        "legacyItemId": "110000000001",
                        "quantity": 1,
                    }
                ],
            },
            {
                "orderId": "dup-ord",
                "lineItems": [
                    {
                        "lineItemId": "li-2",
                        "legacyItemId": "110000000002",
                        "quantity": 1,
                    }
                ],
            },
        ],
    }
    res_dup = normalize_rest_fulfillment_orders(payload_dup)
    assert any(e["code"] == "DUPLICATE_ORDER_ID" for e in res_dup.errors)

    # Invalid non-integer or <= 0 quantity in line item must not default to 1
    payload_invalid_qty = {
        "total": 1,
        "limit": 50,
        "offset": 0,
        "orders": [
            {
                "orderId": "ord-bad-qty",
                "lineItems": [
                    {
                        "lineItemId": "li-1",
                        "legacyItemId": "110000000001",
                        "quantity": -5,
                    }
                ],
            }
        ],
    }
    res_invalid_qty = normalize_rest_fulfillment_orders(payload_invalid_qty)
    assert any(e["code"] == "MALFORMED_ORDER" for e in res_invalid_qty.errors)
    assert res_invalid_qty.listings[0].quantity_sold == 0


def test_rest_fulfillment_orders_multi_page_merger() -> None:
    """REST fulfillment multi-page merger must enforce 25-page cap, offset sequence, and consistent totals."""
    # Respects 25-page max and halts while processing
    pages = [
        {
            "total": 30,
            "limit": 1,
            "offset": i,
            "orders": [
                {
                    "orderId": f"ord-{i}",
                    "lineItems": [
                        {
                            "lineItemId": f"li-{i}",
                            "legacyItemId": f"1100000000{i:02d}",
                            "quantity": 1,
                        }
                    ],
                }
            ],
        }
        for i in range(26)
    ]
    res_merged = merge_rest_fulfillment_orders_pages(pages, max_pages=25)
    assert res_merged.is_truncated is True
    assert res_merged.is_complete is False
    assert any(e["code"] == "MAX_PAGES_EXCEEDED" for e in res_merged.errors)
    assert len(res_merged.listings) == 25

    # Out of order offset sequencing
    bad_sequence = [pages[0], pages[2]]  # Skipped page 1 (offset 1)
    res_seq = merge_rest_fulfillment_orders_pages(bad_sequence, max_pages=25)
    assert any(e["code"] == "PAGINATION_SEQUENCE_ERROR" for e in res_seq.errors)

    # Contradictory total declared
    p1 = {"total": 50, "limit": 10, "offset": 0, "orders": []}
    p2 = {"total": 60, "limit": 10, "offset": 10, "orders": []}
    res_contra = merge_rest_fulfillment_orders_pages([p1, p2], max_pages=25)
    assert any(
        e["code"] == "CONTRADICTORY_PAGINATION_TOTALS" for e in res_contra.errors
    )


def test_composite_modern_sold_unsold_contradictions_and_accounts() -> None:
    """Composite merger detects contradictions (UNSOLD vs SOLD) -> AMBIGUOUS, and enforces account/window parity."""
    # Contradiction: GraphQL says ENDED + quantitySold == 0 (UNSOLD), but REST order reports confirmed sale!
    gql_payload = {
        "data": {
            "sellerListingSearch": {
                "total": 1,
                "accountId": "seller_1",
                "items": [
                    {
                        "itemId": "110000000001",
                        "status": "ENDED",
                        "quantitySold": 0,
                    }
                ],
            }
        }
    }
    gql_res = normalize_graphql_seller_listing_search(gql_payload, window_days=60)

    orders_payload = {
        "total": 1,
        "limit": 50,
        "offset": 0,
        "accountId": "seller_1",
        "orders": [
            {
                "orderId": "ord-conflict",
                "lineItems": [
                    {
                        "lineItemId": "li-1",
                        "legacyItemId": "110000000001",
                        "quantity": 1,
                    }
                ],
            }
        ],
    }
    orders_res = normalize_rest_fulfillment_orders(orders_payload, window_days=60)

    comp = merge_composite_modern_sold_unsold(gql_res, orders_res, window_days=60)
    assert comp.selling_classification["110000000001"] == "ambiguous"
    assert comp.listings[0].sale_classification == SaleClassification.AMBIGUOUS
    assert comp.listings[0].evidence_level == ClassificationEvidence.LOW
    assert any(e["code"] == "MALFORMED_ORDER" for e in comp.errors)

    # Order-only item: item exists in orders but was not found in GraphQL search
    # Must retain lifecycle UNKNOWN (never forced to ENDED, because GTC listings remain active)
    orders_only_payload = {
        "total": 1,
        "limit": 50,
        "offset": 0,
        "orders": [
            {
                "orderId": "ord-active-gtc",
                "lineItems": [
                    {
                        "lineItemId": "li-2",
                        "legacyItemId": "110000000099",
                        "quantity": 1,
                    }
                ],
            }
        ],
    }
    orders_only_res = normalize_rest_fulfillment_orders(orders_only_payload)
    empty_gql = normalize_graphql_seller_listing_search(
        {"data": {"sellerListingSearch": {"total": 0, "items": []}}}
    )
    comp_order_only = merge_composite_modern_sold_unsold(empty_gql, orders_only_res)
    assert comp_order_only.selling_classification["110000000099"] == "sold"
    assert comp_order_only.listings[0].lifecycle_state == ListingLifecycleState.UNKNOWN

    # Account mismatch between GraphQL and REST
    gql_acc_a = normalize_graphql_seller_listing_search(
        {
            "data": {
                "sellerListingSearch": {
                    "total": 0,
                    "accountId": "seller_alpha",
                    "items": [],
                }
            }
        }
    )
    orders_acc_b = normalize_rest_fulfillment_orders(
        {"total": 0, "accountId": "seller_beta", "orders": []}
    )
    comp_acc_mismatch = merge_composite_modern_sold_unsold(gql_acc_a, orders_acc_b)
    assert any(e["code"] == "ACCOUNT_MISMATCH" for e in comp_acc_mismatch.errors)
    assert comp_acc_mismatch.is_complete is False

    # Window mismatch between GraphQL and REST
    gql_w60 = normalize_graphql_seller_listing_search(
        {"data": {"sellerListingSearch": {"total": 0, "items": []}}},
        window_days=60,
    )
    orders_w90 = normalize_rest_fulfillment_orders(
        {"total": 0, "orders": []}, window_days=90
    )
    comp_win_mismatch = merge_composite_modern_sold_unsold(
        gql_w60, orders_w90, window_days=60
    )
    assert any(e["code"] == "WINDOW_MISMATCH" for e in comp_win_mismatch.errors)
    assert comp_win_mismatch.is_complete is False


def test_trading_xml_baseline_false_completeness_and_bounds() -> None:
    """Trading XML parser must reject missing containers, unverified single-page pagination, and enforce 25-page bounds."""
    # Single page declaring TotalNumberOfPages=2 -> PENDING_CONTINUATION
    xml_multi_page = """<?xml version="1.0" encoding="utf-8"?>
    <GetMyeBaySellingResponse xmlns="urn:ebay:apis:eBLBaseComponents">
      <Ack>Success</Ack>
      <SoldList>
        <PaginationResult>
          <TotalNumberOfPages>2</TotalNumberOfPages>
          <TotalNumberOfEntries>2</TotalNumberOfEntries>
        </PaginationResult>
        <ItemArray>
          <Item><ItemID>110000000001</ItemID><QuantitySold>1</QuantitySold></Item>
        </ItemArray>
      </SoldList>
      <UnsoldList>
        <PaginationResult>
          <TotalNumberOfPages>1</TotalNumberOfPages>
          <TotalNumberOfEntries>0</TotalNumberOfEntries>
        </PaginationResult>
      </UnsoldList>
    </GetMyeBaySellingResponse>"""
    res_pending = normalize_trading_xml_sold_unsold(xml_multi_page)
    assert res_pending.is_complete is False
    assert any(e["code"] == "PENDING_CONTINUATION" for e in res_pending.errors)

    # Combined mode missing UnsoldList
    xml_missing_unsold = """<?xml version="1.0" encoding="utf-8"?>
    <GetMyeBaySellingResponse xmlns="urn:ebay:apis:eBLBaseComponents">
      <Ack>Success</Ack>
      <SoldList>
        <PaginationResult>
          <TotalNumberOfPages>1</TotalNumberOfPages>
          <TotalNumberOfEntries>1</TotalNumberOfEntries>
        </PaginationResult>
        <ItemArray>
          <Item><ItemID>110000000001</ItemID><QuantitySold>1</QuantitySold></Item>
        </ItemArray>
      </SoldList>
    </GetMyeBaySellingResponse>"""
    res_missing_unsold = normalize_trading_xml_sold_unsold(
        xml_missing_unsold, requested_list="both"
    )
    assert res_missing_unsold.is_complete is False
    assert any(
        e["code"] == "MISSING_REQUIRED_CONTAINER" for e in res_missing_unsold.errors
    )

    # Missing PaginationResult
    xml_no_pagination = """<?xml version="1.0" encoding="utf-8"?>
    <GetMyeBaySellingResponse xmlns="urn:ebay:apis:eBLBaseComponents">
      <Ack>Success</Ack>
      <SoldList>
        <ItemArray>
          <Item><ItemID>110000000001</ItemID><QuantitySold>1</QuantitySold></Item>
        </ItemArray>
      </SoldList>
      <UnsoldList />
    </GetMyeBaySellingResponse>"""
    res_no_pag = normalize_trading_xml_sold_unsold(xml_no_pagination)
    assert res_no_pag.is_complete is False
    assert any(e["code"] == "MISSING_PAGINATION_METADATA" for e in res_no_pag.errors)

    # 26 pages provided to merge_trading_xml_sold_unsold_pages -> bounded at 25 while processing
    page_template = """<?xml version="1.0" encoding="utf-8"?>
    <GetMyeBaySellingResponse xmlns="urn:ebay:apis:eBLBaseComponents">
      <Ack>Success</Ack>
      <SoldList>
        <PaginationResult>
          <TotalNumberOfPages>30</TotalNumberOfPages>
          <TotalNumberOfEntries>30</TotalNumberOfEntries>
        </PaginationResult>
        <ItemArray>
          <Item><ItemID>1100000000{idx:02d}</ItemID><QuantitySold>1</QuantitySold></Item>
        </ItemArray>
      </SoldList>
    </GetMyeBaySellingResponse>"""
    many_pages = [page_template.format(idx=i) for i in range(26)]
    res_25_cap = merge_trading_xml_sold_unsold_pages(
        many_pages, list_name="sold", max_pages=25
    )
    assert res_25_cap.is_truncated is True
    assert res_25_cap.is_complete is False
    assert any(e["code"] == "MAX_PAGES_EXCEEDED" for e in res_25_cap.errors)
    assert len(res_25_cap.listings) == 25

    # merge_trading_xml_combined_responses merges sold and unsold snapshots
    combined_page = cli_harness.get_synthetic_sold_unsold_xml()
    combined_snap = merge_trading_xml_combined_responses([combined_page])
    assert combined_snap.is_complete is True
    assert combined_snap.sold_items_count == 1
    assert combined_snap.unsold_items_count == 2


def test_parity_engine_unsupported_fields_and_window_mismatch() -> None:
    """Parity engine must report PARITY_UNSUPPORTED for baseline-omitted fields and fail-closed on window mismatch."""
    status, note = _compare_single_field("quantity_sold", None, None)
    assert status == PARITY_UNSUPPORTED
    assert note == "Field not provided in baseline Trading XML"

    status_end, note_end = _compare_single_field("ended_at", None, None)
    assert status_end == PARITY_UNSUPPORTED
    assert note_end == "Field not provided in baseline Trading XML"

    # Window mismatch in snapshot comparison
    snap_xml = normalize_trading_xml_sold_unsold(
        cli_harness.get_synthetic_sold_unsold_xml(), window_days=60
    )
    snap_cand = merge_composite_modern_sold_unsold(
        normalize_graphql_seller_listing_search(
            cli_harness.get_synthetic_graphql_ended_listings(), window_days=90
        ),
        normalize_rest_fulfillment_orders(
            cli_harness.get_synthetic_rest_fulfillment_orders(), window_days=90
        ),
        window_days=90,
    )
    cmp_res = compare_sold_unsold_snapshots(snap_xml, snap_cand)
    assert cmp_res["is_inconclusive"] is True
    assert any(e["code"] == "WINDOW_MISMATCH" for e in cmp_res["errors"])


def test_cli_fixture_strictness_and_live_flag(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    """CLI must reject partial fixture configurations and exit 1 for live mode."""
    xml_file = tmp_path / "dummy.xml"
    xml_file.write_text("<xml/>", encoding="utf-8")

    # Providing only --xml-fixture without graphql and orders fixtures
    ret = cli_harness.main(["--xml-fixture", str(xml_file)])
    assert ret == 1
    err_msg = capsys.readouterr().err
    assert "all three fixtures must be provided" in err_msg

    # Providing non-existent fixture path
    non_existent = tmp_path / "missing.json"
    ret_missing = cli_harness.main(
        [
            "--xml-fixture",
            str(xml_file),
            "--graphql-fixture",
            str(non_existent),
            "--orders-fixture",
            str(xml_file),
        ]
    )
    assert ret_missing == 1
    err_missing = capsys.readouterr().err
    assert "Fixture file not found" in err_missing

    # Passing --live reports NOT_IMPLEMENTED and exits 1
    ret_live = cli_harness.main(["--live"])
    assert ret_live == 1
    captured = capsys.readouterr().out
    assert "NOT_IMPLEMENTED" in captured


def test_rest_fulfillment_terminal_offset_page_and_missing_metadata() -> None:
    """REST Fulfillment normalizer must reject terminal-only offset pages and missing metadata."""
    # Terminal offset page: total=2, offset=1, limit=1, orders=[1 order]
    terminal_payload = {
        "total": 2,
        "offset": 1,
        "limit": 1,
        "orders": [
            {
                "orderId": "09-12345-67890",
                "lineItems": [{"legacyItemId": "110000000001", "quantity": 1}],
            }
        ],
    }
    res_terminal = normalize_rest_fulfillment_orders(terminal_payload)
    assert res_terminal.is_complete is False
    assert res_terminal.is_truncated is True
    assert any(err["code"] == "PENDING_CONTINUATION" for err in res_terminal.errors)

    # Missing required metadata: total omitted
    no_total = {
        "offset": 0,
        "limit": 50,
        "orders": [],
    }
    res_no_total = normalize_rest_fulfillment_orders(no_total)
    assert res_no_total.is_complete is False
    assert any(
        err["code"] == "MISSING_PAGINATION_METADATA" for err in res_no_total.errors
    )

    # Missing required metadata: offset omitted
    no_offset = {
        "total": 1,
        "limit": 50,
        "orders": [
            {
                "orderId": "09-12345-67891",
                "lineItems": [{"legacyItemId": "110000000002", "quantity": 1}],
            }
        ],
    }
    res_no_offset = normalize_rest_fulfillment_orders(no_offset)
    assert res_no_offset.is_complete is False
    assert any(
        err["code"] == "MISSING_PAGINATION_METADATA" for err in res_no_offset.errors
    )


def test_merge_rest_fulfillment_orders_pages_adversarial_pagination() -> None:
    """REST Fulfillment page merger must enforce offset sequences, total consistency, and account IDs."""
    # 1. Page 2 missing total or offset
    pages_missing_meta = [
        {
            "total": 2,
            "offset": 0,
            "limit": 1,
            "orders": [
                {
                    "orderId": "ord-1",
                    "lineItems": [{"legacyItemId": "110000000001", "quantity": 1}],
                }
            ],
        },
        {
            # Missing total and limit!
            "offset": 1,
            "orders": [
                {
                    "orderId": "ord-2",
                    "lineItems": [{"legacyItemId": "110000000002", "quantity": 1}],
                }
            ],
        },
    ]
    res_meta = merge_rest_fulfillment_orders_pages(pages_missing_meta)
    assert res_meta.is_complete is False
    assert any(err["code"] == "MISSING_PAGINATION_METADATA" for err in res_meta.errors)

    # 2. Sequence gap: page 1 offset 0, page 2 offset 5 instead of 1
    pages_gap = [
        {
            "total": 2,
            "offset": 0,
            "limit": 1,
            "orders": [
                {
                    "orderId": "ord-1",
                    "lineItems": [{"legacyItemId": "110000000001", "quantity": 1}],
                }
            ],
        },
        {
            "total": 2,
            "offset": 5,
            "limit": 1,
            "orders": [
                {
                    "orderId": "ord-2",
                    "lineItems": [{"legacyItemId": "110000000002", "quantity": 1}],
                }
            ],
        },
    ]
    res_gap = merge_rest_fulfillment_orders_pages(pages_gap)
    assert res_gap.is_complete is False
    assert any(err["code"] == "PAGINATION_SEQUENCE_ERROR" for err in res_gap.errors)

    # 3. Account conflict across pages
    pages_conflict = [
        {
            "total": 2,
            "offset": 0,
            "limit": 1,
            "accountId": "seller_alpha",
            "orders": [
                {
                    "orderId": "ord-1",
                    "lineItems": [{"legacyItemId": "110000000001", "quantity": 1}],
                }
            ],
        },
        {
            "total": 2,
            "offset": 1,
            "limit": 1,
            "accountId": "seller_beta",
            "orders": [
                {
                    "orderId": "ord-2",
                    "lineItems": [{"legacyItemId": "110000000002", "quantity": 1}],
                }
            ],
        },
    ]
    res_conflict = merge_rest_fulfillment_orders_pages(pages_conflict)
    assert res_conflict.is_complete is False
    assert any(err["code"] == "ACCOUNT_MISMATCH" for err in res_conflict.errors)

    # 4. Truncated collection: total=3 but only 2 orders provided without pending next
    pages_truncated = [
        {
            "total": 3,
            "offset": 0,
            "limit": 1,
            "orders": [
                {
                    "orderId": "ord-1",
                    "lineItems": [{"legacyItemId": "110000000001", "quantity": 1}],
                }
            ],
        },
        {
            "total": 3,
            "offset": 1,
            "limit": 1,
            "orders": [
                {
                    "orderId": "ord-2",
                    "lineItems": [{"legacyItemId": "110000000002", "quantity": 1}],
                }
            ],
        },
    ]
    res_trunc = merge_rest_fulfillment_orders_pages(pages_truncated)
    assert res_trunc.is_complete is False
    assert res_trunc.is_truncated is True
    assert any(err["code"] == "PENDING_CONTINUATION" for err in res_trunc.errors)

    # 5. Empty pages list
    res_empty = merge_rest_fulfillment_orders_pages([])
    assert res_empty.is_complete is False
    assert any(err["code"] == "NO_PAGES_PROVIDED" for err in res_empty.errors)

    # 6. Page is not a mapping
    res_non_map = merge_rest_fulfillment_orders_pages(["not-a-dict"])  # type: ignore[arg-type]
    assert res_non_map.is_complete is False
    assert any(err["code"] == "MALFORMED_PAGE" for err in res_non_map.errors)

    # 7. Page containing API errors array
    pages_with_err = [
        {
            "total": 1,
            "offset": 0,
            "limit": 1,
            "orders": [],
            "errors": [{"errorId": "INTERNAL_SERVER_ERROR"}],
        }
    ]
    res_api_err = merge_rest_fulfillment_orders_pages(pages_with_err)
    assert res_api_err.is_complete is False
    assert any(err["code"] == "INTERNAL_SERVER_ERROR" for err in res_api_err.errors)

    # 8. Extra page after reaching declared total
    pages_after_end = [
        {
            "total": 1,
            "offset": 0,
            "limit": 1,
            "orders": [
                {
                    "orderId": "ord-1",
                    "lineItems": [{"legacyItemId": "110000000001", "quantity": 1}],
                }
            ],
        },
        {
            "total": 1,
            "offset": 1,
            "limit": 1,
            "orders": [
                {
                    "orderId": "ord-extra",
                    "lineItems": [{"legacyItemId": "110000000002", "quantity": 1}],
                }
            ],
        },
    ]
    res_after_end = merge_rest_fulfillment_orders_pages(pages_after_end)
    assert res_after_end.is_complete is False
    assert any(
        err["code"] == "PAGES_AFTER_END_DECLARED" for err in res_after_end.errors
    )

    # 9. Page missing orders container
    pages_no_orders = [
        {
            "total": 1,
            "offset": 0,
            "limit": 1,
            "orders": "invalid_orders_type",
        }
    ]
    res_no_orders = merge_rest_fulfillment_orders_pages(pages_no_orders)
    assert res_no_orders.is_complete is False
    assert any(
        err["code"] == "MISSING_REQUIRED_CONTAINER" for err in res_no_orders.errors
    )


def test_graphql_grouped_bucket_nested_continuation_and_totals_reconciliation() -> None:
    """GraphQL grouped buckets must validate internal pageInfo.hasNextPage and bucket total reconciliation."""
    # Nested bucket continuation in endedListings must block collection completeness
    nested_cont_payload = {
        "data": {
            "sellerListingSearch": {
                "total": 2,
                "endedListings": {
                    "total": 2,
                    "pageInfo": {"hasNextPage": True},
                    "items": [
                        {
                            "itemId": "110000000001",
                            "status": "ENDED",
                            "quantitySold": 0,
                        }
                    ],
                },
            }
        }
    }
    res_nested = normalize_graphql_seller_listing_search(nested_cont_payload)
    assert res_nested.is_complete is False
    assert res_nested.is_truncated is True
    assert any(err["code"] == "PENDING_CONTINUATION" for err in res_nested.errors)

    # Bucket declared total mismatch (declared 5, but items array has 1)
    bucket_mismatch_payload = {
        "data": {
            "sellerListingSearch": {
                "total": 5,
                "endedListings": {
                    "total": 5,
                    "pageInfo": {"hasNextPage": False},
                    "items": [
                        {
                            "itemId": "110000000001",
                            "status": "ENDED",
                            "quantitySold": 0,
                        }
                    ],
                },
            }
        }
    }
    res_mismatch = normalize_graphql_seller_listing_search(bucket_mismatch_payload)
    assert res_mismatch.is_complete is False
    assert any(err["code"] == "TOTAL_RECORDS_MISMATCH" for err in res_mismatch.errors)


def test_graphql_date_window_evidence_off_window_and_undated() -> None:
    """GraphQL listings with off-window or undated endings must be appropriately qualified."""
    # Listing with explicit ended_at outside lookback window (e.g. 100 days ago, window is 60 days)
    off_window_payload = {
        "data": {
            "sellerListingSearch": {
                "total": 1,
                "items": [
                    {
                        "itemId": "110000000001",
                        "status": "ENDED",
                        "quantitySold": 0,
                        "itemEndDate": "2025-01-01T00:00:00.000Z",  # Off-window
                    }
                ],
            }
        }
    }
    res_off = normalize_graphql_seller_listing_search(
        off_window_payload, window_days=60
    )
    assert res_off.selling_classification["110000000001"] == "unknown"
    assert res_off.listings[0].sale_classification == SaleClassification.UNKNOWN

    # Listing with no ended_at (undated): classified as unsold with medium evidence, but window_verified=False
    undated_payload = {
        "data": {
            "sellerListingSearch": {
                "total": 1,
                "items": [
                    {
                        "itemId": "110000000002",
                        "status": "ENDED",
                        "quantitySold": 0,
                    }
                ],
            }
        }
    }
    res_undated = normalize_graphql_seller_listing_search(
        undated_payload, window_days=60
    )
    assert res_undated.selling_classification["110000000002"] == "unsold"
    assert res_undated.listings[0].evidence_level == ClassificationEvidence.MEDIUM
    assert res_undated.window_verified is False
    assert res_undated.reconciliation_ready is False


def test_composite_cross_provider_account_and_window_evidence() -> None:
    """Composite modern merger requires positive external evidence of account identity and window bounds."""
    now_dt = datetime.now(timezone.utc)
    now_iso = now_dt.isoformat()
    start_dt = now_dt - timedelta(days=60)
    end_dt = now_dt

    gql_payload = {
        "data": {
            "sellerListingSearch": {
                "total": 1,
                "accountId": "seller_verified",
                "items": [
                    {
                        "itemId": "110000000001",
                        "status": "ENDED",
                        "quantitySold": 1,
                        "itemEndDate": now_iso,
                    }
                ],
            }
        }
    }
    # Raw fixtures without external trust evidence fail-closed on account/window verification
    gql_res_unverified = normalize_graphql_seller_listing_search(
        gql_payload, window_days=60
    )
    assert gql_res_unverified.account_verified is False
    assert gql_res_unverified.window_verified is False
    assert gql_res_unverified.reconciliation_ready is False

    orders_payload = {
        "total": 1,
        "limit": 50,
        "offset": 0,
        "accountId": "seller_verified",
        "orders": [
            {
                "orderId": "ord-1",
                "lineItems": [{"legacyItemId": "110000000001", "quantity": 1}],
            }
        ],
    }
    orders_res_unverified = normalize_rest_fulfillment_orders(
        orders_payload, window_days=60
    )
    assert orders_res_unverified.account_verified is False
    assert orders_res_unverified.window_verified is False
    assert orders_res_unverified.reconciliation_ready is False

    comp_unverified = merge_composite_modern_sold_unsold(
        gql_res_unverified, orders_res_unverified, window_days=60
    )
    assert comp_unverified.is_complete is True
    assert comp_unverified.account_verified is False
    assert comp_unverified.window_verified is False
    assert comp_unverified.reconciliation_ready is False

    # Now verify with explicit ProviderVerificationEvidence
    evidence = ProviderVerificationEvidence(
        account_verified=True,
        verified_account_id="seller_verified",
        window_verified=True,
        window_start_utc=start_dt,
        window_end_utc=end_dt,
        api_entitlement_verified=True,
        provenance_source="test_harness",
    )
    gql_res_verified = normalize_graphql_seller_listing_search(
        gql_payload, window_days=60, trust_evidence=evidence
    )
    assert gql_res_verified.account_verified is True
    assert gql_res_verified.window_verified is True
    assert gql_res_verified.reconciliation_ready is True

    orders_res_verified = normalize_rest_fulfillment_orders(
        orders_payload, window_days=60, evidence=evidence
    )
    assert orders_res_verified.account_verified is True
    assert orders_res_verified.window_verified is True
    assert orders_res_verified.reconciliation_ready is True

    comp_verified = merge_composite_modern_sold_unsold(
        gql_res_verified, orders_res_verified, window_days=60
    )
    assert comp_verified.is_complete is True
    assert comp_verified.account_verified is True
    assert comp_verified.window_verified is True
    assert comp_verified.reconciliation_ready is True

    # Missing account in GraphQL leaves account_verified=False and reconciliation_ready=False
    gql_no_acc_payload = {
        "data": {
            "sellerListingSearch": {
                "total": 1,
                "items": [
                    {
                        "itemId": "110000000001",
                        "status": "ENDED",
                        "quantitySold": 1,
                        "itemEndDate": now_iso,
                    }
                ],
            }
        }
    }
    gql_no_acc = normalize_graphql_seller_listing_search(
        gql_no_acc_payload, window_days=60
    )
    comp_unverified_acc = merge_composite_modern_sold_unsold(
        gql_no_acc, orders_res_verified, window_days=60
    )
    assert comp_unverified_acc.account_verified is False
    assert comp_unverified_acc.reconciliation_ready is False


def test_compare_sold_unsold_snapshots_unsupported_fields_separation() -> None:
    """Snapshot comparison must separate supported_fields_parity from complete 10-field migration parity."""
    # Baseline Trading XML has 8 fields, but lacks quantity_sold and ended_at (PARITY_UNSUPPORTED)
    xml_data = cli_harness.get_synthetic_sold_unsold_xml()
    xml_snap = normalize_trading_xml_sold_unsold(xml_data)

    gql_data = cli_harness.get_synthetic_graphql_ended_listings()
    gql_snap = normalize_graphql_seller_listing_search(gql_data)

    orders_data = cli_harness.get_synthetic_rest_fulfillment_orders()
    orders_snap = normalize_rest_fulfillment_orders(orders_data)

    cand_snap = merge_composite_modern_sold_unsold(gql_snap, orders_snap)

    report = compare_sold_unsold_snapshots(xml_snap, cand_snap)
    # Supported fields match -> PASS
    assert report["supported_fields_parity"] == "pass"
    assert report["comparable_supported_fields_count"] == 8
    assert report["unsupported_fields_count"] == 2
    # Overall 10-field parity cannot pass due to unsupported fields -> INCONCLUSIVE
    assert report["overall_status"] == "inconclusive"
    assert report["is_inconclusive"] is True
    # Migration readiness blocked
    assert report["migration_readiness"] == "blocked"
    assert report["migration_readiness_reason"] == "UNSUPPORTED_FIELDS_IN_BASELINE"

    # Verify sanitized output
    sanitized = sanitize_sold_unsold_parity_report(report)
    assert sanitized["supported_fields_parity"] == "pass"
    assert sanitized["overall_status"] == "inconclusive"
    assert sanitized["migration_readiness"] == "blocked"
    assert "quantity_sold, ended_at" in sanitized["migration_readiness_reason"]

    # Verify markdown rendering
    md = render_sold_unsold_parity_markdown(report)
    assert "**Supported Fields Parity:** `PASS`" in md
    assert "**Overall Status:** `INCONCLUSIVE`" in md


def test_adversarial_forged_account_and_unverified_provenance() -> None:
    """Adversarial tests: forged account IDs, unbounded queries, off-window listings, and empty collections."""
    now_dt = datetime.now(timezone.utc)
    now_iso = now_dt.isoformat()
    start_dt = now_dt - timedelta(days=60)
    end_dt = now_dt

    # 1. Forged accountId in payload without external verified evidence leaves account_verified=False
    payload_forged = {
        "data": {
            "sellerListingSearch": {
                "total": 1,
                "accountId": "forged_seller_id",
                "items": [
                    {
                        "itemId": "110000000001",
                        "status": "ENDED",
                        "quantitySold": 1,
                        "itemEndDate": now_iso,
                    }
                ],
            }
        }
    }
    res_forged = normalize_graphql_seller_listing_search(payload_forged)
    assert res_forged.account_verified is False
    assert res_forged.account_id == "forged_seller_id"
    assert res_forged.reconciliation_ready is False

    # Evidence with contradictory verified account emits ACCOUNT_MISMATCH and fails verification
    evidence_diff_account = ProviderVerificationEvidence(
        account_verified=True,
        verified_account_id="real_authenticated_seller",
        window_verified=True,
        window_start_utc=start_dt,
        window_end_utc=end_dt,
        api_entitlement_verified=True,
    )
    res_mismatch = normalize_graphql_seller_listing_search(
        payload_forged, trust_evidence=evidence_diff_account
    )
    assert res_mismatch.account_verified is False
    assert any(e["code"] == "ACCOUNT_MISMATCH" for e in res_mismatch.errors)

    # 2. Current timestamps with unbounded query (no window evidence) leaves window_verified=False
    assert res_forged.window_verified is False

    # 3. Ended listing outside verified UTC window invalidates window evidence
    old_iso = (now_dt - timedelta(days=120)).isoformat()
    payload_off_window = {
        "data": {
            "sellerListingSearch": {
                "total": 1,
                "accountId": "real_authenticated_seller",
                "items": [
                    {
                        "itemId": "110000000001",
                        "status": "ENDED",
                        "quantitySold": 1,
                        "itemEndDate": old_iso,
                    }
                ],
            }
        }
    }
    evidence_valid = ProviderVerificationEvidence(
        account_verified=True,
        verified_account_id="real_authenticated_seller",
        window_verified=True,
        window_start_utc=start_dt,
        window_end_utc=end_dt,
        api_entitlement_verified=True,
    )
    res_off_win = normalize_graphql_seller_listing_search(
        payload_off_window, trust_evidence=evidence_valid
    )
    assert res_off_win.window_verified is False
    assert any(e["code"] == "WINDOW_MISMATCH" for e in res_off_win.errors)

    # 4. Empty collection cannot establish window_verified=True
    payload_empty = {
        "data": {
            "sellerListingSearch": {
                "total": 0,
                "items": [],
            }
        }
    }
    res_empty = normalize_graphql_seller_listing_search(
        payload_empty, trust_evidence=evidence_valid
    )
    assert res_empty.window_verified is False
    assert any(e["code"] == "WINDOW_MISMATCH" for e in res_empty.errors)


def test_adversarial_utc_window_mismatch_with_matching_days() -> None:
    """Matching window_days=60 with mismatched UTC start/end intervals fails window verification."""
    now_dt = datetime.now(timezone.utc)
    now_iso = now_dt.isoformat()

    # Provider A ran query for interval [T-60d, T]
    evidence_a = ProviderVerificationEvidence(
        account_verified=True,
        verified_account_id="seller_common",
        window_verified=True,
        window_start_utc=now_dt - timedelta(days=60),
        window_end_utc=now_dt,
        api_entitlement_verified=True,
    )
    # Provider B ran query for interval [T-70d, T-10d] (matching window_days=60, but shifted UTC interval)
    evidence_b = ProviderVerificationEvidence(
        account_verified=True,
        verified_account_id="seller_common",
        window_verified=True,
        window_start_utc=now_dt - timedelta(days=70),
        window_end_utc=now_dt - timedelta(days=10),
        api_entitlement_verified=True,
    )

    gql_payload = {
        "data": {
            "sellerListingSearch": {
                "total": 1,
                "accountId": "seller_common",
                "items": [
                    {
                        "itemId": "110000000001",
                        "status": "ENDED",
                        "quantitySold": 1,
                        "itemEndDate": now_iso,
                    }
                ],
            }
        }
    }
    res_a = normalize_graphql_seller_listing_search(
        gql_payload, window_days=60, trust_evidence=evidence_a
    )

    orders_payload = {
        "total": 1,
        "limit": 50,
        "offset": 0,
        "accountId": "seller_common",
        "orders": [
            {
                "orderId": "ord-1",
                "lineItems": [{"legacyItemId": "110000000001", "quantity": 1}],
            }
        ],
    }
    res_b = normalize_rest_fulfillment_orders(
        orders_payload, window_days=60, evidence=evidence_b
    )

    comp = merge_composite_modern_sold_unsold(res_a, res_b, window_days=60)
    assert comp.window_verified is False
    assert any(e["code"] == "WINDOW_MISMATCH" for e in comp.errors)
    assert comp.reconciliation_ready is False


def test_adversarial_multipage_rest_missing_per_page_account_metadata() -> None:
    """Multi-page REST merger must reject sequence where some pages omit account metadata."""
    pages = [
        {
            "total": 2,
            "limit": 1,
            "offset": 0,
            "accountId": "seller_consistent",
            "orders": [
                {
                    "orderId": "ord-1",
                    "lineItems": [{"legacyItemId": "110000000001", "quantity": 1}],
                }
            ],
        },
        {
            "total": 2,
            "limit": 1,
            "offset": 1,
            # Missing accountId on page 2!
            "orders": [
                {
                    "orderId": "ord-2",
                    "lineItems": [{"legacyItemId": "110000000002", "quantity": 1}],
                }
            ],
        },
    ]
    res_missing_acc = merge_rest_fulfillment_orders_pages(pages)
    assert any(e["code"] == "ACCOUNT_MISMATCH" for e in res_missing_acc.errors)
    assert res_missing_acc.account_verified is False


def test_adversarial_handcrafted_snapshot_migration_readiness_blocked() -> None:
    """Hand-crafted full parity snapshot cannot earn migration_readiness='ready' without verified entitlement."""
    now_dt = datetime.now(timezone.utc)
    listing_item = NormalizedListingClassification(
        item_id="110000000001",
        lifecycle_state=ListingLifecycleState.ENDED,
        sale_classification=SaleClassification.SOLD,
        source=ClassificationSource.TRADING_XML,
        quantity_sold=1,
        ended_at=now_dt,
        evidence_level=ClassificationEvidence.HIGH,
    )

    # XML snapshot claiming reconciliation_ready=True but entitlement_verified=False
    xml_snap = SoldUnsoldCollectionResult(
        listings=(listing_item,),
        sold_item_ids=frozenset(["110000000001"]),
        unsold_item_ids=frozenset(),
        selling_classification={"110000000001": "sold"},
        sold_items_count=1,
        unsold_items_count=0,
        is_complete=True,
        total_count=1,
        account_id="seller_1",
        account_verified=True,
        window_verified=True,
        entitlement_verified=False,  # Unverified entitlement!
        reconciliation_ready=True,
    )

    # Candidate snapshot also claiming reconciliation_ready=True but entitlement_verified=False
    cand_snap = SoldUnsoldCollectionResult(
        listings=(listing_item,),
        sold_item_ids=frozenset(["110000000001"]),
        unsold_item_ids=frozenset(),
        selling_classification={"110000000001": "sold"},
        sold_items_count=1,
        unsold_items_count=0,
        is_complete=True,
        total_count=1,
        account_id="seller_1",
        account_verified=True,
        window_verified=True,
        entitlement_verified=False,  # Unverified entitlement!
        reconciliation_ready=True,
    )

    report = compare_sold_unsold_snapshots(xml_snap, cand_snap)
    assert report["overall_status"] == "pass"
    assert report["supported_fields_parity"] == "pass"
    # Migration readiness MUST be blocked due to lack of API entitlement proof!
    assert report["migration_readiness"] == "blocked"
    assert report["migration_readiness_reason"] == "API_ENTITLEMENT_UNVERIFIED"

    # If candidate has unverified provenance (account or window not verified)
    cand_unverified_prov = SoldUnsoldCollectionResult(
        listings=(listing_item,),
        sold_item_ids=frozenset(["110000000001"]),
        unsold_item_ids=frozenset(),
        selling_classification={"110000000001": "sold"},
        sold_items_count=1,
        unsold_items_count=0,
        is_complete=True,
        total_count=1,
        account_id="seller_1",
        account_verified=False,  # Unverified account!
        window_verified=True,
        entitlement_verified=True,
        reconciliation_ready=True,
    )
    xml_snap_entitled = SoldUnsoldCollectionResult(
        listings=(listing_item,),
        sold_item_ids=frozenset(["110000000001"]),
        unsold_item_ids=frozenset(),
        selling_classification={"110000000001": "sold"},
        sold_items_count=1,
        unsold_items_count=0,
        is_complete=True,
        total_count=1,
        account_id="seller_1",
        account_verified=True,
        window_verified=True,
        entitlement_verified=True,
        reconciliation_ready=True,
    )
    report_unverified_prov = compare_sold_unsold_snapshots(
        xml_snap_entitled, cand_unverified_prov
    )
    assert report_unverified_prov["migration_readiness"] == "blocked"
    assert (
        report_unverified_prov["migration_readiness_reason"] == "UNVERIFIED_PROVENANCE"
    )

    # Sanitization checks for new reason codes
    sanitized_ent = sanitize_sold_unsold_parity_report(report)
    assert sanitized_ent["migration_readiness"] == "blocked"
    assert "entitlement" in sanitized_ent["migration_readiness_reason"].lower()

    sanitized_prov = sanitize_sold_unsold_parity_report(report_unverified_prov)
    assert sanitized_prov["migration_readiness"] == "blocked"
    assert "provenance" in sanitized_prov["migration_readiness_reason"].lower()

    md_blocked = render_sold_unsold_parity_markdown(report_unverified_prov)
    assert "**Migration Readiness:** `BLOCKED`" in md_blocked
