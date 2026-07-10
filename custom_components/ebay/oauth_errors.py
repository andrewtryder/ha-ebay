"""OAuth token request exception compatibility helpers."""

from __future__ import annotations

try:
    from homeassistant.helpers.config_entry_oauth2_flow import (
        OAuth2TokenRequestError,
        OAuth2TokenRequestReauthError,
        OAuth2TokenRequestTransientError,
    )
except ImportError:

    class OAuth2TokenRequestError(Exception):
        """OAuth token request failed."""

    class OAuth2TokenRequestTransientError(OAuth2TokenRequestError):
        """OAuth token request failed due to a transient condition."""

    class OAuth2TokenRequestReauthError(OAuth2TokenRequestError):
        """OAuth token request failed because reauthorization is required."""
