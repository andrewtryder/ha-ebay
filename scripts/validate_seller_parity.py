#!/usr/bin/env python3
"""Read-only eBay Active Seller Listings XML vs. GraphQL contract and parity validation harness.

By default, this script makes **zero network calls** unless ``--live`` is
explicitly passed. It never logs, prints, or persists OAuth tokens, client
secrets, private item IDs, titles, prices, or raw account responses.

Examples:
  # Run offline synthetic parity verification (no network, no credentials):
  uv run --python 3.14 --with-requirements requirements-ha-current.txt \\
    python scripts/validate_seller_parity.py --synthetic-demo

  # Compare local XML and GraphQL response fixtures offline:
  uv run --python 3.14 --with-requirements requirements-ha-current.txt \\
    python scripts/validate_seller_parity.py \\
    --xml-fixture path/to/selling.xml --graphql-fixture path/to/seller_listings.json

  # Run live read-only validation against eBay using a local git-ignored .env file:
  uv run --python 3.14 --with-requirements requirements-ha-current.txt \\
    python scripts/validate_seller_parity.py --live --env-file .env.local \\
    --output seller_parity.local.md
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
from custom_components.ebay.clients.errors import EbayError, EbayGraphQLError  # noqa: E402
from custom_components.ebay.marketplace import marketplace_id_for_site  # noqa: E402
from custom_components.ebay.parsers.payload import _dict_by_item_id  # noqa: E402
from custom_components.ebay.parsers.seller_parity import (  # noqa: E402
    SELLER_CONTRACT_PROBES,
    SellerContractProbe,
    compare_seller_snapshots,
    infer_and_validate_query_operation,
    merge_graphql_seller_pages,
    normalize_graphql_seller_payload,
    rank_seller_probe_candidate,
    render_seller_parity_json,
    render_seller_parity_markdown,
    sanitize_graphql_field_name,
    sanitize_graphql_operation_name,
    sanitize_seller_parity_report,
    sanitize_seller_probe_key,
    summarize_seller_probe_exception,
    validate_all_seller_contract_probes,
)
from custom_components.ebay.parsers.trading_builders import (  # noqa: E402
    SELLING_CALL_NAME,
    build_get_my_ebay_selling_xml,
)
from custom_components.ebay.parsers.trading_items import (  # noqa: E402
    parse_selling_container,
)
from custom_components.ebay.parsers.xml import (  # noqa: E402
    MAX_PAGES,
    _reported_total_pages,
)

# Backward-compatible alias for callers/tests
_summarize_seller_probe_exception = summarize_seller_probe_exception


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
) -> SellerContractProbe:
    """Validate and construct a read-only custom GraphQL probe from local files."""
    query_text = query_file.read_text(encoding="utf-8")
    variables = (
        load_variables_file(variables_file) if variables_file is not None else None
    )
    resolved_op = infer_and_validate_query_operation(
        query_text,
        operation_name=operation_name,
    )
    return SellerContractProbe(
        key="custom_query_file",
        description=f"Custom read-only query from {query_file.name}",
        query=query_text,
        operation_name=resolved_op,
        variables=variables,
    )


async def _fetch_xml_selling_only(
    client: EbayApiClient,
) -> tuple[dict[str, dict[str, Any]], bool]:
    """Fetch only ActiveList items via read-only Trading XML GetMyeBaySelling."""
    selling_items: list[dict[str, Any]] = []
    page = 1
    total_pages = 1
    truncated = False
    while page <= total_pages:
        root = await client.async_call_trading_api(
            SELLING_CALL_NAME,
            build_get_my_ebay_selling_xml(page=page),
        )
        selling_page = parse_selling_container(root)
        selling_items.extend(selling_page)
        active_pages = _reported_total_pages(root, "ActiveList")
        truncated = truncated or active_pages > MAX_PAGES
        total_pages = max(total_pages, min(active_pages, MAX_PAGES))
        page += 1
    return _dict_by_item_id(selling_items), truncated


async def async_fetch_bounded_graphql_pages(
    client: EbayApiClient,
    probe: SellerContractProbe,
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

    if not isinstance(first_page, Mapping):
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

    norm_first = normalize_graphql_seller_payload(first_page)
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
        if not isinstance(next_page, Mapping):
            break
        norm_page = normalize_graphql_seller_payload(next_page)
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
    client: EbayApiClient,
    xml_items: Mapping[str, Mapping[str, Any]] | None = None,
    custom_probe: SellerContractProbe | None = None,
) -> list[str]:
    """Collect sensitive strings to scrub from rendered reports."""
    forbidden: list[str] = []
    for secret in (
        getattr(client, "client_secret", None),
        getattr(client, "refresh_token", None),
        getattr(client, "_access_token", None),
    ):
        if isinstance(secret, str) and secret.strip():
            forbidden.append(secret.strip())

    if custom_probe and custom_probe.operation_name:
        forbidden.append(custom_probe.operation_name.strip())

    if xml_items:
        for item in xml_items.values():
            for sensitive_key in ("item_id", "title", "url", "image"):
                val = item.get(sensitive_key)
                if isinstance(val, str) and val.strip():
                    forbidden.append(val.strip())

    return forbidden


async def run_live_seller_validation(
    client: EbayApiClient,
    *,
    custom_probe: SellerContractProbe | None = None,
    paginate_custom: bool = False,
) -> dict[str, Any]:
    """Execute read-only live validation comparing Trading XML with GraphQL seller probes."""
    xml_selling: dict[str, dict[str, Any]] | None = None
    xml_available = False
    xml_truncated = False
    xml_error_summary: dict[str, Any] | None = None
    operational_warnings: list[str] = []

    try:
        xml_selling, xml_truncated = await _fetch_xml_selling_only(client)
        xml_available = True
    except (
        EbayError,
        aiohttp.ClientError,
        TimeoutError,
        ET.ParseError,
        ValueError,
        TypeError,
    ) as exc:
        xml_error_summary = summarize_seller_probe_exception(exc)
        operational_warnings.append(
            f"Trading XML ActiveList fetch failed: {xml_error_summary['error_type']} "
            f"({xml_error_summary['failure_class']})"
        )

    probes_to_run: list[SellerContractProbe] = list(SELLER_CONTRACT_PROBES)
    if custom_probe is not None:
        probes_to_run.append(custom_probe)

    probe_results: list[dict[str, Any]] = []
    best_graphql_pages: list[dict[str, Any]] | None = None
    best_rank: tuple[Any, ...] | None = None
    selected_probe_key: str | None = None
    selected_error_summary: dict[str, Any] | None = None
    last_candidate_error_summary: dict[str, Any] | None = None

    for probe in probes_to_run:
        should_paginate = (
            probe.key == "custom_query_file"
            and paginate_custom
            and probe.variables is not None
            and "limit" in probe.variables
            and "offset" in probe.variables
        )
        try:
            pages = await async_fetch_bounded_graphql_pages(
                client,
                probe,
                paginate=should_paginate,
            )
            data = pages[0] if pages else {}
            top_keys = (
                [
                    sanitize_graphql_field_name(k, kind="top_level")
                    for k in sorted(str(k) for k in data.keys())
                ]
                if isinstance(data, Mapping)
                else []
            )

            probe_page_errors: list[dict[str, Any]] = []
            for p in pages:
                if isinstance(p, Mapping):
                    raw_errs = p.get("errors") or p.get("error")
                    if isinstance(raw_errs, list):
                        for e in raw_errs:
                            if isinstance(e, dict):
                                probe_page_errors.append(e)
                            else:
                                probe_page_errors.append(
                                    {"code": "MALFORMED_ERROR", "message": str(e)}
                                )
                    elif isinstance(raw_errs, dict):
                        probe_page_errors.append(raw_errs)

            current_probe_err_summary: dict[str, Any] | None = None
            if probe_page_errors:
                exc_partial = EbayGraphQLError(
                    "GraphQL probe returned partial errors",
                    errors=probe_page_errors,
                )
                current_probe_err_summary = summarize_seller_probe_exception(
                    exc_partial
                )

            sanitized_op = sanitize_graphql_operation_name(probe.operation_name)
            sanitized_key = sanitize_seller_probe_key(probe.key)

            if probe.key == "typename_handshake":
                probe_dict_handshake: dict[str, Any] = {
                    "key": sanitized_key,
                    "operation_name": sanitized_op,
                    "outcome": "error" if current_probe_err_summary else "success",
                    "parity_eligible": False,
                    "top_level_keys": top_keys,
                    "reported_total": None,
                    "graphql_selling_count": 0,
                    "pagination_status": "handshake",
                    "summary": f"Succeeded; top-level keys={top_keys}",
                }
                if current_probe_err_summary:
                    probe_dict_handshake["error"] = current_probe_err_summary
                probe_results.append(probe_dict_handshake)
                continue

            merged_preview = merge_graphql_seller_pages(pages)
            is_custom = probe.key == "custom_query_file"
            candidate_dict = {
                "outcome": "error" if current_probe_err_summary else "success",
                "pagination_status": merged_preview["pagination_status"],
                "graphql_selling_count": len(merged_preview["items_by_id"]),
                "items_list_present": merged_preview["items_list_present"],
                "raw_items_count": merged_preview["raw_item_count"],
                "reported_total": merged_preview["reported_total"],
                "duplicate_ids_count": merged_preview["duplicate_id_count"],
                "missing_ids_count": merged_preview["missing_id_count"],
                "is_custom": is_custom,
            }
            candidate_rank = rank_seller_probe_candidate(candidate_dict)

            probe_dict_candidate: dict[str, Any] = {
                "key": sanitized_key,
                "operation_name": sanitized_op,
                "outcome": "error" if current_probe_err_summary else "success",
                "parity_eligible": not current_probe_err_summary,
                "top_level_keys": top_keys,
                "pagination_status": merged_preview["pagination_status"],
                "reported_total": merged_preview["reported_total"],
                "graphql_selling_count": len(merged_preview["items_by_id"]),
                "pages_fetched": len(pages),
                "summary": (
                    f"Succeeded; top-level keys={top_keys}, "
                    f"container_keys={merged_preview['container_keys']}, "
                    f"pagination_status={merged_preview['pagination_status']}"
                ),
            }
            if current_probe_err_summary:
                probe_dict_candidate["error"] = current_probe_err_summary
            probe_results.append(probe_dict_candidate)

            if best_rank is None or candidate_rank > best_rank:
                best_graphql_pages = pages
                best_rank = candidate_rank
                selected_probe_key = probe.key
                selected_error_summary = current_probe_err_summary
        except (
            EbayError,
            aiohttp.ClientError,
            TimeoutError,
            json.JSONDecodeError,
            ValueError,
            TypeError,
        ) as exc:
            err_summary = summarize_seller_probe_exception(exc)
            last_candidate_error_summary = err_summary
            probe_results.append(
                {
                    "key": sanitize_seller_probe_key(probe.key),
                    "operation_name": sanitize_graphql_operation_name(
                        probe.operation_name
                    ),
                    "outcome": "error",
                    "parity_eligible": False,
                    "reported_total": None,
                    "graphql_selling_count": 0,
                    "pagination_status": "error",
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

    selected_probe_dict: dict[str, Any] | None = None
    if best_graphql_pages and selected_probe_key:
        matched = [p for p in probes_to_run if p.key == selected_probe_key]
        raw_op_name = matched[0].operation_name if matched else None
        merged_best = merge_graphql_seller_pages(best_graphql_pages)
        selected_probe_dict = {
            "key": sanitize_seller_probe_key(selected_probe_key),
            "operation_name": sanitize_graphql_operation_name(raw_op_name),
            "outcome": "error" if selected_error_summary else "success",
            "items_field": merged_best["items_field_name"],
            "total_field": merged_best["total_field_name"],
            "reported_total": merged_best["reported_total"],
            "graphql_selling_count": len(merged_best["items_by_id"]),
            "pagination_status": merged_best["pagination_status"],
        }

    graphql_error_for_comparison = (
        selected_error_summary
        if best_graphql_pages is not None
        else last_candidate_error_summary
    )

    comparison = compare_seller_snapshots(
        xml_selling,
        best_graphql_pages,
        xml_available=xml_available,
        xml_truncated=xml_truncated,
        xml_error_summary=xml_error_summary,
        graphql_error_summary=graphql_error_for_comparison,
    )

    forbidden = _collect_client_forbidden_strings(client, xml_selling, custom_probe)
    if best_graphql_pages:
        try:
            merged_for_redaction = merge_graphql_seller_pages(best_graphql_pages)
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
        "selected_probe": selected_probe_dict,
        "operational_warnings": operational_warnings,
        "probes": probe_results,
        **comparison,
    }
    return sanitize_seller_parity_report(raw_report, forbidden_values=forbidden)


def run_offline_fixture_comparison(
    xml_text: str,
    graphql_json_text: str,
    *,
    environment: str = "production",
    site_id: str = "0",
) -> dict[str, Any]:
    """Compare local Trading XML and GraphQL JSON fixtures without network calls."""
    root = ET.fromstring(xml_text)
    selling_list = parse_selling_container(root)
    xml_selling = _dict_by_item_id(selling_list)
    active_pages = _reported_total_pages(root, "ActiveList")
    xml_truncated = active_pages > 1

    raw_gql = json.loads(graphql_json_text)
    collected_errors: list[dict[str, Any]] = []
    gql_data: Any

    def _extract_errors_from_node(node: Mapping[str, Any]) -> None:
        raw_errs = node.get("errors") or node.get("error")
        if not raw_errs:
            return
        if isinstance(raw_errs, list):
            for e in raw_errs:
                if isinstance(e, dict):
                    collected_errors.append(e)
                else:
                    collected_errors.append(
                        {"code": "MALFORMED_ERROR", "message": str(e)}
                    )
        elif isinstance(raw_errs, dict):
            collected_errors.append(raw_errs)
        else:
            collected_errors.append(
                {"code": "MALFORMED_ERROR", "message": str(raw_errs)}
            )

    if isinstance(raw_gql, list):
        pages_data: list[Any] = []
        for page in raw_gql:
            if isinstance(page, dict):
                _extract_errors_from_node(page)
                if "data" in page:
                    if page["data"] is not None:
                        pages_data.append(page["data"])
                else:
                    pages_data.append(page)
            else:
                pages_data.append(page)
        gql_data = pages_data if pages_data else None
    elif isinstance(raw_gql, dict):
        _extract_errors_from_node(raw_gql)
        if "data" in raw_gql:
            gql_data = raw_gql["data"]
        elif not collected_errors:
            gql_data = raw_gql
        else:
            gql_data = None
    else:
        gql_data = raw_gql

    graphql_error_summary: dict[str, Any] | None = None
    if collected_errors:
        exc = EbayGraphQLError(
            "Fixture GraphQL response contained errors",
            errors=collected_errors,
        )
        graphql_error_summary = summarize_seller_probe_exception(exc)

    comparison = compare_seller_snapshots(
        xml_selling,
        gql_data,
        xml_available=True,
        xml_truncated=xml_truncated,
        graphql_error_summary=graphql_error_summary,
    )

    probe_results: list[dict[str, Any]] = []
    selected_probe: dict[str, Any] | None = None
    if gql_data is not None:
        try:
            pages = gql_data if isinstance(gql_data, list) else [gql_data]
            merged = merge_graphql_seller_pages(pages)
            first_page = pages[0] if pages else {}
            top_keys = (
                [
                    sanitize_graphql_field_name(k, kind="top_level")
                    for k in sorted(str(k) for k in first_page.keys())
                ]
                if isinstance(first_page, Mapping)
                else []
            )
            outcome = "error" if collected_errors else "success"
            probe_dict: dict[str, Any] = {
                "key": "offline_fixture",
                "operation_name": "OfflineFixture",
                "outcome": outcome,
                "parity_eligible": not collected_errors,
                "top_level_keys": top_keys,
                "pagination_status": merged["pagination_status"],
                "reported_total": merged["reported_total"],
                "graphql_selling_count": len(merged["items_by_id"]),
                "pages_fetched": len(pages),
                "summary": f"Offline fixture; listings={len(merged['items_by_id'])}",
            }
            if collected_errors:
                probe_dict["error"] = graphql_error_summary
            probe_results.append(probe_dict)
            selected_probe = {
                "key": "offline_fixture",
                "operation_name": "OfflineFixture",
                "outcome": outcome,
                "items_field": merged["items_field_name"],
                "total_field": merged["total_field_name"],
                "reported_total": merged["reported_total"],
                "graphql_selling_count": len(merged["items_by_id"]),
                "pagination_status": merged["pagination_status"],
            }
        except Exception:
            pass
    elif collected_errors:
        probe_dict = {
            "key": "offline_fixture",
            "operation_name": "OfflineFixture",
            "outcome": "error",
            "parity_eligible": False,
            "top_level_keys": [],
            "pagination_status": "error",
            "reported_total": None,
            "graphql_selling_count": 0,
            "pages_fetched": 0,
            "error": graphql_error_summary,
            "summary": "Offline fixture returned only GraphQL errors",
        }
        probe_results.append(probe_dict)

    forbidden: list[str] = []
    for item in xml_selling.values():
        for sensitive_key in ("item_id", "title", "url", "image"):
            val = item.get(sensitive_key)
            if isinstance(val, str) and val:
                forbidden.append(val)

    raw_report = {
        "mode": "offline_fixture",
        "environment": environment,
        "site_id": site_id,
        "marketplace_id": marketplace_id_for_site(site_id) or "EBAY_US",
        "selected_probe_key": "offline_fixture" if selected_probe else None,
        "selected_probe": selected_probe,
        "probes": probe_results,
        **comparison,
    }
    return sanitize_seller_parity_report(raw_report, forbidden_values=forbidden)


def _synthetic_demo_fixtures() -> tuple[str, str]:
    """Return synthetic XML and GraphQL Active Seller Listings payloads for offline verification."""
    xml_fixture = """<?xml version="1.0" encoding="utf-8"?>
