"""Tests for Home Assistant 2026.10 onboarding UX, translations, and empty form regressions."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from custom_components.ebay.clients.scopes import CORE_SCOPE
from custom_components.ebay.config_flow import EbayConfigFlow
from custom_components.ebay.const import (
    CONF_OAUTH_SCOPES,
    CONF_PINNED_ITEM_IDS,
    CONF_WATCHED_PRICE_DROP_CURRENCY,
    CONF_WATCHED_PRICE_DROP_THRESHOLD,
    DOMAIN,
)
from custom_components.ebay.options_flow import EbayOptionsFlow


@pytest.fixture
def repo_root() -> Path:
    """Return repository root path."""
    return Path(__file__).parent.parent


@pytest.fixture
def strings_json(repo_root: Path) -> dict[str, Any]:
    """Load strings.json."""
    path = repo_root / "custom_components" / "ebay" / "strings.json"
    with open(path, encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture
def en_json(repo_root: Path) -> dict[str, Any]:
    """Load translations/en.json."""
    path = repo_root / "custom_components" / "ebay" / "translations" / "en.json"
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def test_strings_and_en_translations_match_and_are_valid(
    strings_json: dict[str, Any], en_json: dict[str, Any]
) -> None:
    """strings.json and translations/en.json must match exactly with identical structure and content."""
    assert strings_json == en_json
    assert "config" in strings_json
    assert "options" in strings_json
    assert "entity" in strings_json


def test_config_step_user_menu_options_and_descriptions(
    strings_json: dict[str, Any],
) -> None:
    """Initial setup menu must have distinct titles and helpful descriptions for all options."""
    user_step = strings_json["config"]["step"]["user"]
    assert "title" in user_step
    assert "description" in user_step
    assert "{developer_console_url}" in user_step["description"]
    assert "{oauth_setup_guide_url}" in user_step["description"]

    menu_options = user_step.get("menu_options", {})
    menu_option_descriptions = user_step.get("menu_option_descriptions", {})

    assert "redirect_guidance" in menu_options
    assert "credentials" in menu_options
    assert menu_options["redirect_guidance"] == "Recommended: Guided setup"
    assert menu_options["credentials"] == "I already have developer credentials"

    assert "redirect_guidance" in menu_option_descriptions
    assert "credentials" in menu_option_descriptions
    assert (
        "New to eBay developer credentials?"
        in menu_option_descriptions["redirect_guidance"]
    )
    assert "Enter your existing App ID" in menu_option_descriptions["credentials"]

    # Backward compatibility aliases for legacy cached translations
    for alias in ("prepare_application", "automatic", "manual"):
        assert alias in menu_options
        assert alias in menu_option_descriptions


def test_redirect_guidance_has_actionable_menu_and_placeholders(
    strings_json: dict[str, Any],
) -> None:
    """RuName guidance step must be an actionable menu with placeholders, not an empty form."""
    guidance_step = strings_json["config"]["step"]["redirect_guidance"]
    assert guidance_step["title"] == "Configure eBay redirect URL (RuName)"
    assert "{callback_url}" in guidance_step["description"]
    assert "{developer_console_url}" in guidance_step["description"]
    assert "{privacy_policy_url}" in guidance_step["description"]
    assert "{oauth_setup_guide_url}" in guidance_step["description"]

    assert guidance_step["menu_options"]["credentials"] == "Continue to credentials"
    assert (
        "Enter your App ID" in guidance_step["menu_option_descriptions"]["credentials"]
    )

    # prepare_application legacy step parity
    prep_step = strings_json["config"]["step"]["prepare_application"]
    assert prep_step["menu_options"]["credentials"] == "Continue to credentials"


def test_credentials_field_labels_and_descriptions(
    strings_json: dict[str, Any],
) -> None:
    """Credentials form must have descriptive labels and helper explanations."""
    creds_step = strings_json["config"]["step"]["credentials"]
    data = creds_step["data"]
    data_desc = creds_step["data_description"]

    for field in (
        "environment",
        "client_id",
        "client_secret",
        "runame",
        "site_id",
        "manual_authorization",
    ):
        assert field in data
        assert field in data_desc
        assert len(data[field]) > 0
        assert len(data_desc[field]) > 0

    assert "Masked as a password" in data_desc["client_secret"]
    assert "NOT the callback URL" in data_desc["runame"]


def test_reauth_confirm_step_translations(
    strings_json: dict[str, Any],
) -> None:
    """Reauth confirmation step must provide continue button translations."""
    reauth_step = strings_json["config"]["step"]["reauth_confirm"]
    assert reauth_step["menu_options"]["credentials"] == "Continue to credentials"
    assert "re-authenticate" in reauth_step["menu_option_descriptions"]["credentials"]


def test_options_diagnostics_step_translations(
    strings_json: dict[str, Any],
) -> None:
    """Diagnostics options step must provide a return-to-menu action."""
    diag_step = strings_json["options"]["step"]["diagnostics"]
    assert diag_step["menu_options"]["init"] == "Return to options menu"
    assert (
        "Back to eBay options overview" in diag_step["menu_option_descriptions"]["init"]
    )


def test_options_init_menu_descriptions(
    strings_json: dict[str, Any],
) -> None:
    """All 7 options menu sections must have titles and descriptions."""
    init_step = strings_json["options"]["step"]["init"]
    menu_options = init_step["menu_options"]
    menu_option_descriptions = init_step["menu_option_descriptions"]

    expected_sections = [
        "general",
        "buying_alerts",
        "selling",
        "seller_operations",
        "entities",
        "diagnostics",
        "save",
    ]
    for section in expected_sections:
        assert section in menu_options
        assert section in menu_option_descriptions
        assert len(menu_options[section]) > 0
        assert len(menu_option_descriptions[section]) > 0


# =========================================================================
# Integration Flow & UX Regression Tests
# =========================================================================


class _MockHass:
    """Minimal mock Home Assistant instance for config flow testing."""

    def __init__(self) -> None:
        self.data = {DOMAIN: {}}
        self.config = MagicMock()
        self.config.api = MagicMock()
        self.config.api.base_url = "https://example.duckdns.org:8123"
        self.config_entries = MagicMock()
        self.config_entries.async_entries = MagicMock(return_value=[])
        self.config_entries.flow = MagicMock()
        self.config_entries.flow.async_init = MagicMock()


@pytest.fixture
def mock_flow() -> EbayConfigFlow:
    """Return an EbayConfigFlow with mock hass."""
    flow = EbayConfigFlow()
    flow.hass = _MockHass()
    return flow


def test_initial_menu_regression_no_blank_rows(
    mock_flow: EbayConfigFlow, strings_json: dict[str, Any]
) -> None:
    """Regression test: Screen 1 setup menu must render valid menu items with localized labels."""
    result = asyncio.run(mock_flow.async_step_user())

    assert result["type"] == "menu"
    assert result["step_id"] == "user"
    menu_options = result["menu_options"]
    assert menu_options == ["redirect_guidance", "credentials"]

    user_translations = strings_json["config"]["step"]["user"]
    for option in menu_options:
        # Every presented option MUST have a non-empty label and description in strings.json
        assert option in user_translations["menu_options"]
        assert len(user_translations["menu_options"][option]) > 0
        assert option in user_translations["menu_option_descriptions"]
        assert len(user_translations["menu_option_descriptions"][option]) > 0


def test_redirect_guidance_regression_no_blank_submit_dialog(
    mock_flow: EbayConfigFlow, strings_json: dict[str, Any]
) -> None:
    """Regression test: Screen 2 guided redirect setup must be a menu with 'Continue to credentials', NOT an empty form."""
    result = asyncio.run(mock_flow.async_step_redirect_guidance())

    assert result["type"] == "menu"
    assert result["step_id"] == "redirect_guidance"
    assert result["menu_options"] == ["credentials"]

    # Verify placeholders are present
    placeholders = result["description_placeholders"]
    assert "callback_url" in placeholders
    assert "https://" in placeholders["callback_url"]
    assert "developer_console_url" in placeholders
    assert "oauth_setup_guide_url" in placeholders

    # Submitting or selecting credentials advances to credentials form
    creds_result = asyncio.run(mock_flow.async_step_redirect_guidance({}))
    assert creds_result["type"] == "form"
    assert creds_result["step_id"] == "credentials"


def test_options_diagnostics_menu_navigation(mock_flow: EbayConfigFlow) -> None:
    """Options diagnostics step must be a menu with option to return to init."""
    entry = MagicMock()
    entry.entry_id = "test_entry"
    entry.options = {}
    entry.data = {CONF_OAUTH_SCOPES: CORE_SCOPE}
    options_flow = EbayOptionsFlow(entry)
    options_flow.hass = mock_flow.hass

    result = asyncio.run(options_flow.async_step_diagnostics())
    assert result["type"] == "menu"
    assert result["step_id"] == "diagnostics"
    assert result["menu_options"] == ["init"]

    # Submitting advances back to init menu
    next_step = asyncio.run(options_flow.async_step_diagnostics({}))
    assert next_step["type"] == "menu"
    assert next_step["step_id"] == "init"


def test_options_save_error_recovery_breaks_infinite_loop(
    mock_flow: EbayConfigFlow,
) -> None:
    """Options save step error display must return to init on acknowledgement instead of looping."""
    entry = MagicMock()
    entry.entry_id = "test_entry"
    # Seed options with invalid price target threshold (threshold set but no currency)
    entry.options = {
        CONF_WATCHED_PRICE_DROP_THRESHOLD: "50",
        CONF_WATCHED_PRICE_DROP_CURRENCY: "",
    }
    entry.data = {CONF_OAUTH_SCOPES: CORE_SCOPE}
    options_flow = EbayOptionsFlow(entry)
    options_flow.hass = mock_flow.hass

    # Initial save attempt without user_input displays error form
    result = asyncio.run(options_flow.async_step_save())
    assert result["type"] == "form"
    assert result["step_id"] == "save"
    assert "watched_price_drop_currency" in result["errors"]

    # Submitting form (acknowledging the error) returns to init
    rebound = asyncio.run(options_flow.async_step_save({}))
    assert rebound["type"] == "menu"
    assert rebound["step_id"] == "init"


def test_options_price_target_add_unpinned_breaks_infinite_loop(
    mock_flow: EbayConfigFlow,
) -> None:
    """Price target add when no pinned items exist returns to price targets on submit."""
    entry = MagicMock()
    entry.entry_id = "test_entry"
    entry.options = {CONF_PINNED_ITEM_IDS: []}
    entry.data = {CONF_OAUTH_SCOPES: CORE_SCOPE}
    options_flow = EbayOptionsFlow(entry)
    options_flow.hass = mock_flow.hass

    # When no pins, displays error form
    result = asyncio.run(options_flow.async_step_price_target_add())
    assert result["type"] == "form"
    assert result["step_id"] == "price_target_add"
    assert result["errors"] == {"base": "target_not_pinned"}

    # Submitting (acknowledging error) returns to price targets menu
    rebound = asyncio.run(options_flow.async_step_price_target_add({}))
    assert rebound["type"] == "menu"
    assert rebound["step_id"] == "price_targets"
