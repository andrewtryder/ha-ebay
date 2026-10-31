# Sold & Unsold Listing Classification Parity Validation (Milestone 7)

## 1. Executive Summary & Architectural Scope

This document specifies the research, data modeling, parity verification harness, and diagnostic privacy protections for **Milestone 7: Sold & Unsold Listing Classification Modernization** within the `ha-ebay` integration.

The primary mission of Milestone 7 is to modernize the classification of sold and unsold listings by evaluating modern eBay data sources—specifically **GraphQL `sellerListingSearch` ended listing queries** and **REST Sell Fulfillment API `getOrders`**—against the authoritative legacy **Trading XML `GetMyeBaySelling` (`SoldList` / `UnsoldList`)** baseline.

> [!IMPORTANT]
> **Production Protection Guarantee**: In accordance with the repository's migration policy, **no live production provider cutovers are performed in Milestone 7**. The Home Assistant runtime polling coordinators continue to use Trading XML `GetMyeBaySelling` as the authoritative source for sold and unsold listing counts and classifications. The modern classification parsers and validation tools execute strictly offline or in user-initiated, isolated, read-only diagnostic validation runs.

**Status Record**:
> **M7: Read-only classification validation completed; production migration blocked pending verified live authorization, complete listing census, and semantic parity.**

---

## 2. Legacy Trading XML Baseline Architecture

### 2.1 Production Ingestion and Entity Contracts **[REPOSITORY VERIFIED]** **[OFFLINE TESTED]**

In the current production architecture, Home Assistant polls seller listings using `GetMyeBaySelling` via Trading XML:
- **XML Operation**: `GetMyeBaySelling`
- **Containers**:
  - `SoldList`: Contains completed sales with `ItemArray/Item/ItemID` and `PaginationResult` (`TotalNumberOfPages`, `TotalNumberOfEntries`).
  - `UnsoldList`: Contains ended listings that did not sell, with `ItemArray/Item/ItemID` and `PaginationResult`.
- **Duration / Window**: Defaults to 60 days lookback (`DurationInDays=60`).
- **Precedence Rule**: If an `ItemID` appears in both `UnsoldList` and `SoldList` within the lookback window, the production implementation in `custom_components/ebay/section_fetch.py` grants **precedence to `SoldList`**:
  ```python
  # Production precedence contract:
  classification = {i_id: "unsold" for i_id in unsold_ids}
  for i_id in sold_ids:
      classification[i_id] = "sold"  # sold overwrites unsold
  ```
- **Dependent Home Assistant Entities**:
  - `sensor.ebay_seller_sold_items`: Reports count of unique items sold in window.
  - `sensor.ebay_seller_unsold_items`: Reports count of unique ended listings unsold.
  - `coordinator.data.selling_classification`: Dictionary mapping `ItemID -> "sold" | "unsold"`.
  - Incomplete data safeguarding: If pagination or response is incomplete, `events.py` guards against publishing erroneous zero counts.

---

## 3. Modern Candidate API Capabilities & Critical Boundaries

### 3.1 GraphQL `sellerListingSearch` (Ended Listings) **[OFFICIALLY DOCUMENTED]** **[OFFLINE TESTED]** **[LIVE UNVERIFIED]**

The eBay GraphQL seller listing search API provides candidate queries for seller listings:
- **Operation / Field**: `sellerListingSearch`
- **Result Structure**: Responses may return a flat list or officially documented grouped buckets:
  - `endedListings`: Listings whose listing period has elapsed or was ended early.
  - `activeListings`: Currently active listings available for purchase.
  - `scheduledListings`: Listings scheduled to start at a future time.
- **Key Fields**:
  - `itemId` / `listingId`: Listing identifier.
  - `status`: Lifecycle state (`ENDED`, `COMPLETED`, `CLOSED`, `ACTIVE`, `SCHEDULED`).
  - `quantitySold`: Number of items sold.
  - `quantity`: Total listing quantity.
  - `itemEndDate` / `endTime`: Listing end timestamp (ISO 8601).
- **Classification Rules**:
  - A listing with `quantitySold == 0` is classified as `UNSOLD` **only** when its lifecycle state is positively verified as `ENDED`.
  - Active, scheduled, or unknown lifecycle listings with `quantitySold == 0` are never classified as unsold.
  - If a GraphQL ended listing reports `quantitySold == 0` (claiming unsold) but REST Fulfillment reports a confirmed completed order for that same listing, the candidate outcome is marked as `AMBIGUOUS` with an error diagnostic rather than silently accepting either source.

