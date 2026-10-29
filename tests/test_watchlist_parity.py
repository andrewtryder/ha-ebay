"""Unit tests for the Milestone 1 Watchlist XML vs. GraphQL parity harness."""

from __future__ import annotations

import asyncio
import json
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest

import scripts.validate_watchlist_parity as cli_harness
from custom_components.ebay.clients.errors import (
    EbayGraphQLError,
)
from custom_components.ebay.parsers.watchlist_parity import (
    MONITORED_WATCHLIST_FIELDS,
    PARITY_DIFFERENT_SEMANTICS,
    PARITY_EQUIVALENT_NORMALIZED,
    PARITY_EXACT_MATCH,
    PARITY_MISSING_FIELD,
    PARITY_UNVERIFIED,
    WATCHLIST_CONTRACT_PROBES,
    compare_watchlist_snapshots,
    format_parity_report_markdown,
    normalize_graphql_watchlist_payload,
    normalize_listing_type_category,
    normalize_watchlist_item_id,
    parity_status,
    sanitize_parity_report,
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
    item = normalized["items_by_id"]["999888777666"]
    assert item["current_price"] == 25.0
    assert item["price_was_string"] is False
    assert item["currency"] == "GBP"
    assert item["listing_type_category"] == "fixed_price"
    assert item["bid_count"] == 0

    # 2. edges[].node shape with duplicate composite IDs
    edges_payload = {
        "userWatchList": {
            "totalCount": 2,
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
    assert len(norm_edges["items_by_id"]) == 1

    # 3. Count-only shape
    count_only = normalize_graphql_watchlist_payload({"userWatchList": {"total": 5}})
    assert count_only["reported_total"] == 5
    assert count_only["items_list_present"] is False
    assert count_only["items_by_id"] == {}

    # 4. Invalid payloads
    with pytest.raises(TypeError, match="must be a mapping"):
        normalize_graphql_watchlist_payload(None)
    with pytest.raises(ValueError, match="missing 'userWatchList'"):
        normalize_graphql_watchlist_payload({})


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

    # 2. Mismatched values -> different_semantics
    mismatched_gql = {
        "userWatchList": {
            "total": 2,
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
                },
                {
                    "itemId": "222",
                    "title": "Extra Item",
                },
            ],
        }
    }
    diff_report = compare_watchlist_snapshots(xml_watched, mismatched_gql)
    fields = diff_report["fields"]
    assert fields["watched_count"]["classification"] == PARITY_DIFFERENT_SEMANTICS
    assert fields["item_id"]["classification"] == PARITY_EXACT_MATCH
    assert fields["title"]["classification"] == PARITY_DIFFERENT_SEMANTICS
    assert fields["current_price"]["classification"] == PARITY_DIFFERENT_SEMANTICS
    assert fields["currency"]["classification"] == PARITY_DIFFERENT_SEMANTICS
    assert fields["listing_type"]["classification"] == PARITY_DIFFERENT_SEMANTICS
    assert fields["end_time"]["classification"] == PARITY_DIFFERENT_SEMANTICS
    assert fields["url"]["classification"] == PARITY_DIFFERENT_SEMANTICS
    assert fields["image"]["classification"] == PARITY_DIFFERENT_SEMANTICS
    assert fields["bid_count"]["classification"] == PARITY_DIFFERENT_SEMANTICS


def test_sanitize_parity_report_redacts_secrets_and_private_values() -> None:
    """sanitize_parity_report strips forbidden keys, Bearer/v^1.1 tokens, and private strings."""
    raw = {
        "mode": "live_authenticated",
        "access_token": "v^1.1#i^1#p^3#secret-token",
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
    """Credentials resolve from .env, environ, or HA core.config_entries without mutation."""
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

    # HA storage file (read-only)
    storage_file = tmp_path / "core.config_entries"
    original_storage = json.dumps(
        {
            "data": {
                "entries": [
                    {"domain": "other", "data": {}},
                    {
                        "domain": "ebay",
                        "data": {
                            "environment": "production",
                            "client_id": "ha-client",
                            "client_secret": "ha-secret",
                            "runame": "ha-runame",
                            "site_id": "77",
                            "oauth_scopes": "https://api.ebay.com/oauth/api_scope",
                            "token": {"refresh_token": "ha-refresh"},
                        },
                    },
                ]
            }
        }
    )
    storage_file.write_text(original_storage, encoding="utf-8")
    ha_creds = cli_harness.resolve_live_credentials(ha_config_entries=storage_file)
    assert ha_creds["client_id"] == "ha-client"
    assert ha_creds["refresh_token"] == "ha-refresh"
    assert ha_creds["site_id"] == "77"
    assert storage_file.read_text(encoding="utf-8") == original_storage

    with pytest.raises(ValueError, match="Missing required eBay live credentials"):
        cli_harness.resolve_live_credentials(
            env_file=tmp_path / "missing.env", environ={}
        )


def test_run_live_watchlist_validation_with_mocked_client() -> None:
    """Live validation harness runs read-only XML and GraphQL probes and sanitizes errors."""
    xml_demo, gql_demo = cli_harness._synthetic_demo_fixtures()
    root = ET.fromstring(xml_demo)
    gql_data = json.loads(gql_demo)["data"]

    client = Mock()
    client.environment = "production"
    client.site_id = "0"
    client._client_id = "secret-client-id"
    client._client_secret = "secret-client-secret"
    client._runame = "secret-runame"
    client._refresh_token = "secret-refresh-token"
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
                "Field watchListCount undefined",
                errors=[
                    {
                        "message": "Field watchListCount undefined",
                        "extensions": {
                            "code": "VALIDATION_ERROR",
                            "category": "REQUEST",
                            "domain": "BUY_WATCHLIST",
                        },
                    }
                ],
            )
        return gql_data

    client.async_post_graphql = AsyncMock(side_effect=_side_effect)

    report = asyncio.run(cli_harness.run_live_watchlist_validation(client))
    serialized = json.dumps(report)

    assert report["mode"] == "live_authenticated"
    assert report["status"] == "compared"
    assert report["common_items_count"] == 2
    assert any(p["outcome"] == "success" for p in report["probes"])
    assert any(p["outcome"] == "error" for p in report["probes"])
    assert "secret-client-id" not in serialized
    assert "secret-client-secret" not in serialized
    assert "secret-refresh-token" not in serialized
    assert "secret-access-token" not in serialized
    assert "Synthetic Vintage Camera Lens" not in serialized
    assert "111222333444" not in serialized

    md = format_parity_report_markdown(report)
    assert "## Contract Probes" in md
    assert "## Field-Level Parity Matrix" in md
