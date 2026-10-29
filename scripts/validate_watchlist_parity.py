#!/usr/bin/env python3
"""Read-only eBay Watchlist XML vs. GraphQL contract and parity validation harness.

By default, this script makes **zero network calls** unless ``--live`` is
explicitly passed. It never logs, prints, or persists OAuth tokens, client
secrets, item IDs, titles, prices, or raw account responses.

Examples:
  # Run offline synthetic parity verification (no network, no credentials):
  uv run --python 3.14 --with-requirements requirements-ha-current.txt \\
    python scripts/validate_watchlist_parity.py --synthetic-demo

  # Compare local XML and GraphQL response fixtures offline:
  uv run --python 3.14 --with-requirements requirements-ha-current.txt \\
    python scripts/validate_watchlist_parity.py \\
    --xml-fixture path/to/buying.xml --graphql-fixture path/to/watchlist.json

  # Run live read-only validation against eBay (requires explicit --live):
  uv run --python 3.14 --with-requirements requirements-ha-current.txt \\
    python scripts/validate_watchlist_parity.py --live
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import aiohttp

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from custom_components.ebay.api import EbayApiClient  # noqa: E402
from custom_components.ebay.clients.errors import (  # noqa: E402
    EbayApiError,
    EbayAuthError,
    EbayError,
    EbayGraphQLError,
)
from custom_components.ebay.clients.graphql import (  # noqa: E402
    validate_read_only_query,
)
from custom_components.ebay.marketplace import marketplace_id_for_site  # noqa: E402
from custom_components.ebay.parsers.payload import _dict_by_item_id  # noqa: E402
from custom_components.ebay.parsers.trading_builders import (  # noqa: E402
    READ_ONLY_CALL_NAME,
    build_get_my_ebay_buying_xml,
)
from custom_components.ebay.parsers.trading_items import (  # noqa: E402
    parse_container,
)
from custom_components.ebay.parsers.watchlist_parity import (  # noqa: E402
    WATCHLIST_CONTRACT_PROBES,
    WatchlistContractProbe,
    compare_watchlist_snapshots,
    format_parity_report_markdown,
    sanitize_parity_report,
    validate_all_contract_probes,
)
from custom_components.ebay.parsers.xml import (  # noqa: E402
    MAX_PAGES,
    _reported_total_pages,
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


def _load_ha_storage_credentials(storage_path: Path) -> dict[str, str]:
    """Read eBay entry credentials from a Home Assistant core.config_entries file (read-only)."""
    payload = json.loads(storage_path.read_text(encoding="utf-8"))
    entries = ((payload.get("data") or {}).get("entries")) or []
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("domain") != "ebay":
            continue
        data = entry.get("data") or {}
        token = data.get("token") if isinstance(data.get("token"), dict) else {}
        refresh_token = token.get("refresh_token") or data.get("refresh_token") or ""
        return {
            "environment": str(data.get("environment") or "production"),
            "client_id": str(data.get("client_id") or ""),
            "client_secret": str(data.get("client_secret") or ""),
            "runame": str(data.get("runame") or ""),
            "refresh_token": str(refresh_token),
            "site_id": str(data.get("site_id") or "0"),
            "oauth_scopes": str(data.get("oauth_scopes") or ""),
        }
    raise ValueError(f"No 'ebay' config entry found in {storage_path}")


def resolve_live_credentials(
    *,
    env_file: Path | None = None,
    ha_config_entries: Path | None = None,
    environ: dict[str, str] | None = None,
) -> dict[str, str]:
    """Resolve read-only eBay credentials from HA storage, environment, or .env."""
    if ha_config_entries is not None:
        creds = _load_ha_storage_credentials(ha_config_entries)
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
            "site_id": (
                env_map.get("EBAY_SITE_ID") or env_map.get("SITE_ID") or "0"
            ).strip(),
            "oauth_scopes": (env_map.get("EBAY_OAUTH_SCOPES") or "").strip(),
        }

    missing = [
        name
        for name in ("client_id", "client_secret", "refresh_token")
        if not creds.get(name)
    ]
    if missing:
        raise ValueError(
            "Missing required eBay live credentials: "
            + ", ".join(missing)
            + ". Provide EBAY_CLIENT_ID, EBAY_CLIENT_SECRET, and EBAY_REFRESH_TOKEN "
            "via environment variables, --env-file, or --ha-config-entries."
        )
    return creds


async def _fetch_xml_watched_only(
    client: EbayApiClient,
) -> tuple[dict[str, dict[str, Any]], bool]:
    """Fetch only WatchList items via read-only Trading XML GetMyeBayBuying."""
    watched_items: list[dict[str, Any]] = []
    page = 1
    total_pages = 1
    truncated = False
    while page <= total_pages:
        root = await client.async_call_trading_api(
            READ_ONLY_CALL_NAME,
            build_get_my_ebay_buying_xml(page=page),
        )
        watched_page = parse_container(root, "WatchList", "watched")
        watched_items.extend(watched_page)
        watch_pages = _reported_total_pages(root, "WatchList")
        truncated = truncated or watch_pages > MAX_PAGES
        total_pages = max(total_pages, min(watch_pages, MAX_PAGES))
        page += 1
    return _dict_by_item_id(watched_items), truncated


def _summarize_probe_exception(exc: EbayError) -> dict[str, Any]:
    """Return a sanitized summary of a GraphQL probe error."""
    if isinstance(exc, EbayGraphQLError):
        error_codes: list[str] = []
        categories: list[str] = []
        domains: list[str] = []
        for err in exc.errors:
            if not isinstance(err, dict):
                continue
            ext = (
                err.get("extensions") if isinstance(err.get("extensions"), dict) else {}
            )
            code = ext.get("code") or ext.get("errorCode") or err.get("errorId")
            if code is not None and str(code) not in error_codes:
                error_codes.append(str(code))
            cat = ext.get("category") or err.get("category")
            if cat and str(cat) not in categories:
                categories.append(str(cat))
            dom = ext.get("domain") or err.get("domain")
            if dom and str(dom) not in domains:
                domains.append(str(dom))
        return {
            "error_type": type(exc).__name__,
            "http_status": 200,
            "category": categories[0] if categories else None,
            "error_codes": error_codes,
            "domains": domains,
            "is_auth_error": False,
            "is_scope_or_permission_error": False,
            "is_rate_limit": "RATE_LIMITED" in error_codes,
        }
    return {
        "error_type": type(exc).__name__,
        "http_status": getattr(exc, "status", None),
        "category": None,
        "error_codes": [],
        "domains": [],
        "is_auth_error": isinstance(exc, EbayAuthError),
        "is_scope_or_permission_error": bool(
            getattr(exc, "is_permission_error", False)
        ),
        "is_rate_limit": False,
    }


async def run_live_watchlist_validation(
    client: EbayApiClient,
    *,
    custom_probe: WatchlistContractProbe | None = None,
) -> dict[str, Any]:
    """Execute read-only Trading XML and GraphQL probes and return a sanitized report."""
    validate_all_contract_probes()
    probes_to_run = list(WATCHLIST_CONTRACT_PROBES)
    if custom_probe is not None:
        validate_read_only_query(
            custom_probe.query, operation_name=custom_probe.operation_name
        )
        probes_to_run.append(custom_probe)

    xml_watched, xml_truncated = await _fetch_xml_watched_only(client)

    probe_results: list[dict[str, Any]] = []
    best_graphql_payload: dict[str, Any] | None = None
    last_error_summary: dict[str, Any] | None = None

    for probe in probes_to_run:
        try:
            data = await client.async_post_graphql(
                probe.query,
                variables=probe.variables,
                operation_name=probe.operation_name,
            )
            top_keys = (
                sorted(str(k) for k in data.keys()) if isinstance(data, dict) else []
            )
            probe_results.append(
                {
                    "key": probe.key,
                    "outcome": "success",
                    "top_level_keys": top_keys,
                    "summary": f"Succeeded; top-level keys={top_keys}",
                }
            )
            if isinstance(data, dict) and isinstance(data.get("userWatchList"), dict):
                uwl = data["userWatchList"]
                has_items = any(
                    isinstance(uwl.get(k), list)
                    for k in ("items", "watchlistItems", "nodes", "edges")
                )
                if best_graphql_payload is None or has_items:
                    best_graphql_payload = data
        except (EbayGraphQLError, EbayAuthError, EbayApiError) as exc:
            err_summary = _summarize_probe_exception(exc)
            last_error_summary = err_summary
            probe_results.append(
                {
                    "key": probe.key,
                    "outcome": "error",
                    "error": err_summary,
                    "summary": (
                        f"{err_summary['error_type']} "
                        f"(status={err_summary['http_status']}, "
                        f"category={err_summary['category']}, "
                        f"codes={err_summary['error_codes']})"
                    ),
                }
            )

    comparison = compare_watchlist_snapshots(
        xml_watched,
        best_graphql_payload,
        graphql_error_summary=last_error_summary,
    )

    forbidden = [
        client._client_id,
        client._client_secret,
        client._runame,
        client._refresh_token or "",
        client._access_token or "",
    ]
    for item in xml_watched.values():
        for sensitive_key in ("item_id", "title", "url", "image"):
            val = item.get(sensitive_key)
            if isinstance(val, str) and val:
                forbidden.append(val)

    raw_report = {
        "mode": "live_authenticated",
        "environment": client.environment,
        "site_id": client.site_id,
        "marketplace_id": marketplace_id_for_site(client.site_id) or "EBAY_US",
        "xml_truncated": xml_truncated,
        "probes": probe_results,
        **comparison,
    }
    return sanitize_parity_report(raw_report, forbidden_values=forbidden)


def run_offline_fixture_comparison(
    xml_text: str,
    graphql_json_text: str,
    *,
    environment: str = "production",
    site_id: str = "0",
) -> dict[str, Any]:
    """Compare local Trading XML and GraphQL JSON fixtures without network calls."""
    root = ET.fromstring(xml_text)
    watched_list = parse_container(root, "WatchList", "watched")
    xml_watched = _dict_by_item_id(watched_list)

    raw_gql = json.loads(graphql_json_text)
    gql_data = (
        raw_gql.get("data")
        if isinstance(raw_gql, dict) and isinstance(raw_gql.get("data"), dict)
        else raw_gql
    )

    comparison = compare_watchlist_snapshots(xml_watched, gql_data)
    forbidden: list[str] = []
    for item in xml_watched.values():
        for sensitive_key in ("item_id", "title", "url", "image"):
            val = item.get(sensitive_key)
            if isinstance(val, str) and val:
                forbidden.append(val)

    raw_report = {
        "mode": "offline_fixture",
        "environment": environment,
        "site_id": site_id,
        "marketplace_id": marketplace_id_for_site(site_id) or "EBAY_US",
        "xml_truncated": False,
        "probes": [],
        **comparison,
    }
    return sanitize_parity_report(raw_report, forbidden_values=forbidden)


def _synthetic_demo_fixtures() -> tuple[str, str]:
    """Return synthetic XML and GraphQL Watchlist payloads for offline verification."""
    xml_fixture = """<?xml version="1.0" encoding="utf-8"?>
