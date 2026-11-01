# Manual Live-Validation Checklist (Milestone 12)

This document provides a comprehensive, opt-in manual verification checklist for maintainers to execute on a real running Home Assistant instance using private eBay developer credentials and live eBay accounts prior to publishing release `0.9.0`.

> [!CAUTION]
> **Safety & Privacy Guidelines:**
> - **Strictly Read-Only Invariant**: Never place test bids, create/modify listings, make purchases, send messages, or perform any mutating actions on live eBay accounts. All validation must rely strictly on observing already-existing account items and activity.
> - **Zero Live 429 Manufacturing**: Never attempt to trigger live HTTP 429 rate-limit errors through high-volume rapid polling against live eBay APIs. Rate-limiting backoff and retry behavior are fully validated in controlled offline automated test suites.
> - **Credential & Storage Privacy**: Never expose, paste, or share raw contents of Home Assistant credential storage files (`.storage/core.config_entries`).
> - **Secret Confidentiality**: Never commit or publish client secrets, OAuth codes, access tokens, refresh tokens, private listing payloads, or raw RuNames in Git commits, PR descriptions, or public issues.
> - **Unverified Baseline**: Every step below is designated as **[UNVERIFIED - PENDING MAINTAINER LIVE RUN]** until explicitly executed and signed off by the maintainer.

---

## Environment Safety Boundaries

To protect existing production setups, maintainers must distinguish between tests that are safe to run against an established Home Assistant installation and tests that require a dedicated, disposable test instance:

| Testing Scope | Safe on Primary HA Instance? | Requires Disposable Test Instance? |
|---|---|---|
| **Read-Only Telemetry** (Watchlist, selling count, calendar, activity events) | **YES** (Strictly non-mutating) | No |
| **Diagnostics Export Privacy Audit** | **YES** (Local redaction check) | No |
| **Manual Refresh Cooldown** (Local UI debounce) | **YES** (Internal cooldown check) | No |
| **Guided Onboarding Flow & Keyset Setup** | **NO** (Creates new config entries) | **YES** |
| **Multi-Account Onboarding & Keyset Sharing** | **NO** (Registers secondary accounts) | **YES** |
| **Legacy Schema Upgrade (v1–v4 $\to$ v5 Migration)** | **NO** (Requires legacy entry fixtures) | **YES** |
| **Atomic Reauthorization & Account Mismatch Check** | **NO** (Tests reauth flows) | **YES** |

---

## Pre-requisites

