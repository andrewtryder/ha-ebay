"""Comprehensive unit tests for Milestone 6: Best Offers and Negotiation History parity harness."""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import aiohttp
import pytest

import scripts.validate_offers_parity as cli_harness
from custom_components.ebay.api import EbayApiClient
from custom_components.ebay.clients.errors import (
    EbayApiError,
    EbayAuthError,
    EbayAuthTransientError,
    EbayGraphQLError,
)
from custom_components.ebay.migration_policy import (
    FeatureKey,
    MigrationPolicy,
    ProviderType,
    ReadinessEvidence,
)
from custom_components.ebay.parsers.offers_parity import (
    ACTIVE_NEGOTIATION_STATUSES,
    FORBIDDEN_REPORT_KEYS,
    OFFERS_CONTRACT_PROBES,
    PARITY_DIFFERENT_SEMANTICS,
    PARITY_EQUIVALENT_NORMALIZED,
    PARITY_EXACT_MATCH,
    PARITY_MISSING_FIELD,
    PARITY_UNVERIFIED,
    NegotiationRole,
    NegotiationStatus,
    NormalizedNegotiation,
    OfferCollectionResult,
    _compare_single_field,
    _extract_declared_variables,
    _extract_money,
    _parse_iso_datetime,
    build_get_my_ebay_buying_best_offers_xml,
    compare_offers_snapshots,
    infer_and_validate_query_operation,
    is_eligible_items_payload,
    is_inventory_offer_payload,
    merge_graphql_offers_pages,
    merge_trading_xml_buyer_offers_pages,
    merge_trading_xml_seller_offers_pages,
    normalize_graphql_offers_payload,
    normalize_item_id,
    normalize_negotiation_status,
    normalize_trading_xml_buyer_offers,
    normalize_trading_xml_seller_offers,
    parse_query_variables,
    pseudonymize_identifier,
    rank_offers_probe_candidate,
    render_offers_parity_json,
    render_offers_parity_markdown,
    sanitize_diagnostic_string,
    sanitize_diagnostic_token,
    sanitize_graphql_field_name,
    sanitize_graphql_operation_name,
    sanitize_offers_parity_report,
    sanitize_offers_probe_key,
    summarize_offers_probe_exception,
    validate_all_offers_contract_probes,
)
from custom_components.ebay.parsers.trading_items import active_offers_by_item_id


# ---------------------------------------------------------------------------
# Test Phase 1 & 4 & 5: Static Query and Probe Validation
# ---------------------------------------------------------------------------
def test_all_builtin_offers_contract_probes_are_strictly_read_only() -> None:
    """All built-in offers contract probes must pass read-only validation."""
    validate_all_offers_contract_probes()
    assert len(OFFERS_CONTRACT_PROBES) >= 5
    for probe in OFFERS_CONTRACT_PROBES:
        assert "mutation" not in probe.query.lower()
        assert "subscription" not in probe.query.lower()
        op_name = infer_and_validate_query_operation(
            probe.query, operation_name=probe.operation_name
        )
        assert op_name == probe.operation_name


def test_infer_and_validate_query_operation_rejects_mutations() -> None:
    """Mutations, subscriptions, or empty queries must be rejected."""
    with pytest.raises(EbayGraphQLError, match="prohibited|Forbidden"):
        infer_and_validate_query_operation("mutation MakeOffer { makeOffer { id } }")

    with pytest.raises(EbayGraphQLError, match="prohibited|Forbidden"):
        infer_and_validate_query_operation(
            "subscription OnOffer { offerUpdate { id } }"
        )

    with pytest.raises(EbayGraphQLError, match="non-empty|prohibited|Forbidden"):
        infer_and_validate_query_operation("   ")


def test_declared_variables_and_parsing() -> None:
    """Extracting declared variables and parsing them must work safely."""
    query = "query GetOffers($limit: Int!, $offset: Int, $role: String) { test }"
    vars_map = _extract_declared_variables(query)
    assert vars_map == {"limit": "Int!", "offset": "Int", "role": "String"}

    parsed = parse_query_variables(query, '{"offset": 10}')
    assert parsed["offset"] == 10
    # Required variable limit without default gets safe fallback
    assert parsed["limit"] == 50

    with pytest.raises(ValueError, match="JSON"):
        parse_query_variables(query, "not-valid-json")

    with pytest.raises(ValueError, match="JSON object"):
        parse_query_variables(query, '["list", "not", "object"]')


# ---------------------------------------------------------------------------
# Test Phase 3: Status Normalization
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Active", NegotiationStatus.ACTIVE),
        ("ACTIVE", NegotiationStatus.ACTIVE),
        ("open", NegotiationStatus.ACTIVE),
        ("Pending", NegotiationStatus.PENDING),
        ("PENDING", NegotiationStatus.PENDING),
        ("PendingBuyerPayment", NegotiationStatus.PENDING),
        ("PendingBuyerConfirmation", NegotiationStatus.PENDING),
        ("AWAITING_RESPONSE", NegotiationStatus.PENDING),
        ("Countered", NegotiationStatus.COUNTERED),
        ("COUNTERED", NegotiationStatus.COUNTERED),
        ("CustomCode", NegotiationStatus.COUNTERED),
        ("COUNTER_OFFERED", NegotiationStatus.COUNTERED),
        ("Accepted", NegotiationStatus.ACCEPTED),
        ("ACCEPTED", NegotiationStatus.ACCEPTED),
        ("AGREED", NegotiationStatus.ACCEPTED),
        ("OFFER_ACCEPTED", NegotiationStatus.ACCEPTED),
        ("Declined", NegotiationStatus.DECLINED),
        ("DECLINED", NegotiationStatus.DECLINED),
        ("REJECTED", NegotiationStatus.DECLINED),
        ("OFFER_DECLINED", NegotiationStatus.DECLINED),
        ("Expired", NegotiationStatus.EXPIRED),
        ("EXPIRED", NegotiationStatus.EXPIRED),
        ("OFFER_EXPIRED", NegotiationStatus.EXPIRED),
        ("Cancelled", NegotiationStatus.CANCELLED),
        ("CANCELED", NegotiationStatus.CANCELLED),
        ("AdminEnded", NegotiationStatus.CANCELLED),
        ("Retracted", NegotiationStatus.CANCELLED),
        ("OFFER_RETRACTED", NegotiationStatus.CANCELLED),
        ("UnknownStatusString", NegotiationStatus.UNKNOWN),
        (None, NegotiationStatus.UNKNOWN),
        ("", NegotiationStatus.UNKNOWN),
    ],
)
def test_normalize_negotiation_status(raw: Any, expected: NegotiationStatus) -> None:
    """Verify complete mapping of raw XML/GraphQL statuses to canonical status enum."""
    assert normalize_negotiation_status(raw) == expected


def test_active_negotiation_statuses_matches_legacy_counting_rule() -> None:
    """ACTIVE, PENDING, and COUNTERED must be the active statuses."""
    assert ACTIVE_NEGOTIATION_STATUSES == {
        NegotiationStatus.ACTIVE,
        NegotiationStatus.PENDING,
        NegotiationStatus.COUNTERED,
    }


# ---------------------------------------------------------------------------
# Test Money, Date, ID Helpers
# ---------------------------------------------------------------------------
def test_normalize_item_id() -> None:
    """Normalize item ID handles string conversion, stripping, and invalid types."""
    assert normalize_item_id(" 110022334455 ") == "110022334455"
    assert normalize_item_id(110022334455) == "110022334455"
    assert normalize_item_id(None) is None
    assert normalize_item_id("") is None
    assert normalize_item_id(True) is None


def test_extract_money_helper() -> None:
    """Extract money extracts amount and currency from diverse dict shapes."""
    assert _extract_money({"value": "45.50", "currency": "usd"}) == (45.5, "USD")
    assert _extract_money({"amount": 100, "currencyCode": "EUR"}) == (100.0, "EUR")
    assert _extract_money({"price": "12.99", "currency": "GBP"}) == (12.99, "GBP")
    assert _extract_money(None) == (None, None)
    assert _extract_money("not-a-dict") == (None, None)
    assert _extract_money({}) == (None, None)