<GetMyeBayBuyingResponse xmlns="urn:ebay:apis:eBLBaseComponents">
  <Ack>Success</Ack>
  <WatchList>
    <ItemArray>
      <Item>
        <ItemID>111222333444</ItemID>
        <Title>Synthetic Vintage Camera Lens</Title>
        <ListingType>Chinese</ListingType>
        <ListingDetails>
          <EndTime>2026-11-15T18:30:00.000Z</EndTime>
          <ViewItemURL>https://www.ebay.com/itm/Synthetic-Vintage-Camera-Lens/111222333444?hash=item123</ViewItemURL>
        </ListingDetails>
        <SellingStatus>
          <CurrentPrice currencyID="USD">149.50</CurrentPrice>
          <BidCount>4</BidCount>
        </SellingStatus>
        <PictureDetails>
          <GalleryURL>https://i.ebayimg.com/images/g/abc123XYZ/s-l140.jpg</GalleryURL>
        </PictureDetails>
      </Item>
      <Item>
        <ItemID>555666777888</ItemID>
        <Title>Synthetic Mechanical Keyboard</Title>
        <ListingType>FixedPriceItem</ListingType>
        <ListingDetails>
          <EndTime>2026-11-20T12:00:00.000Z</EndTime>
          <ViewItemURL>https://www.ebay.com/itm/555666777888</ViewItemURL>
        </ListingDetails>
        <SellingStatus>
          <CurrentPrice currencyID="USD">89.00</CurrentPrice>
          <BidCount>0</BidCount>
        </SellingStatus>
        <PictureDetails>
          <GalleryURL>https://i.ebayimg.com/images/g/def456UVW/s-l225.jpg</GalleryURL>
        </PictureDetails>
      </Item>
    </ItemArray>
    <PaginationResult>
      <TotalNumberOfPages>1</TotalNumberOfPages>
      <TotalNumberOfEntries>2</TotalNumberOfEntries>
    </PaginationResult>
  </WatchList>
