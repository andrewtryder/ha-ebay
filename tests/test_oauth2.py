"""Tests for eBay OAuth behavior."""

from __future__ import annotations

import base64
import asyncio
import json
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest

from homeassistant import config_entries
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
    OAUTH_MODE_MANUAL,
)
from custom_components.ebay.oauth2 import (
    DEVELOPER_CONSOLE_URL,
    OAUTH_SETUP_GUIDE_URL,
    PRIVACY_POLICY_URL,
    EbayOAuth2Implementation,
    legacy_token_from_refresh_token,
)
from custom_components.ebay.oauth_errors import (
    OAuth2TokenRequestReauthError,
    OAuth2TokenRequestTransientError,
)

ROOT = Path(__file__).resolve().parents[1]


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
    implementation = EbayOAuth2Implementation(hass, _flow_data(environment=ENV_SANDBOX))

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

    token = asyncio.run(implementation.async_resolve_external_data({"code": "code-1"}))

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
        implementation.async_refresh_token(
            legacy_token_from_refresh_token("old-refresh")
        )
    )

    assert token["access_token"] == "new-access"
    assert token["refresh_token"] == "old-refresh"
    assert token["expires_at"] > 0


@pytest.mark.parametrize("status", [429, 500, 503])
def test_token_request_transient_errors_do_not_force_reauth(
    monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    """Temporary token endpoint failures are classified as retryable."""
    hass = _Hass()

    class Response:
        request_info = None
        history: tuple = ()
        headers: dict[str, str] = {}

        async def text(self) -> str:
            return json.dumps({"error": "temporarily_unavailable"})

    Response.status = status

    class Session:
        async def post(self, url: str, **kwargs: Any) -> Response:
            return Response()

    monkeypatch.setattr(
        "custom_components.ebay.oauth2.async_get_clientsession",
        lambda hass: Session(),
    )
    implementation = EbayOAuth2Implementation(hass, _flow_data())

    with pytest.raises(OAuth2TokenRequestTransientError):
        asyncio.run(implementation._token_request({"grant_type": "refresh_token"}))


def test_token_request_invalid_grant_requires_reauth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Revoked refresh tokens are classified as reauthorization failures."""
    hass = _Hass()

    class Response:
        status = 400
        request_info = None
        history: tuple = ()
        headers: dict[str, str] = {}

        async def text(self) -> str:
            return json.dumps({"error": "invalid_grant"})

    class Session:
        async def post(self, url: str, **kwargs: Any) -> Response:
            return Response()

    monkeypatch.setattr(
        "custom_components.ebay.oauth2.async_get_clientsession",
        lambda hass: Session(),
    )
    implementation = EbayOAuth2Implementation(hass, _flow_data())

    with pytest.raises(OAuth2TokenRequestReauthError):
        asyncio.run(implementation._token_request({"grant_type": "refresh_token"}))


def test_config_flow_starts_external_step(monkeypatch: pytest.MonkeyPatch) -> None:
    """Automatic mode starts HA's external OAuth step."""
    flow = _flow(monkeypatch)

    result = asyncio.run(flow.async_step_credentials(_flow_data()))

    assert result["type"] == "external"
    assert result["step_id"] == "auth"
    assert "auth.ebay.com/oauth2/authorize" in result["url"]


def test_manual_fallback_still_shows_consent_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Manual mode keeps the paste-code fallback."""
    flow = _flow(monkeypatch)
    flow._oauth_mode = OAUTH_MODE_MANUAL

    result = asyncio.run(flow.async_step_credentials(_flow_data()))

    assert result["type"] == "form"
    assert result["step_id"] == "manual"
    assert (
        "auth.ebay.com/oauth2/authorize"
        in result["description_placeholders"]["consent_url"]
    )


def test_initial_flow_shows_authorization_method_menu(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Initial setup starts with an authorization-method menu."""
    flow = _flow(monkeypatch)

    result = asyncio.run(flow.async_step_user())

    assert result["type"] == "menu"
    assert result["step_id"] == "user"
    assert result["menu_options"] == ["automatic", "manual"]


def test_automatic_selection_shows_prepare_application_step(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Automatic callback shows eBay app preparation before credentials."""
    flow = _flow(monkeypatch)

    result = asyncio.run(flow.async_step_automatic())

    assert result["type"] == "form"
    assert result["step_id"] == "prepare_application"


def test_prepare_step_has_setup_placeholders(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Prepare step includes copyable callback and documentation placeholders."""
    flow = _flow(monkeypatch)

    result = asyncio.run(flow.async_step_prepare_application())

    placeholders = result["description_placeholders"]
    assert placeholders["callback_url"] == "https://my.home-assistant.io/redirect/oauth"
    assert placeholders["developer_console_url"] == DEVELOPER_CONSOLE_URL
    assert placeholders["oauth_setup_guide_url"] == OAUTH_SETUP_GUIDE_URL
    assert placeholders["privacy_policy_url"] == PRIVACY_POLICY_URL


def test_prepare_continue_shows_credentials_form(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Continuing past app preparation asks for credentials."""
    flow = _flow(monkeypatch)

    result = asyncio.run(flow.async_step_prepare_application({}))

    assert result["type"] == "form"
    assert result["step_id"] == "credentials"


def test_credentials_form_has_expected_fields_and_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Credentials form no longer asks for oauth_mode and has helpful defaults."""
    flow = _flow(monkeypatch)

    result = asyncio.run(flow.async_step_credentials())

    fields = _schema_fields(result["data_schema"])
    assert CONF_OAUTH_MODE not in fields
    assert fields[CONF_ENVIRONMENT].default() == "production"
    assert fields[CONF_SITE_ID].default() == "0"


def test_manual_selection_reaches_credentials_then_manual_step(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Manual menu choice still collects credentials before showing consent URL."""
    flow = _flow(monkeypatch)

    credentials = asyncio.run(flow.async_step_manual())
    assert credentials["step_id"] == "credentials"

    manual = asyncio.run(flow.async_step_credentials(_flow_data()))
    assert manual["step_id"] == "manual"


def test_manual_consent_url_is_generated_correctly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Manual consent URL uses configured environment, client ID, RuName, and state."""
    flow = _flow(monkeypatch)
    flow._oauth_mode = OAUTH_MODE_MANUAL

    result = asyncio.run(flow.async_step_credentials(_flow_data()))

    consent_url = result["description_placeholders"]["consent_url"]
    parsed = urlparse(consent_url)
    params = parse_qs(parsed.query)
    assert parsed.netloc == "auth.ebay.com"
    assert params["client_id"] == ["client-id"]
    assert params["redirect_uri"] == ["Example-Runame"]
    assert params["state"] == [flow._state]


def test_reauth_updates_existing_entry(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reauth updates the existing entry after native OAuth completes."""
    flow = _flow(monkeypatch)
    flow.context = {
        "source": config_entries.SOURCE_REAUTH,
        "entry_id": "entry-1",
    }
    entry = type("Entry", (), {"data": _flow_data(), "title": "eBay"})()
    flow._data = _flow_data(client_secret="new-secret")
    updated: dict[str, Any] = {}

    def _update(entry_arg: Any, **kwargs: Any) -> dict[str, Any]:
        updated["entry"] = entry_arg
        updated["data"] = kwargs["data"]
        return {"type": "abort", "reason": "reauth_successful"}

    monkeypatch.setattr(flow, "_get_reauth_entry", lambda: entry)
    monkeypatch.setattr(flow, "async_update_reload_and_abort", _update)

    result = asyncio.run(
        flow.async_oauth_create_entry(
            {
                "auth_implementation": "ebay:production:client-id",
                "token": {
                    "access_token": "access",
                    "refresh_token": "refresh",
                    "expires_in": 7200,
                    "token_type": "Bearer",
                },
            }
        )
    )

    assert result == {"type": "abort", "reason": "reauth_successful"}
    assert updated["entry"] is entry
    assert updated["data"][CONF_CLIENT_SECRET] == "new-secret"
    assert updated["data"]["token"]["refresh_token"] == "refresh"


def test_reauth_credentials_prefill_safe_fields_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reauth prefills app identifiers but still requires the secret again."""
    flow = _flow(monkeypatch)
    flow.context = {
        "source": config_entries.SOURCE_REAUTH,
        "entry_id": "entry-1",
    }
    entry = type(
        "Entry",
        (),
        {
            "data": _flow_data(client_secret="stored-secret", site_id="77"),
            "title": "eBay",
        },
    )()
    monkeypatch.setattr(flow, "_get_reauth_entry", lambda: entry)

    result = asyncio.run(flow.async_step_credentials())

    fields = _schema_fields(result["data_schema"])
    assert fields[CONF_ENVIRONMENT].default() == "production"
    assert fields[CONF_CLIENT_ID].default() == "client-id"
    assert fields[CONF_CLIENT_SECRET].default() == ""
    assert fields[CONF_RUNAME].default() == "Example-Runame"
    assert fields[CONF_SITE_ID].default() == "77"


def test_legacy_manual_alias_still_uses_manual_step(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Legacy in-progress oauth step remains an alias for manual paste."""
    flow = _flow(monkeypatch)
    flow._data = _flow_data()
    flow._consent_url = "https://auth.ebay.com/oauth2/authorize?state=legacy"

    result = asyncio.run(flow.async_step_oauth())

    assert result["step_id"] == "manual"
    assert result["description_placeholders"]["consent_url"] == flow._consent_url


def test_strings_and_english_translations_match_setup_keys() -> None:
    """English translations stay synchronized with base strings."""
    strings = json.loads((ROOT / "custom_components/ebay/strings.json").read_text())
    english = json.loads(
        (ROOT / "custom_components/ebay/translations/en.json").read_text()
    )

    assert strings == english
    steps = strings["config"]["step"]
    for step in (
        "prepare_application",
        "credentials",
        "manual",
    ):
        assert step in steps
    assert "menu_options" in steps["user"]
    assert "authorization_method" not in steps
    assert "oauth_mode" not in steps["credentials"]["data"]


def _flow(monkeypatch: pytest.MonkeyPatch) -> EbayConfigFlow:
    """Return a config flow with lightweight Home Assistant shims."""
    flow = EbayConfigFlow()
    flow.hass = _Hass()
    flow.flow_id = "flow-123"
    monkeypatch.setattr(flow, "async_set_unique_id", _async_noop)
    monkeypatch.setattr(flow, "_abort_if_unique_id_configured", lambda *args: None)
    return flow


def _schema_fields(schema: Any) -> dict[str, Any]:
    """Map a voluptuous schema's fields by Home Assistant config key."""
    return {field.schema: field for field in schema.schema}


async def _async_noop(*args: Any, **kwargs: Any) -> None:
    """Async no-op for patched config-flow helpers."""