<GetMyeBaySellingResponse xmlns="urn:ebay:apis:eBLBaseComponents">
  <Ack>Success</Ack>
  <ActiveList>
    <ItemArray>
      <Item>
        <ItemID>111222333444</ItemID>
        <Title>Synthetic Handcrafted Ceramic Mug</Title>
        <ListingType>FixedPriceItem</ListingType>
        <ListingDetails>
          <EndTime>2026-11-25T18:00:00.000Z</EndTime>
          <ViewItemURL>https://www.ebay.com/itm/111222333444</ViewItemURL>
        </ListingDetails>
        <SellingStatus>
          <CurrentPrice currencyID="USD">28.50</CurrentPrice>
          <QuantitySold>3</QuantitySold>
        </SellingStatus>
        <Quantity>10</Quantity>
        <QuantityAvailable>7</QuantityAvailable>
        <HitCount>42</HitCount>
        <WatchCount>5</WatchCount>
        <QuestionCount>1</QuestionCount>
        <BestOfferDetails>
          <BestOfferCount>2</BestOfferCount>
        </BestOfferDetails>
        <PictureDetails>
          <GalleryURL>https://i.ebayimg.com/images/g/mug123XYZ/s-l140.jpg</GalleryURL>
        </PictureDetails>
      </Item>
      <Item>
        <ItemID>555666777888</ItemID>
        <Title>Synthetic Vintage Mechanical Watch</Title>
        <ListingType>Chinese</ListingType>
        <ListingDetails>
          <EndTime>2026-11-28T12:00:00.000Z</EndTime>
          <ViewItemURL>https://www.ebay.com/itm/555666777888</ViewItemURL>
        </ListingDetails>
        <SellingStatus>
          <CurrentPrice currencyID="USD">195.00</CurrentPrice>
          <QuantitySold>0</QuantitySold>
        </SellingStatus>
        <Quantity>1</Quantity>
        <QuantityAvailable>1</QuantityAvailable>
        <HitCount>120</HitCount>
        <WatchCount>14</WatchCount>
        <QuestionCount>0</QuestionCount>
        <BestOfferDetails>
          <BestOfferCount>0</BestOfferCount>
        </BestOfferDetails>
        <PictureDetails>
          <GalleryURL>https://i.ebayimg.com/images/g/watch456UVW/s-l225.jpg</GalleryURL>
        </PictureDetails>
      </Item>
    </ItemArray>
    <PaginationResult>
      <TotalNumberOfPages>1</TotalNumberOfPages>
      <TotalNumberOfEntries>2</TotalNumberOfEntries>
    </PaginationResult>
  </ActiveList>
