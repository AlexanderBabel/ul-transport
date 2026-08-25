"""strings.json, its translations and icons.json have to agree with the code.

Nothing else catches a drift here: a renamed translation key still loads, and
the entity simply shows up as its object id with no icon. Home Assistant's own
hassfest checks the schema of these files, not whether they cover the keys this
integration actually asks for.
"""

from __future__ import annotations

import json
from pathlib import Path
import re

import pytest

COMPONENT = Path(__file__).parent.parent / "custom_components" / "ul_transport"
STRINGS = json.loads((COMPONENT / "strings.json").read_text())
ICONS = json.loads((COMPONENT / "icons.json").read_text())
TRANSLATIONS = sorted((COMPONENT / "translations").glob("*.json"))

# Every _attr_translation_key in the integration, by platform.
USED_KEYS = {
    "button": {"refresh"},
    "sensor": {"last_update", "next_departure", "realtime_quota", "static_quota"},
}

# The ones with a fixed icon. The departure sensors set `icon` from the traffic
# type at runtime, which icon translations cannot express.
ICON_KEYS = {
    "button": {"refresh"},
    "sensor": {"last_update", "realtime_quota", "static_quota"},
}


def _walk(node, prefix=""):
    """Every leaf path in a nested dict, as dotted strings."""
    for key, value in node.items():
        path = f"{prefix}.{key}" if prefix else key
        if isinstance(value, dict):
            yield from _walk(value, path)
        else:
            yield path


@pytest.mark.parametrize("platform", sorted(USED_KEYS))
def test_every_translation_key_is_named(platform):
    named = STRINGS["entity"][platform]
    for key in USED_KEYS[platform]:
        assert "name" in named.get(key, {}), f"{platform}.{key} has no name"


@pytest.mark.parametrize("platform", sorted(ICON_KEYS))
def test_every_fixed_icon_is_an_icon_translation(platform):
    icons = ICONS["entity"][platform]
    for key in ICON_KEYS[platform]:
        assert icons.get(key, {}).get("default", "").startswith("mdi:")


def test_icons_json_has_no_keys_the_code_never_uses():
    for platform, keys in ICONS["entity"].items():
        assert set(keys) <= USED_KEYS[platform]


@pytest.mark.parametrize("path", TRANSLATIONS, ids=lambda p: p.stem)
def test_translation_covers_every_string(path):
    """A missing key falls back to English silently; a stale one is dead weight."""
    expected = set(_walk(STRINGS))
    actual = set(_walk(json.loads(path.read_text())))
    assert not expected - actual, f"{path.stem} is missing {sorted(expected - actual)}"
    assert not actual - expected, f"{path.stem} has stale {sorted(actual - expected)}"


def test_every_step_the_flow_can_show_has_strings():
    """A step with no strings renders as its raw key, with no title or labels.

    hassfest checks the shape of strings.json, not whether it covers the steps
    this flow actually shows.
    """
    flow = (COMPONENT / "config_flow.py").read_text()
    shown = set(re.findall(r'step_id="([a-z_]+)"', flow))
    described = set(STRINGS["config"]["step"]) | set(STRINGS["options"]["step"])
    assert shown <= described, f"no strings for {sorted(shown - described)}"


def test_the_menu_labels_every_option_it_offers():
    """An unlabelled menu option shows as a blank row."""
    flow = (COMPONENT / "config_flow.py").read_text()
    offered = set(
        re.findall(r"menu_options=\[([^\]]+)\]", flow)[0].replace('"', "").split(", ")
    )
    labelled = set(STRINGS["config"]["step"]["user"]["menu_options"])
    assert offered == labelled