### 3.2 REST Sell Fulfillment API `getOrders` **[OFFICIALLY DOCUMENTED]** **[REPOSITORY VERIFIED]** **[LIVE VERIFIED]**

The eBay REST Sell Fulfillment API provides authoritative order details for sales:
- **Endpoint**: `GET /sell/fulfillment/v1/order`
- **Scopes**: `https://api.ebay.com/oauth/api_scope/sell.fulfillment.readonly` or `sell.fulfillment`
- **Pagination Contract**: Uses **limit/offset pagination** (`limit` query parameter, default 50, maximum 200; `offset` query parameter, integer), returning `total: int`, `href`, `next`, and `prev`.
- **Date Window Support**: When no date filter is provided, the API defaults to orders created within the last 90 days. Using ISO timestamp range filters (`creationdate`, `lastmodifieddate`), historical orders can be retrieved for up to 2 years (730 days) in the past.

---

## 4. Addressing Fundamental Domain Misconceptions

To prevent architectural and data-integrity defects, the following seven misconceptions are explicitly corrected:

### Misconception 1: "Ended automatically means unsold"
- **Correction**: **False**. An ended listing is merely a listing whose lifecycle has terminated. A fixed-price item that sold out, an auction won by the highest bidder, and an item canceled or unsold all have lifecycle state `ENDED`.
- **Rule**: If `quantitySold > 0`, the item is `SOLD`. If `quantitySold == 0`, the item is `UNSOLD`. If `quantitySold` is missing or unverified, the outcome is `UNKNOWN`. Never infer `unsold` purely from lifecycle `ended`.

### Misconception 2: "Fulfillment orders are equivalent to a full sold/unsold listing census"
- **Correction**: **False**. Fulfillment orders only document sales transactions. An unsold listing generates **zero** orders. Therefore, the Fulfillment API cannot discover or verify unsold listings.

### Misconception 3: "Fulfillment uses cursor pagination"
- **Correction**: **False**. REST Sell Fulfillment API `getOrders` uses **limit/offset pagination** (`limit` and `offset` query parameters, returning integer `total`), unlike the GraphQL cursors (`after` / `pageInfo.endCursor`). `docs/api-migration-feasibility.md` line 160 has been corrected accordingly.

### Misconception 4: "Fulfillment order status and listing sale outcome are the same concept"
- **Correction**: **False**. An order's status describes logistical fulfillment (`NOT_STARTED`, `IN_PROGRESS`, `FULFILLED`) or payment status (`PAID`). A listing's sale outcome describes whether an eBay listing resulted in a sale (`SOLD`) or concluded without selling (`UNSOLD`). An order may be cancelled or refunded post-sale while the underlying listing remains classified as sold in historical reporting.

### Misconception 5: "Legacy classification and REST Fulfillment use identical date windows"
- **Correction**: **False**. Legacy Trading XML `GetMyeBaySelling` enforces a configurable duration window (typically 60 days, up to 90 days via `DurationInDays`). The REST Fulfillment API defaults to 90 days when unfiltered and supports historical queries up to 2 years (730 days) using ISO date filters. Date windows must be strictly aligned during parity comparisons to prevent false divergences.

### Misconception 6: "SKU inventory records can be substituted for verified eBay listing ItemIDs"
- **Correction**: **False**. The eBay Sell Inventory API uses merchant-defined SKUs, which are optional and do not map 1:1 to eBay listing ItemIDs (especially for multi-variation listings or sellers who do not use Inventory API). Legacy Home Assistant sensors and events are keyed on numeric `ItemID`.

### Misconception 7: "Official eBay documentation establishes authenticated GraphQL entitlement"
- **Correction**: **False**. Official eBay documentation describes public and partner GraphQL endpoints. However, live access requires specific account entitlement and OAuth scopes. As observed in Milestone 1 and Milestone 5, candidate GraphQL queries return `FORBIDDEN` or unauthenticated without partner onboarding. Documentation does **not** equal live entitlement.

---

## 5. Strict Canonical Models & Data Structures

Milestone 7 introduces typed, immutable classification structures in [`custom_components/ebay/parsers/sold_unsold_parity.py`](../custom_components/ebay/parsers/sold_unsold_parity.py):

### Enums
- `ListingLifecycleState`: `ACTIVE`, `SCHEDULED`, `ENDED`, `UNKNOWN`.
- `SaleClassification`: `SOLD`, `UNSOLD`, `AMBIGUOUS`, `UNKNOWN`.
- `ClassificationEvidence`: `HIGH`, `MEDIUM`, `LOW`, `UNKNOWN`.
- `ClassificationSource`: `TRADING_XML`, `GRAPHQL`, `REST_FULFILLMENT`, `COMPOSITE`.

