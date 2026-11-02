"""Tests for eBay OAuth behavior."""

from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, ClassVar
from urllib.parse import parse_qs, urlparse

import probatio
import pytest
import voluptuous as vol
from homeassistant import config_entries
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.config_entry_oauth2_flow import (
    MY_AUTH_CALLBACK_PATH,
    _decode_jwt,
)
from voluptuous_serialize import convert

from custom_components.ebay.account_identity import (
    AccountBindingState,
    AccountIdentifierType,
    AccountIdentity,
    AccountIdentityProvenance,
)
from custom_components.ebay.clients.endpoints import build_consent_url
from custom_components.ebay.clients.scopes import CORE_SCOPE, DEFAULT_SCOPE
from custom_components.ebay.config_flow import EbayConfigFlow
from custom_components.ebay.const import (
    CONF_ACCOUNT_IDENTITY,
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
    async_get_ebay_callback_url,
    legacy_token_from_refresh_token,
)
from custom_components.ebay.oauth_errors import (
    OAuth2TokenRequestError,
    OAuth2TokenRequestReauthError,
    OAuth2TokenRequestTransientError,
    oauth_token_error,
    oauth_token_request_error,
    oauth_token_transient_error,
)

ROOT = Path(__file__).resolve().parents[1]

FEEDBACK_SCOPE = "https://api.ebay.com/oauth/api_scope/commerce.feedback"
FEEDBACK_READONLY_SCOPE = (
    "https://api.ebay.com/oauth/api_scope/commerce.feedback.readonly"
)
EXPECTED_AUTH_CODE_SCOPES = {
    "https://api.ebay.com/oauth/api_scope",
    "https://api.ebay.com/oauth/api_scope/sell.analytics.readonly",
    "https://api.ebay.com/oauth/api_scope/sell.fulfillment.readonly",
    "https://api.ebay.com/oauth/api_scope/sell.payment.dispute",
    FEEDBACK_SCOPE,
    "https://api.ebay.com/oauth/api_scope/commerce.message",
    "https://api.ebay.com/oauth/api_scope/sell.account.readonly",
    "https://api.ebay.com/oauth/api_scope/sell.finances",
}


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
    components: ClassVar[set[str]] = {"my"}


class _Hass:
    def __init__(self) -> None:
        self.data: dict[str, Any] = {}
        self.config = _Config()


def _scopes_from_url(url: str) -> list[str]:
    """Parse space-delimited OAuth scopes from an authorize URL."""
    params = parse_qs(urlparse(url).query)
    return params["scope"][0].split()


def test_default_scope_union_uses_auth_code_feedback_scope() -> None:
    """Full optional scope union uses commerce.feedback, not the readonly variant."""
    scopes = DEFAULT_SCOPE.split()

    assert FEEDBACK_SCOPE in scopes
    assert FEEDBACK_READONLY_SCOPE not in scopes
    assert set(scopes) == EXPECTED_AUTH_CODE_SCOPES
    assert len(DEFAULT_SCOPE.split()) == len(set(DEFAULT_SCOPE.split()))
    assert len(scopes) == len(set(scopes))


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
    assert params["scope"] == [CORE_SCOPE]
    state = _decode_jwt(hass, params["state"][0])
    assert state["flow_id"] == "flow-123"
    assert state["redirect_uri"] == "https://my.home-assistant.io/redirect/oauth"


def test_oauth2_authorize_url_defaults_to_core_scope() -> None:
    """EbayOAuth2Implementation defaults to the core Trading API scope."""
    hass = _Hass()
    implementation = EbayOAuth2Implementation(hass, _flow_data())

    url = asyncio.run(implementation.async_generate_authorize_url("flow-123"))
    scopes = _scopes_from_url(url)

    assert scopes == [CORE_SCOPE]
    assert FEEDBACK_SCOPE not in scopes


def test_oauth2_authorize_url_uses_requested_scope() -> None:
    """EbayOAuth2Implementation can request an expanded scope set."""
    hass = _Hass()
    implementation = EbayOAuth2Implementation(hass, _flow_data(), scope=DEFAULT_SCOPE)

    url = asyncio.run(implementation.async_generate_authorize_url("flow-123"))
    scopes = _scopes_from_url(url)

    assert FEEDBACK_SCOPE in scopes
    assert FEEDBACK_READONLY_SCOPE not in scopes
    assert set(scopes) == EXPECTED_AUTH_CODE_SCOPES
    assert len(scopes) == len(set(scopes))


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
        headers: ClassVar[dict[str, str]] = {}

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
        headers: ClassVar[dict[str, str]] = {}

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
        headers: ClassVar[dict[str, str]] = {}

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
        headers: ClassVar[dict[str, str]] = {}

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


