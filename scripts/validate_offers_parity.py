#!/usr/bin/env python3
"""Read-only eBay Best Offers & Negotiation History XML vs. GraphQL contract and parity validation harness.

By default, this script makes **zero network calls** unless ``--live`` is
explicitly passed. It never logs, prints, or persists OAuth tokens, client
secrets, private item IDs, offer prices, buyer usernames, or raw account responses.

Examples:
  # Run offline synthetic parity verification (no network, no credentials):
  uv run python scripts/validate_offers_parity.py

  # Run offline synthetic verification for buyer negotiations specifically:
  uv run python scripts/validate_offers_parity.py --role buyer

  # Compare local XML and GraphQL response fixtures offline:
  uv run python scripts/validate_offers_parity.py \
    --xml-fixture path/to/offers.xml --graphql-fixture path/to/negotiations.json

  # Run live read-only validation against eBay using a local git-ignored .env file:
  uv run python scripts/validate_offers_parity.py --live --env-file .env.local \
    --role seller --output offers_parity.local.md
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import aiohttp

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from custom_components.ebay.api import EbayApiClient  # noqa: E402
from custom_components.ebay.clients.errors import EbayError  # noqa: E402
from custom_components.ebay.const import NS  # noqa: E402
from custom_components.ebay.parsers.offers_parity import (  # noqa: E402
    OFFERS_CONTRACT_PROBES,
    NegotiationRole,
    OfferCollectionResult,
    _find_offers_container,
    build_get_my_ebay_buying_best_offers_xml,
    compare_offers_snapshots,
    merge_graphql_offers_pages,
    merge_trading_xml_buyer_offers_pages,
    merge_trading_xml_seller_offers_pages,
    normalize_graphql_offers_payload,
    normalize_trading_xml_buyer_offers,
    normalize_trading_xml_seller_offers,
    rank_offers_probe_candidate,
    render_offers_parity_json,
    render_offers_parity_markdown,
    sanitize_graphql_operation_name,
    sanitize_offers_parity_report,
    sanitize_offers_probe_key,
    summarize_offers_probe_exception,
    validate_max_pages,
)
from custom_components.ebay.parsers.trading_builders import (  # noqa: E402
    BEST_OFFERS_CALL_NAME,
    BEST_OFFERS_MAX_PAGES,
    READ_ONLY_CALL_NAME,
    build_get_best_offers_xml,
)


def _parse_dotenv_file(path: Path) -> dict[str, str]:
    """Parse key=value pairs from a local .env file without executing shell code."""
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in {'"', "'"}:
            val = val[1:-1]
        if key:
            values[key] = val
    return values


def _load_ha_storage_credentials(
    storage_path: Path,
    *,
    entry_id: str | None = None,
) -> dict[str, str]:
    """Read eBay entry credentials from a Home Assistant core.config_entries file (read-only)."""
    payload = json.loads(storage_path.read_text(encoding="utf-8"))
    entries = ((payload.get("data") or {}).get("entries")) or []
    ebay_entries = [
        entry
        for entry in entries
        if isinstance(entry, dict) and entry.get("domain") == "ebay"
    ]
    if not ebay_entries:
        raise ValueError(f"No 'ebay' config entry found in {storage_path}")

    if entry_id is not None:
        target = entry_id.strip()
        matched = [
            e
            for e in ebay_entries
            if str(e.get("entry_id") or "") == target
            or str(e.get("title") or "") == target
        ]
        if not matched:
            available_ids = [
                str(e.get("entry_id") or "<unknown>") for e in ebay_entries
            ]
            raise ValueError(
                f"No 'ebay' config entry matching {target!r} in {storage_path}. "
                f"Available entry_ids: {', '.join(available_ids)}"
            )
        selected = matched[0]
    elif len(ebay_entries) > 1:
        available_ids = [str(e.get("entry_id") or "<unknown>") for e in ebay_entries]
        raise ValueError(
            f"Multiple 'ebay' config entries found in {storage_path} "
            f"({', '.join(available_ids)}). Pass --ha-entry-id to select the "
            "intended entry explicitly."
        )
    else:
        selected = ebay_entries[0]

    data = selected.get("data") or {}
    token = data.get("token") if isinstance(data.get("token"), dict) else {}
    refresh_token = token.get("refresh_token") or data.get("refresh_token") or ""

    access_token = ""
    expires_in_str = ""
    raw_access = token.get("access_token")
    expires_at = token.get("expires_at")
    if isinstance(raw_access, str) and raw_access.strip():
        if isinstance(expires_at, (int, float)):
            remaining = int(expires_at - time.time())
            if remaining > 360:
                access_token = raw_access.strip()
                expires_in_str = str(remaining)

    return {
        "environment": str(data.get("environment") or "production"),
        "client_id": str(data.get("client_id") or ""),
        "client_secret": str(data.get("client_secret") or ""),
        "runame": str(data.get("runame") or "unused-runame"),
        "refresh_token": str(refresh_token),
        "access_token": access_token,
        "access_token_expires_in": expires_in_str,
        "site_id": str(data.get("site_id") or "0"),
        "oauth_scopes": str(data.get("oauth_scopes") or ""),
    }


def resolve_live_credentials(
    *,
    env_file: Path | None = None,
    ha_config_entries: Path | None = None,
    ha_entry_id: str | None = None,
    environ: dict[str, str] | None = None,
) -> dict[str, str]:
    """Resolve read-only eBay credentials from environment, .env, or HA storage."""
    if ha_config_entries is not None:
        creds = _load_ha_storage_credentials(ha_config_entries, entry_id=ha_entry_id)
    else:
        env_map = dict(_parse_dotenv_file(env_file)) if env_file else {}
        source_env = environ if environ is not None else os.environ
        for key, value in source_env.items():
            if value:
                env_map[key] = value
        creds = {
            "environment": env_map.get("EBAY_ENVIRONMENT", "production").strip()
            or "production",
            "client_id": (
                env_map.get("EBAY_CLIENT_ID") or env_map.get("CLIENT_ID") or ""
            ).strip(),
            "client_secret": (
                env_map.get("EBAY_CLIENT_SECRET") or env_map.get("CLIENT_SECRET") or ""
            ).strip(),
            "runame": (
                env_map.get("EBAY_RUNAME") or env_map.get("RUNAME") or "unused-runame"
            ).strip(),
            "refresh_token": (
                env_map.get("EBAY_REFRESH_TOKEN") or env_map.get("REFRESH_TOKEN") or ""
            ).strip(),
            "access_token": (
                env_map.get("EBAY_ACCESS_TOKEN") or env_map.get("ACCESS_TOKEN") or ""
            ).strip(),
            "access_token_expires_in": (
                env_map.get("EBAY_ACCESS_TOKEN_EXPIRES_IN") or "7200"
            ).strip(),
            "site_id": (
                env_map.get("EBAY_SITE_ID") or env_map.get("SITE_ID") or "0"
            ).strip(),
            "oauth_scopes": (env_map.get("EBAY_OAUTH_SCOPES") or "").strip(),
        }

    if creds.get("access_token"):
        return creds

    missing = [
        name
        for name in ("client_id", "client_secret", "refresh_token")
        if not creds.get(name)
    ]
    if missing:
        raise ValueError(
            "Missing required eBay live credentials: "
            + ", ".join(missing)
            + ". Provide EBAY_ACCESS_TOKEN or EBAY_CLIENT_ID, EBAY_CLIENT_SECRET, "
            "and EBAY_REFRESH_TOKEN via environment variables or --env-file."
        )
    return creds


# ---------------------------------------------------------------------------
# Offline Synthetic Fixtures (Zero Network, Zero Credentials)
# ---------------------------------------------------------------------------
def get_synthetic_seller_xml() -> str:
    """Return a realistic GetBestOffersResponse XML fixture."""
    return """<?xml version="1.0" encoding="utf-8"?>
