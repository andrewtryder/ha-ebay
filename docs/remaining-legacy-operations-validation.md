# Milestone 8: Remaining Legacy Trading XML Operations & Identity Parity Validation

> **Repository:** `andrewtryder/ha-ebay`
> **Status:** Completed Validation Milestone (Milestone 8)
> **Target Version Baseline:** Home Assistant 2026.10.0+ / Python 3.14
> **Policy:** Strictly read-only monitoring; zero network calls by default; Trading XML remains 100% authoritative in production; production provider cutover is strictly blocked.

---

## 1. Executive Summary

Milestone 8 concludes the comprehensive audit, normalization, and parity verification of all remaining legacy eBay Trading XML dependencies across the `ha-ebay` integration.

Prior to Milestone 8, early modernization assumptions held that operations such as `GetFeedback` and `GetMemberMessages` were legacy dependencies requiring complex replacement architectures. Milestone 8 audits the actual production codebase and eBay developer landscape, establishing three critical truths:

1. **`GetFeedback` and `GetMemberMessages` Are Already REST-Native:** Neither operation is called in the `ha-ebay` codebase. Home Assistant already uses the modern eBay REST Feedback API (`/buy/feedback/v1/...`) and REST Messages API (`/commerce/message/v1/conversation`).
2. **Authenticated Account Identity (`GetUser` vs. REST Identity `getUser`):** Trading XML `GetUser` is called at `custom_components/ebay/clients/feedback.py:35` solely to resolve the public account handle (`<UserID>`), which is required by REST Feedback query parameters (`user_id`). The modern REST Commerce Identity API (`GET /commerce/identity/v1/user/`) provides this handle under `username`. However, modern `userId` is an immutable, opaque system identifier that **cannot** be passed to REST Feedback. Furthermore, REST Commerce Identity requires `https://api.ebay.com/oauth/api_scope/commerce.identity.readonly`, an OAuth scope that existing Home Assistant users have not granted.
3. **Fail-Closed Production Stability:** Trading XML `GetUser` continues to function reliably under the existing `https://api.ebay.com/oauth/api_scope` core grant. In accordance with zero-disruption principles, Milestone 8 builds an offline parity validation harness and canonical models without performing any live production cutover or forcing OAuth reauthorization.

---

## 2. Master Legacy Trading XML Operations Inventory

Across the entire `ha-ebay` codebase, exactly five Trading XML operations are declared in `custom_components/ebay/parsers/trading_builders.py`:

| Operation | Production Call Site | Purpose & Consumed Fields | Modern Candidate Replacement | Milestone / Parity Status |
| :--- | :--- | :--- | :--- | :--- |
| **`GetUser`** | `clients/feedback.py:35` | Resolves public username (`<User><UserID>`) for REST Feedback endpoints | REST Commerce Identity `getUser` (`GET /commerce/identity/v1/user/`) | **Milestone 8**: Validated offline; production cutover blocked pending identity scope grant & multi-account policy. |
| **`GetMyeBayBuying`** | `section_fetch.py:354` | Retrieves buyer `WatchList` and `BidList` items | Candidate GraphQL `userWatchList` and `userBiddingActivity` | **Milestones 1 & 4**: Validated offline; production cutover blocked by unverified/unreleased GraphQL user scopes. |
| **`GetMyeBaySelling`** | `section_fetch.py:369` | Retrieves seller `ActiveList`, `SoldList`, and `UnsoldList` items | Candidate GraphQL `sellerListingSearch` + REST Sell Fulfillment `getOrders` | **Milestones 5 & 7**: Validated offline; production cutover blocked by unverified GraphQL seller listing scopes. |
| **`GetSellerList`** | `section_fetch.py:407` | Retrieves item `HitCount` (views) as optional fallback when analytics report lacks data | REST Sell Analytics `getTrafficReport` (`GET /sell/analytics/v1/traffic_report`) | **Milestones 5 & 8**: Optional fallback; REST Analytics traffic report is preferred primary. |
| **`GetBestOffers`** | `section_fetch.py:427` | Retrieves active seller best offer counts per listing | Candidate GraphQL `sellerNegotiationHistory` (exploratory, M6) | **Milestone 6**: Validated offline; production cutover blocked. Official REST Sell Negotiation API ([Release Notes](https://www.developer.ebay.com/develop/api/negotiation-api/release-notes)) provides `findEligibleItems` and `sendOfferToInterestedBuyers` (discount offers to buyers), neither of which returns seller Best Offers negotiation history. |

### Retired / Non-Existent Operations Audit

- **`GetFeedback`:** Not called. Early documentation mistakenly suggested `GetFeedback` was in use and lacked modern replacements. Production uses REST Feedback (`GET /buy/feedback/v1/feedback_rating_summary`, `GET /buy/feedback/v1/feedback`, and `GET /buy/feedback/v1/awaiting_feedback`).
- **`GetMemberMessages`:** Not called. Production uses REST Messages (`GET /commerce/message/v1/conversation?conversation_type=FROM_MEMBERS`).
- **`GetMyeBayBuying` (`LostList`):** Not called. Confirmed deprecated and removed by eBay without replacement; never queried by `ha-ebay`.

---

## 3. Authenticated Account Identity: `GetUser` vs. REST Commerce Identity `getUser`

### 3.1 Field-Level Mapping & Semantic Distinction

The comparison between Trading XML `GetUser` and REST Commerce Identity `getUser` reveals a crucial semantic distinction between account identifiers:

```
Trading XML GetUser:
  <User>
    <UserID>alpha_seller</UserID>             <-- Public handle / username
    <FeedbackScore>350</FeedbackScore>        <-- Feedback metric
    <PositiveFeedbackPercent>100.0</PositiveFeedbackPercent>
    <RegistrationDate>2021-04-12...</RegistrationDate>
    <Site>US</Site>
    <Status>Confirmed</Status>
    <SellerInfo>
      <SellerBusinessType>Private</SellerBusinessType>
    </SellerInfo>
  </User>

REST Commerce Identity getUser:
  {
    "userId": "urn:ebay:user:1234567890",     <-- Immutable internal account ID
    "username": "alpha_seller",               <-- Public handle
    "accountType": "INDIVIDUAL",              <-- Normalized account classification
    "registrationMarketplaceId": "EBAY_US"    <-- Registration marketplace
  }
```

| Field Name | Trading XML Source | REST Identity Source | Parity Status | Semantic Notes |
| :--- | :--- | :--- | :--- | :--- |
| `username` | `<User><UserID>` | `username` | `exact_match` | Public handle matches exactly (case-insensitive normalization applied). |
| `user_id` | N/A | `userId` | `different_semantics` | REST `userId` is an immutable, opaque system ID; XML lacks an equivalent plain user ID (only encrypted `EIASToken`). **Cannot be passed to Feedback API.** |
| `account_type` | `<SellerBusinessType>` | `accountType` | `exact_match` | `Commercial` maps to `BUSINESS`; `Private` maps to `INDIVIDUAL`. |
| `registration_marketplace_id` | `<Site>` | `registrationMarketplaceId` | `exact_match` | e.g. `US` maps to `EBAY_US`. |
| `feedback_score` | `<FeedbackScore>` | N/A | `missing_field` | Excluded from REST Identity. Handled independently by REST Feedback API. |
| `registration_date` | `<RegistrationDate>` | N/A | `missing_field` | Excluded from REST Identity. |

### 3.2 Feedback API Interoperability Risk & Data Handling Updates

The REST Feedback API endpoints (`/buy/feedback/v1/...`) are documented to require the public username as the `user_id` query parameter (e.g., `GET /buy/feedback/v1/feedback_rating_summary?user_id=alpha_seller`).

Under eBay's [2025 Data Handling Updates](https://developer.ebay.com/api-docs/static/data-handling-update.html), modern APIs are progressively adopting opaque account identifiers. However, current production Feedback endpoints index summary metrics by public handle, making substitution of modern internal `userId` unverified. Milestone 8 establishes canonical model `NormalizedAccountIdentity` where `username` and `user_id` are strictly typed and differentiated.

---

## 4. OAuth Scope Requirements & Existing-User Safety

### 4.1 Scope Comparison

- **Trading XML `GetUser`:** Authorized under the base core scope `https://api.ebay.com/oauth/api_scope`. All standard Home Assistant configurations include this scope.
- **REST Commerce Identity `getUser`:** Officially specifies `https://api.ebay.com/oauth/api_scope/commerce.identity.readonly`.

### 4.2 Why Production Cutover Is Blocked

Attempting to cut over `GetUser` to REST Identity in production would fail for existing users whose OAuth grants do not contain `commerce.identity.readonly`, returning `INSUFFICIENT_SCOPE` (HTTP 403), breaking Feedback updates and triggering re-authorization prompts. While individual developer tokens might possess varying grants, the integration's standard requested scope set does not include it.

Under Home Assistant architectural policies:
1. No speculative or ungranted OAuth scopes may be forced onto existing users.
2. No re-authentication storms may be triggered.
3. Trading XML `GetUser` remains completely authoritative for production runtime.
4. Modern REST Identity is validated exclusively via offline harness tools (`scripts/validate_legacy_fields_parity.py`).

---

## 5. Offline Parity Validation Harness

The validation harness is implemented in `scripts/validate_legacy_fields_parity.py` and `custom_components/ebay/parsers/identity_parity.py`.

### 5.1 CLI Capabilities

- **Zero-Network Default:** Makes no network calls unless explicitly requested.
- **Privacy Sanitization:** Automatically redacts OAuth tokens, credentials, prices, emails, and raw identifiers into deterministic SHA-256 pseudonyms (`user_<hash[:12]>`, `account_<hash[:12]>`).
- **Comprehensive Dependency Audit:** `--audit-matrix` outputs the complete legacy operations matrix in Markdown or JSON.
- **Custom Fixtures:** Supports `--xml-fixture` and `--rest-fixture` or `--graphql-fixture` for offline developer testing.

### 5.2 Verification Commands

```bash
# Run offline synthetic identity verification:
uv run python scripts/validate_legacy_fields_parity.py

# Export full operations audit matrix:
uv run python scripts/validate_legacy_fields_parity.py --audit-matrix --format markdown

# Run with custom local fixtures:
uv run python scripts/validate_legacy_fields_parity.py \
  --xml-fixture tests/fixtures/get_user.xml \
  --rest-fixture tests/fixtures/identity_user.json
```

---

## 6. Roadmap Transition to Milestone 9

With Milestone 8 complete, all legacy Trading XML operations across buying, selling, bidding, offers, feedback, and identity have been:
1. Audited against real production call sites.
2. Formally compared against candidate modern APIs.
3. Protected with offline-first canonical normalizers and extensive unit tests.
4. Integrated with fail-closed migration readiness policies.

Milestone 9 will build upon this foundation to introduce formal multi-account architecture and identity management, leveraging the canonical models established in Milestone 8.
