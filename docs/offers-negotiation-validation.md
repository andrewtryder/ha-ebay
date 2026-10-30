# eBay Best Offers & Negotiation History API Validation Specification

> **Milestone:** Milestone 6 — Comprehensive Best Offers & Negotiation History Modernization
> **Repository:** `andrewtryder/ha-ebay`
> **Status:** **Read-only validation package completed; production migration blocked pending live entitlement and parity evidence.**
> **Policy:** Strictly read-only monitoring; no mutations (bids, offers sent/accepted/declined/countered, listings, messages, orders, or refunds); fail-closed partial data safety; Trading XML remains authoritative in production.

---

## 1. Executive Summary & Verification Classifications

Milestone 6 establishes authoritative API research, canonical data models, isolated read-only GraphQL contract probes, safe legacy Trading XML baselines, fail-closed parity analysis, and an offline-first CLI validation harness for eBay Best Offers and negotiation history.

All technical statements and specifications in this document are strictly classified using the repository's verification taxonomy:

- **`[OFFICIAL SPEC]`**: Documented in official eBay documentation or OpenAPI/GraphQL schemas.
- **`[REPO_VERIFIED]`**: Independently verified by automated unit tests or codebase contracts in `ha-ebay`.
- **`[LIVE_VERIFIED]`**: Directly verified against live production eBay APIs using valid credentials.
- **`[OFFLINE_TESTED]`**: Verified through reproducible offline fixtures and synthetic harness tests.
- **`[UNVERIFIED]`**: Candidate operations, fields, or scopes documented by eBay but unverified in live integration testing.
- **`[UNSUPPORTED]`**: Features, operations, or fields not supported by eBay's read-only integration architecture.
- **`[BLOCKED]`**: Operations blocked due to missing permissions, unconfirmed entitlement, or unresolved resolver authorization.

### Status Banner

```
========================================================================================
M6: Read-only validation package completed; production migration blocked pending live
entitlement and parity evidence.
Next Planned Workstream: Milestone 7 — Sold / Unsold Listing Classification Package
========================================================================================
```

---

## 2. Official Source Documentation & API Contracts

Official eBay developer documentation references:

- [Modern APIs Migration Guide](https://developer.ebay.com/develop/guides/sell/modern-apis-migration-guide) **`[OFFICIAL SPEC]`**
- [Sell Offer API Reference](https://developer.ebay.com/develop/api/sell/sell_offer) **`[OFFICIAL SPEC]`**
- [Buy Offer API Reference](https://developer.ebay.com/develop/api/buy/buy_offer) **`[OFFICIAL SPEC]`**
- [Sell Negotiation API Reference](https://developer.ebay.com/develop/api/sell/negotiation_api) **`[OFFICIAL SPEC]`**
- [Sell API Call Limits](https://developer.ebay.com/develop/api/sell/api_call_limits) **`[OFFICIAL SPEC]`**
- [Buy API Call Limits](https://developer.ebay.com/develop/api/buy/api_call_limits) **`[OFFICIAL SPEC]`**
- [Trading XML GetBestOffers Reference](https://developer.ebay.com/devzone/xml/docs/reference/ebay/GetBestOffers.html) **`[OFFICIAL SPEC]`**
- Initial eBay GraphQL Offer API release: **October 26, 2026** **`[OFFICIAL SPEC]`**.

---

## 3. Operation & Architecture Distinctions

Milestone 6 resolves critical architectural boundaries between distinct eBay offer technologies:

### 3.1 Seller Best Offer Negotiation History
- **Candidate GraphQL Operation**: `sellerNegotiationHistory` **`[UNVERIFIED]`**.
- **Official Status**: Documented as a top-level query root in eBay's October 26, 2026 release notes **`[OFFICIAL SPEC]`**. However, all nested selection sets, input arguments (such as `limit`, `offset`, `after`), and pagination structure (`pageInfo`, `edges`, `nodes`) remain **exploratory candidate schemas** requiring live resolver verification **`[UNVERIFIED]`**.
- **Legacy XML Operation**: `GetBestOffers` (role `Seller`) **`[OFFICIAL SPEC]`** **`[REPO_VERIFIED]`**.
- **Role**: `Seller`.
- **Purpose**: Retrieves incoming and historical Best Offers submitted by buyers on the seller's active listings, along with seller counteroffers and status updates.

### 3.2 Buyer Best Offer Negotiation History
- **Candidate GraphQL Operation**: `buyerNegotiationHistory` **`[UNVERIFIED]`**.
- **Official Status**: Referenced in migration guides as a conceptual equivalent for buyer offer history, but is an **exploratory candidate operation name**; it does **NOT** constitute proof of live account entitlement or production resolver authorization **`[UNVERIFIED]`**.
- **Legacy XML Operation**: `GetMyeBayBuying` (`BestOfferList`) **`[OFFICIAL SPEC]`** **`[OFFLINE_TESTED]`**.
- **Role**: `Buyer`.
- **Purpose**: Retrieves outgoing Best Offers submitted by the authenticated buyer to sellers, including pending confirmation, counteroffers, and expiration.

### 3.3 Critical Distinction: REST Sell Negotiation API
- **Endpoint**: `GET /sell/negotiation/v1/find_eligible_items` **`[OFFICIAL SPEC]`**.
- **Distinction**: Identifies listings that are **eligible for seller-initiated discount offers to interested buyers**.
- **Important**: It does **NOT** provide Best Offer negotiation history or incoming buyer offer records **`[REPO_VERIFIED]`**.
- Any claim that `/sell/negotiation/v1/offer` or `find_eligible_items` provides complete Best Offer history is false and unverified.
- Furthermore, `POST /sell/negotiation/v1/send_offer_to_interested_buyers` is a **mutating operation and strictly prohibited** in `ha-ebay` **`[REPO_VERIFIED]`**.

### 3.4 Critical Distinction: REST Sell Inventory Listing Offers
- **Endpoints**: `GET /sell/inventory/v1/offer` / `getOffers` **`[OFFICIAL SPEC]`**.
- **Distinction**: Retrieves inventory listing-offer records that map merchant SKUs to marketplace listings and commercial terms.
- **Important**: Inventory offers are **NOT** buyer/seller negotiation objects. Inventory `offerId` values must never be confused with negotiation offer IDs **`[REPO_VERIFIED]`**.

---

## 4. Current Production Baseline Audit

An audit of `ha-ebay` production data-fetching and entity generation confirms:

1. **Seller Offers (`SECTION_SELLING`)**:
   - Polled conditionally during active selling list refresh in `custom_components/ebay/section_fetch.py` using Trading XML `GetBestOffers` (`DetailLevel=ReturnAll`, `BestOfferStatus=Active`) **`[REPO_VERIFIED]`**.
   - Parsed by `active_offers_by_item_id()` in `trading_items.py`, counting offers with status `Active`, `Countered`, or `Pending` **`[REPO_VERIFIED]`**.
   - Feeds summary sensor `{entry_id}_selling_total_offers` (`key="selling_total_offers"`).
   - Feeds per-item selling sensor suffix `offers` (`f"{entry_id}_selling_{item_id}_offers"`).
   - Triggers Home Assistant bus event `offer_received` (part of `SELLING_EVENT_TYPES`) when per-item offers increase **`[REPO_VERIFIED]`**.

2. **Buyer Offers (`SECTION_BUYING`)**:
   - In production, `GetMyeBayBuying` requests only `WatchList` and `BidList` **`[REPO_VERIFIED]`**.
   - `BestOfferList` is **NOT** part of standard Home Assistant production polling.
   - Milestone 6 leaves buyer production polling unchanged and exposes zero unfinished buyer offer entities to production users **`[REPO_VERIFIED]`**.

3. **Migration Policy Baseline**:
   - In `custom_components/ebay/migration_policy.py`, `FeatureKey.OFFERS` represents the seller offers domain.
   - Default baseline provider: `ProviderType.TRADING_XML` **`[REPO_VERIFIED]`**.
   - Expected unique IDs: `f"{entry_id}_selling_total_offers"`.
   - Expected event types: `"offer_received"`.

---

## 5. Canonical Read-Only Negotiation Models

Milestone 6 introduces typed, immutable canonical data models in `custom_components/ebay/parsers/offers_parity.py`:

```python
class NegotiationRole(StrEnum):
    SELLER = "seller"
    BUYER = "buyer"


class NegotiationStatus(StrEnum):
    ACTIVE = "active"
    PENDING = "pending"
    COUNTERED = "countered"
    ACCEPTED = "accepted"
    DECLINED = "declined"
    EXPIRED = "expired"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class NormalizedNegotiation:
    negotiation_id: str | None
    item_id: str | None
    role: NegotiationRole
    status: NegotiationStatus
    raw_status: str | None
    price: float | None
    currency: str | None
    quantity: int | None
    created_at: datetime | None = None
    expires_at: datetime | None = None
    updated_at: datetime | None = None
    counteroffer_price: float | None = None
    counteroffer_currency: str | None = None
    counteroffer_sequence: int | None = None
    initiator_role: str | None = None
    provider: str = "unknown"
    provenance: dict[str, Any] = field(default_factory=dict)
```

### Monitored Fields Parity Matrix

| Field | Source XML | Candidate GraphQL | Parity Classification | Notes |
|---|---|---|---|---|
| `selling_total_offers` | Sum of active offers | Sum of active negotiations | `exact_match` | Total active/pending/countered offers across listings |
| `buyer_total_offers` | Sum of active buyer offers | Sum of active buyer negotiations | `exact_match` | Buyer submitted active negotiations |
| `items_with_offers` | Count of ItemIDs with offers | Count of ItemIDs with offers | `exact_match` | Listings with active negotiation records |
| `active_offers_by_item_id` | Map of `item_id -> count` | Map of `item_id -> count` | `exact_match` | Identical counting rule: `Active`, `Pending`, `Countered` |
| `negotiation_id` | `BestOfferID` | `negotiationId` / `offerId` | `exact_match` | Primary offer identifier |
| `item_id` | `Item.ItemID` | `legacyItemId` / `itemId` | `exact_match` | 12-digit eBay listing ItemID |
| `role` | `ItemBestOffers.Role` | `role` | `exact_match` | `seller` or `buyer` |
| `status` | `BestOffer.Status` | `status` | `equivalent_after_normalization` | Canonical mapping across raw status codes |
| `price` | `BestOffer.Price` | `currentOffer.price.value` | `exact_match` | Normalized floating-point currency value |
| `currency` | `BestOffer.Price.@currencyID` | `currentOffer.price.currency` | `exact_match` | ISO 4217 uppercase currency code |
| `quantity` | `BestOffer.Quantity` | `currentOffer.quantity` | `exact_match` | Integer offer quantity |
| `created_at` | `BestOffer.CreationTime` | `creationDate` | `exact_match` | UTC timezone-aware datetime |
| `expires_at` | `BestOffer.ExpirationTime` | `expirationDate` | `exact_match` | UTC timezone-aware datetime |
| `counteroffer_price` | `CounterOfferPrice` | `counterOffer.price.value` | `exact_match` | Counteroffer price when countered |

---

## 6. Built-in Candidate GraphQL Probes

Built-in probes in `OFFERS_CONTRACT_PROBES` are strictly read-only:

1. `typename_handshake`: `query Handshake { __typename }`
2. `seller_negotiation_total`: `query GetSellerNegotiationsTotal { sellerNegotiationHistory { total } }`
3. `seller_negotiation_count_alt`: `query GetSellerNegotiationsCountAlt { sellerNegotiationHistory { totalCount activeCount } }`
4. `seller_negotiation_paginated`: Probes `sellerNegotiationHistory(limit, offset)` with full negotiation selection set.
5. `seller_negotiation_alt_fields`: Probes candidate `sellerNegotiations { totalCount offers { ... } }`.
6. `buyer_negotiation_total`: `query GetBuyerNegotiationsTotal { buyerNegotiationHistory { total } }`
7. `buyer_negotiation_paginated`: Probes `buyerNegotiationHistory(limit, offset)` with buyer selection set.
8. `buyer_negotiation_alt_fields`: Probes candidate `buyerNegotiations { totalCount offers { ... } }`.

All candidate probes are validated via `validate_read_only_query()` and statically verified to reject any mutation or subscription.

---

## 7. Bounded Pagination & Completeness Engine

To protect Home Assistant against unbounded loops, partial responses, and false-positive parity claims, `normalize_graphql_offers_payload()`, `merge_graphql_offers_pages()`, and `compare_offers_snapshots()` implement a strict **fail-closed completeness contract**:

- **Transport vs Collection Completeness**: A successful `__typename` handshake verifies transport connectivity and token validity, but returns `is_complete=False` (`HANDSHAKE_ONLY`) because no negotiation records were retrieved.
- **Count-Only Operation Safety**: Summary count queries (e.g. `total`, `activeCount`) return reported metrics but return `is_complete=False` (`COUNT_ONLY_QUERY`) because no individual negotiation records were collected.
- **Empty Pages Safeguard**: An empty page sequence passed to merge functions returns `is_complete=False` (`NO_PAGES_PROVIDED`).
- **Pagination Metadata Requirement**: Responses lacking `pageInfo` or containing non-boolean `hasNextPage` values fail closed (`MISSING_PAGINATION_METADATA`, `MALFORMED_PAGE_INFO`).
- **Pending Continuation Safety**: Any collection whose final page declares `hasNextPage: True` (or non-false) fails closed as incomplete (`PENDING_CONTINUATION`).
- **Reconciliation of Declared Totals vs Census**: Total collected records must strictly equal the declared total count (`TOTAL_RECORDS_MISMATCH`). If no total is declared, census completeness cannot be proven (`UNVERIFIED_COLLECTION_CENSUS`).
- **Duplicate & Cycle Detection**: Duplicate page payloads trigger `PAGINATION_CYCLE_DETECTED`; duplicate negotiation IDs across pages trigger `DUPLICATE_OFFER_ID_ACROSS_PAGES`.
- **Pages After End Protection**: Additional pages received after `hasNextPage: false` trigger `PAGES_AFTER_END_DECLARED`.
- **Max Page Bounds**: Pinned to `max_pages=25` (configurable up to 25). Traversal beyond this limit halts and sets `is_truncated=True` (`MAX_PAGES_EXCEEDED`).
- **Strict Identity Set Parity**: Candidate and XML records must have exact identity matching (`xml_ids == cand_ids`). Any extra, missing, or duplicate negotiation IDs cause parity failure.
- **Empty Snapshot Parity (Anti-False-Positive)**: When both XML and Candidate snapshots have 0 records, aggregate counts match (`exact_match`), but all record-level fields are evaluated as `unverified`. The snapshot's overall status is classified as `inconclusive`, preventing false-positive pass claims when no records were available to verify.
- **Partial GraphQL Errors**: HTTP 200 responses containing GraphQL `errors[]` extract sanitized codes and fail closed (`is_complete=False`).

---

## 8. OAuth Scopes & Entitlement Readiness

### Distinction of Authorization Layers
1. **Documented Scope**: Appears in eBay developer documentation (e.g. `https://api.ebay.com/oauth/api_scope/sell.negotiation` for REST negotiation, `https://api.ebay.com/oauth/api_scope/buy.offer.auction` for REST Buy Offer).
2. **User Token Scope**: Granted to the user's specific OAuth token.
3. **App Entitlement**: eBay partner tier or developer account approval (REST Buy Offer is a **Limited Release API** requiring application approval).
4. **GraphQL Resolver Authorization**: GraphQL schema and operation-level authorization (`FORBIDDEN` errors on valid scopes).

### Invariants Maintained
- No guessed OAuth scopes added to `scopes.py` or user configuration flows.
- For candidate GraphQL operations where scope requirements remain unconfirmed, `scopes_contract_established=False` is retained.
- Synthetic fixtures verify implementation logic only; they do not constitute live entitlement evidence.

---

## 9. Privacy & Diagnostic Protection

`sanitize_offers_parity_report()` ensures zero diagnostic leaks, credential exposure, or private financial data leakage in validation reports:

- **Strict Default-Deny Schema**: Only eight permitted top-level keys (`role`, `overall_status`, `is_inconclusive`, `field_counts`, `field_comparisons`, `baseline_summary`, `candidate_summary`, `errors`, `active_offers_by_item_id`). All unexpected free-form keys, nested dictionaries, and debug objects are automatically stripped.
- **Strict Token & Secret Redaction**: Redacts `access_token`, `refresh_token`, `client_secret`, `client_id`, `runame`, `authorization`, `auth_code`, `api_key`, `raw_xml`, `raw_graphql`, `email`, `password`, `secret`.
- **Bearer Token Sanitization**: Recursively scrubs token patterns (`Bearer ...`, `v^1.1#...`, `refresh_token=...`) and email patterns from all report strings, diagnostics, and error messages.
- **Private Financial Data & Identifier Scrubbing**: Regex filters automatically redact private currency amounts (`USD 85.37`, `$85.37`, `85.37` → `[REDACTED_AMOUNT]`), 9-to-19-digit numerical identifiers (e.g. `123456789012` → `[REDACTED_ID]`), and markdown/HTML table injection payloads.
- **Message & PII Scrubbing**: Redacts free-text message bodies (`message`, `buyer_message`, `seller_message`), usernames/IDs (`buyer_username`, `seller_username`, `buyer_id`, `seller_id`), titles (`title`, `listing_title`), and feedback scores.
- **Default-Deny Diagnostic Notes & Error Mapping**: `field_comparisons[*].note` and `errors[*].diagnostic` completely discard arbitrary free-form strings. Comparison notes only allow internal count-only templates or fall back to canonical status descriptions (`GENERIC_FIELD_PARITY_NOTES`), and error diagnostics strictly map known symbolic codes or numeric codes to fixed generic messages (`GENERIC_DIAGNOSTIC_MESSAGES`), ensuring handles, word-form prices, and arbitrary XML tags or Ack values never reach JSON or Markdown reports.
- **Strict Pseudonym Format Validation**: In `active_offers_by_item_id`, keys are strictly validated against `^item_[0-9a-f]{12}$` and must not be raw decimal identifiers spoofing the prefix. Any unverified or raw identifiers are deterministically hashed via SHA-256 (`item_<sha256_12>`), preventing exposure of raw listing IDs or arbitrary item keys.
- **Deterministic SHA-256 Identifier Pseudonymization**: Rather than exposing raw listing IDs in summary maps, `active_offers_by_item_id` converts listing item IDs to deterministic 12-character SHA-256 pseudonyms (`item_<sha256_12>`). This preserves per-item count grouping structure in diagnostic reports without exposing sensitive production eBay listing identifiers.
- **Residual Privacy Limitations (Unsalted Pseudonyms)**: Because the 12-character SHA-256 pseudonyms are unsalted to allow deterministic cross-run correlation, an attacker with a known set of 12-digit numeric eBay item IDs could compute precomputed hash tables (rainbow tables) to reverse the pseudonyms. Therefore, reports also provide aggregate summaries (`items_with_offers`, `total_active_offers`), and diagnostic reports should not be shared across untrusted boundaries where listing identity must remain strictly private.

---

## 10. Offline-First CLI Validation Harness

`scripts/validate_offers_parity.py` provides an isolated testing harness:

- **Bounded Pagination Contract**: The CLI `--max-pages` parameter and all underlying functions (`validate_max_pages`, `merge_graphql_offers_pages`, `merge_trading_xml_seller_offers_pages`, `merge_trading_xml_buyer_offers_pages`, and `async_run_live_validation`) enforce an absolute limit between `1` and `25` pages. Values of `0`, negative numbers, or `> 25` fail closed before any network access.
- **Exact XML Page Reconciliation**: Multi-page Trading XML seller and buyer responses reconcile declared `TotalNumberOfPages`, `TotalNumberOfEntries`, and sequential `PageNumber` on every single page. Any contradictory page totals, missing pagination metadata, sequence gaps, or extra pages arriving after the declared terminal page trigger `CONTRADICTORY_PAGINATION_TOTALS`, `MISSING_PAGINATION_METADATA`, or `PAGES_AFTER_END_DECLARED` and fail closed (`is_complete=False`). Exact completion requires `len(pages) == declared_total_pages`.

```bash
# Offline synthetic verification (default, 0 network, 0 credentials):
uv run python scripts/validate_offers_parity.py

# Offline verification for buyer negotiations specifically:
uv run python scripts/validate_offers_parity.py --role buyer

# Explicit synthetic demonstration mode:
uv run python scripts/validate_offers_parity.py --synthetic-demo --role seller

# Offline comparison of local file fixtures:
uv run python scripts/validate_offers_parity.py \
  --xml-fixture tests/fixtures/offers.xml \
  --graphql-fixture tests/fixtures/negotiations.json

# Opt-in read-only live validation (explicit credentials required, bounded 1..25 pages):
uv run python scripts/validate_offers_parity.py --live --env-file .env.local --role seller --max-pages 5
```

### Synthetic Demo Limitations
The `--synthetic-demo` mode runs entirely locally against bundled synthetic fixtures. It validates parser logic, data normalization, parity comparison algorithms, diagnostic sanitization, and CLI rendering. It makes **zero network requests** and does **NOT** prove:
- Live eBay GraphQL Offer API endpoint availability or stability.
- Account entitlement or resolver authorization on user keysets.
- Field-level compatibility with live production eBay data.
- Live scope grant mechanics or token acceptance.

---

## 11. Production Migration Blockers & Next Workstream

### Explicit Migration Blockers
1. **Live GraphQL Entitlement**: eBay GraphQL Offer API (`sellerNegotiationHistory`, `buyerNegotiationHistory`) resolver authorization has not been confirmed on production keysets.
2. **Scope Contract Unestablished**: Verified OAuth scopes for GraphQL negotiation queries remain unconfirmed by eBay.
3. **Census & Pagination Reconciliation**: Complete census comparison against live high-volume seller accounts requires live testing.
4. **Buyer Production Scope**: Buyer offer monitoring remains excluded from production polling until formal approval.

### Conclusion & Next Workstream
**M6: Read-only validation package completed; production migration blocked pending live entitlement and parity evidence.**

**Next Planned Workstream**: **Milestone 7 — Sold / Unsold Listing Classification Modernization Package** (`feat/ebay-sold-unsold-modernization`).
