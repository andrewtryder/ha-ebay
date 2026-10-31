# One-Click eBay OAuth Architecture, Feasibility & Migration Safety

> **Repository:** `andrewtryder/ha-ebay`
> **Status:** Architecture Decision Record & Feasibility Assessment (Milestone 10)
> **Version Baseline:** Home Assistant 2026.10.0+ (Python 3.14 locked)
> **Production Status:** Research & Decision Document only. Zero production cutover; zero managed credentials; BYO OAuth remains active.

---

## 1. Executive Summary

This document evaluates whether `ha-ebay` can safely provide a streamlined, one-click "Connect eBay Account" user experience without requiring every Home Assistant user to create, register, and configure their own eBay developer application.

### The Decision: CONDITIONAL GO (Phased Long-Term Cloud Linking) / HARD NO-GO on Direct Embedded Keys or Unofficial Proxying

| Assessment Track | Feasibility | Recommendation | Reason |
|---|---|---|---|
| **Option A: BYO Developer Credentials (Current)** | `REPO_VERIFIED` | **CONTINUE AS PRIMARY BASELINE** | Zero operational hosting cost; zero secret compromise risk; zero maintainer liability; 100% compliant with eBay API License Agreement. |
| **Option B: HA Application Credentials Integration** | `OFFICIAL_SPEC` / `REPO_VERIFIED` | **RECOMMENDED NEAR-TERM UX ENHANCEMENT** | Native HA standard UI for entering Client ID, Secret, and RuName once; standardizes configuration flow without external infrastructure. |
| **Option C: Shared Developer Keyset in Client / Repository** | `OFFICIAL_SPEC` / `REPO_VERIFIED` | **ABSOLUTE HARD NO-GO (PROHIBITED)** | Violates eBay API License Agreement; public secret compromise; catastrophic account exposure; single-point ban risk. |
| **Option D1: Self-Hosted / Maintainer-Run OAuth Token Broker** | `UNVERIFIED` / `BLOCKED` | **NO-GO (UNSUSTAINABLE / HIGH LIABILITY)** | Maintainer bears 24/7 infrastructure custody, operational expenses, eBay Compatible Application Check audit requirements, abuse liability, and cross-account data risk. |
| **Option D2: Nabu Casa Home Assistant Cloud Account Linking** | `OFFICIAL_SPEC` / `EXTERNALLY_CONFIRMED` | **CONDITIONAL GO (LONG-TERM STRATEGIC ONLY)** | Technically sound official pattern; requires Nabu Casa corporate sponsorship, commercial eBay Developer Program partnership, and formal application review. Not deliverable inside a standalone custom repository. |

---

## 2. Current OAuth Architecture Audit

### 2.1 Component Breakdown & Data Flow

The current implementation in `ha-ebay` follows the Bring-Your-Own (BYO) developer credentials pattern:

1. **User Enters Credentials** (`custom_components/ebay/config_flow.py`):
   - The user supplies `client_id`, `client_secret`, `runame`, `environment` (`production` / `sandbox`), and `site_id`.
   - Credentials are held in flow memory (`self._data`). No unique ID is assigned at this stage (`REPO_VERIFIED`).
2. **Implementation Instantiation** (`custom_components/ebay/oauth2.py`):
   - An `EbayOAuth2Implementation` (subclassing HA core `LocalOAuth2Implementation`) is constructed.
   - It builds an eBay authorize URL with parameters `client_id`, `redirect_uri` (set to `runame`), `response_type=code`, `scope`, and a signed CSRF `state`.
3. **User Grants Consent**:
   - The user's browser is directed to `https://auth.ebay.com/oauth2/authorize` (or sandbox equivalent).
   - The user signs into their regular eBay account and clicks "Agree and Continue".
4. **Callback & Code Exchange**:
   - eBay redirects to the configured "Auth Accepted URL" registered with the RuName (`https://my.home-assistant.io/redirect/oauth`), carrying `code` and `state`.
   - In manual mode (`async_step_manual`), the user pastes the authorization code directly into Home Assistant.
   - The integration exchanges `code` at `https://api.ebay.com/identity/v1/oauth2/token` via HTTP POST with `Authorization: Basic <base64(client_id:client_secret)>`.
