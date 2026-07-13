"""Validate shipped Home Assistant automation blueprints."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

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
