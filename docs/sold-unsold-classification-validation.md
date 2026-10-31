# Sold & Unsold Listing Classification Parity Validation (Milestone 7)

## 1. Executive Summary & Architectural Scope

This document specifies the research, data modeling, parity verification harness, and diagnostic privacy protections for **Milestone 7: Sold & Unsold Listing Classification Modernization** within the `ha-ebay` integration.

The primary mission of Milestone 7 is to modernize the classification of sold and unsold listings by evaluating modern eBay data sources—specifically **GraphQL `sellerListingSearch` ended listing queries** and **REST Sell Fulfillment API `getOrders`**—against the authoritative legacy **Trading XML `GetMyeBaySelling` (`SoldList` / `UnsoldList`)** baseline.

> [!IMPORTANT]
> **Production Protection Guarantee**: In accordance with the repository's migration policy, **no live production provider cutovers are performed in Milestone 7**. The Home Assistant runtime polling coordinators continue to use Trading XML `GetMyeBaySelling` as the authoritative source for sold and unsold listing counts and classifications. The modern classification parsers and validation tools execute strictly offline or in user-initiated, isolated, read-only diagnostic validation runs.

---

## 2. Legacy Trading XML Baseline Architecture

### 2.1 Production Ingestion and Entity Contracts

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

## 3. Modern Candidate API Capabilities & Distinctions

### 3.1 GraphQL `sellerListingSearch` (Ended Listings)

The eBay GraphQL seller listing search API provides candidate queries for ended seller listings:
- **Operation / Field**: `sellerListingSearch(status: [ENDED], range: ...)`
- **Key Fields**:
  - `itemId` / `listingId`: Listing identifier.
  - `status`: Lifecycle state (`ENDED`, `COMPLETED`, `CLOSED`, `ACTIVE`, `SCHEDULED`).
  - `quantitySold`: Number of items sold.
  - `quantity`: Total listing quantity.
  - `itemEndDate` / `endTime`: Listing end timestamp (ISO 8601).

> [!WARNING]
> **Critical Distinction: Ended Lifecycle State vs. Sale Outcome**:
> An ended listing is **not** automatically classified as unsold!
> A completed auction with a winning bid or a fixed-price listing where all inventory was purchased will also have status `ENDED`.
> Classification must strictly enforce:
> - If `quantitySold > 0` $\rightarrow$ `SaleClassification.SOLD`.
> - If `quantitySold == 0` $\rightarrow$ `SaleClassification.UNSOLD`.
> - If `quantitySold` is `None` or absent $\rightarrow$ `SaleClassification.UNKNOWN`.
> Blindly mapping all ended listings to unsold creates critical inventory and event corruption.

### 3.2 REST Sell Fulfillment API `getOrders`

The eBay REST Sell Fulfillment API provides authoritative order details for sales:
- **Endpoint**: `GET /sell/fulfillment/v1/order`
- **Scopes**: `https://api.ebay.com/oauth/api_scope/sell.fulfillment.readonly` or `sell.fulfillment`
- **Pagination Contract**:
  - Uses **limit/offset pagination** (`limit` query parameter, max 50; `offset` query parameter, integer), returning `total: int`, `href`, `next`, and `prev`.
  - **Correction**: `docs/api-migration-feasibility.md` previously described this as cursor pagination; official documentation and runtime inspection in `custom_components/ebay/clients/fulfillment.py` confirm limit/offset pagination.
- **Key Limitations**:
  - Fulfillment orders only record completed sales (`lineItems[*].legacyItemId`).
  - An unsold listing generates **zero** orders; therefore, the Fulfillment API alone can **never** verify that an item was unsold.
  - Order count $\ne$ sold items count (due to multi-line-item orders and multi-quantity line items).

### 3.3 Composite Modern Normalization

To accurately classify both sold and unsold listings using modern APIs, a **composite architecture** merges the two modern sources:
1. **GraphQL Ended Listings**: Supplies the universe of listings that have ended within the lookback window, along with `quantitySold` and timestamps.
2. **REST Fulfillment Orders**: Supplies confirmed sales and line-item order IDs.
3. **Composite Resolver**:
   - Matches line items and `quantitySold > 0` to confirm `SOLD`.
   - Confirms `UNSOLD` only when `quantitySold == 0` and zero matching orders exist in complete, verified collections.
   - Any ambiguous or missing signals fail closed to `UNKNOWN`.

---

## 4. Strict Canonical Models & Data Structures

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
  - `window_days: int = 60`
  - `errors: tuple[dict[str, Any], ...] = ()`
  - `diagnostics: dict[str, Any] | None = None`

---

## 5. Parity Comparison Engine & 6-State Taxonomy

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
- `unsupported`: Field not supported by candidate source.

### Anti-False-Positive Empty Collection Rule
If both the XML baseline and candidate snapshots return verified empty collections (0 listings), record-level fields cannot be evaluated. The engine classifies record-level fields as `unverified` and reports `overall_status: inconclusive`, preventing false-positive pass claims when no records were tested.

---

## 6. Diagnostic Privacy Architecture (Default-Deny)

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

## 7. Standalone CLI Parity Verification Harness

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
