# Native Home Assistant OAuth Design

## Architecture

The eBay integration uses a custom `EbayOAuth2Implementation` built on Home
Assistant's `config_entry_oauth2_flow` framework. Home Assistant owns external
callback routing, state validation, token expiry fields, and refresh scheduling
through `OAuth2Session`.

## RuName Handling

eBay expects the developer-console RuName in OAuth `redirect_uri`, not the
actual HTTP callback URL. The integration still asks Home Assistant to generate
its normal OAuth state and callback URL, but the eBay authorization URL and
authorization-code token request send the configured RuName as `redirect_uri`.

## Callback URL UX

The setup form displays Home Assistant's OAuth callback URL and asks the user
to register it as both Auth Accepted URL and Auth Declined URL in the eBay
Developer Program before continuing.

## Application Credentials

Application Credentials are not used. eBay's Client ID, Client secret,
environment, and RuName have to travel together to generate correct authorize
and token requests, and Home Assistant's credential model does not safely store
that eBay-specific tuple as one selectable credential.

## Migration

Version 1 entries that store a top-level `refresh_token` are migrated to
version 2 by adding Home Assistant-style `token` data seeded with the existing
refresh token. The old refresh token is preserved and can be used to mint a new
access token on the next poll.

## Reauthorization

Runtime polling uses `OAuth2Session`; token refresh failures surface as
`ConfigEntryAuthFailed`, which starts Home Assistant's native reauth flow. The
reauth flow preserves environment, RuName, Site ID, and options, then updates
the existing config entry after successful authorization.

## Manual Fallback

Manual authorization remains available from the setup form. It generates the
same eBay consent URL, accepts either a full callback URL or a raw authorization
code, validates state when a full callback URL is pasted, and stores token data
in the same version 2 shape as automatic OAuth.