5. **Account Identity Discovery & Verification** (`custom_components/ebay/account_identity.py`):
   - Before committing credentials or creating a `ConfigEntry`, the integration immediately issues an authenticated, read-only Trading XML `GetUser` request.
   - Resolves canonical identity: `account_id` (durable `EIASToken`), `username`, and `binding_state=VERIFIED`.
   - Assigns canonical `unique_id = f"{environment}:{account_id}"`.
6. **Multi-Account Duplicate & Ambiguity Safeguard**:
   - Evaluates three-state identity comparison (`AccountEquivalence.SAME`, `DIFFERENT`, `UNKNOWN`).
   - Aborts `already_configured` if matching EIASToken is found; aborts `account_ambiguous` if legacy entry equivalence is `UNKNOWN`.
7. **Storage & Custody**:
   - Tokens, developer credentials, and verified identity are persisted strictly inside the Home Assistant installation's `.storage/core.config_entries`.
   - Token refresh is executed locally by `EbayOAuth2Implementation._async_refresh_token()`.

### 2.2 Developer App Identity vs. Authenticated User Principal

| Dimension | Developer Application (`App ID / Keyset`) | Authenticated User (`eBay Account`) |
|---|---|---|
| **Identity** | Registered in eBay Developer Portal (`developer.ebay.com/my/keys`) | eBay marketplace account (buyer/seller) |
| **Credentials** | `client_id`, `client_secret` (Cert ID), `runame` | OAuth `access_token`, `refresh_token` |
| **Cardinality** | 1 Developer Application | Can authorize $N$ independent eBay Accounts |
| **Durable ID** | App ID string | `EIASToken` (128-bit encrypted unique principal identifier) |
| **Custody** | User-owned developer registration | User-owned personal marketplace account |

### 2.3 Integration Design vs. Platform Limitations

- **Integration Design Artifacts**:
  - Requiring the user to copy/paste credentials into form fields instead of using HA's `application_credentials` dialog.
  - Manual code copy/paste fallback for instances without `my.home-assistant.io` external access.
- **Fundamental eBay Platform Constraints**:
  - `redirect_uri` in authorization requests **must** be an eBay-assigned `RuName`, not an arbitrary URL (`OFFICIAL_SPEC`).
  - Token exchange requires client secret authentication via HTTP Basic Auth; no public-client PKCE flow is supported (`OFFICIAL_SPEC`).
  - Refresh tokens expire after 18 months (`OFFICIAL_SPEC`).
  - Rate limits are aggregated across all users of an App ID (default 5,000 calls/day per keyset) (`OFFICIAL_SPEC`).

---

## 3. Official eBay OAuth 2.0 Requirements & Specifications

### 3.1 Authorization Code Grant Contract
- **Specification**: RFC 6749 Authorization Code Grant with proprietary eBay parameter naming (`OFFICIAL_SPEC`).
- **Authorize URL**: `https://auth.ebay.com/oauth2/authorize` (Production) / `https://auth.sandbox.ebay.com/oauth2/authorize` (Sandbox).
- **Token URL**: `https://api.ebay.com/identity/v1/oauth2/token` (Production) / `https://api.sandbox.ebay.com/identity/v1/oauth2/token` (Sandbox).
- **Mandatory Authorize Parameters**:
  - `client_id`: The eBay App ID.
  - `redirect_uri`: The **RuName** associated with the keyset. Providing a raw URI triggers an immediate error (`OFFICIAL_SPEC`).
  - `response_type`: `code`.
  - `scope`: Space-delimited scope string (e.g. `https://api.ebay.com/oauth/api_scope`).
  - `state`: CSRF prevention token, reflected unchanged back to the callback.

### 3.2 The RuName Mechanism
- In eBay's developer portal, each redirect endpoint is assigned an opaque identifier called a Redirect URL Name (**RuName**), formatted like `DeveloperName-AppName-PRD-xxxxxxxx-xxxxxxxx` (`OFFICIAL_SPEC`).
- Under the RuName configuration, the developer configures:
  - **Your auth accepted URL**: Where eBay forwards the browser on success (`https://my.home-assistant.io/redirect/oauth`).
  - **Your auth declined URL**: Where eBay forwards the browser on rejection.