def test_parse_iso_datetime_helper() -> None:
    """Parsing ISO datetime and eBay datetime to UTC."""
    dt = _parse_iso_datetime("2026-10-30T10:00:00.000Z")
    assert dt is not None
    assert dt.tzinfo == timezone.utc
    assert dt.year == 2026
    assert dt.hour == 10

    # Passing datetime object returns UTC datetime
    now_naive = datetime(2026, 10, 30, 12, 0, 0)
    dt2 = _parse_iso_datetime(now_naive)
    assert dt2 is not None
    assert dt2.tzinfo == timezone.utc

    assert _parse_iso_datetime(None) is None
    assert _parse_iso_datetime("") is None
    assert _parse_iso_datetime("not-a-date") is None


# ---------------------------------------------------------------------------
# Test Phase 1 Contract Distinctions: REST Inventory vs. Negotiation
# ---------------------------------------------------------------------------
def test_is_inventory_offer_payload_detection() -> None:
    """Inventory offer payload with SKU and pricingSummary must be detected."""
    inv_payload = {
        "sku": "ITEM-SKU-123",
        "pricingSummary": {"price": {"value": "29.99", "currency": "USD"}},
        "listingPolicies": {"fulfillmentPolicyId": "123"},
    }
    assert is_inventory_offer_payload(inv_payload) is True

    inv_list_payload = {
        "offers": [
            {
                "offerId": "inv-offer-99",
                "sku": "SKU-99",
                "pricingSummary": {"price": {"value": "10.00", "currency": "USD"}},
            }
        ]
    }
    assert is_inventory_offer_payload(inv_list_payload) is True

    # Real negotiation payload must NOT be flagged as inventory
    neg_payload = {
        "sellerNegotiationHistory": {
            "total": 1,
            "negotiations": [{"negotiationId": "neg-1", "legacyItemId": "123"}],
        }
    }
    assert is_inventory_offer_payload(neg_payload) is False
    assert is_inventory_offer_payload("invalid") is False


def test_is_eligible_items_payload_detection() -> None:
    """REST Sell Negotiation find_eligible_items payload must be detected."""
    eligible_payload = {
        "eligibleItems": [{"listingId": "110000000001"}],
        "eligibleItemCount": 1,
    }
    assert is_eligible_items_payload(eligible_payload) is True

    neg_payload = {"sellerNegotiationHistory": {"total": 1}}
    assert is_eligible_items_payload(neg_payload) is False
    assert is_eligible_items_payload(None) is False


def test_normalize_graphql_offers_rejects_inventory_and_eligible_payloads() -> None:
    """Normalizing inventory offers or eligible items must return fail-closed error."""
    inv_payload = {"sku": "SKU-1", "pricingSummary": {}}
    res = normalize_graphql_offers_payload(inv_payload)
    assert res.is_complete is False
    assert any(e["code"] == "INVENTORY_OFFER_MISMATCH" for e in res.errors)

    elig_payload = {"eligibleItems": []}
    res2 = normalize_graphql_offers_payload(elig_payload)
    assert res2.is_complete is False
    assert any(e["code"] == "ELIGIBLE_ITEMS_MISMATCH" for e in res2.errors)


# ---------------------------------------------------------------------------
# Test Phase 6: Legacy Trading XML Normalization
# ---------------------------------------------------------------------------
def test_normalize_trading_xml_seller_offers_matches_production_active_counts() -> None:
    """Parsed active_offers_by_item_id must exactly match trading_items.py active_offers_by_item_id."""
    xml_str = cli_harness.get_synthetic_seller_xml()
    root = ET.fromstring(xml_str)

    # Reference production parser from trading_items.py
    prod_counts = active_offers_by_item_id(root)

    # Parity normalizer
    norm_res = normalize_trading_xml_seller_offers(xml_str)

    assert norm_res.active_offers_by_item_id == prod_counts
    assert norm_res.total_active_offers == sum(prod_counts.values())
    assert (
        norm_res.total_active_offers == 2
    )  # 1 Active + 1 Countered on 110000000001; 1 Declined on 110000000002
    assert norm_res.is_complete is True
    assert norm_res.is_truncated is False
    assert len(norm_res.negotiations) == 3


def test_normalize_trading_xml_seller_offers_duplicate_ids() -> None:
    """Duplicate offer IDs in XML are flagged fail-closed."""
    xml_str = """<?xml version="1.0" encoding="utf-8"?>
<GetBestOffersResponse xmlns="urn:ebay:apis:eBLBaseComponents">
  <ItemBestOffersArray>
    <ItemBestOffers>
      <Role>Seller</Role>
      <Item><ItemID>110000000001</ItemID></Item>
      <BestOfferArray>
        <BestOffer><BestOfferID>DUP1</BestOfferID><Status>Active</Status></BestOffer>
        <BestOffer><BestOfferID>DUP1</BestOfferID><Status>Active</Status></BestOffer>
      </BestOfferArray>
    </ItemBestOffers>
  </ItemBestOffersArray>
</GetBestOffersResponse>
"""
    res = normalize_trading_xml_seller_offers(xml_str)
    assert res.is_complete is False
    assert any(e["code"] == "DUPLICATE_OFFER_ID" for e in res.errors)


def test_normalize_trading_xml_buyer_offers() -> None:
    """Normalize buyer BestOfferList XML."""
    xml_str = cli_harness.get_synthetic_buyer_xml()
    res = normalize_trading_xml_buyer_offers(xml_str)

    assert res.is_complete is True
    assert res.total_active_offers == 1
    assert len(res.negotiations) == 1
    neg = res.negotiations[0]
    assert neg.negotiation_id == "9002001"
    assert neg.item_id == "220000000001"
    assert neg.role == NegotiationRole.BUYER
    assert neg.status == NegotiationStatus.ACTIVE
    assert neg.price == 65.00
    assert neg.currency == "USD"


def test_build_get_my_ebay_buying_best_offers_xml() -> None:
    """Isolated buyer BestOfferList XML builder produces valid request."""
    xml = build_get_my_ebay_buying_best_offers_xml(entries_per_page=50, page=2)
    assert "<GetMyeBayBuyingRequest" in xml
    assert "<BestOfferList>" in xml
    assert "<EntriesPerPage>50</EntriesPerPage>" in xml
    assert "<PageNumber>2</PageNumber>" in xml


# ---------------------------------------------------------------------------
# Test Phase 4 & 5: GraphQL Offers Normalization
# ---------------------------------------------------------------------------
def test_normalize_graphql_offers_payload_seller() -> None:
    """Normalize synthetic seller GraphQL payload."""
    payload = cli_harness.get_synthetic_seller_graphql()
    res = normalize_graphql_offers_payload(payload, role=NegotiationRole.SELLER)

    assert res.is_complete is True
    assert res.is_truncated is False
    assert len(res.negotiations) == 3
    assert res.total_active_offers == 2
    assert res.active_offers_by_item_id == {"110000000001": 2, "110000000002": 0}

    neg1 = res.negotiations[0]
    assert neg1.negotiation_id == "9001001"
    assert neg1.item_id == "110000000001"
    assert neg1.role == NegotiationRole.SELLER
    assert neg1.status == NegotiationStatus.ACTIVE
    assert neg1.price == 85.00
    assert neg1.currency == "USD"
    assert neg1.quantity == 1

    neg2 = res.negotiations[1]
    assert neg2.status == NegotiationStatus.COUNTERED
    assert neg2.counteroffer_price == 80.00


def test_normalize_graphql_offers_payload_buyer() -> None:
    """Normalize synthetic buyer GraphQL payload."""
    payload = cli_harness.get_synthetic_buyer_graphql()
    res = normalize_graphql_offers_payload(payload, role=NegotiationRole.BUYER)

    assert res.is_complete is True
    assert res.total_active_offers == 1
    assert len(res.negotiations) == 1
    neg = res.negotiations[0]
    assert neg.negotiation_id == "9002001"
    assert neg.role == NegotiationRole.BUYER
    assert neg.status == NegotiationStatus.ACTIVE


def test_normalize_graphql_offers_handles_http200_with_errors() -> None:
    """HTTP 200 with GraphQL errors must be recorded and marked incomplete."""
    payload = {
        "data": {
            "sellerNegotiationHistory": {
                "total": 1,
                "negotiations": [{"negotiationId": "neg-1", "status": "ACTIVE"}],
                "pageInfo": {"hasNextPage": False},
            }
        },
        "errors": [
            {
                "message": "Field 'sensitiveField' unauthorized",
                "extensions": {
                    "code": "FORBIDDEN",
                    "category": "REQUEST",
                    "domain": "SECURITY",
                },
            }
        ],
    }
    res = normalize_graphql_offers_payload(payload)
    assert res.is_complete is False
    assert any(e.get("code") == "FORBIDDEN" for e in res.errors)
    for err in res.errors:
        if err.get("code") == "FORBIDDEN":
            assert err["category"] == "REQUEST"
            assert err["domain"] == "SECURITY"