<GetBestOffersResponse xmlns="urn:ebay:apis:eBLBaseComponents">
  <Ack>Success</Ack>
  <ItemBestOffersArray>
    <ItemBestOffers>
      <Role>Seller</Role>
      <Item>
        <ItemID>110000000001</ItemID>
        <Title>Vintage Camera</Title>
      </Item>
      <BestOfferArray>
        <BestOffer>
          <BestOfferID>9001001</BestOfferID>
          <Price currencyID="USD">85.00</Price>
          <Status>Active</Status>
          <Quantity>1</Quantity>
          <CreationTime>2026-10-30T10:00:00.000Z</CreationTime>
          <ExpirationTime>2026-11-01T10:00:00.000Z</ExpirationTime>
        </BestOffer>
        <BestOffer>
          <BestOfferID>9001002</BestOfferID>
          <Price currencyID="USD">75.00</Price>
          <Status>Countered</Status>
          <Quantity>1</Quantity>
          <CounterOfferPrice currencyID="USD">80.00</CounterOfferPrice>
          <CreationTime>2026-10-29T12:00:00.000Z</CreationTime>
          <ExpirationTime>2026-10-31T12:00:00.000Z</ExpirationTime>
        </BestOffer>
      </BestOfferArray>
    </ItemBestOffers>
    <ItemBestOffers>
      <Role>Seller</Role>
      <Item>
        <ItemID>110000000002</ItemID>
        <Title>Audio Interface</Title>
      </Item>
      <BestOfferArray>
        <BestOffer>
          <BestOfferID>9001003</BestOfferID>
          <Price currencyID="USD">140.00</Price>
          <Status>Declined</Status>
          <Quantity>1</Quantity>
          <CreationTime>2026-10-28T09:00:00.000Z</CreationTime>
          <ExpirationTime>2026-10-28T10:00:00.000Z</ExpirationTime>
        </BestOffer>
      </BestOfferArray>
    </ItemBestOffers>
  </ItemBestOffersArray>
  <PaginationResult>
    <TotalNumberOfPages>1</TotalNumberOfPages>
    <TotalNumberOfEntries>3</TotalNumberOfEntries>
  </PaginationResult>
