"""Validate shipped Home Assistant automation blueprints."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import yaml
from homeassistant.components.blueprint import BLUEPRINT_SCHEMA, Blueprint
from homeassistant.components.blueprint.errors import InvalidBlueprint
from homeassistant.core import HomeAssistant
from homeassistant.util import yaml as yaml_util

BLUEPRINTS_DIR = (
    Path(__file__).resolve().parents[1] / "blueprints" / "automation" / "ebay"
)

EXPECTED_BLUEPRINTS = {
    "notify_when_outbid.yaml": "outbid",
    "notify_when_price_drops_below_target.yaml": "watched_item_price_dropped_below",
    "notify_when_watched_item_ending_soon.yaml": "watched_item_ending_soon",
    "notify_when_order_overdue.yaml": "shipment_overdue",
    "notify_when_buyer_question.yaml": "new_buyer_question",
    "notify_when_seller_at_risk.yaml": "seller_standard_at_risk",
}


class _BlueprintLoader(yaml.SafeLoader):
    """SafeLoader that accepts Home Assistant ``!input`` tags."""


def _input_constructor(loader: yaml.SafeLoader, node: yaml.Node) -> str:
    return loader.construct_scalar(node)


_BlueprintLoader.add_constructor("!input", _input_constructor)


def _load_blueprint(path: Path) -> dict:
    data = yaml.load(path.read_text(encoding="utf-8"), Loader=_BlueprintLoader)
    assert isinstance(data, dict)
    return data


def test_expected_blueprints_exist() -> None:
    found = {path.name for path in BLUEPRINTS_DIR.glob("*.yaml")}
    assert found == set(EXPECTED_BLUEPRINTS)


@pytest.mark.parametrize("filename,event_type", sorted(EXPECTED_BLUEPRINTS.items()))
def test_blueprint_structure(filename: str, event_type: str) -> None:
    path = BLUEPRINTS_DIR / filename
    data = _load_blueprint(path)
    meta = data["blueprint"]

    assert meta["domain"] == "automation"
    assert meta["name"].startswith("eBay - ")
    assert "source_url" in meta
    assert meta["source_url"].endswith(filename)
    assert meta["homeassistant"]["min_version"] == "2026.3.0"
    assert "notify_entity" in meta["input"]
    assert data["mode"] == "queued"
    assert data["triggers"]
    assert data["actions"]

    rendered = path.read_text(encoding="utf-8")
    assert f"'{event_type}'" in rendered or f'"{event_type}"' in rendered
    assert "notify.send_message" in rendered
    assert "integration: ebay" in rendered


@pytest.mark.parametrize("filename", sorted(EXPECTED_BLUEPRINTS))
def test_blueprint_validates_with_home_assistant_schema(filename: str) -> None:
    """Blueprints must pass Home Assistant's Blueprint schema used on import."""
    path = BLUEPRINTS_DIR / filename
    data = yaml_util.load_yaml(str(path))
    assert isinstance(data, dict)
    blueprint = Blueprint(
        data,
        path=str(path),
        expected_domain="automation",
        schema=BLUEPRINT_SCHEMA,
    )
    assert blueprint.domain == "automation"
    assert blueprint.name.startswith("eBay - ")


async def test_blueprint_imports_via_hass_config_path(hass: HomeAssistant) -> None:
    """Copy a blueprint into the HA config tree and load it like a user import."""
    import logging

    from homeassistant.components.blueprint import BLUEPRINT_SCHEMA
    from homeassistant.components.blueprint.models import DomainBlueprints

    filename = "notify_when_outbid.yaml"
    dest_dir = Path(hass.config.path("blueprints", "automation", "ebay"))
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / filename
    shutil.copy2(BLUEPRINTS_DIR / filename, dest)

    async def _reload(_hass: HomeAssistant, _path: str) -> None:
        return None

    domain_blueprints = DomainBlueprints(
        hass,
        "automation",
        logging.getLogger(__name__),
        lambda _hass, _path: False,
        _reload,
        BLUEPRINT_SCHEMA,
    )
    blueprint = await domain_blueprints.async_get_blueprint(f"ebay/{filename}")
    assert blueprint.domain == "automation"
    assert blueprint.name.startswith("eBay - ")
    assert "outbid" in dest.read_text(encoding="utf-8")


def test_invalid_blueprint_domain_is_rejected() -> None:
    """Wrong blueprint domain must raise InvalidBlueprint."""
    path = BLUEPRINTS_DIR / "notify_when_outbid.yaml"
    data = yaml_util.load_yaml(str(path))
    assert isinstance(data, dict)
    data["blueprint"]["domain"] = "script"
    with pytest.raises(InvalidBlueprint):
        Blueprint(
            data,
            path=str(path),
            expected_domain="automation",
            schema=BLUEPRINT_SCHEMA,
        )
