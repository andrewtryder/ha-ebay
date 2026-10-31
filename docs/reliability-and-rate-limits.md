# eBay Production Reliability, Rate Limits & Quota Handling

This document specifies the reliability architecture, rate-limiting semantics, retry policies, data-preservation rules, event suppression behavior, and Home Assistant lifecycle contracts implemented in [`andrewtryder/ha-ebay`](https://github.com/andrewtryder/ha-ebay) as part of **Milestone 11**.

---

## 1. Architectural Scope & Read-Only Invariant

`ha-ebay` is **strictly read-only**. The integration monitors eBay activity and exposes telemetry, sensors, buttons, calendars, and event entities into Home Assistant. It never executes write operations, places bids, creates/edits listings, sends messages, issues refunds, or mutates remote accounts.

In accordance with Milestone 11 objectives:
- **No unverified GraphQL/REST cutover**: Established Trading XML providers remain the authoritative production baseline for Watchlist, Bidding, and Active Selling.
- **No managed token brokers**: Users configure and own their developer application keysets.
- **Fail-closed & last-good-data preservation**: Failures never masquerade as empty collections or zero counts.

---

## 2. Endpoint Families & Quota Models

eBay does not operate a single global 5,000-call quota pool. Limits vary significantly by endpoint family and keyset tier:

| Endpoint Family | Underlying Protocol | Official Baseline Quota | Observed Error Behavior | Throttling & Retry Behavior |
|---|---|---|---|---|
| **Trading API** | SOAP / XML (`/ws/api.dll`) | Keyset-level daily call limit (historically 5,000/day for default keysets, expandable upon application review). | HTTP 429, or HTTP 200 with XML `Ack=Failure` and `ErrorCode` 518 ("Call usage limit reached") or 17 ("Daily limit exceeded"). | **Uses shared tracker:** Recorded in `RateLimitTracker` under `EndpointFamily.TRADING`. Shared keyset cooldown applied; fast-fail if cooldown > 2.0s. Unified 3-attempt ceiling across 5xx retries and 401 token refresh. Permanent 403, 400, and 404 fail immediately with zero retries and zero refreshes. |
| **REST APIs** | JSON over HTTPS (`/sell/fulfillment`, `/sell/finances`, `/sell/analytics`, `/commerce/feedback`, etc.) | Resource-specific rate limits (often expressed per minute / hour per developer application or user token). Although individual eBay REST resources have varying quota semantics, a conservative REST-family cooldown is applied to protect against cascading 429s. | HTTP 429 with standard `Retry-After` header (delta-seconds or HTTP-date). | **Uses shared tracker:** Recorded in `RateLimitTracker` under `EndpointFamily.REST`. Respects server `Retry-After` (stored up to 24 hours); fast-fail if cooldown > 2.0s. Unified 3-attempt ceiling across 5xx retries and 401 token refresh. Permanent 403, 400, and 404 fail immediately with zero retries. |
| **GraphQL API** | JSON over HTTPS (`/commerce/user/graphql`) | Read-only validation queries only; not cut over in production. | HTTP 429 or execution throttled extension. | **Does NOT use shared tracker:** Uses independent, validation-only bounded retries (`MAX_GRAPHQL_RETRIES = 2`) for transient system or rate errors. Throttling is local to query execution; does not share cooldown state with Trading or REST. |
| **OAuth Services** | Form POST (`/identity/v1/oauth2/token`) | Per-application token exchange and refresh rate limits. Independent token handling with 30s timeout. | HTTP 429 or `invalid_grant` / `invalid_request`. | **Does NOT use `BoundedRetryPolicy`:** Enforces explicit 30s socket timeout and double-checked locking under `_token_refresh_lock` to coordinate concurrent callers. Transient transport failures raise bounded exceptions; permanent invalid grant triggers native Home Assistant reauthorization. |

---

## 3. Polling Cadence & Request Volume

### Scheduled Polling

Coordinator polling cadence is user-configurable via `CONF_POLL_INTERVAL` (default: **30 minutes**). Per-section intervals allow finer scheduling where supported:
- **Full Refresh**: Traverses enabled sections (Buying, Selling, Analytics, Fulfillment, Seller Standards, Feedback, Messages, Account Privileges, Finances).
- **Page Traversal**: Paginated sections (e.g., Active Selling, Watchlist, Bidding) traverse up to `MAX_PAGES` (default 20 pages, 100-200 entries/page, defined in `custom_components/ebay/parsers/xml.py`).
- **Request Volume**: A standard 30-minute poll of Buying + Selling typically executes 2-4 Trading API calls. Enabling optional REST sections adds 1-2 calls per section, well within standard daily limits.
- **Manual Refresh Cooldown**: `MANUAL_REFRESH_COOLDOWN_SECONDS` prevents rapid manual triggers from amplifying API usage.

---

## 4. Canonical Bounded Retry & Rate-Limit Tracking

### Bounded Retry Policy (`BoundedRetryPolicy`)

Located in [`custom_components/ebay/clients/resilience.py`](../custom_components/ebay/clients/resilience.py) and enforced by the outer logical request methods `RestClientMixin._async_rest_get` and `TradingClientMixin.async_call_trading_api`:
- **Unified Attempt Ceiling**: Default max 2 retries (up to 3 total endpoint attempts), encompassing both transient 5xx/network retries and at most one 401 token-refresh replay. A 401 replay never restarts a fresh 3-attempt allowance.
- **Deterministic Backoff**: Exponential progression ($0.5 \times 2^{\text{attempt}}$: 0.5s, 1.0s, 2.0s, capped at 10.0s).
- **Transient Retry Eligibility**: Only transient HTTP 500, 502, 503, 504 and transient transport errors (`TimeoutError`, `ClientConnectorError`, `ServerDisconnectedError`, `ClientPayloadError`, `ClientOSError`) are retried.
- **No Immediate Retry on 429**: HTTP 429 rate limits are never retried in an immediate loop; they are recorded in `RateLimitTracker` and fail fast as partial failures to prevent quota exhaustion and request amplification.
- **Strict 401 vs 403 Separation**:
  - HTTP 401 raises `EbayAuthError` and permits at most one authorized token-refresh and replay sequence, provided total endpoint attempts remain within the 3-attempt ceiling and time budget.
  - HTTP 403 indicates permission or entitlement denial and **fails immediately** (as `EbayApiError(status=403)` on Trading or `EbayPartialFailure` on REST) with zero retries and zero token refreshes.
  - HTTP 400 and 404 fail immediately without retries or refreshes.
- **Bounded Request Timeout Budget**: All requests enforce an overall total timeout budget (`total_timeout = 35.0s`), capping per-attempt timeouts to remaining budget time.
- **Accurate Retry Counting**: The diagnostic counter `_last_retry_count` increments strictly when an additional network attempt is actually dispatched. If the timeout budget expires before a proposed backoff delay completes, or if the wait is cancelled, no phantom retry is counted.
- **Non-blocking Waits**: All delays use cancellation-aware asynchronous sleeping (`await policy.async_wait(delay)`).

### Shared-Keyset Rate-Limit Tracker Registry (`RateLimitTracker`)

Tracks observed provider throttling without assuming theoretical usage:
- **Thread/Async-Safe**: In-memory monotonic timestamps.
- **HA-Instance Shared Keyset Registry**: Managed in `hass.data[DOMAIN]["rate_limit_trackers"]`, keyed by safe hash `f"{environment}:{sha256(client_id)[:12]}"`. Multiple Home Assistant config entries sharing the same developer application keyset share cooldown state, preventing cascading 429s across accounts.
- **Strict Keyset & Environment Isolation**: Production and Sandbox environments never share cooldowns. Different developer application keysets maintain distinct trackers. No OAuth tokens, credentials, or response data are ever shared across accounts.
- **Failure-Safe Reference Lifecycle**: Trackers track holding `entry_id` references. If setup fails after tracker acquisition (during coordinator first-refresh or platform forwarding), a `finally` block safely releases the acquired reference. Clean unload and entry removal safely remove entries and purge the tracker when the final entry using that keyset unloads.
- **Fast-Fail on Active Cooldown**: If an endpoint family has an active cooldown > 2.0s, outgoing requests fast-fail immediately with `EbayPartialFailure` (REST) or `EbayApiError` (Trading) without hitting the network, preventing request amplification. If cooldown $\le 2.0$s, the client waits the remaining time before proceeding.

### `Retry-After` Header Parsing & Dual-Horizon Semantics

The function `parse_retry_after()` supports both:
1. **Delta-seconds**: Integer or float values (e.g., `"30"` $\rightarrow$ 30.0s).
2. **RFC 7231 / RFC 9110 HTTP-dates**: Full date strings (e.g., `"Sun, 06 Nov 1994 08:49:37 GMT"` $\rightarrow$ computed offset against `time.time()`).
3. **Dual-Horizon Semantics**: Server cooldown storage is bounded up to 24 hours (`DEFAULT_MAX_COOLDOWN = 86400.0s`) to ensure long server-mandated cooldowns are remembered truthfully. The active polling wait budget is strictly separated: any cooldown > 2.0s fails fast as a partial failure without stalling Home Assistant polling. Negative values clamp to 0.0s; missing or unparseable headers fall back to a safe 60.0s default.

---

## 5. Preservation of Last-Good Data & Pagination Integrity

A critical requirement in Home Assistant is that temporary API outages, rate limits, or truncated pagination must **never** cause entities to drop to 0, disappear, or be erroneously pruned.

In `custom_components/ebay/section_fetch.py`:
1. **Truncated Pagination Merging**: If pagination truncates before reading all items (e.g. `watched_truncated` or `selling_truncated` is True), previously confirmed items missing from the partial pages are preserved (`retained = dict(base[collection]); retained.update(fetched)`). Collections are never replaced by a truncated subset, preventing false drops in counts.
2. **Success Stamping Separation**: When a section experiences truncated pagination or partial failure, only `section_last_attempt` is updated; `section_last_success` is **not** stamped until a complete, untruncated fetch succeeds.
3. **Genuine Empty Collections**: When a fetch completes with 0 items and is not truncated (`total_pages == 1`, `truncated == False`), counts cleanly update to 0, genuine empty states are reported, and entity lifecycles proceed normally.
4. **Sold / Unsold Classification**: If `async_fetch_sold_unsold_classification()` fails due to transient API errors, the prior `selling_classification` dictionary, `sold_items_count`, and `unsold_items_count` are retained from `previous`. `truncated_collections["sold_unsold"]` is flagged.
5. **Listing Views**: If `_fetch_seller_list_views()` fails, each item in `selling` retains its prior `views` count from `previous_selling`.
6. **Best Offers**: If `GetBestOffers` fails, each item in `selling` retains its prior `offers` count from `previous_selling`.
7. **Traffic Analytics**: If `async_fetch_analytics_views()` fails or partially returns, items in `selling` retain their prior `analytics_views_30d` metrics.
8. **Seller-Ops Collections**: Orders, payment disputes, seller standards, feedback, and messages retain prior payload dictionaries upon partial failure.

---

## 6. Event Suppression & Churn Prevention

The domain event detection layer in [`custom_components/ebay/events.py`](../custom_components/ebay/events.py) evaluates payload diffs between consecutive polls:

- **Incomplete / Partial Refresh Detection**: If a section experienced truncation (`_collection_truncated`) or reported partial failures (e.g., `selling`, `selling_truncated`, `buying`, `buying_truncated`, `sold_unsold_classification`), the suppression flag `suppress_incomplete` is set to `True`.
- **Suppressed Transitions**:
  - `selling`: Suppresses `item_disappeared_unknown`, `item_sold`, and `item_ended_unsold` when selling listings or classification are incomplete.
  - `watching`: Suppresses `watched_item_disappeared_unknown`, `watched_item_added`, and false price-drop alerts when buying or watchlists are incomplete.
  - `bidding`: Suppresses false outbid, lost, or won events when bidding lists are incomplete.
- **Genuine Transitions**: Legitimate completed refreshes continue to detect price changes, bid increments, new offers, messages, and order updates.

---

## 7. OAuth2 Concurrency & Lifecycle Protection

- **Request Timeout**: All OAuth token requests via `EbayOAuth2Implementation._token_request()` specify an explicit 30s `aiohttp.ClientTimeout(total=30)` to prevent indefinite socket hangs.
- **Concurrency Locking**: `RestClientMixin.async_refresh_access_token()` uses double-checked locking under `_token_refresh_lock` to ensure multiple concurrent callers share a single refresh request.
- **Safe Persistence**: Routine token refreshes update `entry.data["token"]` via `hass.config_entries.async_update_entry()`. `_loaded_config_snapshot()` excludes the entire OAuth token dictionary, preventing integration reloads during automatic token renewal.
- **Clean Unload (`async_shutdown`)**: On integration unload, `coordinator.async_shutdown()`:
  1. Cancels active ending-soon event timers.
  2. Clears registered timer listeners and event callbacks.
  3. Cancels any in-flight background save tasks (`_ending_soon_fired_save_task`).
  4. Calls `super().async_shutdown()` to cancel Home Assistant coordinator refresh intervals.

---

## 8. Diagnostics Privacy & Observability

Diagnostic data exported via `async_get_config_entry_diagnostics()` includes non-sensitive operational telemetry:
- `rate_limiting`:
  - `throttled`: Boolean indicating if REST or Trading cooldowns are active.
  - `rest_cooldown_remaining_seconds`: Remaining REST cooldown in seconds.
  - `trading_cooldown_remaining_seconds`: Remaining Trading API cooldown in seconds.
  - `last_retry_count`: Number of additional endpoint attempts dispatched during the most recent tracked logical request or polling cycle.
- **Strict Privacy**:
  - No access tokens, refresh tokens, client secrets, authorization codes, or user passwords are ever included.
  - Sensitive identifiers (`account_id`, `client_id`, `runame`) are redacted.
  - upstream URLs with sensitive parameters are scrubbed.

---

## 9. Operational Limitations & Live Provider Verification

1. **Undocumented Rate Limits**: eBay does not publish dynamic rate-limit thresholds for every REST resource. The integration relies strictly on observed HTTP 429 headers and Trading XML error codes rather than synthetic rate-capping.
2. **Mocked Offline Validation**: All CI and regression tests run in 100% mocked offline environments without live eBay credentials or network calls.
