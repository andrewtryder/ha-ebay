#!/usr/bin/env python3
"""Read-only eBay Remaining Legacy Operations & Authenticated Identity Parity Validation Harness.

By default, this script makes **zero network calls** unless ``--live`` is
explicitly passed. It never logs, prints, or persists OAuth tokens, client
secrets, raw usernames, private account IDs, or credentials.

Used in Milestone 8 to audit all remaining Trading XML operations across the codebase,
validate authenticated account identity parity between Trading XML ``GetUser`` and
modern REST Commerce Identity ``getUser``, and verify migration safety.

Examples:
  # Run offline synthetic identity parity verification (no network, no credentials):
  uv run python scripts/validate_legacy_fields_parity.py

  # Run explicit synthetic demonstration mode in JSON format:
  uv run python scripts/validate_legacy_fields_parity.py --synthetic-demo --format json

  # Display the complete remaining Trading XML operations audit matrix:
  uv run python scripts/validate_legacy_fields_parity.py --audit-matrix

  # Compare custom local XML and REST identity fixtures offline:
  uv run python scripts/validate_legacy_fields_parity.py \
    --xml-fixture path/to/get_user.xml --rest-fixture path/to/identity_user.json
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from custom_components.ebay.parsers.identity_parity import (  # noqa: E402
    MAX_IDENTITY_PAGES,
    compare_identity_snapshots,
    normalize_graphql_user,
    normalize_rest_identity_get_user,
    normalize_trading_xml_get_user,
    render_identity_parity_json,
    render_identity_parity_markdown,
    sanitize_identity_parity_report,
    validate_all_identity_contract_probes,
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
def get_synthetic_get_user_xml() -> str:
    """Return a realistic GetUser XML fixture."""
    return """<?xml version="1.0" encoding="utf-8"?>
<GetUserResponse xmlns="urn:ebay:apis:eBLBaseComponents">
  <Ack>Success</Ack>
  <User>
    <UserID>synthetic_seller_test</UserID>
    <FeedbackScore>245</FeedbackScore>
    <PositiveFeedbackPercent>100.0</PositiveFeedbackPercent>
    <RegistrationDate>2021-04-12T10:00:00.000Z</RegistrationDate>
    <Site>US</Site>
    <Status>Confirmed</Status>
    <SellerInfo>
      <SellerBusinessType>Private</SellerBusinessType>
    </SellerInfo>
  </User>
