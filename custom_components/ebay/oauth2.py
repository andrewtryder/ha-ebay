"""OAuth helpers for the eBay integration."""

from __future__ import annotations

import base64
from http import HTTPStatus
import json
import logging
import time
from typing import Any, cast

from aiohttp import ClientError, ClientResponseError
from yarl import URL

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.config_entry_oauth2_flow import (
    MY_AUTH_CALLBACK_PATH,
    LocalOAuth2Implementation,
    async_get_redirect_uri,
)

from .api import DEFAULT_SCOPE, endpoints_for
from .const import (
    CONF_CLIENT_ID,
    CONF_CLIENT_SECRET,
    CONF_ENVIRONMENT,
    CONF_RUNAME,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

DEVELOPER_CONSOLE_URL = "https://developer.ebay.com/my/keys"
OAUTH_SETUP_GUIDE_URL = (
    "https://github.com/andrewtryder/ha-ebay/blob/main/docs/ebay-oauth-setup.md"
)
PRIVACY_POLICY_URL = "https://github.com/andrewtryder/ha-ebay/blob/main/PRIVACY.md"


def auth_implementation_domain(environment: str, client_id: str) -> str:
    """Return the implementation key stored with config entries."""
    return f"{DOMAIN}:{environment}:{client_id}"


def async_get_ebay_callback_url(hass: HomeAssistant) -> str:
    """Return the Home Assistant OAuth callback URL for setup instructions."""
    try:
        return async_get_redirect_uri(hass)
    except RuntimeError:
        return MY_AUTH_CALLBACK_PATH


class EbayOAuth2Implementation(LocalOAuth2Implementation):
    """eBay OAuth implementation that uses RuName as provider redirect_uri."""

    def __init__(self, hass: HomeAssistant, data: dict[str, Any]) -> None:
        """Initialize the eBay OAuth implementation."""
        environment = data[CONF_ENVIRONMENT]
        endpoints = endpoints_for(environment)
        super().__init__(
            hass,
            auth_implementation_domain(environment, data[CONF_CLIENT_ID]),
            data[CONF_CLIENT_ID],
            data[CONF_CLIENT_SECRET],
            endpoints.authorize,
            endpoints.token,
        )
        self.environment = environment
        self.runame = data[CONF_RUNAME]

    @property
    def name(self) -> str:
        """Name of the implementation."""
        return f"eBay {self.environment}"

    @property
    def redirect_uri(self) -> str:
        """Return Home Assistant's browser callback URI."""
        return async_get_ebay_callback_url(self.hass)

    @property
    def extra_authorize_data(self) -> dict[str, str]:
        """Return eBay OAuth authorize parameters."""
        return {"scope": DEFAULT_SCOPE}

    async def async_generate_authorize_url(self, flow_id: str) -> str:
        """Generate an eBay authorize URL with HA state and eBay RuName."""
        state_url = URL(await super().async_generate_authorize_url(flow_id))
        state = state_url.query["state"]
        return str(
            URL(self.authorize_url).with_query(
                {
                    "client_id": self.client_id,
                    "redirect_uri": self.runame,
                    "response_type": "code",
                    "scope": DEFAULT_SCOPE,
                    "state": state,
                }
            )
        )

    async def async_resolve_external_data(self, external_data: Any) -> dict[str, Any]:
        """Exchange callback data for eBay OAuth tokens."""
        return await self._token_request(
            {
                "grant_type": "authorization_code",
                "code": external_data["code"],
                "redirect_uri": self.runame,
            }
        )

    async def _async_refresh_token(self, token: dict[str, Any]) -> dict[str, Any]:
        """Refresh tokens, preserving eBay refresh_token omissions."""
        new_token = await self._token_request(
            {
                "grant_type": "refresh_token",
                "refresh_token": token["refresh_token"],
            }
        )
        return {**token, **new_token}

    async def _token_request(self, data: dict[str, str]) -> dict[str, Any]:
        """Make an authenticated eBay token request."""
        session = async_get_clientsession(self.hass)
        headers = {
            "Authorization": _basic_auth_header(self.client_id, self.client_secret),
            "Content-Type": "application/x-www-form-urlencoded",
        }
        _LOGGER.debug("Sending eBay OAuth token request to %s", self.token_url)
        resp = await session.post(self.token_url, headers=headers, data=data)
        if resp.status >= HTTPStatus.BAD_REQUEST:
            await _log_oauth_error(resp.status, resp)
            resp.raise_for_status()
        try:
            payload = cast(dict[str, Any], await resp.json(content_type=None))
        except (ClientError, json.JSONDecodeError) as exc:
            raise ClientResponseError(
                resp.request_info,
                resp.history,
                status=resp.status,
                message="eBay OAuth response was not valid JSON",
                headers=resp.headers,
            ) from exc
        return payload


def legacy_token_from_refresh_token(refresh_token: str) -> dict[str, Any]:
    """Return a HA OAuth token dict from a legacy refresh token."""
    return {
        "access_token": "",
        "refresh_token": refresh_token,
        "expires_in": 0,
        "expires_at": 0,
        "token_type": "Bearer",
    }


def normalize_new_token(token: dict[str, Any]) -> dict[str, Any]:
    """Return token data with HA expiry fields populated."""
    normalized = dict(token)
    normalized["expires_in"] = int(normalized["expires_in"])
    normalized["expires_at"] = time.time() + normalized["expires_in"]
    return normalized


def _basic_auth_header(client_id: str, client_secret: str) -> str:
    basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    return f"Basic {basic}"


async def _log_oauth_error(status: int, resp: Any) -> None:
    """Log an OAuth error without sensitive request data."""
    try:
        body = await resp.text()
        payload = json.loads(body)
        detail = payload.get("error_description") or payload.get("error") or "unknown"
    except (ClientError, ValueError, AttributeError):
        detail = "unknown"
    _LOGGER.debug("eBay OAuth token request failed (%s): %s", status, detail)