def test_normalize_graphql_offers_handshake_typename() -> None:
    """Handshake query with __typename succeeds as handshake probe but is not complete collection."""
    payload = {"data": {"__typename": "Query"}}
    res = normalize_graphql_offers_payload(payload)
    assert res.is_complete is False
    assert any(e["code"] == "HANDSHAKE_ONLY" for e in res.errors)
    assert res.diagnostics.get("handshake_typename") == "Query"
    assert res.diagnostics.get("operation_kind") == "handshake_only"


def test_normalize_graphql_offers_malformed_payload() -> None:
    """Non-dict or missing data block returns incomplete result."""
    assert normalize_graphql_offers_payload("not-dict").is_complete is False
    assert normalize_graphql_offers_payload({}).is_complete is False
    assert normalize_graphql_offers_payload({"data": {}}).is_complete is False


# ---------------------------------------------------------------------------
# Test Bounded Multi-Page Merging
# ---------------------------------------------------------------------------
def test_merge_graphql_offers_pages_normal() -> None:
    """Merging clean paged responses combines negotiations and active counts."""
    p1 = {
        "data": {
            "sellerNegotiationHistory": {
                "total": 2,
                "negotiations": [
                    {
                        "negotiationId": "neg-1",
                        "legacyItemId": "item-A",
                        "status": "ACTIVE",
                    }
                ],
                "pageInfo": {"hasNextPage": True},
            }
        }
    }
    p2 = {
        "data": {
            "sellerNegotiationHistory": {
                "total": 2,
                "negotiations": [
                    {
                        "negotiationId": "neg-2",
                        "legacyItemId": "item-B",
                        "status": "COUNTERED",
                    }
                ],
                "pageInfo": {"hasNextPage": False},
            }
        }
    }

    res = merge_graphql_offers_pages([p1, p2])
    assert res.is_complete is True
    assert res.total_count == 2
    assert len(res.negotiations) == 2
    assert res.total_active_offers == 2
    assert res.active_offers_by_item_id == {"item-A": 1, "item-B": 1}


def test_merge_graphql_offers_pages_cycle_detection() -> None:
    """Duplicate payload across pages flags pagination cycle fail-closed."""
    p1 = {
        "data": {
            "sellerNegotiationHistory": {
                "total": 10,
                "negotiations": [{"negotiationId": "neg-1", "legacyItemId": "item-1"}],
                "pageInfo": {"hasNextPage": True},
            }
        }
    }
    # Exactly identical second page
    res = merge_graphql_offers_pages([p1, p1])
    assert res.is_complete is False
    assert res.is_truncated is True
    assert any(e["code"] == "PAGINATION_CYCLE_DETECTED" for e in res.errors)


def test_merge_graphql_offers_pages_contradictory_totals() -> None:
    """Contradictory total counts across pages flags error."""
    p1 = {
        "data": {
            "sellerNegotiationHistory": {
                "total": 5,
                "negotiations": [{"negotiationId": "neg-1"}],
                "pageInfo": {"hasNextPage": True},
            }
        }
    }
    p2 = {
        "data": {
            "sellerNegotiationHistory": {
                "total": 99,
                "negotiations": [{"negotiationId": "neg-2"}],
                "pageInfo": {"hasNextPage": False},
            }
        }
    }
    res = merge_graphql_offers_pages([p1, p2])
    assert res.is_complete is False
    assert any(e["code"] == "CONTRADICTORY_PAGINATION_TOTALS" for e in res.errors)


def test_merge_graphql_offers_pages_after_end_declared() -> None:
    """Pages arriving after hasNextPage=False flags error."""
    p1 = {
        "data": {
            "sellerNegotiationHistory": {
                "total": 1,
                "negotiations": [{"negotiationId": "neg-1"}],
                "pageInfo": {"hasNextPage": False},
            }
        }
    }
    p2 = {
        "data": {
            "sellerNegotiationHistory": {
                "total": 1,
                "negotiations": [{"negotiationId": "neg-2"}],
                "pageInfo": {"hasNextPage": False},
            }
        }
    }
    res = merge_graphql_offers_pages([p1, p2])
    assert res.is_complete is False
    assert any(e["code"] == "PAGES_AFTER_END_DECLARED" for e in res.errors)


def test_merge_graphql_offers_pages_max_limit_exceeded() -> None:
    """Exceeding max_pages truncates and flags error."""
    pages = [
        {
            "data": {
                "sellerNegotiationHistory": {
                    "total": 100,
                    "negotiations": [{"negotiationId": f"neg-{i}"}],
                    "pageInfo": {"hasNextPage": True},
                }
            }
        }
        for i in range(10)
    ]
    res = merge_graphql_offers_pages(pages, max_pages=5)
    assert res.is_truncated is True
    assert res.is_complete is False
    assert any(e["code"] == "MAX_PAGES_EXCEEDED" for e in res.errors)


# ---------------------------------------------------------------------------
# Test Phase 7: Complete Parity Comparison Engine
# ---------------------------------------------------------------------------
def test_compare_offers_snapshots_exact_match() -> None:
    """Matching snapshots produce PASS with exact and normalized match counts."""
    xml_res = normalize_trading_xml_seller_offers(
        cli_harness.get_synthetic_seller_xml()
    )
    gql_res = normalize_graphql_offers_payload(
        cli_harness.get_synthetic_seller_graphql(), role=NegotiationRole.SELLER
    )
    comp = compare_offers_snapshots(xml_res, gql_res, role=NegotiationRole.SELLER)

    assert comp["overall_status"] == "pass"
    assert comp["is_inconclusive"] is False
    counts = comp["field_counts"]
    assert counts[PARITY_EXACT_MATCH] >= 10
    assert counts[PARITY_EQUIVALENT_NORMALIZED] >= 1
    assert counts[PARITY_DIFFERENT_SEMANTICS] == 0
    assert counts[PARITY_MISSING_FIELD] == 0
    assert counts[PARITY_UNVERIFIED] == 0


def test_compare_offers_snapshots_inconclusive_on_errors() -> None:
    """Incomplete snapshots or snapshots with errors fail-closed as inconclusive."""
    xml_res = normalize_trading_xml_seller_offers(
        cli_harness.get_synthetic_seller_xml()
    )
    err_gql = OfferCollectionResult(
        negotiations=(),
        total_count=None,
        active_offers_by_item_id={},
        total_active_offers=0,
        is_complete=False,
        is_truncated=False,
        errors=({"code": "FORBIDDEN"},),
    )

    comp = compare_offers_snapshots(xml_res, err_gql, role=NegotiationRole.SELLER)
    assert comp["overall_status"] == "inconclusive"
    assert comp["is_inconclusive"] is True
    assert all(
        v["status"] == PARITY_UNVERIFIED for v in comp["field_comparisons"].values()
    )


def test_compare_offers_snapshots_field_mismatches() -> None:
    """Price, count, and status differences are accurately categorized."""
    st, _ = _compare_single_field("price", 10.0, 15.0)
    assert st == PARITY_DIFFERENT_SEMANTICS

    st, _ = _compare_single_field("currency", "USD", "EUR")
    assert st == PARITY_DIFFERENT_SEMANTICS

    st, _ = _compare_single_field("status", "Active", "Declined")
    assert st == PARITY_DIFFERENT_SEMANTICS

    st, _ = _compare_single_field("status", "Active", "ACTIVE")
    assert st == PARITY_EQUIVALENT_NORMALIZED

    st, _ = _compare_single_field("price", 10.0, None)
    assert st == PARITY_MISSING_FIELD

    st, _ = _compare_single_field("counteroffer_price", None, 50.0)
    assert st == PARITY_UNVERIFIED