- Wildcards, dynamic ports, and arbitrary IP addresses are strictly rejected by the eBay portal (`OFFICIAL_SPEC`).

### 3.3 Token Exchange and Secret Handling
- **Authentication**: `POST /identity/v1/oauth2/token` mandates HTTP Basic Authentication: `Authorization: Basic <base64(client_id:client_secret)>` (`OFFICIAL_SPEC`).
- **PKCE Support**: eBay does **not** support OAuth 2.0 PKCE (RFC 7636) for secret-less authorization code exchange. Any attempt to omit the client secret results in `401 Unauthorized` (`OFFICIAL_SPEC`).
- **Secret Redistribution Prohibition**: The eBay API License Agreement Section 5 strictly prohibits embedding, publishing, or distributing the `client_secret` (Cert ID) in client-side code, distributed desktop/mobile software, or open-source repositories (`OFFICIAL_SPEC`).

### 3.4 Token Lifetimes and Invalidation
- **Access Token**: Valid for **2 hours** (7,200 seconds) (`OFFICIAL_SPEC`).
- **Refresh Token**: Valid for **18 months** (47,304,000 seconds) (`OFFICIAL_SPEC`).
- **Early Invalidation Events**:
  - User password change or username rename (`OFFICIAL_SPEC`).
  - User manual revocation via eBay Account Settings (`OFFICIAL_SPEC`).
  - Security revocation or account restriction by eBay Trust & Safety (`OFFICIAL_SPEC`).
- **Non-Transferability**: An OAuth authorization grant and refresh token are cryptographically bound to the specific `client_id` that initiated the flow. **A refresh token issued under Developer App A cannot be used, exchanged, or refreshed by Developer App B** (`OFFICIAL_SPEC`).

### 3.5 Application Call Limits & Growth Check
- Default rate limit for new production keysets is **5,000 API calls per day** (`OFFICIAL_SPEC`).
- Home Assistant polling (15-minute interval across monitored sensors) consumes ~96 calls/day per account.
- **Threshold**: A single developer keyset reaches the 5,000 call/day limit with just ~52 active Home Assistant users (`REPO_VERIFIED`).
- **Compatible Application Check**: Raising limits beyond 5,000 calls/day requires submitting the application for formal review and approval by eBay Developer Technical Support under the Compatible Application Check / Application Growth Check program (`OFFICIAL_SPEC`).

---

## 4. Home Assistant OAuth Integration Standards

Home Assistant Core supports three primary patterns for OAuth integrations:

### 4.1 Pattern 1: User-Provided Application Credentials (`homeassistant.components.application_credentials`)
- **Mechanism**: The integration declares `"dependencies": ["application_credentials"]` in `manifest.json` and implements `application_credentials.py`.
- **Workflow**:
  - If no credentials exist, the user is presented with a standard dialog asking for `Client ID`, `Client Secret`, and optional fields (`RuName`).
  - Credentials are saved in `.storage/application_credentials` and reused across all subsequent account authorizations under that domain.
- **Applicability**: Fully supported for custom integrations in HACS (`OFFICIAL_SPEC`).
- **User Experience**: User still registers a developer application on eBay, but only enters credentials once for any number of eBay accounts.

### 4.2 Pattern 2: Cloud Account Linking (`homeassistant.helpers.config_entry_oauth2_flow.async_register_implementation`)
- **Mechanism**: Home Assistant Core partners with Nabu Casa to provide a zero-credential cloud linking proxy.
- **Workflow**:
  - Nabu Casa hosts a secure, certified OAuth client backend that holds the production developer secrets.
  - The user clicks "Link Account", is redirected through `account.nabucasa.com`, authorizes the provider, and tokens are returned directly to the user's local instance.
- **Applicability**: Strictly restricted to core Home Assistant integrations maintained by the core team. Nabu Casa does **not** provide arbitrary proxying for third-party HACS custom repositories (`OFFICIAL_SPEC`).

