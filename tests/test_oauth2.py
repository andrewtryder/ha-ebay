"""Tests for eBay OAuth behavior."""

from __future__ import annotations

import base64
import asyncio
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest

from homeassistant.helpers.config_entry_oauth2_flow import _decode_jwt

from custom_components.ebay.config_flow import EbayConfigFlow
from custom_components.ebay.const import (
    CONF_CLIENT_ID,
    CONF_CLIENT_SECRET,
    CONF_ENVIRONMENT,
    CONF_OAUTH_MODE,
    CONF_RUNAME,
    CONF_SITE_ID,
    ENV_SANDBOX,
    OAUTH_MODE_CALLBACK,
    OAUTH_MODE_MANUAL,
)
from custom_components.ebay.oauth2 import (
    EbayOAuth2Implementation,
    legacy_token_from_refresh_token,
)


def _flow_data(**overrides: str) -> dict[str, str]:
    data = {
        CONF_ENVIRONMENT: "production",
        CONF_CLIENT_ID: "client-id",
        CONF_CLIENT_SECRET: "client-secret",
        CONF_RUNAME: "Example-Runame",
        CONF_SITE_ID: "0",
    }
    data.update(overrides)
    return data


class _Config:
    components = {"my"}


class _Hass:
    def __init__(self) -> None:
        self.data: dict[str, Any] = {}
        self.config = _Config()


def test_authorize_url_uses_runame_and_ha_state() -> None:
    """Authorize URL sends eBay RuName while state tracks HA callback."""
    hass = _Hass()
    implementation = EbayOAuth2Implementation(hass, _flow_data())

    url = asyncio.run(implementation.async_generate_authorize_url("flow-123"))

    parsed = urlparse(url)
    params = parse_qs(parsed.query)
    assert parsed.scheme == "https"
    assert parsed.netloc == "auth.ebay.com"
    assert params["client_id"] == ["client-id"]
    assert params["redirect_uri"] == ["Example-Runame"]
    assert params["response_type"] == ["code"]
    assert "https://api.ebay.com/oauth/api_scope" in params["scope"][0]
    state = _decode_jwt(hass, params["state"][0])
    assert state["flow_id"] == "flow-123"
    assert state["redirect_uri"] == "https://my.home-assistant.io/redirect/oauth"


def test_authorize_url_uses_sandbox_endpoint() -> None:
    """Sandbox credentials use sandbox eBay authorize endpoint."""
    hass = _Hass()
    implementation = EbayOAuth2Implementation(
        hass, _flow_data(environment=ENV_SANDBOX)
    )

    url = asyncio.run(implementation.async_generate_authorize_url("flow-123"))

    assert urlparse(url).netloc == "auth.sandbox.ebay.com"


def test_token_exchange_uses_basic_auth_and_runame(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Token exchange authenticates with Basic auth and sends RuName."""
    hass = _Hass()
    requests: list[dict[str, Any]] = []

    class Response:
        status = 200
        request_info = None
        history: tuple = ()
        headers: dict[str, str] = {}

        async def json(self, *, content_type: str | None = None) -> dict[str, Any]:
            return {
                "access_token": "access",
                "refresh_token": "refresh",
                "expires_in": 7200,
                "token_type": "Bearer",
            }

    class Session:
        async def post(self, url: str, **kwargs: Any) -> Response:
            requests.append({"url": url, **kwargs})
            return Response()

    monkeypatch.setattr(
        "custom_components.ebay.oauth2.async_get_clientsession",
        lambda hass: Session(),
    )
    implementation = EbayOAuth2Implementation(hass, _flow_data())

    token = asyncio.run(
        implementation.async_resolve_external_data({"code": "code-1"})
    )

    assert token["refresh_token"] == "refresh"
    request = requests[0]
    assert request["url"] == "https://api.ebay.com/identity/v1/oauth2/token"
    assert request["data"] == {
        "grant_type": "authorization_code",
        "code": "code-1",
        "redirect_uri": "Example-Runame",
    }
    expected = base64.b64encode(b"client-id:client-secret").decode()
    assert request["headers"]["Authorization"] == f"Basic {expected}"


def test_refresh_preserves_refresh_token_when_omitted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """eBay may omit a replacement refresh token during refresh."""
    hass = _Hass()

    class Response:
        status = 200
        request_info = None
        history: tuple = ()
        headers: dict[str, str] = {}

        async def json(self, *, content_type: str | None = None) -> dict[str, Any]:
            return {"access_token": "new-access", "expires_in": 7200}

    class Session:
        async def post(self, url: str, **kwargs: Any) -> Response:
            return Response()

    monkeypatch.setattr(
        "custom_components.ebay.oauth2.async_get_clientsession",
        lambda hass: Session(),
    )
    implementation = EbayOAuth2Implementation(hass, _flow_data())

    token = asyncio.run(
        implementation.async_refresh_token(legacy_token_from_refresh_token("old-refresh"))
    )

    assert token["access_token"] == "new-access"
    assert token["refresh_token"] == "old-refresh"
    assert token["expires_at"] > 0


def test_config_flow_starts_external_step(monkeypatch: pytest.MonkeyPatch) -> None:
    """Automatic mode starts HA's external OAuth step."""
    flow = EbayConfigFlow()
    flow.hass = _Hass()
    flow.flow_id = "flow-123"
    monkeypatch.setattr(flow, "async_set_unique_id", _async_noop)
    monkeypatch.setattr(flow, "_abort_if_unique_id_configured", lambda *args: None)

    result = asyncio.run(
        flow.async_step_user({**_flow_data(), CONF_OAUTH_MODE: OAUTH_MODE_CALLBACK})
    )

    assert result["type"] == "external"
    assert result["step_id"] == "auth"
    assert "auth.ebay.com/oauth2/authorize" in result["url"]


def test_manual_fallback_still_shows_consent_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Manual mode keeps the paste-code fallback."""
    flow = EbayConfigFlow()
    flow.hass = _Hass()
    flow.flow_id = "flow-123"
    monkeypatch.setattr(flow, "async_set_unique_id", _async_noop)
    monkeypatch.setattr(flow, "_abort_if_unique_id_configured", lambda *args: None)

    result = asyncio.run(
        flow.async_step_user({**_flow_data(), CONF_OAUTH_MODE: OAUTH_MODE_MANUAL})
    )

    assert result["type"] == "form"
    assert result["step_id"] == "manual"
    assert "auth.ebay.com/oauth2/authorize" in result["description_placeholders"][
        "consent_url"
    ]


async def _async_noop(*args: Any, **kwargs: Any) -> None:
    """Async no-op for patched config-flow helpers."""