</GetBestOffersResponse>
"""


def get_synthetic_seller_graphql() -> dict[str, Any]:
    """Return a candidate sellerNegotiationHistory GraphQL response fixture."""
    return {
        "data": {
            "sellerNegotiationHistory": {
                "total": 3,
                "negotiations": [
                    {
                        "negotiationId": "9001001",
                        "legacyItemId": "110000000001",
                        "role": "SELLER",
                        "status": "ACTIVE",
                        "currentOffer": {
                            "offerId": "9001001",
                            "price": {"value": 85.00, "currency": "USD"},
                            "quantity": 1,
                            "creationDate": "2026-10-30T10:00:00.000Z",
                            "expirationDate": "2026-11-01T10:00:00.000Z",
                            "initiatorRole": "BUYER",
                        },
                    },
                    {
                        "negotiationId": "9001002",
                        "legacyItemId": "110000000001",
                        "role": "SELLER",
                        "status": "COUNTERED",
                        "currentOffer": {
                            "offerId": "9001002",
                            "price": {"value": 75.00, "currency": "USD"},
                            "quantity": 1,
                            "creationDate": "2026-10-29T12:00:00.000Z",
                            "expirationDate": "2026-10-31T12:00:00.000Z",
                            "initiatorRole": "BUYER",
                        },
                        "counterOffer": {
                            "offerId": "9001002-c1",
                            "price": {"value": 80.00, "currency": "USD"},
                        },
                    },
                    {
                        "negotiationId": "9001003",
                        "legacyItemId": "110000000002",
                        "role": "SELLER",
                        "status": "DECLINED",
                        "currentOffer": {
                            "offerId": "9001003",
                            "price": {"value": 140.00, "currency": "USD"},
                            "quantity": 1,
                            "creationDate": "2026-10-28T09:00:00.000Z",
                            "expirationDate": "2026-10-28T10:00:00.000Z",
                            "initiatorRole": "BUYER",
                        },
                    },
                ],
                "pageInfo": {
                    "hasNextPage": False,
                },
            }
        }
    }


def get_synthetic_buyer_xml() -> str:
    """Return a realistic GetMyeBayBuying BestOfferList XML fixture."""
    return """<?xml version="1.0" encoding="utf-8"?>
<GetMyeBayBuyingResponse xmlns="urn:ebay:apis:eBLBaseComponents">
  <Ack>Success</Ack>
  <BestOfferList>
    <ItemArray>
      <Item>
        <ItemID>220000000001</ItemID>
        <Title>Mechanical Keyboard</Title>
        <BestOfferDetails>
          <BestOffer>
            <BestOfferID>9002001</BestOfferID>
            <Price currencyID="USD">65.00</Price>
            <Status>Active</Status>
            <Quantity>1</Quantity>
            <CreationTime>2026-10-30T11:00:00.000Z</CreationTime>
            <ExpirationTime>2026-11-01T11:00:00.000Z</ExpirationTime>
          </BestOffer>
        </BestOfferDetails>
      </Item>
    </ItemArray>
    <PaginationResult>
      <TotalNumberOfPages>1</TotalNumberOfPages>
      <TotalNumberOfEntries>1</TotalNumberOfEntries>
    </PaginationResult>
  </BestOfferList>