def test_rank_offers_probe_candidate() -> None:
    """Ranking helper scores inconclusive negative and high matches positive."""
    inconclusive_dict = {"overall_status": "inconclusive"}
    assert rank_offers_probe_candidate(inconclusive_dict) == -100

    pass_dict = {
        "overall_status": "pass",
        "field_counts": {
            PARITY_EXACT_MATCH: 10,
            PARITY_EQUIVALENT_NORMALIZED: 2,
            PARITY_DIFFERENT_SEMANTICS: 0,
            PARITY_MISSING_FIELD: 0,
            PARITY_UNVERIFIED: 0,
        },
    }
    assert rank_offers_probe_candidate(pass_dict) == 100 + 16


# ---------------------------------------------------------------------------
# Test Privacy Protections
# ---------------------------------------------------------------------------
def test_sanitize_offers_parity_report_removes_sensitive_keys_and_tokens() -> None:
    """Reports must strip tokens, secrets, buyer IDs, and messages."""
    raw_report = {
        "access_token": "secret_token_123",
        "client_secret": "top_secret_xyz",
        "message": "Please accept my offer!",
        "buyer_username": "buyer_123",
        "nested": {
            "authorization": "Bearer v^1.1#secret_bearer_token",
            "safe_field": "valid",
        },
        "description": "Token leak: Bearer v^1.1#another_token_here",
    }
    sanitized = sanitize_offers_parity_report(raw_report)

    for forbidden in FORBIDDEN_REPORT_KEYS:
        assert forbidden not in sanitized
        assert forbidden not in sanitized.get("nested", {})

    assert "secret_bearer_token" not in str(sanitized)
    assert "another_token_here" not in str(sanitized)
    assert "[REDACTED_TOKEN]" in sanitized["description"]


def test_render_offers_parity_markdown_and_json() -> None:
    """Render markdown and JSON produces valid strings without crashing."""
    comp = compare_offers_snapshots(
        normalize_trading_xml_seller_offers(cli_harness.get_synthetic_seller_xml()),
        normalize_graphql_offers_payload(
            cli_harness.get_synthetic_seller_graphql(), role=NegotiationRole.SELLER
        ),
        role=NegotiationRole.SELLER,
    )
    md = render_offers_parity_markdown(comp)
    assert "# Best Offers & Negotiation History Parity Report" in md
    assert "| `selling_total_offers` | Exact match |" in md

    js = render_offers_parity_json(comp)
    loaded = json.loads(js)
    assert loaded["overall_status"] == "pass"


# ---------------------------------------------------------------------------
# Test Phase 8: CLI Validator Offline Harness
# ---------------------------------------------------------------------------
def test_cli_run_offline_demo_both_roles() -> None:
    """Running offline demo returns passes for both seller and buyer."""
    res = cli_harness.run_offline_demo(role="both")
    assert res["mode"] == "offline_synthetic_demo"
    assert res["reports"]["seller"]["overall_status"] == "pass"
    assert res["reports"]["buyer"]["overall_status"] == "pass"


def test_cli_run_offline_fixtures(tmp_path: Path) -> None:
    """Running offline fixtures loads files and executes comparison."""
    xml_file = tmp_path / "test_seller.xml"
    xml_file.write_text(cli_harness.get_synthetic_seller_xml(), encoding="utf-8")
    gql_file = tmp_path / "test_seller.json"
    gql_file.write_text(
        json.dumps(cli_harness.get_synthetic_seller_graphql()), encoding="utf-8"
    )

    res = cli_harness.run_offline_fixtures(xml_file, gql_file, role="seller")
    assert res["mode"] == "offline_fixtures"
    assert res["reports"]["seller"]["overall_status"] == "pass"


def test_cli_resolve_live_credentials_errors() -> None:
    """Missing credentials raises ValueError with helpful message."""
    with pytest.raises(ValueError, match="Missing required eBay live credentials"):
        cli_harness.resolve_live_credentials(environ={})


def test_cli_main_offline_exit_code() -> None:
    """Running main() with default args runs offline demo and exits 0."""
    with patch("sys.argv", ["validate_offers_parity.py", "--role", "both"]):
        exit_code = cli_harness.main()
        assert exit_code == 0


# ---------------------------------------------------------------------------
# Test Phase 9: Migration Policy Integration for FeatureKey.OFFERS
# ---------------------------------------------------------------------------
def test_migration_policy_offers_baseline_provider_is_trading_xml() -> None:
    """Ground truth baseline for FeatureKey.OFFERS is Trading XML."""
    assert (
        MigrationPolicy.get_default_provider(FeatureKey.OFFERS)
        == ProviderType.TRADING_XML
    )


def test_migration_policy_offers_fails_closed_without_positive_evidence() -> None:
    """Candidate provider for OFFERS fails closed if evidence is missing or unverified."""
    from custom_components.ebay.migration_policy import ReadinessState

    # Default evidence (missing positive flags)
    default_ev = ReadinessEvidence()
    res = MigrationPolicy.evaluate_readiness(
        FeatureKey.OFFERS, ProviderType.GRAPHQL, default_ev
    )
    assert res.is_ready is False
    assert res.state == ReadinessState.BLOCKED_AUTHORIZATION

    # Incomplete evidence (fallback and identity continuity not confirmed)
    incomplete_ev = ReadinessEvidence(
        account_identity_consistent=False,
        fallback_available=False,
        scopes_contract_established=False,
    )
    res_inc = MigrationPolicy.evaluate_readiness(
        FeatureKey.OFFERS, ProviderType.GRAPHQL, incomplete_ev
    )
    assert res_inc.is_ready is False

    # Confirmed positive evidence allows candidate readiness evaluation
    complete_ev = ReadinessEvidence(
        schema_validated=True,
        scopes_contract_established=True,
        required_scopes=("https://api.ebay.com/oauth/api_scope/sell.negotiation",),
        verified_scopes=("https://api.ebay.com/oauth/api_scope/sell.negotiation",),
        api_permission_granted=True,
        response_complete=True,
        pagination_verified=True,
        field_parity_verified=True,
        account_identity_consistent=True,
        fallback_available=True,
    )
    res_ready = MigrationPolicy.evaluate_readiness(
        FeatureKey.OFFERS, ProviderType.GRAPHQL, complete_ev
    )
    assert res_ready.is_ready is True
    assert res_ready.state == ReadinessState.READY


def test_offers_home_assistant_contract_preservation() -> None:
    """Offers feature preserves expected sensor keys and event types."""
    entry_id = "test_entry"
    assert MigrationPolicy.get_expected_unique_ids(entry_id, FeatureKey.OFFERS) == (
        f"{entry_id}_selling_total_offers",
    )
    assert MigrationPolicy.get_expected_event_types(FeatureKey.OFFERS) == (
        "offer_received",
    )


# ---------------------------------------------------------------------------
# Additional Edge Case Coverage for Coverage Threshold
# ---------------------------------------------------------------------------
def test_sanitization_helpers_comprehensive() -> None:
    """Verify all branches of operation, probe key, field name, and diagnostic token sanitizers."""
    # sanitize_graphql_operation_name
    assert sanitize_graphql_operation_name(None) is None
    assert sanitize_graphql_operation_name(True) is None
    assert sanitize_graphql_operation_name("") is None
    assert sanitize_graphql_operation_name("-") is None
    assert sanitize_graphql_operation_name("None") is None
    assert sanitize_graphql_operation_name("Handshake") == "Handshake"
    assert sanitize_graphql_operation_name("[CUSTOM_OPERATION]") == "[CUSTOM_OPERATION]"
    assert sanitize_graphql_operation_name("ArbitraryPrivateOp") == "[CUSTOM_OPERATION]"

    # sanitize_offers_probe_key
    assert sanitize_offers_probe_key(None) == "[CUSTOM_PROBE]"
    assert sanitize_offers_probe_key(False) == "[CUSTOM_PROBE]"
    assert sanitize_offers_probe_key("") == "[CUSTOM_PROBE]"
    assert sanitize_offers_probe_key("-") == "[CUSTOM_PROBE]"
    assert sanitize_offers_probe_key("None") == "[CUSTOM_PROBE]"
    assert sanitize_offers_probe_key("typename_handshake") == "typename_handshake"
    assert sanitize_offers_probe_key("unknown_probe") == "[CUSTOM_PROBE]"

    # sanitize_graphql_field_name
    assert sanitize_graphql_field_name(None) == "[UNKNOWN]"
    assert sanitize_graphql_field_name(True) == "[UNKNOWN]"
    assert sanitize_graphql_field_name("") == "[UNKNOWN]"
    assert (
        sanitize_graphql_field_name("sellerNegotiationHistory", kind="top_level")
        == "sellerNegotiationHistory"
    )
    assert (
        sanitize_graphql_field_name("unknownTop", kind="top_level")
        == "[ALIASED_TOP_LEVEL_FIELD]"
    )
    assert (
        sanitize_graphql_field_name("sellerNegotiations", kind="container")
        == "sellerNegotiations"
    )
    assert (
        sanitize_graphql_field_name("unknownContainer", kind="container")
        == "[ALIASED_CONTAINER]"
    )
    assert sanitize_graphql_field_name("negotiationId") == "negotiationId"
    assert sanitize_graphql_field_name("unknownField") == "[ALIASED_FIELD]"

    # sanitize_diagnostic_token
    assert sanitize_diagnostic_token(None) == "UNKNOWN_CODE"
    assert sanitize_diagnostic_token(True) == "UNKNOWN_CODE"
    assert sanitize_diagnostic_token("") == "UNKNOWN_CODE"
    assert sanitize_diagnostic_token("FORBIDDEN") == "FORBIDDEN"
    assert sanitize_diagnostic_token("1001") == "1001"
    assert sanitize_diagnostic_token("UNKNOWN_RANDOM_CODE") == "UNKNOWN_CODE"
    assert sanitize_diagnostic_token("REQUEST", kind="category") == "REQUEST"
    assert (
        sanitize_diagnostic_token("RANDOM_CAT", kind="category") == "UNKNOWN_CATEGORY"
    )
    assert sanitize_diagnostic_token("SECURITY", kind="domain") == "SECURITY"
    assert sanitize_diagnostic_token("RANDOM_DOM", kind="domain") == "UNKNOWN_DOMAIN"