</GetUserResponse>
"""


def get_synthetic_rest_identity_json() -> str:
    """Return a realistic REST Commerce Identity getUser JSON fixture."""
    return """{
  "userId": "urn:ebay:user:1234567890",
  "username": "synthetic_seller_test",
  "accountType": "INDIVIDUAL",
  "registrationMarketplaceId": "EBAY_US"
}
"""


# ---------------------------------------------------------------------------
# Master Trading XML Operations & Dependencies Audit Matrix
# ---------------------------------------------------------------------------
LEGACY_OPERATIONS_INVENTORY: tuple[dict[str, Any], ...] = (
    {
        "operation": "GetUser",
        "codebase_callsite": "custom_components/ebay/clients/feedback.py:35",
        "purpose": "Resolve public username for REST Feedback API (GET /buy/feedback/v1/...)",
        "fields_consumed": ("User/UserID (public username)",),
        "modern_candidate": "REST Commerce Identity getUser (GET /commerce/identity/v1/user/)",
        "scope_required": "https://api.ebay.com/oauth/api_scope/commerce.identity.readonly",
        "milestone": "M8",
        "status": "Validated offline; production cutover blocked pending identity scope & multi-account policy",
        "baseline_provider": "hybrid",
    },
    {
        "operation": "GetMyeBayBuying",
        "codebase_callsite": "custom_components/ebay/section_fetch.py:354",
        "purpose": "Fetch buyer watched items (WatchList) and buyer bids (BidList)",
        "fields_consumed": ("WatchList", "BidList"),
        "modern_candidate": "GraphQL userWatchList / userBiddingActivity",
        "scope_required": "buy.watchlist.read, buy.bidding.read (speculative/unreleased)",
        "milestone": "M1 (Watchlist), M4 (Bidding)",
        "status": "Validated offline; production cutover blocked by OAuth scope unavailability",
        "baseline_provider": "trading_xml",
    },
    {
        "operation": "GetMyeBaySelling",
        "codebase_callsite": "custom_components/ebay/section_fetch.py:369",
        "purpose": "Fetch active seller listings (ActiveList) and ended listings (SoldList, UnsoldList)",
        "fields_consumed": ("ActiveList", "SoldList", "UnsoldList"),
        "modern_candidate": "GraphQL sellerListingSearch + REST Sell Fulfillment getOrders",
        "scope_required": "sell.fulfillment.readonly (granted), GraphQL sellerListingSearch scope (unreleased)",
        "milestone": "M5 (ActiveList), M7 (Sold/Unsold)",
        "status": "Validated offline; production cutover blocked by GraphQL seller scope unavailability",
        "baseline_provider": "trading_xml",
    },
    {
        "operation": "GetSellerList",
        "codebase_callsite": "custom_components/ebay/section_fetch.py:407",
        "purpose": "Fetch view counts (HitCount) per active item as fallback when analytics traffic report lacks data",
        "fields_consumed": ("ItemArray/Item/HitCount",),
        "modern_candidate": "REST Sell Analytics getTrafficReport (GET /sell/analytics/v1/traffic_report)",
        "scope_required": "https://api.ebay.com/oauth/api_scope/sell.analytics.readonly",
        "milestone": "M5 / M8",
        "status": "Optional fallback; REST Analytics traffic report is preferred modern primary",
        "baseline_provider": "trading_xml (fallback)",
    },
    {
        "operation": "GetBestOffers",
        "codebase_callsite": "custom_components/ebay/section_fetch.py:427",
        "purpose": "Fetch seller active best offer counts per listing",
        "fields_consumed": ("BestOfferArray/BestOffer", "Item/BestOfferDetails"),
        "modern_candidate": "REST Sell Negotiation findBestOffers (GET /sell/negotiation/v1/offer)",
        "scope_required": "https://api.ebay.com/oauth/api_scope/sell.inventory (or negotiation scope)",
        "milestone": "M6",
        "status": "Validated offline; production cutover blocked pending negotiation scope alignment",
        "baseline_provider": "trading_xml",
    },
    {
        "operation": "GetFeedback",
        "codebase_callsite": "N/A (Not called in codebase)",
        "purpose": "Legacy feedback score/entries lookup",
        "fields_consumed": (),
        "modern_candidate": "REST Feedback API (GET /buy/feedback/v1/feedback_rating_summary)",
        "scope_required": "https://api.ebay.com/oauth/api_scope/commerce.feedback",
        "milestone": "M8 Audit",
        "status": "Already REST-native in production! (Feasibility matrix corrected; no migration required)",
        "baseline_provider": "rest (summary/items) + hybrid (username)",
    },
    {
        "operation": "GetMemberMessages",
        "codebase_callsite": "N/A (Not called in codebase)",
        "purpose": "Legacy buyer/seller member messages",
        "fields_consumed": (),
        "modern_candidate": "REST Commerce Messages API (GET /commerce/message/v1/conversation)",
        "scope_required": "https://api.ebay.com/oauth/api_scope/commerce.message",
        "milestone": "M8 Audit",
        "status": "Already REST-native in production! (No XML dependency; no migration required)",
        "baseline_provider": "rest",
    },
)


def render_audit_matrix_markdown() -> str:
    """Render the master legacy operations inventory and feasibility audit matrix."""
    lines: list[str] = [
        "# Master eBay Legacy Operations & Dependencies Audit Matrix (Milestone 8)",
        "",
        "| Operation | Production Call Site | Purpose & Fields | Modern Candidate | Baseline Provider | Status |",
        "| :--- | :--- | :--- | :--- | :--- | :--- |",
    ]
    for row in LEGACY_OPERATIONS_INVENTORY:
        op = row["operation"]
        callsite = row["codebase_callsite"]
        purpose = row["purpose"]
        modern = row["modern_candidate"]
        provider = row["baseline_provider"]
        status = row["status"]
        lines.append(
            f"| `{op}` | `{callsite}` | {purpose} | {modern} | `{provider}` | {status} |"
        )

    lines.append("")
    lines.append("## Architectural Findings & Corrections")
    lines.append("")
    lines.append("1. **`GetFeedback` & `GetMemberMessages` Ground Truth:**")
    lines.append(
        "   - Early planning documents incorrectly categorized `GetFeedback` and `GetMemberMessages` as missing modern replacements."
    )
    lines.append(
        "   - Codebase audit confirms neither operation is called in production."
    )
    lines.append(
        "   - Feedback integrates natively with REST Feedback (`/buy/feedback/v1`), using Trading XML `GetUser` solely for username lookup."
    )
    lines.append(
        "   - Messages integrates natively with REST Messages (`/commerce/message/v1/conversation`)."
    )
    lines.append(
        "2. **Authenticated Account Identity (`GetUser` vs. REST Identity `getUser`):**"
    )
    lines.append(
        "   - Trading XML `GetUser` returns `UserID`, which represents the public handle (username)."
    )
    lines.append(
        "   - REST Identity `getUser` returns `userId` (opaque immutable ID) and `username` (public handle)."
    )
    lines.append(
        "   - The REST Feedback API requires the public username in `user_id` query parameters; substituting REST `userId` causes API errors."
    )
    lines.append(
        "   - REST Identity requires scope `https://api.ebay.com/oauth/api_scope/commerce.identity.readonly`, which is not granted to existing users."
    )
    lines.append("3. **Production Isolation:**")
    lines.append(
        "   - Milestone 8 is strictly read-only validation. Production Home Assistant coordinators and entities remain unchanged."
    )
    return "\n".join(lines)


def run_synthetic_parity_verification() -> dict[str, Any]:
    """Execute offline synthetic verification without credentials or network access."""
    xml_data = get_synthetic_get_user_xml()
    rest_data = get_synthetic_rest_identity_json()

    xml_snap = normalize_trading_xml_get_user(xml_data)
    rest_snap = normalize_rest_identity_get_user(rest_data)

    return compare_identity_snapshots(xml_snap, rest_snap)


# ---------------------------------------------------------------------------
# CLI Argument Parsing & Entrypoint
# ---------------------------------------------------------------------------
def _validate_cli_max_pages(val: str) -> int:
    """Validate max-pages CLI argument."""
    try:
        parsed = int(val)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"max-pages must be an integer, got {val!r}"
        ) from exc
    try:
        return validate_max_pages(parsed)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def build_parser() -> argparse.ArgumentParser:
    """Build CLI argument parser."""
    parser = argparse.ArgumentParser(
        description="eBay Remaining Legacy Operations & Identity Parity Validator (Milestone 8)",
    )
    parser.add_argument(
        "--xml-fixture",
        type=Path,
        help="Path to local GetUser XML response fixture file",
    )
    parser.add_argument(
        "--rest-fixture",
        type=Path,
        help="Path to local REST Commerce Identity getUser JSON response fixture file",
    )
    parser.add_argument(
        "--graphql-fixture",
        type=Path,
        help="Path to local GraphQL viewer/user JSON response fixture file",
    )
    parser.add_argument(
        "--synthetic-demo",
        action="store_true",
        help="Run explicit synthetic demonstration mode (zero network, zero credentials)",
    )
    parser.add_argument(
        "--audit-matrix",
        action="store_true",
        help="Display the master remaining legacy operations and dependencies audit matrix",
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="Attempt live validation against eBay API (offline harness: returns NOT_IMPLEMENTED)",
    )
    parser.add_argument(
        "--format",
        choices=["markdown", "json"],
        default="markdown",
        help="Output format (default: markdown)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="File path to write report output",
    )
    parser.add_argument(
        "--max-pages",
        type=_validate_cli_max_pages,
        default=MAX_IDENTITY_PAGES,
        help=f"Maximum pages to traverse (1..{MAX_IDENTITY_PAGES}, default: {MAX_IDENTITY_PAGES})",
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        help="Reserved / non-functional for Milestone 8 (live validation is not implemented)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entrypoint."""
    parser = build_parser()
    args = parser.parse_args(argv)

    # Verify built-in GraphQL probes
    validate_all_identity_contract_probes()

    if args.audit_matrix:
        if args.format == "json":
            matrix_str = json.dumps(LEGACY_OPERATIONS_INVENTORY, indent=2)
        else:
            matrix_str = render_audit_matrix_markdown()
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(matrix_str, encoding="utf-8")
            print(f"Audit matrix written to {args.output}")
        else:
            print(matrix_str)
        return 0

    if args.live:
        sys.stderr.write(
            "Live identity validation is not implemented for Milestone 8 (strictly offline validation).\n"
        )
        return 2

    if args.xml_fixture or args.rest_fixture or args.graphql_fixture:
        if not (args.xml_fixture and (args.rest_fixture or args.graphql_fixture)):
            sys.stderr.write(
                "Error: When using custom fixtures, --xml-fixture and at least one modern fixture "
                "(--rest-fixture or --graphql-fixture) must be provided.\n"
            )
            return 1

        if not args.xml_fixture.is_file():
            sys.stderr.write(f"Error: XML fixture file not found: {args.xml_fixture}\n")
            return 1

        xml_snap = normalize_trading_xml_get_user(
            args.xml_fixture.read_text(encoding="utf-8")
        )

        if args.rest_fixture:
            if not args.rest_fixture.is_file():
                sys.stderr.write(
                    f"Error: REST fixture file not found: {args.rest_fixture}\n"
                )
                return 1
            mod_snap = normalize_rest_identity_get_user(
                args.rest_fixture.read_text(encoding="utf-8")
            )
        else:
            assert args.graphql_fixture is not None
            if not args.graphql_fixture.is_file():
                sys.stderr.write(
                    f"Error: GraphQL fixture file not found: {args.graphql_fixture}\n"
                )
                return 1
            mod_snap = normalize_graphql_user(
                args.graphql_fixture.read_text(encoding="utf-8")
            )

        report = compare_identity_snapshots(xml_snap, mod_snap)
    else:
        # Default synthetic demo
        report = run_synthetic_parity_verification()

    sanitized = sanitize_identity_parity_report(report)

    if args.format == "json":
        output_str = render_identity_parity_json(sanitized)
    else:
        output_str = render_identity_parity_markdown(sanitized)

    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(output_str, encoding="utf-8")
        print(f"Report written to {args.output}")
    else:
        print(output_str)

    # In synthetic demo mode, exit 0 if username matches and no errors
    if sanitized.get("username_matches"):
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
