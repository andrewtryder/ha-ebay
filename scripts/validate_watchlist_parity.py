#!/usr/bin/env python3
"""Read-only eBay Watchlist XML vs. GraphQL contract and parity validation harness.

By default, this script makes **zero network calls** unless ``--live`` is
explicitly passed. It never logs, prints, or persists OAuth tokens, client
secrets, item IDs, titles, prices, or raw account responses.

Examples:
  # Run offline synthetic parity verification (no network, no credentials):
  uv run python scripts/validate_watchlist_parity.py --synthetic-demo

  # Compare local XML and GraphQL response fixtures offline:
  uv run python scripts/validate_watchlist_parity.py \
    --xml-fixture path/to/buying.xml --graphql-fixture path/to/watchlist.json

  # Run live read-only validation against eBay using a local git-ignored .env file:
  uv run python scripts/validate_watchlist_parity.py --live --env-file .env.local \
    --output watchlist_parity.local.md
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
import xml.etree.ElementTree as ET
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import aiohttp

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from custom_components.ebay.api import EbayApiClient  # noqa: E402
from custom_components.ebay.clients.errors import EbayError  # noqa: E402
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
    infer_and_validate_query_operation,
    merge_graphql_watchlist_pages,
    normalize_graphql_watchlist_payload,
    rank_graphql_probe_candidate,
    sanitize_parity_report,
    summarize_probe_exception,
    validate_all_contract_probes,
)
from custom_components.ebay.parsers.xml import (  # noqa: E402
    MAX_PAGES,
    _reported_total_pages,
)

# Keep backward-compatible alias for tests/callers
_summarize_probe_exception = summarize_probe_exception


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
            + ". Provide EBAY_ACCESS_TOKEN (short-lived token) or "
            "EBAY_CLIENT_ID, EBAY_CLIENT_SECRET, and EBAY_REFRESH_TOKEN "
            "via environment variables or --env-file."
        )
    return creds


def load_variables_file(path: Path) -> dict[str, Any]:
    """Load and validate a GraphQL variables JSON object from a local file."""
    raw_text = path.read_text(encoding="utf-8")
    parsed = json.loads(raw_text)
    if not isinstance(parsed, dict):
        raise ValueError(
            f"GraphQL variables file {path.name} must contain a JSON object (mapping)"
        )
    return {str(k): v for k, v in parsed.items()}


def build_custom_probe(
    query_file: Path,
    *,
    operation_name: str | None = None,
    variables_file: Path | None = None,
) -> WatchlistContractProbe:
    """Validate and construct a read-only custom GraphQL probe from local files."""
    query_text = query_file.read_text(encoding="utf-8")
    variables = (
        load_variables_file(variables_file) if variables_file is not None else None
    )
    resolved_op = infer_and_validate_query_operation(
        query_text,
        operation_name=operation_name,
        variables=variables,
    )
    return WatchlistContractProbe(
        key="custom_query_file",
        description=f"Custom read-only query from {query_file.name}",
        query=query_text,
        operation_name=resolved_op,
        variables=variables,
    )


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


async def async_fetch_bounded_graphql_pages(
    client: EbayApiClient,
    probe: WatchlistContractProbe,
    *,
    paginate: bool = False,
    max_pages: int = MAX_PAGES,
) -> list[dict[str, Any]]:
    """Execute a validated GraphQL probe, with optional bounded offset pagination.

    When ``paginate=False`` (default for unverified contract discovery), executes
    only a single request. When ``paginate=True`` and the probe uses integer
    ``limit`` and ``offset`` variables, fetches up to ``max_pages`` pages and stops
    immediately if a page returns no new unique item IDs, encounters duplicate/missing
    IDs, or reaches ``reported_total``.
    """
    first_page = await client.async_post_graphql(
        probe.query,
        variables=probe.variables,
        operation_name=probe.operation_name,
    )
    pages: list[dict[str, Any]] = [first_page]
    if not paginate or max_pages <= 1:
        return pages

    if not isinstance(first_page, Mapping) or not isinstance(
        first_page.get("userWatchList"), Mapping
    ):
        return pages

    base_vars = dict(probe.variables) if isinstance(probe.variables, Mapping) else {}
    limit = base_vars.get("limit")
    offset = base_vars.get("offset", 0)
    if (
        not isinstance(limit, int)
        or limit <= 0
        or not isinstance(offset, int)
        or offset < 0
    ):
        return pages

    norm_first = normalize_graphql_watchlist_payload(first_page)
    reported_total = norm_first["reported_total"]
    if (
        not norm_first["items_list_present"]
        or reported_total is None
        or norm_first["raw_item_count"] <= 0
        or norm_first["raw_item_count"] >= reported_total
        or norm_first["duplicate_id_count"] > 0
        or norm_first["missing_id_count"] > 0
    ):
        return pages

    seen_ids = set(norm_first["items_by_id"].keys())
    total_raw_retrieved = norm_first["raw_item_count"]
    current_offset = offset

    for _ in range(1, max_pages):
        if total_raw_retrieved >= reported_total:
            break
        current_offset += limit
        page_vars = {**base_vars, "limit": limit, "offset": current_offset}
        next_page = await client.async_post_graphql(
            probe.query,
            variables=page_vars,
            operation_name=probe.operation_name,
        )
        pages.append(next_page)
        if not isinstance(next_page, Mapping) or not isinstance(
            next_page.get("userWatchList"), Mapping
        ):
            break
        norm_page = normalize_graphql_watchlist_payload(next_page)
        page_ids = set(norm_page["items_by_id"].keys())
        new_ids = page_ids - seen_ids
        total_raw_retrieved += norm_page["raw_item_count"]
        if (
            norm_page["raw_item_count"] == 0
            or not new_ids
            or norm_page["duplicate_id_count"] > 0
            or norm_page["missing_id_count"] > 0
        ):
            break
        seen_ids.update(new_ids)

    return pages


def _collect_client_forbidden_strings(
    client: Any,
    xml_watched: Mapping[str, Mapping[str, Any]] | None,
    custom_probe: WatchlistContractProbe | None = None,
) -> list[str]:
    """Collect secrets and private item strings that must never appear in reports."""
    forbidden: list[str] = []
    for attr in (
        "client_id",
        "_client_id",
        "_client_secret",
        "client_secret",
        "runame",
        "_runame",
        "refresh_token",
        "_refresh_token",
        "_access_token",
        "access_token",
    ):
        val = getattr(client, attr, None)
        if isinstance(val, str) and val.strip():
            forbidden.append(val.strip())

    if xml_watched:
        for item in xml_watched.values():
            for sensitive_key in ("item_id", "title", "url", "image"):
                val = item.get(sensitive_key)
                if isinstance(val, str) and val.strip():
                    forbidden.append(val.strip())

    if custom_probe and isinstance(custom_probe.variables, Mapping):
        for var_val in custom_probe.variables.values():
            if isinstance(var_val, str) and len(var_val.strip()) >= 4:
                forbidden.append(var_val.strip())

    return forbidden


async def run_live_watchlist_validation(
    client: EbayApiClient,
    *,
    custom_probe: WatchlistContractProbe | None = None,
    paginate_custom_probe: bool = False,
) -> dict[str, Any]:
    """Execute read-only Trading XML and GraphQL probes and return a sanitized report."""
    validate_all_contract_probes()
    probes_to_run = list(WATCHLIST_CONTRACT_PROBES)
    if custom_probe is not None:
        resolved_op = infer_and_validate_query_operation(
            custom_probe.query,
            operation_name=custom_probe.operation_name,
            variables=custom_probe.variables,
        )
        custom_probe = WatchlistContractProbe(
            key=custom_probe.key,
            description=custom_probe.description,
            query=custom_probe.query,
            operation_name=resolved_op,
            variables=custom_probe.variables,
        )
        probes_to_run.append(custom_probe)

    xml_watched: dict[str, dict[str, Any]] | None = None
    xml_truncated = False
    xml_available = True
    xml_error_summary: dict[str, Any] | None = None

    try:
        xml_watched, xml_truncated = await _fetch_xml_watched_only(client)
    except (
        EbayError,
        ET.ParseError,
        aiohttp.ClientError,
        TimeoutError,
        ValueError,
        TypeError,
    ) as exc:
        xml_available = False
        xml_watched = None
        xml_error_summary = summarize_probe_exception(exc)

    probe_results: list[dict[str, Any]] = []
    best_graphql_pages: list[dict[str, Any]] | None = None
    best_rank: tuple[int, int, int, int, int, int] | None = None
    selected_probe_key: str | None = None
    last_error_summary: dict[str, Any] | None = None

    has_refresh_token = bool(
        getattr(client, "refresh_token", None)
        or getattr(client, "_refresh_token", None)
    )
    operational_warnings: list[str] = []
    if not has_refresh_token:
        operational_warnings.append(
            "Running with a short-lived access token and no refresh token: "
            "if the token expires or returns HTTP 401 / UNAUTHENTICATED, automatic "
            "OAuth token refresh and retry cannot succeed. Mint a fresh access token "
            "or provide EBAY_CLIENT_ID, EBAY_CLIENT_SECRET, and EBAY_REFRESH_TOKEN."
        )

    for probe in probes_to_run:
        should_paginate = paginate_custom_probe and probe.key == "custom_query_file"
        try:
            pages = await async_fetch_bounded_graphql_pages(
                client,
                probe,
                paginate=should_paginate,
            )
            data = pages[0] if pages else {}
            top_keys = (
                sorted(str(k) for k in data.keys()) if isinstance(data, Mapping) else []
            )

            if probe.key == "typename_handshake":
                probe_results.append(
                    {
                        "key": probe.key,
                        "outcome": "success",
                        "parity_eligible": False,
                        "top_level_keys": top_keys,
                        "summary": f"Succeeded; top-level keys={top_keys}",
                    }
                )
                continue

            if not isinstance(data, Mapping) or not isinstance(
                data.get("userWatchList"), Mapping
            ):
                probe_results.append(
                    {
                        "key": probe.key,
                        "outcome": "unsupported_shape",
                        "parity_eligible": False,
                        "top_level_keys": top_keys,
                        "summary": (
                            f"Succeeded (top-level keys={top_keys}), but response "
                            "lacks unaliased 'userWatchList' object required for "
                            "Watchlist parity comparison"
                        ),
                    }
                )
                continue

            merged_preview = merge_graphql_watchlist_pages(pages)
            is_custom = probe.key == "custom_query_file"
            candidate_rank = rank_graphql_probe_candidate(
                merged_preview, is_custom=is_custom
            )
            probe_results.append(
                {
                    "key": probe.key,
                    "outcome": "success",
                    "parity_eligible": True,
                    "top_level_keys": top_keys,
                    "pagination_status": merged_preview["pagination_status"],
                    "pages_fetched": len(pages),
                    "summary": (
                        f"Succeeded; top-level keys={top_keys}, "
                        f"container_keys={merged_preview['container_keys']}, "
                        f"pagination_status={merged_preview['pagination_status']}"
                    ),
                }
            )

            if best_rank is None or candidate_rank > best_rank:
                best_graphql_pages = pages
                best_rank = candidate_rank
                selected_probe_key = probe.key
        except (
            EbayError,
            aiohttp.ClientError,
            TimeoutError,
            json.JSONDecodeError,
            ValueError,
            TypeError,
        ) as exc:
            err_summary = summarize_probe_exception(exc)
            last_error_summary = err_summary
            probe_results.append(
                {
                    "key": probe.key,
                    "outcome": "error",
                    "parity_eligible": False,
                    "error": err_summary,
                    "summary": (
                        f"{err_summary['error_type']} "
                        f"(failure_class={err_summary['failure_class']}, "
                        f"status={err_summary['http_status']}, "
                        f"category={err_summary['category']}, "
                        f"codes={err_summary['error_codes']}, "
                        f"domains={err_summary['domains']})"
                    ),
                }
            )

    comparison = compare_watchlist_snapshots(
        xml_watched,
        best_graphql_pages,
        xml_available=xml_available,
        xml_truncated=xml_truncated,
        xml_error_summary=xml_error_summary,
        graphql_error_summary=last_error_summary,
    )

    forbidden = _collect_client_forbidden_strings(client, xml_watched, custom_probe)
    if best_graphql_pages:
        try:
            merged_for_redaction = merge_graphql_watchlist_pages(best_graphql_pages)
            for item in merged_for_redaction["items_by_id"].values():
                for sensitive_key in (
                    "item_id",
                    "raw_item_id",
                    "title",
                    "url",
                    "image",
                ):
                    val = item.get(sensitive_key)
                    if isinstance(val, str) and val.strip():
                        forbidden.append(val.strip())
        except (TypeError, ValueError):
            pass

    raw_report = {
        "mode": "live_authenticated",
        "environment": client.environment,
        "site_id": client.site_id,
        "marketplace_id": marketplace_id_for_site(client.site_id) or "EBAY_US",
        "selected_probe_key": selected_probe_key,
        "operational_warnings": operational_warnings,
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
    watch_pages = _reported_total_pages(root, "WatchList")
    xml_truncated = watch_pages > 1

    raw_gql = json.loads(graphql_json_text)
    gql_data: Any
    if isinstance(raw_gql, list):
        gql_data = [
            page.get("data")
            if isinstance(page, dict) and isinstance(page.get("data"), dict)
            else page
            for page in raw_gql
        ]
    elif isinstance(raw_gql, dict) and isinstance(raw_gql.get("data"), dict):
        gql_data = raw_gql["data"]
    else:
        gql_data = raw_gql

    comparison = compare_watchlist_snapshots(
        xml_watched,
        gql_data,
        xml_available=True,
        xml_truncated=xml_truncated,
    )
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
            "Advanced/optional read-only path to an offline Home Assistant "
            "core.config_entries copy. Prefer --env-file to avoid concurrent "
            "OAuth refreshes against a running Home Assistant instance."
        ),
    )
    parser.add_argument(
        "--ha-entry-id",
        default=None,
        help="Specific Home Assistant config entry_id or title when using --ha-config-entries.",
    )
    parser.add_argument(
        "--query-file",
        type=Path,
        help="Optional path to a custom read-only GraphQL query file to probe in --live mode.",
    )
    parser.add_argument(
        "--variables-file",
        type=Path,
        help="Optional path to a local JSON file containing GraphQL variables for --query-file.",
    )
    parser.add_argument(
        "--operation-name",
        default=None,
        help=(
            "Optional operation name for --query-file. If omitted, the single "
            "query operation name in the document is inferred automatically."
        ),
    )
    parser.add_argument(
        "--paginate-custom-query",
        action="store_true",
        help=(
            "Enable bounded multi-page fetching (up to MAX_PAGES) for a verified "
            "--query-file that accepts integer $limit and $offset variables."
        ),
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

    if args.variables_file is not None and args.query_file is None:
        parser.error("--variables-file requires --query-file.")

    if args.live:
        custom_probe: WatchlistContractProbe | None = None
        if args.query_file is not None:
            custom_probe = build_custom_probe(
                args.query_file,
                operation_name=args.operation_name,
                variables_file=args.variables_file,
            )

        creds = resolve_live_credentials(
            env_file=args.env_file,
            ha_config_entries=args.ha_config_entries,
            ha_entry_id=args.ha_entry_id,
        )
        async with aiohttp.ClientSession() as session:
            client = EbayApiClient(
                session,
                environment=creds["environment"],
                client_id=creds["client_id"] or "unused-client-id",
                client_secret=creds["client_secret"] or "unused-client-secret",
                runame=creds["runame"] or "unused-runame",
                refresh_token=creds["refresh_token"] or None,
                site_id=creds["site_id"],
            )
            if creds.get("access_token"):
                expires_in = int(creds.get("access_token_expires_in") or 7200)
                client._store_access_token(
                    {
                        "access_token": creds["access_token"],
                        "expires_in": expires_in,
                    }
                )
            report = await run_live_watchlist_validation(
                client,
                custom_probe=custom_probe,
                paginate_custom_probe=args.paginate_custom_query,
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