def test_summarize_offers_probe_exception_all_types() -> None:
    """Verify summarize_offers_probe_exception handles all error classes."""
    # 1. EbayGraphQLError
    gql_err = EbayGraphQLError(
        "GraphQL fail", errors=[{"extensions": {"code": "FORBIDDEN"}}]
    )
    s_gql = summarize_offers_probe_exception(gql_err)
    assert s_gql["error_type"] == "EbayGraphQLError"
    assert s_gql["error_codes"] == ["FORBIDDEN"]
    assert s_gql["is_permission_error"] is True

    # 2. EbayAuthTransientError
    trans_err = EbayAuthTransientError("Token expired")
    s_trans = summarize_offers_probe_exception(trans_err)
    assert s_trans["status"] == "transient_auth_error"
    assert s_trans["is_token_expired"] is True

    # 3. EbayAuthError
    auth_err = EbayAuthError("Unauthorized")
    s_auth = summarize_offers_probe_exception(auth_err)
    assert s_auth["status"] == "auth_error"
    assert s_auth["is_permission_error"] is True

    # 4. EbayApiError (rate limit 429 and server error 500)
    api_429 = EbayApiError("Too Many Requests", status=429)
    s_429 = summarize_offers_probe_exception(api_429)
    assert s_429["is_rate_limit"] is True
    assert s_429["http_status"] == 429

    api_500 = EbayApiError("Server Error", status=500)
    s_500 = summarize_offers_probe_exception(api_500)
    assert s_500["is_rate_limit"] is False
    assert s_500["http_status"] == 500

    # 5. aiohttp.ClientError and TimeoutError
    net_err = aiohttp.ClientError("Connection failed")
    s_net = summarize_offers_probe_exception(net_err)
    assert s_net["status"] == "network_error"

    timeout_err = TimeoutError("Timed out")
    s_timeout = summarize_offers_probe_exception(timeout_err)
    assert s_timeout["status"] == "network_error"

    # 6. Generic Exception
    gen_err = RuntimeError("Unexpected fault")
    s_gen = summarize_offers_probe_exception(gen_err)
    assert s_gen["status"] == "unexpected_error"


def test_xml_offers_parsing_edge_cases() -> None:
    """Verify XML parsing branches for errors, non-seller roles, and missing containers."""
    # XML with Errors block
    err_xml = """<?xml version="1.0" encoding="utf-8"?>
<GetBestOffersResponse xmlns="urn:ebay:apis:eBLBaseComponents">
  <Ack>Failure</Ack>
  <Errors>
    <ErrorCode>1001</ErrorCode>
    <SeverityCode>Error</SeverityCode>
    <ShortMessage>Database temporarily unavailable</ShortMessage>
  </Errors>
</GetBestOffersResponse>
"""
    res_err = normalize_trading_xml_seller_offers(err_xml)
    assert res_err.is_complete is False
    assert any(e["code"] == "1001" for e in res_err.errors)
    assert any(e["code"] == "XML_ACK_FAILURE" for e in res_err.errors)

    # XML with Role="Buyer" or missing ItemID
    skip_xml = """<?xml version="1.0" encoding="utf-8"?>
<GetBestOffersResponse xmlns="urn:ebay:apis:eBLBaseComponents">
  <Ack>Success</Ack>
  <ItemBestOffersArray>
    <ItemBestOffers>
      <Role>Buyer</Role>
      <Item><ItemID>111</ItemID></Item>
      <BestOfferArray><BestOffer><BestOfferID>b1</BestOfferID></BestOffer></BestOfferArray>
    </ItemBestOffers>
    <ItemBestOffers>
      <Role>Seller</Role>
      <Item></Item>
      <BestOfferArray><BestOffer><BestOfferID>b2</BestOfferID></BestOffer></BestOfferArray>
    </ItemBestOffers>
  </ItemBestOffersArray>
</GetBestOffersResponse>
"""
    res_skip = normalize_trading_xml_seller_offers(skip_xml)
    assert len(res_skip.negotiations) == 0

    # Buyer XML with Errors and missing BestOfferList
    buyer_err_xml = """<?xml version="1.0" encoding="utf-8"?>
<GetMyeBayBuyingResponse xmlns="urn:ebay:apis:eBLBaseComponents">
  <Ack>Warning</Ack>
  <Errors>
    <ErrorCode>1002</ErrorCode>
    <SeverityCode>Warning</SeverityCode>
  </Errors>
</GetMyeBayBuyingResponse>
"""
    res_buyer_err = normalize_trading_xml_buyer_offers(buyer_err_xml)
    assert res_buyer_err.is_complete is False
    assert len(res_buyer_err.negotiations) == 0


def test_graphql_normalization_sequence_and_edges() -> None:
    """Verify GraphQL normalization handles sequence container, edges format, and empty snapshots."""
    # Sequence container
    seq_payload = {
        "data": {
            "sellerNegotiationHistory": [
                {
                    "negotiationId": "seq-1",
                    "legacyItemId": "item-seq",
                    "status": "ACTIVE",
                    "currentOffer": {"price": {"value": 25.0, "currency": "USD"}},
                }
            ]
        }
    }
    res_seq = normalize_graphql_offers_payload(seq_payload)
    assert len(res_seq.negotiations) == 1
    assert res_seq.negotiations[0].negotiation_id == "seq-1"

    # Edges format
    edges_payload = {
        "data": {
            "sellerNegotiationHistory": {
                "edges": [
                    {
                        "node": {
                            "negotiationId": "edge-1",
                            "legacyItemId": "item-edge",
                            "status": "ACTIVE",
                            "currentOffer": {
                                "price": {"value": 30.0, "currency": "USD"},
                                "initiatorRole": "BUYER",
                            },
                        }
                    }
                ]
            }
        }
    }
    res_edges = normalize_graphql_offers_payload(edges_payload)
    assert len(res_edges.negotiations) == 1
    assert res_edges.negotiations[0].negotiation_id == "edge-1"
    assert res_edges.negotiations[0].initiator_role == "buyer"

    # Both empty snapshots comparison: fail-closed / inconclusive for record-level fields
    empty_xml = OfferCollectionResult(
        negotiations=(),
        total_count=0,
        active_offers_by_item_id={},
        total_active_offers=0,
        is_complete=True,
        is_truncated=False,
    )
    empty_cand = OfferCollectionResult(
        negotiations=(),
        total_count=0,
        active_offers_by_item_id={},
        total_active_offers=0,
        is_complete=True,
        is_truncated=False,
    )
    comp_empty = compare_offers_snapshots(empty_xml, empty_cand)
    assert comp_empty["overall_status"] == "inconclusive"
    assert comp_empty["is_inconclusive"] is True
    assert comp_empty["field_counts"][PARITY_UNVERIFIED] == 10

    # No matched IDs
    mismatch_cand = OfferCollectionResult(
        negotiations=(
            NormalizedNegotiation(
                negotiation_id="cand-only-id",
                item_id="item-cand",
                role=NegotiationRole.SELLER,
                status=NegotiationStatus.ACTIVE,
                raw_status="ACTIVE",
                price=10.0,
                currency="USD",
                quantity=1,
            ),
        ),
        total_count=1,
        active_offers_by_item_id={"item-cand": 1},
        total_active_offers=1,
        is_complete=True,
        is_truncated=False,
    )
    xml_with_item = OfferCollectionResult(
        negotiations=(
            NormalizedNegotiation(
                negotiation_id="xml-only-id",
                item_id="item-xml",
                role=NegotiationRole.SELLER,
                status=NegotiationStatus.ACTIVE,
                raw_status="Active",
                price=10.0,
                currency="USD",
                quantity=1,
            ),
        ),
        total_count=1,
        active_offers_by_item_id={"item-xml": 1},
        total_active_offers=1,
        is_complete=True,
        is_truncated=False,
    )
    comp_mismatch = compare_offers_snapshots(xml_with_item, mismatch_cand)
    assert comp_mismatch["overall_status"] == "fail"
    assert comp_mismatch["field_counts"][PARITY_MISSING_FIELD] >= 5