### 4.3 Pattern 3: Custom Embedded OAuth Implementation (Current `ha-ebay`)
- **Mechanism**: Integration implements its own `config_flow.py` and `oauth2.py` subclassing `LocalOAuth2Implementation`.
- **Workflow**: Captures credentials in form, constructs per-entry or per-session auth handlers, manages manual/callback flows.
- **Applicability**: Standard for HACS integrations requiring specialized parameters (e.g. RuName, environment selection, site ID).

---

## 5. Architectural Options Comparison Matrix

| Evaluation Criteria | Option A: BYO Credentials (Current) | Option B: HA Application Credentials | Option C: Shared Client Secret in Code | Option D1: Maintainer-Hosted Token Broker | Option D2: Nabu Casa Cloud Account Linking |
|---|---|---|---|---|---|
| **Technical Feasibility** | **High** (`REPO_VERIFIED`) | **High** (`REPO_VERIFIED`) | **Zero** (Prohibited) | **Medium** (`UNVERIFIED`) | **High** (`OFFICIAL_SPEC`) |
| **Official Support** | Fully Supported | Fully Supported | Expressly Prohibited | Permitted with App Check | Requires Nabu Casa Approval |
| **User Setup Clicks** | 6–8 steps | 4–5 steps | 1 click | 1 click | 1 click |
| **External Hosting Required** | None | None | None | Cloud Server (24/7) | Nabu Casa Infrastructure |
| **Secret Exposure Risk** | **Zero** (User owns keyset) | **Zero** (User owns keyset) | **Catastrophic** (Public) | High (Broker target) | Low (Enterprise HSM) |
| **OAuth Grant Custody** | Direct to User HA | Direct to User HA | Compromised | Transits Broker | Transits Nabu Casa |
| **Rate Limit Impact** | Isolated (5,000/user) | Isolated (5,000/user) | Shared (Collapses at 52 users) | Shared (Collapses at 52 users) | Enterprise Tier Required |
| **Multi-Account Safety** | **Proven** (M9 Isolated) | **Proven** (M9 Isolated) | Severe Collision Risk | Complex Routing | Compatible |
| **Existing-User Migration** | 100% Seamless | 100% Seamless | Requires Fresh Auth | Requires Fresh Auth | Requires Fresh Auth |
| **Operating Cost** | \$0.00 / mo | \$0.00 / mo | \$0.00 / mo | \$25–\$100+ / mo | Absorbed by Nabu Casa |
| **Legal / Compliance Risk** | Zero | Zero | Severe (Terms Breach) | High (Data Processor) | Formal Legal Agreement |
| **Recommendation** | **CONTINUE** | **RECOMMENDED NEAR-TERM** | **REJECTED (NO-GO)** | **REJECTED (NO-GO)** | **CONDITIONAL LONG-TERM GO** |

---

## 6. Threat Model & Security Boundaries

```
                 Option A / B: Direct Local OAuth (Current / Recommended)
                 ────────────────────────────────────────────────────────
 [ User HA Instance ] ──( RuName redirect via my.home-assistant.io )──> [ eBay Auth Portal ]
         │                                                                     │
         │ <───────── Code returned to user's local instance ──────────────────┘
         │
         └─────────── Authenticated POST (User Client Secret) ────────────────> [ eBay Token API ]
                                (Tokens stored only locally)

                 Option D1: Maintainer-Hosted Broker (High Risk / Rejected)
                 ────────────────────────────────────────────────────────
 [ User HA Instance ] ──( Initiates Auth )──> [ Broker Service ] ──> [ eBay Auth Portal ]
                                                    │                         │
                                          Holds Shared Secret                 │
                                                    │ <── Code return ────────┘
                                                    │
                                                    ├── Token Exchange ─────> [ eBay Token API ]
                                                    │   (Broker sees tokens)
                                                    ▼
                                          Transmits Tokens to HA
```

### Threat Scenarios Analyzed:
1. **Client Secret Disclosure (Option C)**:
   - Embedding a shared `client_secret` in a public HACS repository enables any third party to extract the secret and forge requests or impersonate the integration. eBay automated scrapers routinely detect and immediately revoke exposed keysets (`OFFICIAL_SPEC`).
