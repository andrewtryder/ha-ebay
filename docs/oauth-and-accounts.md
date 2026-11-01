# eBay OAuth Setup, Account Identity & Multi-Account Architecture

This guide covers authenticating Home Assistant with eBay, configuring the Bring-Your-Own (BYO) developer keyset, managing multiple eBay accounts, and the underlying account identity model.

---

## 1. Quick Start: Bring-Your-Own (BYO) OAuth Setup

eBay does not allow public, unauthenticated API access. Every Home Assistant installation connects via eBay's official OAuth 2.0 Authorization Code grant using an eBay Developer Application keyset owned by the user.

### Prerequisites

1. A regular eBay account (buyer and/or seller).
2. An [eBay Developers Program](https://developer.ebay.com/) account (free, linked to or separate from your regular eBay account).
3. Home Assistant 2026.10.0 or newer.

---

### Step-by-Step Walkthrough

#### Step 1: Start Setup in Home Assistant
1. In Home Assistant, navigate to **Settings** -> **Devices & Services** -> **Add Integration**.
2. Search for **eBay** and click to start the wizard.
3. The first screen provides guidance and displays the exact **RuName** / **Redirect URL** options needed for your eBay Developer Console.

#### Step 2: Sign In to eBay Developer Portal & Create Keyset
1. Go to [developer.ebay.com/my/keys](https://developer.ebay.com/my/keys) and sign in.
2. Under **Production**, click **Create a keyset** (or view your existing Production keyset).
3. Note your credentials:
   - **App ID (Client ID)**
   - **Cert ID (Client Secret)**

#### Step 3: Configure Redirect URL (RuName)
1. On your Production Keyset card, click **User Tokens** -> **Get a Token from eBay via Your Application** (or **Manage RuNames**).
2. In the **Your eBay Sign-in Settings** section:
   - Click **Add RuName** or edit your existing RuName.
   - Set **Your redirect URL (OAuth Accept URL)**:
     - For automatic callback: `https://my.home-assistant.io/redirect/oauth` (or your internal/external Home Assistant URL + `/auth/external/callback`).
     - For manual authorization fallback: eBay's default sign-in success page.
   - Note the generated **RuName (eBay Redirect URL name)** (e.g., `YourName-YourApp-PRD-xxxxxxxx-xxxxxxxx`).

#### Step 4: Enter Keyset in Home Assistant
1. Back in Home Assistant, enter:
   - **Environment**: Production (default) or Sandbox.
   - **App ID (Client ID)**
   - **Cert ID (Client Secret)**
   - **RuName (eBay Redirect URL name)**
2. Click **Submit**.

#### Step 5: Authorize Your eBay Account
1. Home Assistant presents a link to eBay's authorization page.
2. Click the link and sign in with the regular eBay account you want to monitor in Home Assistant.
3. Review and grant the requested permissions.
4. If using `my.home-assistant.io`, the browser redirects back to Home Assistant automatically.
5. If using manual fallback, copy the redirected URL or authorization code from the browser address bar and paste it into Home Assistant.

---

## 2. Canonical Account Identity & Uniqueness

To reliably support multiple eBay accounts on a single Home Assistant instance, `ha-ebay` implements a strict Canonical Account Identity model.

### Why Canonical Identity is Required
- eBay usernames are user-mutable (changeable once every 30 days). Using a username as the sole unique identifier leads to broken entity IDs or duplicate entries upon username changes.
- eBay User IDs (numeric, e.g. `123456789`) are immutable across the account lifecycle.
- Multiple environments (Production vs. Sandbox) must never collide even if identical usernames are registered.

### Identity Model
Each config entry computes a canonical identity structure:
1. **Environment**: `production` or `sandbox`.
2. **Canonical Account ID**: The immutable numeric eBay user ID retrieved via Trading `GetUser`.
3. **Username**: The current display username (used for friendly names, fallback logging, and diagnostics).
4. **Unique ID**: Formatted as `{environment}_{canonical_account_id}`.

### Duplicate Detection Matrix
When a new account is added or reauthorized:
- **Same environment + same canonical ID**: Rejected as a duplicate during setup; updates credentials atomically during reauth.
- **Same environment + different canonical ID**: Permitted (creates a second distinct config entry).
- **Different environment + same user ID**: Permitted (completely isolated configs and entity registries).

---

## 3. Multiple eBay Accounts

`ha-ebay` fully supports running multiple distinct eBay accounts concurrently:

- **Shared Developer Keyset**: Multiple family or business accounts can all share the same App ID, Cert ID, and RuName. Each account completes its own OAuth consent flow using its respective eBay user login.
- **Isolated State & Coordinators**: Each account runs an isolated `EbayDataUpdateCoordinator`, separate token storage, distinct poll timers, and separate repair issues.
- **Entity Unique IDs**: All entities include the unique config entry ID / canonical account ID in their unique IDs (e.g., `sensor.ebay_buyer_orders_<unique_id>`), guaranteeing zero naming conflicts.
- **Rate Limit Budget**: Different user accounts sharing an App ID share the application-level daily call limit (typically 5,000,000 Trading calls/day and 250,000 REST calls/day), which is more than sufficient for normal polling.

---

## 4. Reauthorization & Atomic Credential Updates

If OAuth tokens expire or additional scopes are enabled via Options:
1. Home Assistant flags the config entry for reauthorization (spawning a repair issue if configured).
2. The user initiates reauth via **Settings** -> **Devices & Services** -> **eBay** -> **Reconfigure / Reauthorize**.
3. The reauth flow ensures that the user logs in with the *same* eBay account. If a different account is authenticated by mistake, the flow rejects the update and prevents state corruption.
4. Token updates are written atomically to Home Assistant storage.

---

## 5. Architectural Rationale: Why Bring-Your-Own Keyset?

During development, three alternative authentication patterns were evaluated:
1. **Central Hosted Cloud Proxy**: A public multi-tenant OAuth broker hosted by integration maintainers.
   - *Rejected*: Poses immense operational liability, security risks (storing thousands of user refresh tokens), single point of failure, and severe compliance overhead with eBay data policies.
2. **Home Assistant Cloud / `application_credentials`**:
   - *Rejected*: eBay OAuth requires custom redirect URLs and RuName registration tied to the specific developer keyset. RuNames cannot be dynamically shared across arbitrary user domains without a centralized proxy.
3. **Guided BYO Keyset (Chosen)**:
   - *Approved*: Zero ongoing hosting cost, user retains 100% data ownership and privacy, direct communication between Home Assistant and eBay APIs, and independent quota allocation.