def test_cli_harness_dotenv_and_storage_loaders(tmp_path: Path) -> None:
    """Verify .env and HA storage credential loaders in validate_offers_parity."""
    # Dotenv parser
    env_file = tmp_path / "test.env"
    env_file.write_text(
        "# Comment line\n"
        'export EBAY_CLIENT_ID="quoted-client-id"\n'
        "EBAY_CLIENT_SECRET='quoted-secret'\n"
        "EBAY_REFRESH_TOKEN=raw-token\n"
        "EMPTY_LINE=\n"
        "\n",
        encoding="utf-8",
    )
    parsed_env = cli_harness._parse_dotenv_file(env_file)
    assert parsed_env["EBAY_CLIENT_ID"] == "quoted-client-id"
    assert parsed_env["EBAY_CLIENT_SECRET"] == "quoted-secret"
    assert parsed_env["EBAY_REFRESH_TOKEN"] == "raw-token"

    # HA storage credentials loader
    storage_file = tmp_path / "core.config_entries"
    storage_payload = {
        "data": {
            "entries": [
                {
                    "domain": "ebay",
                    "entry_id": "entry_1",
                    "title": "eBay Account",
                    "data": {
                        "client_id": "ha_client_id",
                        "client_secret": "ha_client_secret",
                        "refresh_token": "ha_refresh_token",
                    },
                }
            ]
        }
    }
    storage_file.write_text(json.dumps(storage_payload), encoding="utf-8")
    loaded_creds = cli_harness._load_ha_storage_credentials(storage_file)
    assert loaded_creds["client_id"] == "ha_client_id"
    assert loaded_creds["client_secret"] == "ha_client_secret"
    assert loaded_creds["refresh_token"] == "ha_refresh_token"


@pytest.mark.asyncio
async def test_cli_async_run_live_validation_mocked() -> None:
    """Verify async_run_live_validation orchestration with mocked client."""
    creds = {
        "client_id": "test_id",
        "client_secret": "test_secret",
        "refresh_token": "test_refresh",
        "access_token": "test_access",
    }
    mock_xml_root = ET.fromstring(cli_harness.get_synthetic_seller_xml())
    mock_gql_data = cli_harness.get_synthetic_seller_graphql()

    with (
        patch.object(
            EbayApiClient, "async_call_trading_api", new_callable=AsyncMock
        ) as mock_trading,
        patch.object(
            EbayApiClient, "async_post_graphql", new_callable=AsyncMock
        ) as mock_gql,
    ):
        mock_trading.return_value = mock_xml_root
        mock_gql.return_value = mock_gql_data

        res = await cli_harness.async_run_live_validation(
            creds, role="seller", max_pages=1
        )
        assert res["mode"] == "live_validation"
        assert res["reports"]["seller"]["overall_status"] == "pass"


# ---------------------------------------------------------------------------
# Test PR #100 Hardening: Pagination, Census, XML Failure, and Privacy
# ---------------------------------------------------------------------------
def test_normalize_graphql_offers_count_only_fails_closed() -> None:
    """Count-only GraphQL query (total without negotiations) fails closed."""
    payload = {
        "data": {
            "sellerNegotiationHistory": {
                "total": 5,
                "pageInfo": {"hasNextPage": False},
            }
        }
    }
    res = normalize_graphql_offers_payload(payload)
    assert res.is_complete is False
    assert any(e["code"] == "COUNT_ONLY_QUERY" for e in res.errors)


def test_normalize_graphql_offers_missing_page_info_fails_closed() -> None:
    """Missing pageInfo metadata cannot be treated as evidence of completion."""
    payload = {
        "data": {
            "sellerNegotiationHistory": {
                "total": 1,
                "negotiations": [{"negotiationId": "neg-1", "status": "ACTIVE"}],
            }
        }
    }
    res = normalize_graphql_offers_payload(payload)
    assert res.is_complete is False
    assert any(e["code"] == "MISSING_PAGINATION_METADATA" for e in res.errors)


def test_normalize_graphql_offers_malformed_page_info_fails_closed() -> None:
    """Non-boolean hasNextPage fails closed as malformed page info."""
    payload = {
        "data": {
            "sellerNegotiationHistory": {
                "total": 1,
                "negotiations": [{"negotiationId": "neg-1", "status": "ACTIVE"}],
                "pageInfo": {"hasNextPage": "true"},  # String, not boolean
            }
        }
    }
    res = normalize_graphql_offers_payload(payload)
    assert res.is_complete is False
    assert res.is_truncated is True
    assert any(e["code"] == "MALFORMED_PAGE_INFO" for e in res.errors)


def test_normalize_graphql_offers_missing_negotiation_id_fails_closed() -> None:
    """Negotiation item missing ID fails closed."""
    payload = {
        "data": {
            "sellerNegotiationHistory": {
                "total": 1,
                "negotiations": [{"status": "ACTIVE", "price": {"value": 10.0}}],
                "pageInfo": {"hasNextPage": False},
            }
        }
    }
    res = normalize_graphql_offers_payload(payload)
    assert res.is_complete is False
    assert any(e["code"] == "MISSING_NEGOTIATION_ID" for e in res.errors)


def test_normalize_graphql_offers_role_mismatch_fails_closed() -> None:
    """Negotiation with conflicting role fails closed."""
    payload = {
        "data": {
            "sellerNegotiationHistory": {
                "total": 1,
                "negotiations": [
                    {
                        "negotiationId": "neg-1",
                        "role": "BUYER",  # conflicting with default role SELLER
                        "status": "ACTIVE",
                    }
                ],
                "pageInfo": {"hasNextPage": False},
            }
        }
    }
    res = normalize_graphql_offers_payload(payload, role=NegotiationRole.SELLER)
    assert res.is_complete is False
    assert any(e["code"] == "ROLE_MISMATCH" for e in res.errors)


def test_normalize_graphql_offers_total_records_mismatch_fails_closed() -> None:
    """Declared total differing from collected records fails closed."""
    payload = {
        "data": {
            "sellerNegotiationHistory": {
                "total": 5,
                "negotiations": [{"negotiationId": "neg-1", "status": "ACTIVE"}],
                "pageInfo": {"hasNextPage": False},
            }
        }
    }
    res = normalize_graphql_offers_payload(payload)
    assert res.is_complete is False
    assert any(e["code"] == "TOTAL_RECORDS_MISMATCH" for e in res.errors)


def test_merge_graphql_offers_pages_empty_fails_closed() -> None:
    """Empty list of pages is not evidence of a verified empty collection."""
    res = merge_graphql_offers_pages([])
    assert res.is_complete is False
    assert any(e["code"] == "NO_PAGES_PROVIDED" for e in res.errors)