### Models
- `NormalizedListingClassification`:
  - `item_id: str`
  - `lifecycle_state: ListingLifecycleState`
  - `sale_classification: SaleClassification`
  - `source: ClassificationSource`
  - `quantity_sold: int | None = None`
  - `quantity_total: int | None = None`
  - `order_ids: tuple[str, ...] = ()`
  - `ended_at: datetime | None = None`
  - `evidence_level: ClassificationEvidence = ClassificationEvidence.UNKNOWN`

- `SoldUnsoldCollectionResult`:
  - `listings: tuple[NormalizedListingClassification, ...]`
  - `sold_item_ids: frozenset[str]`
  - `unsold_item_ids: frozenset[str]`
  - `selling_classification: dict[str, str]`
  - `sold_items_count: int`
  - `unsold_items_count: int`
  - `is_complete: bool`
  - `is_truncated: bool`
  - `total_count: int | None = None`
  - `account_id: str | None = None`
  - `account_verified: bool = False`
  - `window_days: int = 60`
  - `window_verified: bool = False`
  - `reconciliation_ready: bool = False`
  - `errors: tuple[dict[str, Any], ...] = ()`
  - `diagnostics: dict[str, Any] | None = None`

---

## 6. Parity Comparison Engine, Supported-Fields Parity & Migration Readiness

The parity comparison engine compares XML baseline and candidate snapshots across all monitored fields:

### Monitored Fields
1. `sold_items_count`: Total count of unique sold items.
2. `unsold_items_count`: Total count of unique unsold items.
3. `sold_item_ids`: Set of sold item identifiers.
4. `unsold_item_ids`: Set of unsold item identifiers.
5. `selling_classification`: Full dictionary mapping `{item_id: "sold" | "unsold"}`.
6. `item_id`: Exact listing identifier.
7. `lifecycle_state`: Canonical lifecycle status (`ended`).
8. `sale_classification`: Outcome (`sold` vs `unsold`).
9. `quantity_sold`: Count of units sold.
10. `ended_at`: Timestamp when the listing concluded.

### 6-State Parity Taxonomy
- `exact_match`: Values match identically.
- `equivalent_normalized`: Values differ in raw formatting but match after canonical normalization (e.g., ISO UTC timestamps).
- `different_semantics`: Substantive value difference or conflicting classification.
- `missing_field`: Field present in baseline but missing in candidate.
- `unverified`: Snapshot incomplete, truncated, or census cannot be proven.
- `unsupported`: Field not supported by baseline or candidate source.

### Supported Fields Parity vs. Full 10-Field Migration Parity
- **Baseline Trading XML Limitation**: The legacy Trading XML `GetMyeBaySelling` baseline does not supply per-listing `quantity_sold` or explicit `ended_at` timestamps on unsold items. Consequently, these 2 fields are classified as `unsupported` (`PARITY_UNSUPPORTED`).
- **Supported Fields Parity (`supported_fields_parity`)**: Measures equivalence across the 8 comparable fields supported by both sources (`sold_items_count`, `unsold_items_count`, `sold_item_ids`, `unsold_item_ids`, `selling_classification`, `item_id`, `lifecycle_state`, `sale_classification`). In offline synthetic verification, this achieves `pass`.
- **Full Parity (`overall_status`)**: Requires full coverage across all 10 monitored fields without any `unsupported` or `unverified` states. Because `quantity_sold` and `ended_at` remain unsupported in baseline Trading XML, `overall_status` evaluates to `inconclusive` (never `pass`).
- **Migration Readiness (`migration_readiness`)**: Evaluates whether modern endpoints are ready for autonomous production cutover. Migration readiness is strictly `blocked` (`migration_readiness_reason: UNSUPPORTED_FIELDS_IN_BASELINE`) because Trading XML baseline limitations and unverified GraphQL schema entitlement prevent verified parity.

### Positive Account & Window Reconciliation Readiness and Provenance Integrity
Offline collection completeness (`is_complete`) measures syntactic and page-level integrity of individual response collections. Stronger **cross-provider reconciliation readiness** (`reconciliation_ready`) requires external proof of request provenance:
1. `account_verified`: Requires external authenticated identity verification (`ProviderVerificationEvidence`). Raw payload fields such as `accountId` or `sellerId` in unauthenticated responses NEVER self-certify account identity. Multi-page REST sequences must have uniform, consistent account identity across all pages.
2. `window_verified`: Proof of bounded date intervals (`window_start_utc` and `window_end_utc`) matching between providers. Undated listings, missing range filters, empty collections, or listings falling outside the lookback interval invalidate window verification.
3. `entitlement_verified`: Verified API entitlement and OAuth scopes for candidate endpoints.
4. `is_complete`: Full offset-zero census without gaps or pending continuations.
5. `errors == 0`: Zero structural, parsing, or pagination errors.