2. **Token Broker Compromise (Option D1)**:
   - A central broker server handling authorization codes and client secrets becomes an attractive target for attackers. Compromise grants access to authorization codes, refresh tokens, and buyer/seller data for every connected user (`OFFICIAL_SPEC`).
3. **Cross-Account Session Confusion**:
   - In a shared broker setup, simultaneous callbacks from multiple users must be disambiguated with cryptographic state tokens. Any state mismatch or race condition risks binding User A's eBay account to User B's Home Assistant instance (`REPO_VERIFIED`).
4. **Denial of Service via Global Rate Limiting**:
   - In a shared application model (Options C, D1, D2), all users share the daily API quota. A single buggy installation or aggressive polling configuration exhausts the quota, causing all integration instances globally to fail (`OFFICIAL_SPEC`).
5. **Data Minimization & Compliance Invariant**:
   - Under GDPR and CCPA, an entity operating an authentication broker service acts as a Data Processor. The repository maintainer would assume legal obligations for security breach notifications, data handling records, and compliance audits (`OFFICIAL_SPEC`).

---

## 7. Multi-Account Safety & M9 Invariants

Any future OAuth evolution must preserve the multi-account guarantees established in Milestone 9:

1. **Durable Identity Binding**:
   - Account identity is anchored to `User.EIASToken` via read-only `GetUser` (`AccountIdentifierType.EIASTOKEN`).
   - Developer application identity (`client_id`) must never be conflated with the authenticated eBay account (`REPO_VERIFIED`).
2. **Three-State Equivalence**:
   - Identity comparison strictly distinguishes `SAME`, `DIFFERENT`, and `UNKNOWN`.
   - Mutable `USER_ID` alone can never prove account continuity (`REPO_VERIFIED`).
3. **Fail-Closed Ambiguity**:
   - When an account cannot be proved distinct from an existing entry, setup aborts with `account_ambiguous` (`REPO_VERIFIED`).
4. **Independent ConfigEntries**:
   - Authorizing multiple accounts—regardless of whether they use the same developer app or a shared broker—must yield separate `ConfigEntry` instances with unique IDs `f"{environment}:{account_id}"` (`REPO_VERIFIED`).
5. **Atomic Reauthorization**:
   - Reauthorization must commit data, title, and options in a single atomic transaction without modifying existing entry credentials on abort or error (`REPO_VERIFIED`).

---

## 8. Existing-User Migration & Rollback Strategy

### 8.1 The Non-Transferability Invariant
> [!IMPORTANT]
> **OAuth grants are cryptographically and legally tied to the issuing `client_id` (`OFFICIAL_SPEC`).**
> An existing refresh token obtained under a user's personal developer app CANNOT be refreshed or imported by a new shared application or cloud linking service. Any transition to a shared application requires every user to perform a fresh interactive OAuth sign-in and consent flow.

### 8.2 Migration Plan for Any Future Shared Path
If a shared or cloud-linked path is ever introduced:
1. **Strictly Opt-In**:
   - Existing users retain their functional BYO credentials indefinitely. No forced migration.
2. **Account Verification Prior to Replacement**:
   - If a user chooses to migrate an existing ConfigEntry to the shared path, the fresh authorization must execute read-only `GetUser` to verify `compare_account_identities(existing, new) == AccountEquivalence.SAME`.
   - If the user inadvertently authorizes a different eBay account, the migration aborts with `account_mismatch`, leaving the original credentials and tokens 100% intact.
3. **Rollback Guarantee**:
   - Users can revert to BYO credentials at any time by entering their original keyset via the reauthorization flow.
   - If the shared service becomes unavailable, the integration falls back to local BYO mode without registry invalidation.

---

## 9. Operational Feasibility, Costs & Maintenance Burden

Operating a dedicated authorization proxy (Option D1) imposes substantial non-software overhead on the maintainer:

| Category | Responsibility & Requirements | Status |
|---|---|---|
| **Hosting Infrastructure** | High-availability cloud server (AWS/GCP/Cloudflare Workers) with 99.9% uptime SLA. | \$25–\$100+ / month (`UNVERIFIED`) |
| **Secret Management** | Hardware Security Module (HSM) or Secret Manager with strict access controls. | Mandatory (`OFFICIAL_SPEC`) |
| **eBay Audits & Compliance** | Mandatory submission for eBay Compatible Application Check; compliance with eBay User Privacy and Data Protection policies. | `BLOCKED` (Requires formal business registration) |
| **Incident Response** | 24/7 on-call duty for credential compromises, DDoS attacks, or service interruptions. | Maintainer liability |
| **User Support** | Triaging token exchange failures, IP blocks, and rate limit exhaustion across global user base. | High ongoing effort |

---

## 10. Concrete Architecture Decision

### Final Recommendation: **CONDITIONAL GO (Phased Evolution)**

```
               PHASE 1 (Immediate / Current Baseline)
               BYO Developer Application Credentials
               - Retain existing robust M9 OAuth flow
               - Complete documentation & setup guides
                               │
                               ▼
               PHASE 2 (Near-Term UX Enhancement)
               Home Assistant Application Credentials
               - Add application_credentials support
               - Standardize UI credential entry
               - Retain user-owned keyset isolation
                               │
                               ▼
               PHASE 3 (Long-Term Strategic Only)
               Official Home Assistant Cloud Linking
               - Propose eBay integration for Home Assistant Core
               - Nabu Casa manages production eBay Partner keyset
               - Zero maintainer hosting burden; 100% compliant
```

### Actionable Roadmap:
1. **Immediate (M10 Conclusion)**:
   - Maintain the current BYO architecture as the only secure, zero-liability mechanism for a HACS integration.
   - Document the architectural decision clearly to set user expectations.
2. **Next Logical Step (Milestone 11 / 12)**:
   - Implement Phase 2 (`application_credentials` dependency) to streamline credential input without hosting external infrastructure.
3. **Core Inclusion Path**:
   - If one-click OAuth is deemed essential by the community, the sanctioned path is migrating `ha-ebay` into Home Assistant Core, where Nabu Casa can formally sponsor and manage the OAuth application keys.

---

## 11. Evidence Tagging & Claims Audit

| Claim | Verification Level | Reference |
|---|---|---|
| eBay OAuth 2.0 requires RuName in `redirect_uri` parameter | `OFFICIAL_SPEC` | [eBay Authorization Code Grant](https://developer.ebay.com/api-docs/static/oauth-auth-code-grant-request.html) |
| eBay token endpoint requires HTTP Basic Auth with Client Secret | `OFFICIAL_SPEC` | [eBay Token Request Guide](https://developer.ebay.com/api-docs/static/oauth-auth-code-grant-request.html) |
| PKCE is unsupported for public client secret-less token exchange | `OFFICIAL_SPEC` | [eBay Developer Portal Best Practices](https://developer.ebay.com/api-docs/static/oauth-best-practices.html) |
| Refresh tokens expire after 18 months and are non-transferable | `OFFICIAL_SPEC` | [eBay Refresh Token Expiry](https://developer.ebay.com/api-docs/static/oauth-refresh-token-request.html) |
| Default application rate limit is 5,000 API calls per day | `OFFICIAL_SPEC` | [eBay API Rate Limits](https://developer.ebay.com/develop/api/buy/api_call_limits) |
| Sharing or embedding client secret breaches Developer Agreement | `OFFICIAL_SPEC` | [eBay API License Agreement Sec. 5](https://developer.ebay.com/terms-conditions/api-license-agreement) |
| M9 establishes durable `EIASToken` identity and 3-state equivalence | `REPO_VERIFIED` | [`account_identity.py`](../custom_components/ebay/account_identity.py) |
| Atomic reauthorization commits data and title in single call | `REPO_VERIFIED` | [`config_flow.py`](../custom_components/ebay/config_flow.py) |
| GraphQL Watchlist requires ungranted scope and returns `FORBIDDEN` | `LIVE_TESTED` / `BLOCKED` | [`api-migration-feasibility.md`](api-migration-feasibility.md) |
| Nabu Casa Cloud Account Linking is restricted to HA Core | `OFFICIAL_SPEC` | [Home Assistant Developer Documentation](https://developers.home-assistant.io) |