def test_merge_graphql_offers_pages_pending_continuation_fails_closed() -> None:
    """Final page with hasNextPage=True never produces is_complete=True."""
    p1 = {
        "data": {
            "sellerNegotiationHistory": {
                "total": 2,
                "negotiations": [{"negotiationId": "neg-1"}],
                "pageInfo": {"hasNextPage": True},
            }
        }
    }
    res = merge_graphql_offers_pages([p1])
    assert res.is_complete is False
    assert res.is_truncated is True
    assert any(e["code"] == "PENDING_CONTINUATION" for e in res.errors)


def test_merge_graphql_offers_pages_duplicate_id_across_pages() -> None:
    """Duplicate negotiation ID across pages fails closed."""
    p1 = {
        "data": {
            "sellerNegotiationHistory": {
                "total": 2,
                "negotiations": [{"negotiationId": "neg-repeat"}],
                "pageInfo": {"hasNextPage": True},
            }
        }
    }
    p2 = {
        "data": {
            "sellerNegotiationHistory": {
                "total": 2,
                "negotiations": [{"negotiationId": "neg-repeat"}],
                "pageInfo": {"hasNextPage": False},
            }
        }
    }
    res = merge_graphql_offers_pages([p1, p2])
    assert res.is_complete is False
    assert any(e["code"] == "DUPLICATE_OFFER_ID_ACROSS_PAGES" for e in res.errors)


def test_merge_graphql_offers_pages_unverified_census_when_total_none() -> None:
    """Pages lacking declared total count cannot be verified complete."""
    p1 = {
        "data": {
            "sellerNegotiationHistory": {
                "negotiations": [{"negotiationId": "neg-1"}],
                "pageInfo": {"hasNextPage": False},
            }
        }
    }
    res = merge_graphql_offers_pages([p1])
    assert res.is_complete is False
    assert any(e["code"] == "UNVERIFIED_COLLECTION_CENSUS" for e in res.errors)


def test_trading_xml_seller_offers_multi_page_merge() -> None:
    """Multi-page seller Trading XML responses merge into a complete collection."""
    xml_p1 = """<?xml version="1.0" encoding="utf-8"?>
<GetBestOffersResponse xmlns="urn:ebay:apis:eBLBaseComponents">
  <Ack>Success</Ack>
  <ItemBestOffersArray>
    <ItemBestOffers>
      <Role>Seller</Role>
      <Item><ItemID>item-1</ItemID></Item>
      <BestOfferArray>
        <BestOffer><BestOfferID>off-1</BestOfferID><Status>Active</Status></BestOffer>
      </BestOfferArray>
    </ItemBestOffers>
  </ItemBestOffersArray>
  <PaginationResult>
    <TotalNumberOfPages>2</TotalNumberOfPages>
    <TotalNumberOfEntries>2</TotalNumberOfEntries>
  </PaginationResult>
</GetBestOffersResponse>
"""
    xml_p2 = """<?xml version="1.0" encoding="utf-8"?>
<GetBestOffersResponse xmlns="urn:ebay:apis:eBLBaseComponents">
  <Ack>Success</Ack>
  <ItemBestOffersArray>
    <ItemBestOffers>
      <Role>Seller</Role>
      <Item><ItemID>item-2</ItemID></Item>
      <BestOfferArray>
        <BestOffer><BestOfferID>off-2</BestOfferID><Status>Active</Status></BestOffer>
      </BestOfferArray>
    </ItemBestOffers>
  </ItemBestOffersArray>
  <PaginationResult>
    <TotalNumberOfPages>2</TotalNumberOfPages>
    <TotalNumberOfEntries>2</TotalNumberOfEntries>
  </PaginationResult>
</GetBestOffersResponse>
"""
    res = merge_trading_xml_seller_offers_pages([xml_p1, xml_p2])
    assert res.is_complete is True
    assert len(res.negotiations) == 2
    assert res.total_active_offers == 2
    assert res.active_offers_by_item_id == {"item-1": 1, "item-2": 1}


def test_trading_xml_seller_offers_multi_page_truncated() -> None:
    """Seller Trading XML with missing pages is flagged truncated and incomplete."""
    xml_p1 = """<?xml version="1.0" encoding="utf-8"?>
<GetBestOffersResponse xmlns="urn:ebay:apis:eBLBaseComponents">
  <Ack>Success</Ack>
  <ItemBestOffersArray>
    <ItemBestOffers>
      <Role>Seller</Role>
      <Item><ItemID>item-1</ItemID></Item>
      <BestOfferArray>
        <BestOffer><BestOfferID>off-1</BestOfferID><Status>Active</Status></BestOffer>
      </BestOfferArray>
    </ItemBestOffers>
  </ItemBestOffersArray>
  <PaginationResult>
    <TotalNumberOfPages>2</TotalNumberOfPages>
    <TotalNumberOfEntries>2</TotalNumberOfEntries>
  </PaginationResult>
</GetBestOffersResponse>
"""
    res = merge_trading_xml_seller_offers_pages([xml_p1])
    assert res.is_complete is False
    assert res.is_truncated is True
    assert any(e["code"] == "PENDING_CONTINUATION" for e in res.errors)


def test_trading_xml_seller_offers_multi_page_duplicate_id() -> None:
    """Duplicate offer ID across XML pages fails closed."""
    xml_p1 = """<?xml version="1.0" encoding="utf-8"?>
<GetBestOffersResponse xmlns="urn:ebay:apis:eBLBaseComponents">
  <Ack>Success</Ack>
  <ItemBestOffersArray>
    <ItemBestOffers>
      <Role>Seller</Role>
      <Item><ItemID>item-1</ItemID></Item>
      <BestOfferArray>
        <BestOffer><BestOfferID>off-dup</BestOfferID><Status>Active</Status></BestOffer>
      </BestOfferArray>
    </ItemBestOffers>
  </ItemBestOffersArray>
  <PaginationResult>
    <TotalNumberOfPages>2</TotalNumberOfPages>
    <TotalNumberOfEntries>2</TotalNumberOfEntries>
  </PaginationResult>
</GetBestOffersResponse>
"""
    xml_p2 = """<?xml version="1.0" encoding="utf-8"?>
<GetBestOffersResponse xmlns="urn:ebay:apis:eBLBaseComponents">
  <Ack>Success</Ack>
  <ItemBestOffersArray>
    <ItemBestOffers>
      <Role>Seller</Role>
      <Item><ItemID>item-2</ItemID></Item>
      <BestOfferArray>
        <BestOffer><BestOfferID>off-dup</BestOfferID><Status>Active</Status></BestOffer>
      </BestOfferArray>
    </ItemBestOffers>
  </ItemBestOffersArray>
  <PaginationResult>
    <TotalNumberOfPages>2</TotalNumberOfPages>
    <TotalNumberOfEntries>2</TotalNumberOfEntries>
  </PaginationResult>
</GetBestOffersResponse>
"""
    res = merge_trading_xml_seller_offers_pages([xml_p1, xml_p2])
    assert res.is_complete is False
    assert any(e["code"] == "DUPLICATE_OFFER_ID_ACROSS_PAGES" for e in res.errors)


def test_trading_xml_buyer_offers_multi_page_merge() -> None:
    """Multi-page buyer Trading XML responses merge into a complete collection."""
    xml_b1 = """<?xml version="1.0" encoding="utf-8"?>
<GetMyeBayBuyingResponse xmlns="urn:ebay:apis:eBLBaseComponents">
  <Ack>Success</Ack>
  <BestOfferList>
    <ItemArray>
      <Item>
        <ItemID>item-b1</ItemID>
        <BestOfferDetails><BestOffer><BestOfferID>off-b1</BestOfferID><Status>Active</Status></BestOffer></BestOfferDetails>
      </Item>
    </ItemArray>
    <PaginationResult>
      <TotalNumberOfPages>2</TotalNumberOfPages>
      <TotalNumberOfEntries>2</TotalNumberOfEntries>
    </PaginationResult>
  </BestOfferList>
</GetMyeBayBuyingResponse>
"""
    xml_b2 = """<?xml version="1.0" encoding="utf-8"?>
<GetMyeBayBuyingResponse xmlns="urn:ebay:apis:eBLBaseComponents">
  <Ack>Success</Ack>
  <BestOfferList>
    <ItemArray>
      <Item>
        <ItemID>item-b2</ItemID>
        <BestOfferDetails><BestOffer><BestOfferID>off-b2</BestOfferID><Status>Active</Status></BestOffer></BestOfferDetails>
      </Item>
    </ItemArray>
    <PaginationResult>
      <TotalNumberOfPages>2</TotalNumberOfPages>
      <TotalNumberOfEntries>2</TotalNumberOfEntries>
    </PaginationResult>
  </BestOfferList>
</GetMyeBayBuyingResponse>
"""
    res = merge_trading_xml_buyer_offers_pages([xml_b1, xml_b2])
    assert res.is_complete is True
    assert len(res.negotiations) == 2