- A dedicated Home Assistant `2026.10.0+` test instance.
- HACS custom component `ha-ebay` installed from branch `test/ebay-end-to-end-release` (commit corresponding to PR #107). Note: release `0.9.0` is currently pending and unpublished.
- An eBay Developers Program account with a registered Application Keyset (App ID, Cert ID, RuName).
- One primary regular eBay account (with existing watched, bidding, or selling items).
- (Optional for multi-account check) A second distinct regular eBay account.

---

## 1. Guided BYO OAuth Onboarding Flow (Disposable Instance)

- [ ] **[UNVERIFIED - PENDING MAINTAINER LIVE RUN] Launch guided setup**:
  - In the test instance, navigate to **Settings → Devices & Services → Add Integration → eBay**.
  - Verify that the Welcome screen displays clear options: **Start guided setup (Recommended)** and **Enter credentials directly**.
- [ ] **[UNVERIFIED - PENDING MAINTAINER LIVE RUN] RuName guidance & callback verification**:
  - Select guided setup.
  - Verify that the step clearly displays the exact callback URL (e.g. `https://my.home-assistant.io/redirect/oauth`).
- [ ] **[UNVERIFIED - PENDING MAINTAINER LIVE RUN] Credential entry & password masking**:
  - Enter App ID, Cert ID (masked password selector), and RuName.
  - Test validation error handling: enter whitespace or invalid text; verify helpful validation messages appear and confirm the Client Secret / Cert ID is **never redisclosed** or prepopulated in form fields.
- [ ] **[UNVERIFIED - PENDING MAINTAINER LIVE RUN] Consent redirect & token exchange**:
  - Click submit; verify browser redirects to eBay's official consent page.
  - Sign in with the primary regular eBay account and grant access.
  - Verify redirect back to Home Assistant and successful entry creation (`eBay (<username>)`).

---

## 2. Existing-User Upgrade & Schema Migration (Disposable Instance)

- [ ] **[UNVERIFIED - PENDING MAINTAINER LIVE RUN] Upgrade migration to Version 5**:
  - Set up a pre-M12 installation on a disposable test instance (with a simulated v1, v2, v3, or v4 config entry).
  - Update `ha-ebay` to the M12 branch and restart Home Assistant.
  - Download diagnostics for the entry; verify `account_identity` shows `binding_state: "legacy_compatible"` and `identifier_type: "legacy_app_environment"`.
  - Confirm existing options and tokens are preserved without requiring manual reauthorization.
- [ ] **[UNVERIFIED - PENDING MAINTAINER LIVE RUN] Entity Unique ID stability**:
  - Confirm existing dashboard cards and automations referencing `sensor.ebay_*` continue working without orphaned `_2` entity suffixes.

---

## 3. Real Watchlist & Bidding Polling (Safe on Live Account)

- [ ] **[UNVERIFIED - PENDING MAINTAINER LIVE RUN] Watchlist polling**:
  - Verify `sensor.ebay_<user>_watched_items` matches the exact number of watched items on `ebay.com/myb/WatchList`.
  - In Balanced or Detailed entity mode, verify per-item sensors show correct current price, bids, and end time.
- [ ] **[UNVERIFIED - PENDING MAINTAINER LIVE RUN] Bidding activity observation**:
  - Observe existing active bids (do **NOT** place new bids); verify `sensor.ebay_<user>_bidding_items` reflects active bids.
  - Verify winning / outbid status accurately reflects listing state on `event.ebay_bidding_activity`.

---

## 4. Active Selling & Seller Operations (Safe on Live Account)

- [ ] **[UNVERIFIED - PENDING MAINTAINER LIVE RUN] Active listings observation**:
  - Verify `sensor.ebay_<user>_active_selling_items` matches active listings in Seller Hub.
- [ ] **[UNVERIFIED - PENDING MAINTAINER LIVE RUN] Seller operations (optional)**:
  - If enabled, verify orders awaiting shipment, overdue orders, buyer messages, and seller level.
  - Confirm zero write operations: integration never modifies listings, answers questions, or creates refunds.

---

## 5. Multi-Account Onboarding & Keyset Sharing (Disposable Instance)

- [ ] **[UNVERIFIED - PENDING MAINTAINER LIVE RUN] Onboard second account**:
  - Add integration again with the same App ID, Cert ID, and RuName.
  - In the browser consent page, sign in as a **second distinct eBay account**.
  - Verify Home Assistant creates a distinct second config entry (`eBay (<second_user>)`).
  - Verify both accounts appear side-by-side with isolated entities, independent sensors, and isolated activity events.
- [ ] **[UNVERIFIED - PENDING MAINTAINER LIVE RUN] Keyset rate limit tracker sharing**:
  - Verify both accounts share the keyset `RateLimitTracker` under `hass.data["ebay"]["rate_limit_trackers"]`.
  - Confirm that tracker sharing does NOT leak private tokens or entity data between accounts.
  - Note: Do NOT attempt to manufacture live 429s; shared backoff logic is verified in automated unit/integration tests.

---

## 6. Entity State, Attributes & Calendars (Safe on Live Account)

- [ ] **[UNVERIFIED - PENDING MAINTAINER LIVE RUN] Entity attributes & states**:
  - Verify summary sensor states have correct numerical values and `SensorStateClass.MEASUREMENT`.
  - Verify item end times match eBay listing end times across timezones.
- [ ] **[UNVERIFIED - PENDING MAINTAINER LIVE RUN] Calendars**:
  - Open `calendar.ebay_watched_items` and `calendar.ebay_selling_items` in Home Assistant Calendar dashboard.
  - Confirm events are positioned at the exact listing conclusion times.

---

## 7. Automation Events & Blueprints (Safe on Live Account)

- [ ] **[UNVERIFIED - PENDING MAINTAINER LIVE RUN] Ending-soon event**:
  - Observe an existing item ending within the configured threshold (e.g. 60 minutes).
  - Verify `event.ebay_watching_activity` fires `item_ending_soon` once.
  - Verify restart/reload does NOT duplicate the ending-soon event (deduplication check).
- [ ] **[UNVERIFIED - PENDING MAINTAINER LIVE RUN] Blueprints**:
  - Import blueprints from `docs/blueprints.md`; confirm triggers fire on simulated or real transitions.

---

## 8. Manual Refresh & Local Debounce (Safe on Live Account)

- [ ] **[UNVERIFIED - PENDING MAINTAINER LIVE RUN] Manual refresh button**:
  - Press `button.ebay_refresh`.
  - Verify coordinator performs an immediate poll of enabled sections.
  - Rapidly press `button.ebay_refresh` a second time; confirm UI displays the 60-second in-memory cooldown notice without error.
  - Confirm that no high-volume network flood is sent to eBay.

---

## 9. Reload, Restart & State Restoration (Safe on Live Account)

- [ ] **[UNVERIFIED - PENDING MAINTAINER LIVE RUN] Integration reload**:
  - Go to **Settings → Devices & Services → eBay → ⋮ → Reload**.
  - Verify integration reloads cleanly without duplicate entity registrations.
- [ ] **[UNVERIFIED - PENDING MAINTAINER LIVE RUN] Full Home Assistant restart**:
  - Restart Home Assistant.
  - Verify persisted states (`ebay_ending_soon_fired_*`, `ebay_repair_streaks_*`, `ebay_inactive_item_sensors_*`) restore without errors.

---

## 10. Diagnostics Privacy Audit (Safe on Live Account)

- [ ] **[UNVERIFIED - PENDING MAINTAINER LIVE RUN] Download integration diagnostics**:
  - Go to **Settings → Devices & Services → eBay → ⋮ → Download diagnostics**.
  - Open the JSON file in an editor and search for:
    - `client_secret` (must not appear or must be `**REDACTED**`)
    - `access_token` (must not appear or must be `**REDACTED**`)
    - `refresh_token` (must not appear or must be `**REDACTED**`)
    - `runame` (must not appear or must be `**REDACTED**`)
  - Verify no sensitive OAuth tokens or private credentials exist in the export.
  - Do not share or publish the diagnostics export publicly if it contains account usernames or item titles.