### Migration Readiness Permanently Blocked in Milestone 7
Migration readiness (`migration_readiness`) evaluates whether candidate modern endpoints are safe for production cutover. In Milestone 7:
- Hand-crafted synthetic snapshots or offline fixtures cannot earn `migration_readiness: ready`.
- If external API entitlement is unverified: `migration_readiness: blocked` with reason `API_ENTITLEMENT_UNVERIFIED`.
- If account or window provenance is unverified: `migration_readiness: blocked` with reason `UNVERIFIED_PROVENANCE`.
- If baseline Trading XML lacks monitored fields: `migration_readiness: blocked` with reason `UNSUPPORTED_FIELDS_IN_BASELINE`.
- In all offline verification contexts, migration readiness remains safely and permanently `blocked`.

### Anti-False-Positive Empty Collection Rule
If both the XML baseline and candidate snapshots return verified empty collections (0 listings), record-level fields cannot be evaluated. The engine classifies record-level fields as `unverified` and reports `overall_status: inconclusive`, preventing false-positive pass claims when no records were tested.

---

## 7. Diagnostic Privacy Architecture (Default-Deny)

To protect user confidentiality during diagnostic reporting, Milestone 7 implements strict default-deny sanitization:

1. **Default-Deny Schema**: Only allowlisted top-level keys (`overall_status`, `is_inconclusive`, `field_counts`, `field_comparisons`, `baseline_summary`, `candidate_summary`, `errors`, `selling_classification`, `sold_item_ids`, `unsold_item_ids`) are passed to reports.
2. **Deterministic Pseudonymization**:
   - Real eBay item IDs are pseudonymized using unsalted SHA-256 hashes truncated to 12 hex characters: `item_<sha256[:12]>`.
   - Pseudonym validation enforces format `^item_[0-9a-f]{12}$` and rejects all decimal-only suffixes (`item_110000000001`), usernames, or arbitrary strings.
3. **No Unrecognized Free-Text**:
   - Field comparison notes only pass if matched against exact allowlists or strict regex patterns (`^All \d+ item IDs matched exactly$`, etc.). Unrecognized text falls back to generic fixed strings (`Value or semantic difference between baseline and candidate`).
   - Diagnostic errors only pass allowlisted symbolic error codes (`ALLOWED_SYMBOLIC_ERROR_CODES`) or approved eBay numeric error codes (`ALLOWED_NUMERIC_ERROR_CODES`). Unrecognized error strings fall back to `UNKNOWN_ERROR` and generic diagnostic messages.
4. **Token & Credential Scrubbing**: Auth tokens, refresh tokens, passwords, emails, currency amounts, and long numeric IDs are completely redacted.

---

## 8. Standalone CLI Parity Verification Harness

The standalone CLI tool [`scripts/validate_sold_unsold_parity.py`](../scripts/validate_sold_unsold_parity.py) allows running parity comparisons completely offline or against live endpoints with bounded page traversal (1..25 pages).

### Offline Synthetic Demonstration
Run verification against bundled synthetic fixtures:
```bash
uv run --locked python scripts/validate_sold_unsold_parity.py --synthetic-demo
```

### JSON Format Output
```bash
uv run --locked python scripts/validate_sold_unsold_parity.py --synthetic-demo --format json
```

### Custom Local Fixtures
```bash
uv run --locked python scripts/validate_sold_unsold_parity.py \
  --xml-fixture tests/fixtures/myeBaySelling_sold_unsold.xml \
  --graphql-fixture tests/fixtures/graphql_ended_listings.json \
  --orders-fixture tests/fixtures/fulfillment_orders.json \
  --format markdown \
  --output report.md
```

### Page Limit Enforcement
The CLI and underlying live validation functions enforce `--max-pages` between 1 and 25 (`MAX_CLASSIFICATION_PAGES`). Values outside this range are rejected before any network access.

### Reserved Live Mode
The `--live` option is reserved and returns `NOT_IMPLEMENTED`. Live validation cannot be enabled until eBay confirms production entitlement and published schemas for `sellerListingSearch`. The CLI exits 1 with a structured diagnostic message if `--live` is passed.