</GetMyeBayBuyingResponse>
"""


def get_synthetic_buyer_graphql() -> dict[str, Any]:
    """Return a candidate buyerNegotiationHistory GraphQL response fixture."""
    return {
        "data": {
            "buyerNegotiationHistory": {
                "total": 1,
                "negotiations": [
                    {
                        "negotiationId": "9002001",
                        "legacyItemId": "220000000001",
                        "role": "BUYER",
                        "status": "ACTIVE",
                        "currentOffer": {
                            "offerId": "9002001",
                            "price": {"value": 65.00, "currency": "USD"},
                            "quantity": 1,
                            "creationDate": "2026-10-30T11:00:00.000Z",
                            "expirationDate": "2026-11-01T11:00:00.000Z",
                            "initiatorRole": "BUYER",
                        },
                    }
                ],
                "pageInfo": {
                    "hasNextPage": False,
                },
            }
        }
    }


# ---------------------------------------------------------------------------
# Runner logic
# ---------------------------------------------------------------------------
def run_offline_demo(role: str = "both") -> dict[str, Any]:
    """Execute complete offline synthetic demo verification with zero network calls."""
    results: dict[str, Any] = {"mode": "offline_synthetic_demo", "reports": {}}

    if role in ("seller", "both"):
        seller_xml = normalize_trading_xml_seller_offers(get_synthetic_seller_xml())
        seller_gql = normalize_graphql_offers_payload(
            get_synthetic_seller_graphql(), role=NegotiationRole.SELLER
        )
        seller_comp = compare_offers_snapshots(
            seller_xml, seller_gql, role=NegotiationRole.SELLER
        )
        results["reports"]["seller"] = seller_comp

    if role in ("buyer", "both"):
        buyer_xml = normalize_trading_xml_buyer_offers(get_synthetic_buyer_xml())
        buyer_gql = normalize_graphql_offers_payload(
            get_synthetic_buyer_graphql(), role=NegotiationRole.BUYER
        )
        buyer_comp = compare_offers_snapshots(
            buyer_xml, buyer_gql, role=NegotiationRole.BUYER
        )
        results["reports"]["buyer"] = buyer_comp

    return results


def run_offline_fixtures(
    xml_path: Path,
    graphql_path: Path,
    role: str = "seller",
) -> dict[str, Any]:
    """Execute offline comparison against user-provided file fixtures."""
    xml_content = xml_path.read_text(encoding="utf-8")
    gql_content = json.loads(graphql_path.read_text(encoding="utf-8"))

    neg_role = NegotiationRole.SELLER if role == "seller" else NegotiationRole.BUYER

    if neg_role == NegotiationRole.SELLER:
        xml_res = normalize_trading_xml_seller_offers(xml_content)
    else:
        xml_res = normalize_trading_xml_buyer_offers(xml_content)

    gql_res = normalize_graphql_offers_payload(gql_content, role=neg_role)
    comp = compare_offers_snapshots(xml_res, gql_res, role=neg_role)

    return {
        "mode": "offline_fixtures",
        "role": role,
        "reports": {role: comp},
    }


async def async_run_live_validation(
    creds: dict[str, str],
    *,
    role: str = "seller",
    max_pages: int = 25,
) -> dict[str, Any]:
    """Execute strictly read-only live validation against eBay APIs with user-provided credentials."""
    max_pages = validate_max_pages(max_pages)
    neg_role = NegotiationRole.SELLER if role == "seller" else NegotiationRole.BUYER

    async with aiohttp.ClientSession() as session:
        client = EbayApiClient(
            session,
            environment=creds.get("environment", "production"),
            client_id=creds.get("client_id") or "unused-client-id",
            client_secret=creds.get("client_secret") or "unused-client-secret",
            runame=creds.get("runame") or "unused-runame",
            refresh_token=creds.get("refresh_token") or None,
            site_id=creds.get("site_id") or "0",
        )
        if creds.get("access_token"):
            expires_in = int(creds.get("access_token_expires_in") or 7200)
            client._store_access_token(
                {
                    "access_token": creds["access_token"],
                    "expires_in": expires_in,
                }
            )

        # 1. Fetch live XML baseline
        xml_res: OfferCollectionResult
        if neg_role == NegotiationRole.SELLER:
            page = 1
            all_xml_offers: list[ET.Element] = []
            while page <= min(max_pages, BEST_OFFERS_MAX_PAGES):
                root = await client.async_call_trading_api(
                    BEST_OFFERS_CALL_NAME, build_get_best_offers_xml(page=page)
                )
                all_xml_offers.append(root)
                total_pages_el = root.find(
                    "e:PaginationResult/e:TotalNumberOfPages", namespaces=NS
                )
                total_pages = 1
                if total_pages_el is not None and total_pages_el.text:
                    try:
                        total_pages = int(total_pages_el.text.strip())
                    except ValueError:
                        break
                if page >= total_pages:
                    break
                page += 1
            xml_res = merge_trading_xml_seller_offers_pages(
                all_xml_offers, max_pages=max_pages
            )
        else:
            page = 1
            all_buyer_xml: list[ET.Element] = []
            while page <= min(max_pages, 25):
                root = await client.async_call_trading_api(
                    READ_ONLY_CALL_NAME,
                    build_get_my_ebay_buying_best_offers_xml(page=page),
                )
                all_buyer_xml.append(root)
                container = root.find("e:BestOfferList", namespaces=NS)
                total_pages_el = (
                    container.find(
                        "e:PaginationResult/e:TotalNumberOfPages", namespaces=NS
                    )
                    if container is not None
                    else None
                )
                total_pages = 1
                if total_pages_el is not None and total_pages_el.text:
                    try:
                        total_pages = int(total_pages_el.text.strip())
                    except ValueError:
                        break
                if page >= total_pages:
                    break
                page += 1
            xml_res = merge_trading_xml_buyer_offers_pages(
                all_buyer_xml, max_pages=max_pages
            )

        # 2. Probe candidate GraphQL operations
        matching_probes = [
            p
            for p in OFFERS_CONTRACT_PROBES
            if p.role == str(neg_role) or p.key == "typename_handshake"
        ]

        probe_results: list[dict[str, Any]] = []
        best_candidate: OfferCollectionResult | None = None
        best_rank: int = -999

        for probe in matching_probes:
            try:
                # Support schema-grounded pagination if variables are declared
                gql_pages: list[dict[str, Any]] = []
                current_vars = dict(probe.variables or {})
                page_idx = 0
                while page_idx < max_pages:
                    raw_gql = await client.async_post_graphql(
                        probe.query,
                        variables=current_vars,
                        operation_name=probe.operation_name,
                    )
                    gql_pages.append(raw_gql)
                    page_idx += 1

                    data_block = (
                        raw_gql.get("data") if isinstance(raw_gql, dict) else None
                    )
                    if not isinstance(data_block, dict):
                        break
                    _, c_block = _find_offers_container(data_block)
                    if not isinstance(c_block, dict):
                        break
                    p_info = c_block.get("pageInfo")
                    if not isinstance(p_info, dict) or not p_info.get("hasNextPage"):
                        break
                    end_cursor = p_info.get("endCursor")
                    if "after" in current_vars and end_cursor:
                        current_vars["after"] = end_cursor
                    elif "offset" in current_vars:
                        current_vars["offset"] = current_vars.get(
                            "offset", 0
                        ) + current_vars.get("limit", 20)
                    else:
                        break

                cand_res = merge_graphql_offers_pages(
                    gql_pages, role=neg_role, max_pages=max_pages
                )
                comp = compare_offers_snapshots(xml_res, cand_res, role=neg_role)
                rnk = rank_offers_probe_candidate(comp)

                probe_results.append(
                    {
                        "key": sanitize_offers_probe_key(probe.key),
                        "operation_name": sanitize_graphql_operation_name(
                            probe.operation_name
                        ),
                        "outcome": "success"
                        if cand_res.is_complete
                        else "partial_error",
                        "rank": rnk,
                        "overall_status": comp.get("overall_status"),
                    }
                )

                if rnk > best_rank:
                    best_rank = rnk
                    best_candidate = cand_res
            except (EbayError, aiohttp.ClientError, TimeoutError) as exc:
                probe_results.append(
                    {
                        "key": sanitize_offers_probe_key(probe.key),
                        "operation_name": sanitize_graphql_operation_name(
                            probe.operation_name
                        ),
                        "outcome": "error",
                        "error": summarize_offers_probe_exception(exc),
                    }
                )

        final_comp = (
            compare_offers_snapshots(xml_res, best_candidate, role=neg_role)
            if best_candidate is not None
            else {
                "role": str(neg_role),
                "overall_status": "inconclusive",
                "is_inconclusive": True,
                "errors": [
                    {
                        "code": "NO_SUCCESSFUL_GRAPHQL_PROBE",
                        "diagnostic": "All GraphQL candidate probes failed",
                    }
                ],
            }
        )

        return {
            "mode": "live_validation",
            "role": role,
            "probe_results": probe_results,
            "reports": {role: final_comp},
        }


def _validate_cli_max_pages(val: str) -> int:
    """Validate that --max-pages is an integer between 1 and 25 inclusive."""
    try:
        ival = int(val)
    except (ValueError, TypeError):
        raise argparse.ArgumentTypeError(f"--max-pages must be an integer, got '{val}'")
    try:
        return validate_max_pages(ival)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def main() -> int:
    """CLI entry point for validate_offers_parity."""
    parser = argparse.ArgumentParser(
        description="Validate eBay Best Offers & Negotiation History XML vs GraphQL contracts (read-only)."
    )
    parser.add_argument(
        "--role",
        choices=["seller", "buyer", "both"],
        default="both",
        help="Negotiation role to validate (default: both).",
    )
    parser.add_argument(
        "--synthetic-demo",
        action="store_true",
        default=False,
        help="Execute offline synthetic verification with sample fixtures (default mode).",
    )
    parser.add_argument(
        "--xml-fixture",
        type=Path,
        help="Path to an offline Trading XML response fixture file.",
    )
    parser.add_argument(
        "--graphql-fixture",
        type=Path,
        help="Path to an offline GraphQL JSON response fixture file.",
    )
    parser.add_argument(
        "--live",
        action="store_true",
        default=False,
        help="Opt-in flag to run read-only live API validation against eBay.",
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        help="Path to a local .env file containing eBay credentials for --live.",
    )
    parser.add_argument(
        "--ha-config-entries",
        type=Path,
        help="Path to Home Assistant core.config_entries file for --live.",
    )
    parser.add_argument(
        "--ha-entry-id",
        type=str,
        help="Target entry_id within core.config_entries.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="File path to write sanitized report output.",
    )
    parser.add_argument(
        "--format",
        choices=["markdown", "json"],
        default="markdown",
        help="Output report format (default: markdown).",
    )
    parser.add_argument(
        "--max-pages",
        type=_validate_cli_max_pages,
        default=25,
        help="Maximum pages to fetch in live pagination (1..25, default: 25).",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print verbose execution progress.",
    )

    args = parser.parse_args()

    try:
        validate_max_pages(args.max_pages)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2

    # Determine execution mode
    report_data: dict[str, Any]
    if args.live:
        try:
            creds = resolve_live_credentials(
                env_file=args.env_file,
                ha_config_entries=args.ha_config_entries,
                ha_entry_id=args.ha_entry_id,
            )
        except Exception as exc:
            print(f"Error resolving live credentials: {exc}", file=sys.stderr)
            return 2

        live_role = "seller" if args.role == "both" else args.role
        report_data = asyncio.run(
            async_run_live_validation(creds, role=live_role, max_pages=args.max_pages)
        )
    elif args.xml_fixture or args.graphql_fixture:
        if not (args.xml_fixture and args.graphql_fixture):
            print(
                "Both --xml-fixture and --graphql-fixture are required for fixture mode.",
                file=sys.stderr,
            )
            return 2
        fixture_role = "seller" if args.role == "both" else args.role
        report_data = run_offline_fixtures(
            args.xml_fixture, args.graphql_fixture, role=fixture_role
        )
    else:
        # Default: offline synthetic demo
        report_data = run_offline_demo(role=args.role)

    sanitized = sanitize_offers_parity_report(report_data)

    # Format output
    rendered: str
    if args.format == "json":
        rendered = render_offers_parity_json(sanitized)
    else:
        # Markdown: render each role report
        md_sections: list[str] = [
            "# Best Offers & Negotiation History Validation Harness",
            f"**Execution Mode:** `{sanitized.get('mode', 'unknown')}`",
            "",
        ]
        for r_name, r_rep in sanitized.get("reports", {}).items():
            md_sections.append(render_offers_parity_markdown(r_rep))
            md_sections.append("\n---\n")
        rendered = "\n".join(md_sections)

    if args.output:
        args.output.write_text(rendered, encoding="utf-8")
        if args.verbose:
            print(f"Sanitized report written to {args.output}")
    else:
        print(rendered)

    # Return non-zero if any report failed or is inconclusive
    all_reports = sanitized.get("reports", {})
    all_pass = all(
        rep.get("overall_status") == "pass" and not rep.get("is_inconclusive")
        for rep in all_reports.values()
    )
    return 0 if all_pass else 1


if __name__ == "__main__":
    sys.exit(main())
