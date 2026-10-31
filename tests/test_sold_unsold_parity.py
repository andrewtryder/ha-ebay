"""Comprehensive unit tests for Milestone 7: Sold & Unsold Listing Classification Modernization."""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
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
    PARITY_UNVERIFIED,
    SOLD_UNSOLD_CONTRACT_PROBES,
    ClassificationEvidence,
    ClassificationSource,
    ListingLifecycleState,
    NormalizedListingClassification,
    SaleClassification,
    SoldUnsoldCollectionResult,
    _is_safe_item_pseudonym,
    _parse_iso_datetime,
    _resolve_safe_field_comparison_note,
    compare_sold_unsold_snapshots,
    merge_composite_modern_sold_unsold,
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
    """Live validation helper fails closed if unauthenticated."""
    res = await cli_harness.async_run_live_validation(None, max_pages=10)
    assert res["overall_status"] == "inconclusive"
    assert any(e["code"] == "UNAUTHENTICATED" for e in res["errors"])


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
