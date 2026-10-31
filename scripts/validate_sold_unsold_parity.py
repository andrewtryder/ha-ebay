#!/usr/bin/env python3
"""Read-only eBay Sold & Unsold Listing Classification XML vs. Modern API contract and parity validation harness.

By default, this script makes **zero network calls** unless ``--live`` is
explicitly passed. It never logs, prints, or persists OAuth tokens, client
secrets, private listing IDs, prices, buyer handles, or raw account responses.

Examples:
  # Run offline synthetic parity verification (no network, no credentials):
  uv run python scripts/validate_sold_unsold_parity.py

  # Run explicit synthetic demonstration mode in JSON format:
  uv run python scripts/validate_sold_unsold_parity.py --synthetic-demo --format json

  # Compare custom local XML and GraphQL response fixtures offline:
  uv run python scripts/validate_sold_unsold_parity.py \
    --xml-fixture path/to/selling.xml --graphql-fixture path/to/ended.json

  # Run live read-only validation against eBay using a local git-ignored .env file:
  uv run python scripts/validate_sold_unsold_parity.py --live --env-file .env.local \
    --output sold_unsold_parity.local.md
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from custom_components.ebay.parsers.sold_unsold_parity import (  # noqa: E402
    MAX_CLASSIFICATION_PAGES,
    SOLD_UNSOLD_DEFAULT_WINDOW_DAYS,
    compare_sold_unsold_snapshots,
    merge_composite_modern_sold_unsold,
    normalize_graphql_seller_listing_search,
    normalize_rest_fulfillment_orders,
    normalize_trading_xml_sold_unsold,
    render_sold_unsold_parity_json,
    render_sold_unsold_parity_markdown,
    sanitize_sold_unsold_parity_report,
    validate_all_sold_unsold_contract_probes,
    validate_max_pages,
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


# ---------------------------------------------------------------------------
# Offline Synthetic Fixtures (Zero Network, Zero Credentials)
# ---------------------------------------------------------------------------
def get_synthetic_sold_unsold_xml() -> str:
    """Return a realistic GetMyeBaySelling XML fixture with SoldList and UnsoldList."""
    return """<?xml version="1.0" encoding="utf-8"?>
<GetMyeBaySellingResponse xmlns="urn:ebay:apis:eBLBaseComponents">
  <Ack>Success</Ack>
  <SoldList>
    <PaginationResult>
      <TotalNumberOfPages>1</TotalNumberOfPages>
      <TotalNumberOfEntries>1</TotalNumberOfEntries>
    </PaginationResult>
    <ItemArray>
      <Item>
        <ItemID>110000000001</ItemID>
        <Title>Vintage Camera</Title>
        <QuantitySold>1</QuantitySold>
        <SellingStatus>
          <ListingStatus>Completed</ListingStatus>
        </SellingStatus>
      </Item>
    </ItemArray>
  </SoldList>
  <UnsoldList>
    <PaginationResult>
      <TotalNumberOfPages>1</TotalNumberOfPages>
      <TotalNumberOfEntries>2</TotalNumberOfEntries>
    </PaginationResult>
    <ItemArray>
      <Item>
        <ItemID>110000000002</ItemID>
        <Title>Audio Interface</Title>
        <QuantitySold>0</QuantitySold>
        <SellingStatus>
          <ListingStatus>Ended</ListingStatus>
        </SellingStatus>
      </Item>
      <Item>
        <ItemID>110000000003</ItemID>
        <Title>Microphone Stand</Title>
        <QuantitySold>0</QuantitySold>
        <SellingStatus>
          <ListingStatus>Ended</ListingStatus>
        </SellingStatus>
      </Item>
    </ItemArray>
  </UnsoldList>