def test_trading_xml_normalization_failures() -> None:
    """Verify XML failures: invalid root, Ack=Failure, Ack=PartialFailure, and empty string."""
    assert normalize_trading_xml_seller_offers("").is_complete is False
    assert normalize_trading_xml_seller_offers("<InvalidRoot/>").is_complete is False
    assert normalize_trading_xml_seller_offers("<NotClosed").is_complete is False

    fail_xml = """<?xml version="1.0" encoding="utf-8"?>
<GetBestOffersResponse xmlns="urn:ebay:apis:eBLBaseComponents">
  <Ack>Failure</Ack>
  <Errors><ErrorCode>931</ErrorCode></Errors>
</GetBestOffersResponse>
"""
    res_fail = normalize_trading_xml_seller_offers(fail_xml)
    assert res_fail.is_complete is False
    assert any(e["code"] == "XML_ACK_FAILURE" for e in res_fail.errors)

    partial_xml = """<?xml version="1.0" encoding="utf-8"?>
<GetBestOffersResponse xmlns="urn:ebay:apis:eBLBaseComponents">
  <Ack>PartialFailure</Ack>
</GetBestOffersResponse>
"""
    res_part = normalize_trading_xml_seller_offers(partial_xml)
    assert res_part.is_complete is False
    assert any(e["code"] == "XML_ACK_PARTIAL_FAILURE" for e in res_part.errors)


def test_compare_offers_snapshots_equal_counts_different_ids() -> None:
    """Matching counts but completely different IDs fails parity."""
    xml_snap = OfferCollectionResult(
        negotiations=(
            NormalizedNegotiation(
                negotiation_id="id-100",
                item_id="item-A",
                role=NegotiationRole.SELLER,
                status=NegotiationStatus.ACTIVE,
                price=10.0,
            ),
        ),
        total_count=1,
        active_offers_by_item_id={"item-A": 1},
        total_active_offers=1,
        is_complete=True,
    )
    cand_snap = OfferCollectionResult(
        negotiations=(
            NormalizedNegotiation(
                negotiation_id="id-200",  # Different ID!
                item_id="item-A",
                role=NegotiationRole.SELLER,
                status=NegotiationStatus.ACTIVE,
                price=10.0,
            ),
        ),
        total_count=1,
        active_offers_by_item_id={"item-A": 1},
        total_active_offers=1,
        is_complete=True,
    )
    comp = compare_offers_snapshots(xml_snap, cand_snap)
    assert comp["overall_status"] == "fail"
    assert (
        comp["field_comparisons"]["negotiation_id"]["status"]
        == PARITY_DIFFERENT_SEMANTICS
    )


def test_compare_offers_snapshots_extra_and_missing_records() -> None:
    """Extra candidate records or missing records cause parity failure."""
    neg1 = NormalizedNegotiation(
        negotiation_id="id-1",
        item_id="item-1",
        role=NegotiationRole.SELLER,
        status=NegotiationStatus.ACTIVE,
    )
    neg2 = NormalizedNegotiation(
        negotiation_id="id-2",
        item_id="item-2",
        role=NegotiationRole.SELLER,
        status=NegotiationStatus.ACTIVE,
    )

    xml_snap = OfferCollectionResult(
        negotiations=(neg1,),
        total_count=1,
        active_offers_by_item_id={"item-1": 1},
        total_active_offers=1,
        is_complete=True,
    )
    cand_extra = OfferCollectionResult(
        negotiations=(neg1, neg2),
        total_count=2,
        active_offers_by_item_id={"item-1": 1, "item-2": 1},
        total_active_offers=2,
        is_complete=True,
    )

    comp = compare_offers_snapshots(xml_snap, cand_extra)
    assert comp["overall_status"] == "fail"
    assert "extra in candidate" in comp["field_comparisons"]["negotiation_id"]["note"]


def test_compare_offers_snapshots_role_conflicts() -> None:
    """Role mismatch between XML and candidate records flags different semantics."""
    neg_xml = NormalizedNegotiation(
        negotiation_id="id-1",
        item_id="item-1",
        role=NegotiationRole.SELLER,
        status=NegotiationStatus.ACTIVE,
    )
    neg_cand = NormalizedNegotiation(
        negotiation_id="id-1",
        item_id="item-1",
        role=NegotiationRole.BUYER,
        status=NegotiationStatus.ACTIVE,
    )

    xml_snap = OfferCollectionResult(
        negotiations=(neg_xml,),
        total_count=1,
        active_offers_by_item_id={"item-1": 1},
        total_active_offers=1,
        is_complete=True,
    )
    cand_snap = OfferCollectionResult(
        negotiations=(neg_cand,),
        total_count=1,
        active_offers_by_item_id={"item-1": 1},
        total_active_offers=1,
        is_complete=True,
    )

    comp = compare_offers_snapshots(xml_snap, cand_snap)
    assert comp["overall_status"] == "fail"
    assert comp["field_comparisons"]["role"]["status"] == PARITY_DIFFERENT_SEMANTICS


def test_privacy_adversarial_tokens_credentials_and_emails_scrubbed() -> None:
    """Report sanitization scrubs Bearer tokens, refresh tokens, emails, secrets, and raw item IDs."""
    dirty_report = {
        "role": "seller",
        "overall_status": "pass",
        "active_offers_by_item_id": {
            "110000000001": 2,
            "110000000002": 1,
        },
        "field_comparisons": {
            "selling_total_offers": {
                "status": "exact_match",
                "note": "Token Bearer v^1.1#secret-raw-token leaked!",
            },
            "status": {
                "status": "exact_match",
                "note": "Contact user@example.com for refresh_token=secret_refresh_val",
            },
        },
        "errors": [
            {
                "code": "FORBIDDEN",
                "diagnostic": "Failed with client_secret=very_secret_key and access_token=Bearer tok123",
            }
        ],
        "access_token": "Bearer should-be-dropped",
        "refresh_token": "should-be-dropped",
        "client_secret": "should-be-dropped",
    }

    sanitized = sanitize_offers_parity_report(dirty_report)

    # 1. Forbidden keys must be dropped
    assert "access_token" not in sanitized
    assert "refresh_token" not in sanitized
    assert "client_secret" not in sanitized

    # 2. Raw item IDs must be pseudonymized
    for key in sanitized["active_offers_by_item_id"]:
        assert key.startswith("item_")
        assert "110000000001" not in key
        assert "110000000002" not in key

    # 3. String notes and diagnostics must have tokens and secrets redacted
    md_text = render_offers_parity_markdown(dirty_report)
    json_text = render_offers_parity_json(dirty_report)

    for text in (md_text, json_text):
        assert "v^1.1#secret-raw-token" not in text
        assert "user@example.com" not in text
        assert "secret_refresh_val" not in text
        assert "very_secret_key" not in text
        assert "should-be-dropped" not in text
        assert "[REDACTED_TOKEN]" in text
        assert "[REDACTED_EMAIL]" in text


def test_pseudonymize_identifier_and_sanitize_diagnostic_string() -> None:
    """Direct tests for deterministic pseudonymization and diagnostic string scrubbing."""
    assert pseudonymize_identifier("123456789012").startswith("item_")
    assert pseudonymize_identifier("123456789012") == pseudonymize_identifier(
        "123456789012"
    )
    assert pseudonymize_identifier("item-1") != pseudonymize_identifier("item-2")
    assert pseudonymize_identifier(None) is None
    assert pseudonymize_identifier(12345) == "12345"

    dirty_str = "Auth Bearer v^1.1#tok email dev@example.com refresh_token=secret_val"
    cleaned = sanitize_diagnostic_string(dirty_str)
    assert "v^1.1#tok" not in cleaned
    assert "dev@example.com" not in cleaned
    assert "secret_val" not in cleaned
    assert "[REDACTED_TOKEN]" in cleaned
    assert "[REDACTED_EMAIL]" in cleaned