</GetMyeBayBuyingResponse>
"""
    graphql_fixture = json.dumps(
        {
            "data": {
                "userWatchList": {
                    "total": 2,
                    "items": [
                        {
                            "itemId": "v1|111222333444|0",
                            "title": "Synthetic Vintage Camera Lens",
                            "formatType": "AUCTION",
                            "itemEndDate": "2026-11-15T18:30:00Z",
                            "itemWebUrl": "https://www.ebay.com/itm/111222333444",
                            "price": {"value": "149.50", "currency": "USD"},
                            "image": {
                                "imageUrl": "https://i.ebayimg.com/images/g/abc123XYZ/s-l500.jpg"
                            },
                        },
                        {
                            "itemId": "555666777888",
                            "title": "Synthetic Mechanical Keyboard",
                            "formatType": "FIXED_PRICE",
                            "itemEndDate": "2026-11-20T12:00:00.000Z",
                            "itemWebUrl": "https://www.ebay.com/itm/555666777888",
                            "price": {"value": "89.00", "currency": "USD"},
                            "image": {
                                "imageUrl": "https://i.ebayimg.com/images/g/def456UVW/s-l225.jpg"
                            },
                        },
                    ],
                }
            }
        }
    )
    return xml_fixture, graphql_fixture


def build_arg_parser() -> argparse.ArgumentParser:
    """Construct the CLI argument parser."""
    parser = argparse.ArgumentParser(
        description=(
            "Read-only eBay Watchlist Trading XML vs. GraphQL contract and parity "
            "validation harness. Makes zero network calls unless --live is passed."
        )
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="Explicit opt-in to perform read-only authenticated API calls to eBay.",
    )
    parser.add_argument(
        "--synthetic-demo",
        action="store_true",
        help="Run offline parity comparison against built-in synthetic fixtures.",
    )
    parser.add_argument(
        "--xml-fixture",
        type=Path,
        help="Path to a local GetMyeBayBuying XML fixture file for offline comparison.",
    )
    parser.add_argument(
        "--graphql-fixture",
        type=Path,
        help="Path to a local GraphQL JSON fixture file for offline comparison.",
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        default=Path(".env"),
        help="Path to local .env file when running with --live (default: .env).",
    )
    parser.add_argument(
        "--ha-config-entries",
        type=Path,
        help=(
            "Optional path to Home Assistant .storage/core.config_entries "
            "(read-only) to load existing eBay entry credentials in --live mode."
        ),
    )
    parser.add_argument(
        "--query-file",
        type=Path,
        help="Optional path to a custom read-only GraphQL query file to probe in --live mode.",
    )
    parser.add_argument(
        "--operation-name",
        default="CustomWatchListProbe",
        help="Operation name for --query-file when running in --live mode.",
    )
    parser.add_argument(
        "--format",
        choices=("markdown", "json"),
        default="markdown",
        help="Output format for the sanitized parity report (default: markdown).",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Optional local file path to write the sanitized report.",
    )
    return parser


async def _async_main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    if args.live:
        creds = resolve_live_credentials(
            env_file=args.env_file,
            ha_config_entries=args.ha_config_entries,
        )
        custom_probe: WatchlistContractProbe | None = None
        if args.query_file is not None:
            query_text = args.query_file.read_text(encoding="utf-8")
            validate_read_only_query(query_text, operation_name=args.operation_name)
            custom_probe = WatchlistContractProbe(
                key="custom_query_file",
                description=f"Custom read-only query from {args.query_file.name}",
                query=query_text,
                operation_name=args.operation_name,
            )
        async with aiohttp.ClientSession() as session:
            client = EbayApiClient(
                session,
                environment=creds["environment"],
                client_id=creds["client_id"],
                client_secret=creds["client_secret"],
                runame=creds["runame"],
                refresh_token=creds["refresh_token"],
                site_id=creds["site_id"],
            )
            report = await run_live_watchlist_validation(
                client,
                custom_probe=custom_probe,
            )
    elif args.xml_fixture and args.graphql_fixture:
        report = run_offline_fixture_comparison(
            args.xml_fixture.read_text(encoding="utf-8"),
            args.graphql_fixture.read_text(encoding="utf-8"),
        )
    elif args.synthetic_demo:
        xml_demo, gql_demo = _synthetic_demo_fixtures()
        report = run_offline_fixture_comparison(xml_demo, gql_demo)
    else:
        parser.error(
            "Refusing to make network calls without explicit --live opt-in. "
            "Pass --synthetic-demo, --xml-fixture/--graphql-fixture, or --live."
        )
        return 2

    rendered = (
        json.dumps(report, indent=2, sort_keys=True)
        if args.format == "json"
        else format_parity_report_markdown(report)
    )
    if args.output is not None:
        args.output.write_text(rendered + "\n", encoding="utf-8")
    else:
        print(rendered)
    return 0


def main(argv: list[str] | None = None) -> int:
    """Entrypoint for the CLI harness."""
    return asyncio.run(_async_main(argv))


if __name__ == "__main__":
    raise SystemExit(main())
