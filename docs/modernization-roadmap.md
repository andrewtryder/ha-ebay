# eBay Modernization Roadmap

This document tracks the multi-milestone engineering roadmap for modernizing eBay API access, OAuth onboarding, and reliability in [`andrewtryder/ha-ebay`](https://github.com/andrewtryder/ha-ebay).

---

## Core Architectural Principles

1. **Strictly Read-Only Monitoring**: The integration never places bids, purchases items, creates/updates/ends listings, responds to offers, sends messages, ships orders, issues refunds, or executes GraphQL mutations.
2. **Backward Compatibility**: Existing config entries, credentials, entity IDs, unique IDs, sensor states/attributes, binary sensors, buttons, calendars, event entities, and automation event payloads are preserved.
3. **Fail-Closed Partial-Failure Safety**: Unavailable or failed API fields are never silently replaced with `0`, `False`, or empty lists `[]`. Incomplete responses suppress diff-based removal/outbid/won/lost/sold/unsold events.
4. **Comprehensive Milestone Packages**: From Milestone 6 onward, modernization work is delivered as comprehensive, self-contained packages (research, contract validation, normalization, regression tests, migration readiness, documentation, and CI) rather than fragmented across partial PRs.

---

## Milestone Status Overview

| Milestone | Title | Branch | PR | Status |
|---|---|---|---|---|
| **Pre-Phase** | Simplified OAuth Onboarding (BYO Credentials) | `feat/simplify-ebay-oauth-onboarding` | [#92](https://github.com/andrewtryder/ha-ebay/pull/92) | **Merged** |
| **Milestone 0** | Complete GraphQL Phase 0 & Resolve OAuth Lifecycle Issues | `feat/ebay-graphql-transport` | [#94](https://github.com/andrewtryder/ha-ebay/pull/94) | **Merged** (`60f656f`) |
| **Milestone 1** | Safe Live eBay API Contract & Parity Test Harness | `feat/ebay-live-api-validation` | [#95](https://github.com/andrewtryder/ha-ebay/pull/95) | **Completed / Merged** (`14ce43f`; Live Run Complete, GraphQL Blocker Documented) |
| **Milestone 2** | Implement GraphQL Watchlist Normalization | `feat/ebay-graphql-watchlist` | TBD | **BLOCKED / DEFERRED** (GraphQL resolver `FORBIDDEN`; Trading XML authoritative) |
| **Milestone 3** | Migrate Watchlist Carefully | `feat/ebay-watchlist-cutover` | TBD | Planned (Pending M2 authorization resolution) |
| **Milestone 4** | Auction & Bidding Modernization Preparation & Parity Harness | `feat/ebay-auction-validation` | [#97](https://github.com/andrewtryder/ha-ebay/pull/97) | **Merged** (`4c2f394`) |
| **Milestone 5** | Active Seller Listings Normalization and Parity Validation | `feat/ebay-seller-listings-validation` | [#98](https://github.com/andrewtryder/ha-ebay/pull/98) | **Merged** (`4ac5c80`) |
| **Tooling & Foundation** | Modernize uv Tooling & Prepare Existing-User Migration | `chore/uv-ha-2026-10-migration-foundation` | [#99](https://github.com/andrewtryder/ha-ebay/pull/99) | **Merged** (`e0a957b`) |
| **Milestone 6** | Best Offers & Negotiation History Modernization Package | `feat/ebay-offers-modernization` | PR | **Completed** (Read-only validation package completed; production migration blocked pending live entitlement and parity evidence) |
| **Milestone 7** | Sold / Unsold Listing Classification Modernization Package | `feat/ebay-sold-unsold-modernization` | TBD | **Next Planned Workstream** |
| **Milestone 8** | Remaining Legacy Fields & API Operations Package | `feat/ebay-legacy-fields-modernization` | TBD | Planned |
| **Milestone 9** | Account Identity & Multiple eBay Accounts | `feat/ebay-multiple-accounts` | TBD | Planned |
| **Milestone 10** | One-Click eBay OAuth Architecture Assessment | Design deliverable | TBD | Planned |
| **Milestone 11** | Reliability, Quotas, & Compatibility Hardening | `feat/ebay-modern-api-reliability` | TBD | Planned |
| **Milestone 12** | End-to-End Home Assistant Testing & Release | `test/ebay-end-to-end-release` | TBD | Planned |

---

## Verified API & Lifecycle Findings

### 1. OAuth & Home Assistant Config-Entry Lifecycle (Pre-Phase & Milestone 0)
- **Home Assistant 2026.12 Reload Compatibility**: Integrations registering `entry.add_update_listener(...)` must use `async_update_and_abort()` during reauthorization rather than `async_update_reload_and_abort()` to avoid deprecation warnings and duplicate reloads.
- **Snapshot-Based Update Listener (`_loaded_config_snapshot`)**:
  - `OAuth2Session.async_ensure_token_valid()` and forced token refreshes persist updated `token` dicts via `hass.config_entries.async_update_entry()`, which invokes all registered config-entry update listeners.
  - Comparing only `entry.options` is insufficient because reauthorization (`_finish_reauth()`) can update `client_id`, `client_secret`, `runame`, `environment`, `site_id`, `auth_implementation`, `oauth_scopes`, or `refresh_token` while leaving `entry.options` unchanged.
  - `_async_update_listener` compares `_loaded_config_snapshot(entry)` against the snapshot captured at setup (`coordinator._loaded_config`), excluding the **entire** OAuth `token` dictionary (`access_token`, `refresh_token`, `expires_in`, `expires_at`) and in-flight `pending_options` drafts so routine OAuth renewals—even if eBay rotates `refresh_token`—trigger zero reloads.
  - Explicit user-completed reauthorizations increment a nonsecret `reauth_generation` marker in `EbayConfigFlow._finish_reauth()`. Any completed reauthorization (even with unchanged credentials and options), credential/marketplace/scope change, or committed option change triggers a single reload, while legacy entries without `reauth_generation` default cleanly to generation `0`.
- **Home Assistant Version Baseline (2026.10.0+)**:
  - Declared integration baseline is Home Assistant `2026.10.0+` (requires Python `>=3.14.2`).
  - `ConfigEntry.async_start_reauth_if_available(hass)` is standard across supported Home Assistant versions (2026.10.0+). Forced refresh error handling uses it directly with defensive fallback.
- **REST Invariant**: `RestClientMixin._clear_access_token()` strictly clears in-memory cached token state and never writes to the Home Assistant config entry.

### 2. eBay GraphQL Transport (Milestone 0)
- **Production Endpoint**: `https://graphqlapi.ebay.com/graphql` (**[OFFICIAL SPEC]**).
- **Headers**: Requires `Authorization: Bearer <token>`, `Content-Type: application/json`, and `X-EBAY-C-MARKETPLACE-ID` (**[OFFICIAL SPEC]**).
- **Error Categories**: eBay documents `APPLICATION`, `BUSINESS`, and `REQUEST` under `errors[].extensions.category` ([Using eBay GraphQL APIs](https://developer.ebay.com/develop/guides/sell/using-ebay-graphql-apis)). `SYSTEM` is treated only as a defensive fallback.
- **Schema Introspection**: Disabled on eBay's GraphQL endpoint (`__schema` / `__type` queries are unsupported).
- **Read-Only Enforcement**: `validate_read_only_query()` strips string literals and comments, parses top-level definitions, enforces a single `query` operation (plus optional fragments), rejects `mutation` and `subscription` definitions anywhere in the document, and validates `operation_name` alignment before network dispatch.

### 3. Watchlist Contract & Parity Harness (Milestone 1)
- **Milestone 1 Completion & Merged PR**:
  - Implemented on `feat/ebay-live-api-validation` and merged into `main` ([#95](https://github.com/andrewtryder/ha-ebay/pull/95), commit `14ce43f`).
  - Safe, local, opt-in contract and parity validation harness is complete and verified against Home Assistant `2026.10.0`.
- **Explicit External Authorization Blocker Documented**:
  - Live authenticated production testing confirmed that `query Handshake { __typename }` succeeded (HTTP 200) and `userWatchList { watchListCount watchListMaximum }` passed schema validation (HTTP 200), but the resolver returned `errorCode: "FORBIDDEN"`.
  - Required OAuth scope or developer application entitlement is unknown; candidate scope `buy.watchlist.read` remains unverified.
  - Existing Trading XML `GetMyeBayBuying` remains the sole authoritative production data source; Watchlist monitoring remains fully operational.
  - Milestone 2 (GraphQL Watchlist migration) is deferred and blocked until eBay's authorization requirements are identified.
- **Read-Only Opt-In Validation Tool**:
  - Added [`custom_components/ebay/parsers/watchlist_parity.py`](../custom_components/ebay/parsers/watchlist_parity.py) and [`scripts/validate_watchlist_parity.py`](../scripts/validate_watchlist_parity.py).
  - Makes **zero network calls** unless `--live` is explicitly passed.
  - Compares all 10 monitored Watchlist fields using the 6 roadmap classifications (`exact_match`, `equivalent_after_normalization`, `different_semantics`, `missing_field`, `unverified`, `unsupported`).
  - Fails closed (`status: "inconclusive"`) when either XML or GraphQL is truncated, partial, contradictory, or unreconciled.

### 4. Auction & Bidding Modernization Preparation (Milestone 4)
- **Modern API Feasibility Research (Milestones 2–8)**:
  - Documented modern replacements across all remaining roadmap milestones in [`docs/api-migration-feasibility.md`](api-migration-feasibility.md).
  - Identified that Trading XML `GetMyeBayBuying` (`BidList`) is designated for replacement by eBay GraphQL `userBiddingActivity` / `itemBiddingActivity` (Buy Auction API) or Buy Offer REST API (`/buy/offer/v1_beta/bidding/{item_id}`).
  - Documented that `GetMyeBayBuying` (`LostList`) is deprecated with no modern replacement, while `GetFeedback` and `GetMemberMessages` have no direct modern equivalents.
- **Read-Only Auction & Bidding Parity Validation Module**:
  - Added [`custom_components/ebay/parsers/bidding_parity.py`](../custom_components/ebay/parsers/bidding_parity.py) and CLI harness [`scripts/validate_bidding_parity.py`](../scripts/validate_bidding_parity.py).
  - Strictly read-only: performs **zero network calls** unless `--live` is explicitly passed.
  - Compares all 12 monitored bidding fields with full error isolation and privacy sanitization.
  - Merged into `main` ([#97](https://github.com/andrewtryder/ha-ebay/pull/97), commit `4c2f394`).

### 5. Active Seller Listings Normalization and Parity Validation (Milestone 5)
- **Active Seller Listings Architecture & Identity Analysis**:
  - Researched eBay modern listing representations across GraphQL (`sellerListingSearch`), Sell Inventory REST API, and Sell Fulfillment REST API.
  - Established that Sell Inventory API records operate on SKUs and cannot be assumed to constitute a full census of active listings without proof.
- **Read-Only Seller Listings Parity Validation Module**:
  - Added [`custom_components/ebay/parsers/seller_parity.py`](../custom_components/ebay/parsers/seller_parity.py) and CLI harness [`scripts/validate_seller_parity.py`](../scripts/validate_seller_parity.py).
  - Evaluates all 16 monitored seller fields with fail-closed pagination and cross-page total consistency checks.
  - Merged into `main` ([#98](https://github.com/andrewtryder/ha-ebay/pull/98), commit `4ac5c80`).

### 6. Development Tooling Modernization & Migration Policy Foundation
- **Unified uv & pyproject.toml Environment**:
  - Development dependencies, pytest options, coverage requirements (branch coverage $\ge 90\%$), and Ruff configuration are consolidated into `pyproject.toml` and locked in `uv.lock`.
  - Pytest harness pinned to Home Assistant `2026.10.0` using `pytest-homeassistant-custom-component==0.13.371`.
  - Removed obsolete `requirements-*.txt` files and `pytest.ini`.
  - Refactored `.github/workflows/test.yml` into a single, high-speed Python test job using `astral-sh/setup-uv` pinned by commit SHA.
- **Existing-User Migration Policy Layer**:
  - Added [`custom_components/ebay/migration_policy.py`](../custom_components/ebay/migration_policy.py) and architectural guide [`docs/existing-user-migration.md`](existing-user-migration.md).
  - Implemented 8 strict fail-closed readiness gates for evaluating candidate modern API providers without modifying production polling coordinators.
  - Proved contract preservation for entity unique IDs, options, pinned items, price targets, and automation event payloads.
  - Designed future user-controlled migration rollout with shadow validation and instant rollback to Trading XML.

### 7. Best Offers and Negotiation History Validation (Milestone 6)
- **Authoritative API Research & Contract Discovery**:
  - Researched eBay GraphQL Offer API (`sellerNegotiationHistory`, `buyerNegotiationHistory`, released October 26, 2026).
  - Established critical distinctions between incoming Best Offer negotiations, REST Sell Negotiation discount eligibility (`find_eligible_items`), and REST Sell Inventory listing-offer objects (`getOffer`).
  - Preserved existing production baseline: seller `GetBestOffers` active offer counts (`active_offers_by_item_id()`) feeding `{entry_id}_selling_total_offers`, per-item `offers` sensors, and `offer_received` events.
  - Maintained zero production buyer offer polling to prevent exposing unfinished buyer entities.
- **Read-Only Offers Parity Validation Module & Offline Harness**:
  - Added [`custom_components/ebay/parsers/offers_parity.py`](../custom_components/ebay/parsers/offers_parity.py) and CLI harness [`scripts/validate_offers_parity.py`](../scripts/validate_offers_parity.py).
  - Built typed, immutable models (`NormalizedNegotiation`, `OfferCollectionResult`) and 8 strictly read-only GraphQL candidate probes.
  - Evaluates all monitored offer fields across the 6-state parity taxonomy with fail-closed pagination and loop detection.
  - Preserved privacy protections: zero tokens, secrets, client credentials, buyer usernames, or private offer prices in reports.
  - Milestone status: **Read-only validation package completed; production migration blocked pending live entitlement and parity evidence.** Next planned workstream: **Milestone 7 (Sold/Unsold Listing Classification Package)**.