def test_token_request_network_error_is_transient(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Transport failures on the token endpoint are retryable."""
    from aiohttp import ClientError

    hass = _Hass()

    class Session:
        async def post(self, url: str, **kwargs: Any) -> Any:
            raise ClientError("connection reset")

    monkeypatch.setattr(
        "custom_components.ebay.oauth2.async_get_clientsession",
        lambda hass: Session(),
    )
    implementation = EbayOAuth2Implementation(hass, _flow_data())

    with pytest.raises(OAuth2TokenRequestTransientError):
        asyncio.run(implementation._token_request({"grant_type": "refresh_token"}))


def test_token_request_non_json_success_raises_generic_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 200 response that is not JSON becomes a generic token request error."""
    hass = _Hass()

    class Response:
        status = 200

        async def json(self, *, content_type: str | None = None) -> Any:
            raise json.JSONDecodeError("Expecting value", "", 0)

    class Session:
        async def post(self, url: str, **kwargs: Any) -> Response:
            return Response()

    monkeypatch.setattr(
        "custom_components.ebay.oauth2.async_get_clientsession",
        lambda hass: Session(),
    )
    implementation = EbayOAuth2Implementation(hass, _flow_data())

    with pytest.raises(OAuth2TokenRequestError):
        asyncio.run(implementation._token_request({"grant_type": "refresh_token"}))


def test_token_request_non_json_error_body_still_classifies_by_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unreadable error bodies still map HTTP 400 to reauthorization."""
    hass = _Hass()

    class Response:
        status = 400
        request_info = None
        history: tuple = ()
        headers: ClassVar[dict[str, str]] = {}

        async def text(self) -> str:
            return "<!html>gateway error"

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


@pytest.mark.parametrize(
    ("status", "error_code", "expected"),
    [
        (400, "invalid_client", OAuth2TokenRequestReauthError),
        (403, "access_denied", OAuth2TokenRequestReauthError),
        (418, "teapot", OAuth2TokenRequestError),
        (402, None, OAuth2TokenRequestError),
    ],
)
def test_oauth_token_error_classifier_matrix(
    status: int, error_code: str | None, expected: type[Exception]
) -> None:
    """oauth_token_error maps statuses and error codes to the right HA exception."""
    exc = oauth_token_error(status, error_code=error_code)
    assert isinstance(exc, expected)
    assert type(exc) is expected


def test_oauth_token_helpers_accept_request_info_and_urls() -> None:
    from aiohttp import RequestInfo
    from multidict import CIMultiDict, CIMultiDictProxy
    from yarl import URL

    url = URL("https://example.test/token")
    info = RequestInfo(
        url=url,
        method="POST",
        headers=CIMultiDictProxy(CIMultiDict()),
        real_url=url,
    )
    transient = oauth_token_error(503, request_info=info)
    assert isinstance(transient, OAuth2TokenRequestTransientError)
    assert transient.request_info is info

    custom = oauth_token_transient_error(
        "temp", token_url="https://api.sandbox.ebay.com/identity/v1/oauth2/token"
    )
    assert isinstance(custom, OAuth2TokenRequestTransientError)
    assert "sandbox" in str(custom.request_info.url)

    generic = oauth_token_request_error("failed", status=418, request_info=info)
    assert isinstance(generic, OAuth2TokenRequestError)
    assert generic.request_info is info


def test_oauth_implementation_name_includes_environment() -> None:
    implementation = EbayOAuth2Implementation(_Hass(), _flow_data())
    assert implementation.name == "eBay production"
    sandbox = EbayOAuth2Implementation(_Hass(), _flow_data(environment=ENV_SANDBOX))
    assert sandbox.name == f"eBay {ENV_SANDBOX}"


def test_oauth_errors_compat_classes_when_ha_import_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fallback exception classes are defined when HA OAuth helpers are missing."""
    import importlib
    import sys

    monkeypatch.setitem(
        sys.modules, "homeassistant.helpers.config_entry_oauth2_flow", None
    )
    sys.modules.pop("custom_components.ebay.oauth_errors", None)
    mod = importlib.import_module("custom_components.ebay.oauth_errors")
    err = mod.OAuth2TokenRequestError("boom")
    assert str(err) == "boom"
    assert issubclass(mod.OAuth2TokenRequestTransientError, mod.OAuth2TokenRequestError)
    assert issubclass(mod.OAuth2TokenRequestReauthError, mod.OAuth2TokenRequestError)
    blank = mod.OAuth2TokenRequestError()
    assert "OAuth token request failed" in str(blank)

    monkeypatch.undo()
    sys.modules.pop("custom_components.ebay.oauth_errors", None)
    importlib.import_module("custom_components.ebay.oauth_errors")


def test_callback_url_falls_back_when_redirect_uri_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Setup instructions fall back to My callback path without a redirect URI."""
    monkeypatch.setattr(
        "custom_components.ebay.oauth2.async_get_redirect_uri",
        lambda hass: (_ for _ in ()).throw(RuntimeError("no my component")),
    )
    assert async_get_ebay_callback_url(_Hass()) == MY_AUTH_CALLBACK_PATH


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


def test_user_step_shows_guided_menu(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Initial setup presents a menu offering guided onboarding or direct credential entry."""
    flow = _flow(monkeypatch)

    result = asyncio.run(flow.async_step_user())

    assert result["type"] == "menu"
    assert result["step_id"] == "user"
    assert result["menu_options"] == ["redirect_guidance", "credentials"]
    assert (
        result["description_placeholders"]["developer_console_url"]
        == DEVELOPER_CONSOLE_URL
    )
    assert (
        result["description_placeholders"]["oauth_setup_guide_url"]
        == OAUTH_SETUP_GUIDE_URL
    )


def test_user_step_with_credentials_bypasses_menu(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Submitting credentials directly to async_step_user proceeds to authorization."""
    flow = _flow(monkeypatch)

    result = asyncio.run(flow.async_step_user(_flow_data()))

    assert result["type"] == "external"


def test_redirect_guidance_step_shows_copyable_urls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Guided redirect step displays exact runtime callback URLs and setup links."""
    flow = _flow(monkeypatch)

    result = asyncio.run(flow.async_step_redirect_guidance())

    assert result["type"] == "form"
    assert result["step_id"] == "redirect_guidance"
    assert (
        result["description_placeholders"]["callback_url"]
        == "https://my.home-assistant.io/redirect/oauth"
    )
    assert (
        result["description_placeholders"]["developer_console_url"]
        == DEVELOPER_CONSOLE_URL
    )
    assert (
        result["description_placeholders"]["privacy_policy_url"] == PRIVACY_POLICY_URL
    )
    assert (
        result["description_placeholders"]["oauth_setup_guide_url"]
        == OAUTH_SETUP_GUIDE_URL
    )


def test_redirect_guidance_submit_advances_to_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Submitting the redirect guidance informational form proceeds to credentials."""
    flow = _flow(monkeypatch)

    result = asyncio.run(flow.async_step_redirect_guidance({}))

    assert result["type"] == "form"
    assert result["step_id"] == "credentials"


def test_automatic_selection_shows_credentials_step(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Automatic callback goes directly to credentials."""
    flow = _flow(monkeypatch)

    result = asyncio.run(flow.async_step_automatic())

    assert result["type"] == "form"
    assert result["step_id"] == "credentials"


def test_prepare_application_alias_shows_redirect_guidance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Legacy prepare_application step alias forwards to redirect_guidance."""
    flow = _flow(monkeypatch)

    result = asyncio.run(flow.async_step_prepare_application())

    assert result["type"] == "form"
    assert result["step_id"] == "redirect_guidance"


def test_authorization_method_alias_shows_user_menu(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Legacy authorization_method step alias forwards to async_step_user."""
    flow = _flow(monkeypatch)

    result = asyncio.run(flow.async_step_authorization_method())

    assert result["type"] == "menu"
    assert result["step_id"] == "user"


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


def test_credentials_schema_is_frontend_serializable() -> None:
    """Credential schema must serialize for Home Assistant's frontend forms."""
    flow = EbayConfigFlow()
    schema = flow._credentials_schema({})

    convert(schema, custom_serializer=cv.custom_serializer)


def test_credentials_reject_blank_required_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Blank app credentials are rejected before OAuth starts."""
    flow = _flow(monkeypatch)

    result = asyncio.run(
        flow.async_step_credentials(
            _flow_data(
                client_id=" ",
                client_secret="\t",
                runame="\n",
                site_id=" ",
            )
        )
    )

    assert result["type"] == "form"
    assert result["step_id"] == "credentials"
    assert result["errors"] == {
        CONF_CLIENT_ID: "required",
        CONF_CLIENT_SECRET: "required",
        CONF_RUNAME: "required",
        CONF_SITE_ID: "required",
    }


@pytest.mark.parametrize(
    "malformed_runame,expected_error",
    [
        ("https://my.home-assistant.io/redirect/oauth", "invalid_runame_url"),
        ("http://example.com/callback", "invalid_runame_url"),
        ("invalid/runame", "invalid_runame_url"),
        ("Home Assistant", "invalid_runame_title"),
        ("homeassistant", "invalid_runame_title"),
        ("Display Title", "invalid_runame_title"),
        ("My App RuName", "invalid_runame_title"),
        ("RuName\tWithTab", "invalid_runame"),
        ("RuName\nWithNewline", "invalid_runame"),
        ("RuName\rWithCR", "invalid_runame"),
        ("RuName\0WithNull", "invalid_runame"),
        ("RuName\u00a0WithNBSP", "invalid_runame"),
        ("RuName\u200bWithZeroWidth", "invalid_runame"),
    ],
)
def test_credentials_reject_invalid_runame_formats(
    monkeypatch: pytest.MonkeyPatch,
    malformed_runame: str,
    expected_error: str,
) -> None:
    """URLs, display titles, and control chars pasted into RuName are rejected with actionable errors."""
    flow = _flow(monkeypatch)

    result = asyncio.run(
        flow.async_step_credentials(_flow_data(runame=malformed_runame))
    )

    assert result["type"] == "form"
    assert result["step_id"] == "credentials"
    assert result["errors"] == {CONF_RUNAME: expected_error}


@pytest.mark.parametrize(
    "malformed_client_id,runame_val,expected_error",
    [
        ("https://developer.ebay.com", "Example-Runame", "invalid_client_id_url"),
        ("http://localhost:8123", "Example-Runame", "invalid_client_id_url"),
        ("client/id", "Example-Runame", "invalid_client_id_url"),
        ("client id with space", "Example-Runame", "invalid_client_id"),
        ("client\tid\twith\ttab", "Example-Runame", "invalid_client_id"),
        ("client\nid\nwith\nnewline", "Example-Runame", "invalid_client_id"),
        ("client\0id", "Example-Runame", "invalid_client_id"),
        ("client\u00a0id", "Example-Runame", "invalid_client_id"),
        ("client\u200bid", "Example-Runame", "invalid_client_id"),
        ("Example-Runame", "Example-Runame", "invalid_client_id_runame"),
    ],
)
def test_credentials_reject_invalid_client_id_formats(
    monkeypatch: pytest.MonkeyPatch,
    malformed_client_id: str,
    runame_val: str,
    expected_error: str,
) -> None:
    """URLs, whitespace, control chars, and RuNames pasted into Client ID are rejected."""
    flow = _flow(monkeypatch)

    result = asyncio.run(
        flow.async_step_credentials(
            _flow_data(client_id=malformed_client_id, runame=runame_val)
        )
    )

    assert result["type"] == "form"
    assert result["step_id"] == "credentials"
    assert result["errors"] == {CONF_CLIENT_ID: expected_error}


def test_credentials_preserves_valid_fields_when_validation_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Valid non-secret credentials are preserved in form defaults when field validation fails."""
    flow = _flow(monkeypatch)

    result = asyncio.run(
        flow.async_step_credentials(
            _flow_data(
                client_id="my-valid-client",
                client_secret="my-valid-secret",
                runame="https://my.home-assistant.io/redirect/oauth",
                site_id="0",
            )
        )
    )

    assert result["type"] == "form"
    assert result["step_id"] == "credentials"
    assert result["errors"] == {CONF_RUNAME: "invalid_runame_url"}

    fields = _schema_fields(result["data_schema"])
    assert fields[CONF_CLIENT_ID].default() == "my-valid-client"
    assert fields[CONF_CLIENT_SECRET].default is vol.UNDEFINED
    assert fields[CONF_SITE_ID].default() == "0"

    # Verify serialized fields via probatio: client_secret must never have a default
    serialized = probatio.to_field_list(
        result["data_schema"], custom_serializer=cv.custom_serializer
    )
    secret_field = next(f for f in serialized if f["name"] == CONF_CLIENT_SECRET)
    assert "default" not in secret_field
    assert secret_field["selector"] == {
        "text": {"type": "password", "multiline": False, "multiple": False}
    }
    assert "my-valid-secret" not in str(result)


def test_reauth_bypasses_introductory_guidance_and_preserves_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reauthorization skips intro and redirect screens and pre-fills non-secret credentials."""
    flow = _flow(monkeypatch)
    flow.context = {
        "source": config_entries.SOURCE_REAUTH,
        "entry_id": "reauth_entry_id",
    }
    mock_entry = SimpleNamespace(
        entry_id="reauth_entry_id",
        unique_id="production:EIASTOKEN123",
        title="eBay (testuser)",
        data={
            CONF_ENVIRONMENT: "production",
            CONF_CLIENT_ID: "existing-client-id",
            CONF_CLIENT_SECRET: "existing-client-secret",
            CONF_RUNAME: "Existing-Runame",
            CONF_SITE_ID: "0",
        },
        options={},
    )
    monkeypatch.setattr(flow, "_get_reauth_entry", lambda: mock_entry)

    # Reauth confirm step
    result_confirm = asyncio.run(flow.async_step_reauth_confirm())
    assert result_confirm["type"] == "form"
    assert result_confirm["step_id"] == "reauth_confirm"

    # Confirming reauth leads directly to credentials without showing user or redirect_guidance
    result_creds = asyncio.run(flow.async_step_reauth_confirm({}))
    assert result_creds["type"] == "form"
    assert result_creds["step_id"] == "credentials"

    fields = _schema_fields(result_creds["data_schema"])
    assert fields[CONF_CLIENT_ID].default() == "existing-client-id"
    assert fields[CONF_CLIENT_SECRET].default is vol.UNDEFINED
    assert fields[CONF_RUNAME].default() == "Existing-Runame"
    assert fields[CONF_SITE_ID].default() == "0"
    assert "existing-client-secret" not in str(result_creds)


def test_credentials_are_stripped_before_unique_id_and_oauth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stored credentials and unique IDs use normalized form values."""
    flow = _flow(monkeypatch)
    unique_ids: list[str] = []

    async def _record_unique_id(unique_id: str) -> None:
        unique_ids.append(unique_id)

    monkeypatch.setattr(flow, "async_set_unique_id", _record_unique_id)

    result = asyncio.run(
        flow.async_step_credentials(
            _flow_data(
                client_id=" client-id ",
                client_secret=" client-secret ",
                runame=" Example-Runame ",
                site_id=" 0 ",
            )
        )
    )

    assert result["type"] == "external"
    assert flow._data[CONF_CLIENT_ID] == "client-id"
    assert flow._data[CONF_CLIENT_SECRET] == "client-secret"
    assert flow._data[CONF_RUNAME] == "Example-Runame"
    assert flow._data[CONF_SITE_ID] == "0"

    # In M9, unique ID is set after OAuth upon verified account identity
    asyncio.run(
        flow.async_oauth_create_entry(
            {
                "auth_implementation": "ebay:production:client-id",
                "token": {
                    "access_token": "access",
                    "refresh_token": "refresh",
                    "expires_in": 7200,
                },
            }
        )
    )
    assert unique_ids == ["production:test_user_id"]


def test_credentials_reject_invalid_site_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unknown or invalid marketplace Site IDs are rejected."""
    flow = _flow(monkeypatch)

    result = asyncio.run(flow.async_step_credentials(_flow_data(site_id="999")))

    assert result["type"] == "form"
    assert result["step_id"] == "credentials"
    assert result["errors"] == {CONF_SITE_ID: "invalid_site_id"}


def test_credentials_manual_checkbox_transitions_to_manual_step(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Checking manual_authorization opens manual consent step directly."""
    flow = _flow(monkeypatch)

    result = asyncio.run(
        flow.async_step_credentials({**_flow_data(), "manual_authorization": True})
    )

    assert result["type"] == "form"
    assert result["step_id"] == "manual"
    assert (
        "auth.ebay.com/oauth2/authorize"
        in result["description_placeholders"]["consent_url"]
    )


def test_automatic_creation_abort_fails_cleanly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When automatic OAuth token resolution fails, flow aborts cleanly."""
    flow = _flow(monkeypatch)
    asyncio.run(flow.async_step_credentials(_flow_data()))

    flow.external_data = {"code": "bad-code"}

    async def _fail_resolve(data: Any) -> dict[str, Any]:
        raise oauth_token_request_error(
            "oauth_failed", token_url="https://api.ebay.com"
        )

    monkeypatch.setattr(
        flow.flow_impl,
        "async_resolve_external_data",
        _fail_resolve,
    )

    result = asyncio.run(flow.async_step_creation())

    assert result["type"] == "abort"
    assert result["reason"] == "oauth_failed"


def test_credentials_normalizes_and_strips_manual_authorization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """manual_authorization is never stored in flow._data or created entry."""
    flow = _flow(monkeypatch)

    result = asyncio.run(
        flow.async_step_credentials({**_flow_data(), "manual_authorization": True})
    )

    assert result["type"] == "form"
    assert result["step_id"] == "manual"
    assert "manual_authorization" not in flow._data

    entry_result = asyncio.run(
        flow.async_oauth_create_entry(
            {
                "auth_implementation": "ebay:production:client-id",
                "token": {
                    "access_token": "access",
                    "refresh_token": "refresh",
                    "expires_in": 7200,
                },
            }
        )
    )
    assert "manual_authorization" not in entry_result["data"]


def test_marketplace_dropdown_options_serialize_correctly() -> None:
    """Marketplace dropdown options match supported marketplaces."""
    from custom_components.ebay.marketplace import (
        MARKETPLACE_NAMES,
        SITE_ID_TO_MARKETPLACE,
    )

    flow = EbayConfigFlow()
    schema = flow._credentials_schema({})
    fields = _schema_fields(schema)

    assert set(MARKETPLACE_NAMES.keys()) == set(SITE_ID_TO_MARKETPLACE.keys())
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


def test_manual_consent_url_requests_core_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Manual OAuth consent URL requests only the core scope for new installs."""
    flow = _flow(monkeypatch)
    flow._oauth_mode = OAUTH_MODE_MANUAL

    result = asyncio.run(flow.async_step_credentials(_flow_data()))
    scopes = _scopes_from_url(result["description_placeholders"]["consent_url"])

    assert scopes == [CORE_SCOPE]
    assert FEEDBACK_SCOPE not in scopes


def test_manual_submit_rejects_mismatched_callback_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pasted callback URLs with the wrong OAuth state are rejected."""
    flow = _flow(monkeypatch)
    flow._oauth_mode = OAUTH_MODE_MANUAL
    flow._data = _flow_data()
    flow._state = "expected-state"
    flow._consent_url = "https://auth.ebay.com/oauth2/authorize"

    result = asyncio.run(
        flow.async_step_manual(
            {
                "authorization_code": (
                    "https://my.home-assistant.io/redirect/oauth"
                    "?code=auth-code&state=wrong-state"
                )
            }
        )
    )

    assert result["type"] == "form"
    assert result["step_id"] == "manual"
    assert result["errors"] == {"base": "invalid_state"}


def test_manual_submit_auth_error_becomes_cannot_connect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Token exchange failures on the manual path surface cannot_connect."""
    from custom_components.ebay.api import EbayAuthError

    flow = _flow(monkeypatch)
    flow._oauth_mode = OAUTH_MODE_MANUAL
    flow._data = _flow_data()
    flow._state = "state-1"
    flow._consent_url = "https://auth.ebay.com/oauth2/authorize"
    flow._requested_scopes = CORE_SCOPE

    class Client:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        async def async_exchange_authorization_code(self, code: str) -> dict[str, Any]:
            raise EbayAuthError("exchange failed")

    monkeypatch.setattr(
        "custom_components.ebay.config_flow.EbayApiClient",
        Client,
    )
    monkeypatch.setattr(
        "custom_components.ebay.config_flow.async_get_clientsession",
        lambda hass: object(),
    )

    result = asyncio.run(
        flow.async_step_manual({"authorization_code": "raw-auth-code"})
    )

    assert result["type"] == "form"
    assert result["step_id"] == "manual"
    assert result["errors"] == {"base": "cannot_connect"}


def test_manual_reauth_updates_existing_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Successful manual reauth updates the existing entry instead of creating."""
    flow = _flow(monkeypatch)
    flow.context = {
        "source": config_entries.SOURCE_REAUTH,
        "entry_id": "entry-1",
    }
    flow._oauth_mode = OAUTH_MODE_MANUAL
    flow._data = _flow_data(client_secret="new-secret")
    flow._state = "state-1"
    flow._consent_url = "https://auth.ebay.com/oauth2/authorize"
    flow._requested_scopes = CORE_SCOPE
    entry = type(
        "Entry",
        (),
        {
            "entry_id": "entry-1",
            "title": "eBay (test_user)",
            "data": {
                **_flow_data(),
                CONF_ACCOUNT_IDENTITY: {
                    "entry_id": "entry-1",
                    "environment": "production",
                    "client_id": "client-id",
                    "account_id": "test_user_id",
                    "identifier_type": "eias_token",
                    "username": "test_user",
                    "binding_state": "verified",
                    "provenance": "authenticated_get_user",
                    "reauth_generation": 0,
                },
            },
        },
    )()
    updated: dict[str, Any] = {}

    class Client:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        async def async_exchange_authorization_code(self, code: str) -> dict[str, Any]:
            return {
                "access_token": "access",
                "refresh_token": "refresh",
                "expires_in": 7200,
                "token_type": "Bearer",
            }

    def _update(entry_arg: Any, **kwargs: Any) -> dict[str, Any]:
        updated["entry"] = entry_arg
        updated["data"] = kwargs["data"]
        return {"type": "abort", "reason": "reauth_successful"}

    monkeypatch.setattr(
        "custom_components.ebay.config_flow.EbayApiClient",
        Client,
    )
    monkeypatch.setattr(
        "custom_components.ebay.config_flow.async_get_clientsession",
        lambda hass: object(),
    )
    monkeypatch.setattr(flow, "_get_reauth_entry", lambda: entry)
    monkeypatch.setattr(flow, "async_update_and_abort", _update)
    flow.flow_impl = EbayOAuth2Implementation(flow.hass, flow._data)

    result = asyncio.run(
        flow.async_step_manual({"authorization_code": "raw-auth-code"})
    )

    assert result == {"type": "abort", "reason": "reauth_successful"}
    assert updated["entry"] is entry
    assert updated["data"][CONF_CLIENT_SECRET] == "new-secret"
    assert updated["data"]["token"]["refresh_token"] == "refresh"
    assert "manual_authorization" not in updated["data"]


def test_build_consent_url_defaults_to_core_scope() -> None:
    """build_consent_url defaults to the core Trading API scope."""
    url = build_consent_url(
        "production",
        "client-id",
        "Example-Runame",
        "state-1",
    )
    scopes = _scopes_from_url(url)

    assert scopes == [CORE_SCOPE]
    assert FEEDBACK_SCOPE not in scopes


def test_build_consent_url_accepts_expanded_scopes() -> None:
    """build_consent_url embeds an explicit expanded Authorization Code scope set."""
    url = build_consent_url(
        "production",
        "client-id",
        "Example-Runame",
        "state-1",
        scope=DEFAULT_SCOPE,
    )
    scopes = _scopes_from_url(url)

    assert FEEDBACK_SCOPE in scopes
    assert FEEDBACK_READONLY_SCOPE not in scopes
    assert set(scopes) == EXPECTED_AUTH_CODE_SCOPES
    assert len(scopes) == len(set(scopes))


def test_reauth_updates_existing_entry(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reauth updates the existing entry after native OAuth completes."""
    flow = _flow(monkeypatch)
    flow.context = {
        "source": config_entries.SOURCE_REAUTH,
        "entry_id": "entry-1",
    }
    entry = type(
        "Entry",
        (),
        {
            "entry_id": "entry-1",
            "title": "eBay (test_user)",
            "data": {
                **_flow_data(),
                CONF_ACCOUNT_IDENTITY: {
                    "entry_id": "entry-1",
                    "environment": "production",
                    "client_id": "client-id",
                    "account_id": "test_user_id",
                    "identifier_type": "eias_token",
                    "username": "test_user",
                    "binding_state": "verified",
                    "provenance": "authenticated_get_user",
                    "reauth_generation": 0,
                },
            },
        },
    )()
    flow._data = _flow_data(client_secret="new-secret")
    updated: dict[str, Any] = {}

    def _update(entry_arg: Any, **kwargs: Any) -> dict[str, Any]:
        updated["entry"] = entry_arg
        updated["data"] = kwargs["data"]
        return {"type": "abort", "reason": "reauth_successful"}

    monkeypatch.setattr(flow, "_get_reauth_entry", lambda: entry)
    monkeypatch.setattr(flow, "async_update_and_abort", _update)

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
    assert "manual_authorization" not in updated["data"]


def test_reauth_uses_async_update_and_abort_avoiding_duplicate_reload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_finish_reauth calls async_update_and_abort avoiding duplicate reloads."""
    flow = _flow(monkeypatch)
    flow.context = {
        "source": config_entries.SOURCE_REAUTH,
        "entry_id": "entry-1",
    }
    entry = type(
        "Entry",
        (),
        {
            "data": {**_flow_data(), "manual_authorization": True},
            "options": {},
            "title": "eBay",
        },
    )()
    called: dict[str, Any] = {}

    def _mock_update_and_abort(entry_arg: Any, **kwargs: Any) -> dict[str, Any]:
        called["update_and_abort"] = kwargs
        return {"type": "abort", "reason": "reauth_successful"}

    def _mock_update_reload_and_abort(entry_arg: Any, **kwargs: Any) -> dict[str, Any]:
        called["update_reload_and_abort"] = kwargs
        return {"type": "abort", "reason": "reauth_successful"}

    monkeypatch.setattr(flow, "_get_reauth_entry", lambda: entry)
    monkeypatch.setattr(flow, "async_update_and_abort", _mock_update_and_abort)
    monkeypatch.setattr(
        flow, "async_update_reload_and_abort", _mock_update_reload_and_abort
    )

    result = flow._finish_reauth({**_flow_data(), "manual_authorization": True})

    assert result == {"type": "abort", "reason": "reauth_successful"}
    assert "update_and_abort" in called
    assert "update_reload_and_abort" not in called
    assert "manual_authorization" not in called["update_and_abort"]["data"]


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
    assert fields[CONF_CLIENT_SECRET].default is vol.UNDEFINED
    assert fields[CONF_RUNAME].default() == "Example-Runame"
    assert fields[CONF_SITE_ID].default() == "77"
    assert "stored-secret" not in str(result)


def test_reauth_confirm_credentials_form_serializes_and_validates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reauth confirmation reaches a serializable credentials form before OAuth."""
    flow = _flow(monkeypatch)
    flow.context = {
        "source": config_entries.SOURCE_REAUTH,
        "entry_id": "entry-1",
    }
    entry = type(
        "Entry",
        (),
        {
            "data": _flow_data(client_secret="stored-secret"),
            "title": "eBay",
        },
    )()
    monkeypatch.setattr(flow, "_get_reauth_entry", lambda: entry)

    confirm = asyncio.run(flow.async_step_reauth(entry.data))
    assert confirm["type"] == "form"
    assert confirm["step_id"] == "reauth_confirm"

    credentials = asyncio.run(flow.async_step_reauth_confirm({}))
    assert credentials["type"] == "form"
    assert credentials["step_id"] == "credentials"
    convert(credentials["data_schema"], custom_serializer=cv.custom_serializer)

    blank = asyncio.run(
        flow.async_step_credentials(
            _flow_data(
                client_id=" ",
                client_secret="",
                runame="\n",
                site_id=" ",
            )
        )
    )
    assert blank["type"] == "form"
    assert blank["step_id"] == "credentials"
    assert blank["errors"] == {
        CONF_CLIENT_ID: "required",
        CONF_CLIENT_SECRET: "required",
        CONF_RUNAME: "required",
        CONF_SITE_ID: "required",
    }

    oauth = asyncio.run(flow.async_step_credentials(_flow_data()))
    assert oauth["type"] == "external"


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
    assert "invalid_environment" in strings["config"]["error"]
    steps = strings["config"]["step"]
    for step in (
        "prepare_application",
        "redirect_guidance",
        "credentials",
        "manual",
    ):
        assert step in steps
    assert "menu_options" in steps["user"]
    assert "redirect_guidance" in steps["user"]["menu_options"]
    assert "credentials" in steps["user"]["menu_options"]
    assert "authorization_method" not in steps
    assert "oauth_mode" not in steps["credentials"]["data"]


@pytest.mark.parametrize(
    "bad_env",
    [
        "prod",
        "sandboxx",
        "invalid",
        "staging",
        "development",
        "production1",
        "null",
        "true",
        "0",
    ],
)
def test_credentials_rejects_invalid_environments_fail_closed(
    monkeypatch: pytest.MonkeyPatch, bad_env: str
) -> None:
    """Explicit invalid environments are rejected fail-closed without silent fallback."""
    flow = _flow(monkeypatch)
    result = asyncio.run(flow.async_step_credentials(_flow_data(environment=bad_env)))

    assert result["type"] == "form"
    assert result["step_id"] == "credentials"
    assert result["errors"] == {CONF_ENVIRONMENT: "invalid_environment"}
    assert flow._consent_url is None


@pytest.mark.parametrize(
    "env_input,expected_env",
    [
        ("production", "production"),
        ("sandbox", "sandbox"),
        (" PRODUCTION ", "production"),
        ("  sandbox  ", "sandbox"),
        ("Sandbox", "sandbox"),
        ("Production", "production"),
        (None, "production"),
    ],
)
def test_credentials_accepts_valid_environments(
    monkeypatch: pytest.MonkeyPatch, env_input: str | None, expected_env: str
) -> None:
    """Valid environment choices are accepted, normalized, or defaulted when absent."""
    flow = _flow(monkeypatch)
    data = _flow_data()
    if env_input is None:
        data.pop(CONF_ENVIRONMENT, None)
    else:
        data[CONF_ENVIRONMENT] = env_input
    result = asyncio.run(flow.async_step_credentials(data))

    assert result["type"] == "external"
    assert flow._data[CONF_ENVIRONMENT] == expected_env


def test_credentials_accepts_valid_hyphenated_identifiers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Legitimate identifiers containing hyphens and alphanumeric characters are accepted."""
    flow = _flow(monkeypatch)
    result = asyncio.run(
        flow.async_step_credentials(
            _flow_data(
                client_id="Developer-App-PRD-123456789-abcdef",
                runame="My-Developer-App-PRD-12345",
            )
        )
    )

    assert result["type"] == "external"
    assert flow._data[CONF_CLIENT_ID] == "Developer-App-PRD-123456789-abcdef"
    assert flow._data[CONF_RUNAME] == "My-Developer-App-PRD-12345"


def test_client_secret_is_never_redisclosed_in_any_form_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Client Secret is never serialized into form definitions across initial, retry, and reauth."""
    flow = _flow(monkeypatch)

    # 1. Initial credentials step
    init_res = asyncio.run(flow.async_step_credentials())
    init_fields = probatio.to_field_list(
        init_res["data_schema"], custom_serializer=cv.custom_serializer
    )
    init_secret = next(f for f in init_fields if f["name"] == CONF_CLIENT_SECRET)
    assert "default" not in init_secret
    assert init_secret["selector"]["text"]["type"] == "password"

    # 2. Retry after validation error
    err_res = asyncio.run(
        flow.async_step_credentials(
            _flow_data(
                client_id="client-id",
                client_secret="super-sensitive-secret-value-123",
                runame="invalid/runame",
            )
        )
    )
    assert err_res["type"] == "form"
    assert err_res["errors"] == {CONF_RUNAME: "invalid_runame_url"}
    err_fields = probatio.to_field_list(
        err_res["data_schema"], custom_serializer=cv.custom_serializer
    )
    err_secret = next(f for f in err_fields if f["name"] == CONF_CLIENT_SECRET)
    assert "default" not in err_secret
    assert "super-sensitive-secret-value-123" not in str(err_res)

    # 3. Reauth credentials step
    reauth_flow = _flow(monkeypatch)
    reauth_flow.context = {
        "source": config_entries.SOURCE_REAUTH,
        "entry_id": "reauth-entry-id",
    }
    mock_entry = SimpleNamespace(
        entry_id="reauth-entry-id",
        unique_id="production:EIASTOKEN123",
        title="eBay (testuser)",
        data=_flow_data(client_secret="existing-secret-456"),
        options={},
    )
    monkeypatch.setattr(reauth_flow, "_get_reauth_entry", lambda: mock_entry)

    reauth_res = asyncio.run(reauth_flow.async_step_credentials())
    reauth_fields = probatio.to_field_list(
        reauth_res["data_schema"], custom_serializer=cv.custom_serializer
    )
    reauth_secret = next(f for f in reauth_fields if f["name"] == CONF_CLIENT_SECRET)
    assert "default" not in reauth_secret
    assert "existing-secret-456" not in str(reauth_res)


def _flow(monkeypatch: pytest.MonkeyPatch) -> EbayConfigFlow:
    """Return a config flow with lightweight Home Assistant shims."""
    flow = EbayConfigFlow()
    flow.hass = _Hass()
    flow.flow_id = "flow-123"
    monkeypatch.setattr(flow, "async_set_unique_id", _async_noop)
    monkeypatch.setattr(flow, "_abort_if_unique_id_configured", lambda *args: None)
    monkeypatch.setattr(
        flow, "_abort_if_unique_id_mismatch", lambda *args, **kwargs: None
    )
    monkeypatch.setattr(flow, "_async_current_entries", lambda: [])
    monkeypatch.setattr(
        "custom_components.ebay.config_flow.async_get_clientsession",
        lambda hass: object(),
    )

    async def _mock_verify(*args: Any, **kwargs: Any) -> AccountIdentity:
        return AccountIdentity(
            account_id="test_user_id",
            username="test_user",
            provenance=AccountIdentityProvenance.AUTHENTICATED_GET_USER,
            binding_state=AccountBindingState.VERIFIED,
            identifier_type=AccountIdentifierType.EIASTOKEN,
        )

    monkeypatch.setattr(
        "custom_components.ebay.config_flow.async_verify_authenticated_account",
        _mock_verify,
    )
    return flow


def _schema_fields(schema: Any) -> dict[str, Any]:
    """Map a voluptuous schema's fields by Home Assistant config key."""
    return {field.schema: field for field in schema.schema}


async def _async_noop(*args: Any, **kwargs: Any) -> None:
    """Async no-op for patched config-flow helpers."""


def test_options_flow_menu_sections_and_save() -> None:
    import voluptuous as vol

    from custom_components.ebay.config_flow import EbayOptionsFlow
    from custom_components.ebay.const import CONF_OAUTH_SCOPES

    entry = type(
        "Entry",
        (),
        {
            "entry_id": "entry-1",
            "options": {},
            "data": {CONF_OAUTH_SCOPES: CORE_SCOPE},
        },
    )()
    flow = EbayOptionsFlow(entry)
    hass = _Hass()
    hass.async_create_task = lambda coro: coro.close()  # type: ignore[method-assign]
    flow.hass = hass

    menu = asyncio.run(flow.async_step_init())
    assert menu["type"] == "menu"
    assert "general" in menu["menu_options"]
    assert "save" in menu["menu_options"]

    general = asyncio.run(flow.async_step_general())
    assert general["type"] == "form"
    schema = general["data_schema"]
    with pytest.raises(vol.Invalid):
        schema({"poll_interval": 1})

    returned = asyncio.run(flow.async_step_general({"poll_interval": 45}))
    assert returned["type"] == "menu"
    assert flow._draft["poll_interval"] == 45

    entities = asyncio.run(
        flow.async_step_entities(
            {
                "entity_mode": "detailed",
                "per_item_cap": 50,
                "pinned_item_ids": ["1"],
            }
        )
    )
    assert entities["type"] == "menu"
    assert flow._draft["entity_mode"] == "detailed"
    assert flow._draft["pinned_item_ids"] == ["1"]

    selling = asyncio.run(
        flow.async_step_selling({"selling_enabled": True, "analytics_enabled": False})
    )
    assert selling["type"] == "menu"

    created = asyncio.run(flow.async_step_save())
    assert created["type"] == "create_entry"
    assert created["data"]["poll_interval"] == 45
    assert created["data"]["entity_mode"] == "detailed"
    assert created["data"]["analytics_enabled"] is False


def test_options_flow_disables_analytics_when_selling_off() -> None:
    """Analytics cannot remain enabled without Selling."""
    from custom_components.ebay.config_flow import EbayOptionsFlow

    entry = type("Entry", (), {"options": {}, "data": {}})()
    flow = EbayOptionsFlow(entry)
    flow.hass = _Hass()
    flow._draft = {
        "poll_interval": 30,
        "ending_soon_threshold": 60,
        "entity_mode": "balanced",
        "per_item_cap": 25,
        "pinned_item_ids": [],
        "pinned_item_price_targets": [],
        "watched_price_change_min_percent": 0,
        "watched_price_drop_threshold": "",
        "watched_price_drop_currency": "",
        "buying_enabled": True,
        "selling_enabled": True,
        "analytics_enabled": True,
        "fulfillment_enabled": False,
        "seller_standards_enabled": False,
        "feedback_enabled": False,
        "messages_enabled": False,
    }
    asyncio.run(
        flow.async_step_selling({"selling_enabled": False, "analytics_enabled": True})
    )
    assert flow._draft["selling_enabled"] is False
    assert flow._draft["analytics_enabled"] is False


def test_options_flow_starts_reauth_when_scopes_missing() -> None:
    """Enabling a module without granted scopes stores pending options and reauths."""
    from custom_components.ebay.config_flow import EbayOptionsFlow
    from custom_components.ebay.const import CONF_OAUTH_SCOPES, CONF_PENDING_OPTIONS

    entry = type(
        "Entry",
        (),
        {
            "entry_id": "entry-1",
            "options": {"feedback_enabled": False},
            "data": {CONF_OAUTH_SCOPES: CORE_SCOPE},
        },
    )()
    flow = EbayOptionsFlow(entry)
    hass = _Hass()
    started: list[Any] = []
    updates: list[dict[str, Any]] = []

    def _create_task(coro: Any) -> None:
        started.append(coro)
        coro.close()

    def _async_update_entry(target: Any, **kwargs: Any) -> bool:
        updates.append(kwargs)
        if "data" in kwargs:
            target.data = dict(kwargs["data"])
        if "options" in kwargs:
            target.options = dict(kwargs["options"])
        return True

    hass.async_create_task = _create_task  # type: ignore[method-assign]
    hass.config_entries = type(
        "ConfigEntries",
        (),
        {
            "async_update_entry": staticmethod(_async_update_entry),
            "flow": type(
                "Flow",
                (),
                {"async_init": staticmethod(lambda *args, **kwargs: asyncio.sleep(0))},
            )(),
        },
    )()
    flow.hass = hass

    asyncio.run(flow.async_step_init())
    asyncio.run(
        flow.async_step_seller_operations(
            {
                "fulfillment_enabled": False,
                "seller_standards_enabled": False,
                "feedback_enabled": True,
                "messages_enabled": False,
            }
        )
    )
    result = asyncio.run(flow.async_step_save())

    assert result["type"] == "create_entry"
    assert result["data"]["feedback_enabled"] is False
    assert entry.options["feedback_enabled"] is False
    assert entry.data[CONF_PENDING_OPTIONS]["feedback_enabled"] is True
    assert len(started) == 1
    assert len(updates) == 1


def test_reauth_success_commits_pending_options(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Successful reauth commits pending options and clears the pending draft."""
    from custom_components.ebay.const import CONF_OAUTH_SCOPES, CONF_PENDING_OPTIONS

    flow = _flow(monkeypatch)
    flow.context = {
        "source": config_entries.SOURCE_REAUTH,
        "entry_id": "entry-1",
    }
    pending = {
        "feedback_enabled": True,
        "fulfillment_enabled": False,
        "poll_interval": 45,
    }
    entry = type(
        "Entry",
        (),
        {
            "entry_id": "entry-1",
            "data": {
                **_flow_data(),
                CONF_OAUTH_SCOPES: CORE_SCOPE,
                CONF_PENDING_OPTIONS: pending,
                CONF_ACCOUNT_IDENTITY: {
                    "entry_id": "entry-1",
                    "environment": "production",
                    "client_id": "client-id",
                    "account_id": "test_user_id",
                    "identifier_type": "eias_token",
                    "username": "test_user",
                    "binding_state": "verified",
                    "provenance": "authenticated_get_user",
                    "reauth_generation": 0,
                },
            },
            "options": {"feedback_enabled": False},
            "title": "eBay",
        },
    )()
    flow._data = _flow_data(client_secret="new-secret")
    flow._requested_scopes = CORE_SCOPE
    updated: dict[str, Any] = {}

    def _update(entry_arg: Any, **kwargs: Any) -> dict[str, Any]:
        updated["entry"] = entry_arg
        updated["data"] = kwargs["data"]
        updated["options"] = kwargs.get("options")
        if "data" in kwargs:
            entry_arg.data = dict(kwargs["data"])
        if "options" in kwargs:
            entry_arg.options = dict(kwargs["options"])
        return {"type": "abort", "reason": "reauth_successful"}

    monkeypatch.setattr(flow, "_get_reauth_entry", lambda: entry)
    monkeypatch.setattr(flow, "async_update_and_abort", _update)

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
    assert updated["options"] == pending
    assert CONF_PENDING_OPTIONS not in updated["data"]
    assert "manual_authorization" not in updated["data"]
    assert entry.options["feedback_enabled"] is True
    assert CONF_PENDING_OPTIONS not in entry.data


def test_reauth_with_pending_options_uses_async_update_and_abort(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_finish_reauth with pending options calls async_update_and_abort."""
    from custom_components.ebay.const import CONF_PENDING_OPTIONS

    flow = _flow(monkeypatch)
    flow.context = {
        "source": config_entries.SOURCE_REAUTH,
        "entry_id": "entry-1",
    }
    pending = {"poll_interval": 30}
    entry = type(
        "Entry",
        (),
        {
            "data": {**_flow_data(), CONF_PENDING_OPTIONS: pending},
            "options": {},
            "title": "eBay",
        },
    )()
    called: dict[str, Any] = {}

    def _mock_update_and_abort(entry_arg: Any, **kwargs: Any) -> dict[str, Any]:
        called["update_and_abort"] = kwargs
        return {"type": "abort", "reason": "reauth_successful"}

    monkeypatch.setattr(flow, "_get_reauth_entry", lambda: entry)
    monkeypatch.setattr(flow, "async_update_and_abort", _mock_update_and_abort)

    result = flow._finish_reauth(
        {**_flow_data(), "manual_authorization": True, CONF_PENDING_OPTIONS: pending}
    )

    assert result == {"type": "abort", "reason": "reauth_successful"}
    assert called["update_and_abort"]["options"] == pending
    assert CONF_PENDING_OPTIONS not in called["update_and_abort"]["data"]
    assert "manual_authorization" not in called["update_and_abort"]["data"]


def test_reauth_with_staged_title_uses_async_update_and_abort(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_finish_reauth passes staged new title atomically into async_update_and_abort."""
    flow = _flow(monkeypatch)
    flow.context = {
        "source": config_entries.SOURCE_REAUTH,
        "entry_id": "entry-1",
    }
    entry = type(
        "Entry",
        (),
        {
            "data": _flow_data(),
            "options": {},
            "title": "eBay (old_user)",
        },
    )()
    called: dict[str, Any] = {}

    def _mock_update_and_abort(entry_arg: Any, **kwargs: Any) -> dict[str, Any]:
        called["update_and_abort"] = kwargs
        return {"type": "abort", "reason": "reauth_successful"}

    monkeypatch.setattr(flow, "_get_reauth_entry", lambda: entry)
    monkeypatch.setattr(flow, "async_update_and_abort", _mock_update_and_abort)

    # 1. New title is passed when it differs from entry.title
    result = flow._finish_reauth(_flow_data(), title="eBay (new_user)")
    assert result == {"type": "abort", "reason": "reauth_successful"}
    assert called["update_and_abort"]["title"] == "eBay (new_user)"

    # 2. When title is identical to entry.title, title is not in kwargs
    called.clear()
    result = flow._finish_reauth(_flow_data(), title="eBay (old_user)")
    assert result == {"type": "abort", "reason": "reauth_successful"}
    assert "title" not in called["update_and_abort"]


def test_reauth_cancel_discards_pending_options(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancelling reauth discards pending options without changing live options."""
    from custom_components.ebay.const import CONF_PENDING_OPTIONS

    flow = _flow(monkeypatch)
    flow.context = {
        "source": config_entries.SOURCE_REAUTH,
        "entry_id": "entry-1",
    }
    entry = type(
        "Entry",
        (),
        {
            "data": {
                **_flow_data(),
                CONF_PENDING_OPTIONS: {"feedback_enabled": True},
            },
            "options": {"feedback_enabled": False},
            "title": "eBay",
        },
    )()
    updates: list[dict[str, Any]] = []

    def _async_update_entry(target: Any, **kwargs: Any) -> bool:
        updates.append(kwargs)
        if "data" in kwargs:
            target.data = dict(kwargs["data"])
        if "options" in kwargs:
            target.options = dict(kwargs["options"])
        return True

    flow.hass.config_entries = type(
        "ConfigEntries",
        (),
        {"async_update_entry": staticmethod(_async_update_entry)},
    )()
    monkeypatch.setattr(flow, "_get_reauth_entry", lambda: entry)

    flow.async_remove()

    assert len(updates) == 1
    assert CONF_PENDING_OPTIONS not in entry.data
    assert entry.options["feedback_enabled"] is False
    assert "options" not in updates[0]


def test_options_flow_buying_alerts_continues_to_price_targets() -> None:
    """Buying alerts saves globals then opens the price-targets menu."""
    from custom_components.ebay.config_flow import EbayOptionsFlow
    from custom_components.ebay.const import CONF_OAUTH_SCOPES

    entry = type(
        "Entry",
        (),
        {
            "entry_id": "entry-1",
            "options": {},
            "data": {CONF_OAUTH_SCOPES: CORE_SCOPE},
        },
    )()
    flow = EbayOptionsFlow(entry)
    flow.hass = _Hass()
    asyncio.run(flow.async_step_init())

    result = asyncio.run(
        flow.async_step_buying_alerts(
            {
                "buying_enabled": True,
                "ending_soon_threshold": 60,
                "watched_price_change_min_percent": 0,
                "watched_price_drop_threshold": "",
                "watched_price_drop_currency": "",
            }
        )
    )
    assert result["type"] == "menu"
    assert result["step_id"] == "price_targets"
    assert "price_target_add" in result["menu_options"]
    assert "price_targets_done" in result["menu_options"]


def test_options_flow_price_target_add_and_remove() -> None:
    """Structured price targets can be added and removed for pinned IDs."""
    from custom_components.ebay.config_flow import EbayOptionsFlow
    from custom_components.ebay.const import CONF_OAUTH_SCOPES

    entry = type(
        "Entry",
        (),
        {
            "entry_id": "entry-1",
            "options": {"pinned_item_ids": ["abc"]},
            "data": {CONF_OAUTH_SCOPES: CORE_SCOPE},
            "runtime_data": SimpleNamespace(
                data={
                    "watched": {"abc": {"item_id": "abc", "title": "Pinned Camera"}},
                    "bidding": {},
                    "selling": {},
                }
            ),
        },
    )()
    flow = EbayOptionsFlow(entry)
    flow.hass = _Hass()
    asyncio.run(flow.async_step_init())
    assert flow._draft["pinned_item_ids"] == ["abc"]

    add_form = asyncio.run(flow.async_step_price_target_add())
    assert add_form["type"] == "form"
    assert add_form["step_id"] == "price_target_add"

    rejected = asyncio.run(
        flow.async_step_price_target_add(
            {"item_id": "not-pinned", "amount": 10, "currency": "USD"}
        )
    )
    assert rejected["type"] == "form"
    assert rejected["errors"]["item_id"] == "target_not_pinned"

    added = asyncio.run(
        flow.async_step_price_target_add(
            {"item_id": "abc", "amount": 25.5, "currency": "usd"}
        )
    )
    assert added["type"] == "menu"
    assert added["step_id"] == "price_targets"
    assert flow._draft["pinned_item_price_targets"] == [
        {"item_id": "abc", "amount": "25.5", "currency": "USD"}
    ]
    assert "price_target_remove" in added["menu_options"]

    removed = asyncio.run(flow.async_step_price_target_remove({"item_id": "abc"}))
    assert removed["type"] == "menu"
    assert flow._draft["pinned_item_price_targets"] == []


def test_options_flow_entities_multi_select_and_prune_targets() -> None:
    """Pinned multi-select coerces IDs and drops targets for removed pins."""
    from custom_components.ebay.config_flow import EbayOptionsFlow
    from custom_components.ebay.const import CONF_OAUTH_SCOPES

    entry = type(
        "Entry",
        (),
        {
            "entry_id": "entry-1",
            "options": {
                "pinned_item_ids": "keep,drop",
                "pinned_item_price_targets": "keep=10 USD\ndrop=5 EUR",
            },
            "data": {CONF_OAUTH_SCOPES: CORE_SCOPE},
            "runtime_data": SimpleNamespace(
                data={
                    "watched": {
                        "keep": {"item_id": "keep", "title": "Keep Me"},
                        "drop": {"item_id": "drop", "title": "Drop Me"},
                    },
                    "bidding": {},
                    "selling": {},
                }
            ),
        },
    )()
    flow = EbayOptionsFlow(entry)
    flow.hass = _Hass()
    asyncio.run(flow.async_step_init())
    assert flow._draft["pinned_item_ids"] == ["drop", "keep"]
    assert len(flow._draft["pinned_item_price_targets"]) == 2

    result = asyncio.run(
        flow.async_step_entities(
            {
                "entity_mode": "balanced",
                "per_item_cap": 25,
                "pinned_item_ids": ["keep", "typed-id"],
            }
        )
    )
    assert result["type"] == "menu"
    assert flow._draft["pinned_item_ids"] == ["keep", "typed-id"]
    assert flow._draft["pinned_item_price_targets"] == [
        {"item_id": "keep", "amount": "10", "currency": "USD"}
    ]


def test_options_flow_save_rejects_inconsistent_price_targets() -> None:
    from custom_components.ebay.config_flow import EbayOptionsFlow
    from custom_components.ebay.const import CONF_OAUTH_SCOPES

    entry = type(
        "Entry",
        (),
        {
            "entry_id": "entry-1",
            "options": {
                "pinned_item_price_targets": "999=10 USD",
                "pinned_item_ids": "",
            },
            "data": {CONF_OAUTH_SCOPES: CORE_SCOPE},
        },
    )()
    flow = EbayOptionsFlow(entry)
    flow.hass = _Hass()
    asyncio.run(flow.async_step_init())
    result = asyncio.run(flow.async_step_save())
    assert result["type"] == "form"
    assert result["step_id"] == "save"
    assert "pinned_item_price_targets" in result["errors"]


def test_oauth_create_entry_stores_core_scopes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """New installs store the core scope grant."""
    from custom_components.ebay.const import CONF_OAUTH_SCOPES

    flow = _flow(monkeypatch)
    flow._data = _flow_data()
    flow._requested_scopes = CORE_SCOPE
    created: dict[str, Any] = {}

    def _create_entry(**kwargs: Any) -> dict[str, Any]:
        created.update(kwargs)
        return {"type": "create_entry", **kwargs}

    monkeypatch.setattr(flow, "async_create_entry", _create_entry)

    result = asyncio.run(
        flow.async_oauth_create_entry(
            {
                "auth_implementation": "ebay:production:client-id",
                "token": {
                    "access_token": "access",
                    "refresh_token": "refresh",
                    "expires_in": 7200,
                },
            }
        )
    )

    assert result["type"] == "create_entry"
    assert created["data"][CONF_OAUTH_SCOPES] == CORE_SCOPE
    assert created["options"]["feedback_enabled"] is False
    assert created["options"]["fulfillment_enabled"] is False