</GetMyeBaySellingResponse>
"""
    graphql_fixture = json.dumps(
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
            }
        }
    )
    return xml_fixture, graphql_fixture


def build_arg_parser() -> argparse.ArgumentParser:
    """Construct the CLI argument parser."""
    parser = argparse.ArgumentParser(
        description=(
            "Read-only eBay Active Seller Listings Trading XML vs. GraphQL contract and parity "
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
        help="Path to a local GetMyeBaySelling XML fixture file for offline comparison.",
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
    parser.add_argument(
        "--json-output",
        type=Path,
        help="Optional local file path to write the sanitized report in JSON format.",
    )
    return parser


async def _async_main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    if args.variables_file is not None and args.query_file is None:
        parser.error("--variables-file requires --query-file.")

    if args.live:
        custom_probe: SellerContractProbe | None = None
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
            report = await run_live_seller_validation(
                client,
                custom_probe=custom_probe,
                paginate_custom=args.paginate_custom_query,
            )

    elif args.xml_fixture or args.graphql_fixture:
        if not args.xml_fixture or not args.graphql_fixture:
            parser.error(
                "--xml-fixture and --graphql-fixture must be provided together."
            )
        xml_text = args.xml_fixture.read_text(encoding="utf-8")
        gql_text = args.graphql_fixture.read_text(encoding="utf-8")
        report = run_offline_fixture_comparison(xml_text, gql_text)

    elif args.synthetic_demo:
        xml_text, gql_text = _synthetic_demo_fixtures()
        report = run_offline_fixture_comparison(xml_text, gql_text)

    else:
        parser.error(
            "Refusing to make network calls without explicit --live opt-in. "
            "Pass --synthetic-demo, --xml-fixture/--graphql-fixture, or --live."
        )
        return 2

    rendered_md = render_seller_parity_markdown(report)
    rendered_json = render_seller_parity_json(report)

    if args.json_output is not None:
        args.json_output.parent.mkdir(parents=True, exist_ok=True)
        args.json_output.write_text(rendered_json + "\n", encoding="utf-8")

    primary_rendered = rendered_json if args.format == "json" else rendered_md
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(primary_rendered + "\n", encoding="utf-8")
    elif args.json_output is None:
        print(primary_rendered)

    return 0


def main(argv: list[str] | None = None) -> int:
    """Entry point for validate_seller_parity CLI."""
    return asyncio.run(_async_main(argv))


if __name__ == "__main__":
    sys.exit(main())