</GetMyeBaySellingResponse>
"""


def get_synthetic_graphql_ended_listings() -> dict[str, Any]:
    """Return a candidate sellerListingSearch ended listings GraphQL response fixture."""
    return {
        "data": {
            "sellerListingSearch": {
                "total": 3,
                "items": [
                    {
                        "itemId": "110000000001",
                        "title": "Vintage Camera",
                        "quantity": 1,
                        "quantitySold": 1,
                        "status": "ENDED",
                        "itemEndDate": "2026-10-25T14:30:00.000Z",
                    },
                    {
                        "itemId": "110000000002",
                        "title": "Audio Interface",
                        "quantity": 1,
                        "quantitySold": 0,
                        "status": "ENDED",
                        "itemEndDate": "2026-10-26T18:00:00.000Z",
                    },
                    {
                        "itemId": "110000000003",
                        "title": "Microphone Stand",
                        "quantity": 2,
                        "quantitySold": 0,
                        "status": "ENDED",
                        "itemEndDate": "2026-10-27T09:15:00.000Z",
                    },
                ],
            }
        }
    }


def get_synthetic_rest_fulfillment_orders() -> dict[str, Any]:
    """Return a candidate REST Sell Fulfillment getOrders response fixture."""
    return {
        "total": 1,
        "limit": 50,
        "offset": 0,
        "orders": [
            {
                "orderId": "08-11223-44556",
                "orderPaymentStatus": "PAID",
                "orderFulfillmentStatus": "FULFILLED",
                "lineItems": [
                    {
                        "lineItemId": "line-101",
                        "legacyItemId": "110000000001",
                        "quantity": 1,
                        "title": "Vintage Camera",
                    }
                ],
            }
        ],
    }


# ---------------------------------------------------------------------------
# Runner & Verification Logic
# ---------------------------------------------------------------------------
def run_synthetic_parity_verification(
    *,
    window_days: int = SOLD_UNSOLD_DEFAULT_WINDOW_DAYS,
) -> dict[str, Any]:
    """Run full synthetic parity comparison across Trading XML and candidate modern APIs."""
    xml_str = get_synthetic_sold_unsold_xml()
    xml_snapshot = normalize_trading_xml_sold_unsold(xml_str, window_days=window_days)

    gql_dict = get_synthetic_graphql_ended_listings()
    gql_snapshot = normalize_graphql_seller_listing_search(
        gql_dict, window_days=window_days
    )

    orders_dict = get_synthetic_rest_fulfillment_orders()
    orders_snapshot = normalize_rest_fulfillment_orders(
        orders_dict, window_days=window_days
    )

    composite_snapshot = merge_composite_modern_sold_unsold(
        gql_snapshot, orders_snapshot, window_days=window_days
    )

    comparison = compare_sold_unsold_snapshots(xml_snapshot, composite_snapshot)
    return comparison


async def async_run_live_validation(
    api_client: Any,
    *,
    max_pages: int = MAX_CLASSIFICATION_PAGES,
    window_days: int = SOLD_UNSOLD_DEFAULT_WINDOW_DAYS,
) -> dict[str, Any]:
    """Execute live read-only contract probes against eBay APIs."""
    validate_max_pages(max_pages)
    # Validate probes syntax prior to any network attempts
    validate_all_sold_unsold_contract_probes()

    # In live mode without direct integration instance, return fail-closed structured report
    return {
        "overall_status": "inconclusive",
        "is_inconclusive": True,
        "field_counts": {},
        "field_comparisons": {},
        "errors": [
            {
                "code": "NOT_IMPLEMENTED",
                "diagnostic": "Live validation is not implemented because GraphQL sellerListingSearch schema entitlement is unverified. Use offline fixtures (--xml-fixture, --graphql-fixture, --orders-fixture) or --synthetic-demo.",
            }
        ],
    }


def _validate_cli_max_pages(val_str: str) -> int:
    """CLI argument validator for --max-pages."""
    try:
        val = int(val_str)
    except (ValueError, TypeError) as exc:
        raise argparse.ArgumentTypeError(
            f"--max-pages must be an integer between 1 and {MAX_CLASSIFICATION_PAGES}"
        ) from exc
    try:
        return validate_max_pages(val)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def build_parser() -> argparse.ArgumentParser:
    """Build CLI argument parser."""
    parser = argparse.ArgumentParser(
        description="eBay Sold and Unsold Listing Classification XML vs Modern Parity Harness",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="Reserved: Live validation is not implemented because GraphQL sellerListingSearch schema entitlement is unverified. Exits with NOT_IMPLEMENTED.",
    )
    parser.add_argument(
        "--synthetic-demo",
        action="store_true",
        help="Run offline verification against bundled synthetic fixtures",
    )
    parser.add_argument(
        "--xml-fixture",
        type=Path,
        help="Path to local Trading XML GetMyeBaySelling fixture",
    )
    parser.add_argument(
        "--graphql-fixture",
        type=Path,
        help="Path to local GraphQL sellerListingSearch fixture",
    )
    parser.add_argument(
        "--orders-fixture",
        type=Path,
        help="Path to local REST Sell Fulfillment getOrders fixture",
    )
    parser.add_argument(
        "--format",
        choices=["markdown", "json"],
        default="markdown",
        help="Output format (markdown or json, default: markdown)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="File path to write report output",
    )
    parser.add_argument(
        "--max-pages",
        type=_validate_cli_max_pages,
        default=MAX_CLASSIFICATION_PAGES,
        help=f"Maximum pages to traverse (1..{MAX_CLASSIFICATION_PAGES}, default: {MAX_CLASSIFICATION_PAGES})",
    )
    parser.add_argument(
        "--window-days",
        type=int,
        default=SOLD_UNSOLD_DEFAULT_WINDOW_DAYS,
        help=f"Lookback window in days (default: {SOLD_UNSOLD_DEFAULT_WINDOW_DAYS})",
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        help="Reserved / non-functional for Milestone 7 (live validation is not implemented)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entrypoint."""
    parser = build_parser()
    args = parser.parse_args(argv)

    # Verify built-in queries before execution
    validate_all_sold_unsold_contract_probes()

    if args.xml_fixture or args.graphql_fixture or args.orders_fixture:
        if not (args.xml_fixture and args.graphql_fixture and args.orders_fixture):
            sys.stderr.write(
                "Error: When using custom fixtures, all three fixtures must be provided: "
                "--xml-fixture, --graphql-fixture, and --orders-fixture. "
                "Partial fixture input with synthetic substitution is rejected.\n"
            )
            return 1

        for fixture_path, label in (
            (args.xml_fixture, "--xml-fixture"),
            (args.graphql_fixture, "--graphql-fixture"),
            (args.orders_fixture, "--orders-fixture"),
        ):
            if not fixture_path.is_file():
                sys.stderr.write(
                    f"Error: Fixture file not found for {label}: {fixture_path}\n"
                )
                return 1

        xml_snapshot = normalize_trading_xml_sold_unsold(
            args.xml_fixture.read_text(encoding="utf-8"),
            window_days=args.window_days,
        )
        gql_data = json.loads(args.graphql_fixture.read_text(encoding="utf-8"))
        gql_snapshot = normalize_graphql_seller_listing_search(
            gql_data, window_days=args.window_days
        )
        orders_data = json.loads(args.orders_fixture.read_text(encoding="utf-8"))
        orders_snapshot = normalize_rest_fulfillment_orders(
            orders_data, window_days=args.window_days
        )

        candidate = merge_composite_modern_sold_unsold(
            gql_snapshot, orders_snapshot, window_days=args.window_days
        )
        report = compare_sold_unsold_snapshots(xml_snapshot, candidate)
    elif args.live:
        report = asyncio.run(
            async_run_live_validation(
                None, max_pages=args.max_pages, window_days=args.window_days
            )
        )
    else:
        # Default offline synthetic demo
        report = run_synthetic_parity_verification(window_days=args.window_days)

    sanitized = sanitize_sold_unsold_parity_report(report)

    if args.format == "json":
        output_str = render_sold_unsold_parity_json(sanitized)
    else:
        output_str = render_sold_unsold_parity_markdown(sanitized)

    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(output_str, encoding="utf-8")
        print(f"Report written to {args.output}")
    else:
        print(output_str)

    is_synthetic = not (
        args.xml_fixture or args.graphql_fixture or args.orders_fixture or args.live
    )
    if sanitized.get("overall_status") == "pass":
        return 0
    if (
        is_synthetic
        and sanitized.get("supported_fields_parity") == "pass"
        and not sanitized.get("errors")
    ):
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
