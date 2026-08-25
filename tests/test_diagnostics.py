"""Diagnostics have to say enough to debug with, and must not leak the keys."""

from __future__ import annotations

from homeassistant.core import HomeAssistant

from custom_components.ul_transport.const import (
    CONF_GTFS_REALTIME_KEY,
    CONF_GTFS_STATIC_KEY,
)
from custom_components.ul_transport.coordinator import ULTransportDataUpdateCoordinator
from custom_components.ul_transport.diagnostics import (
    async_get_config_entry_diagnostics,
)

from .conftest import MOCK_STOP_ID, MOCK_STOP_NAME, add_stop

STATIC_KEY = "0123456789abcdef0123456789abcdef"
REALTIME_KEY = "fedcba9876543210fedcba9876543210"


async def _diagnostics(hass: HomeAssistant) -> dict:
    coordinator = ULTransportDataUpdateCoordinator(
        hass, None, MOCK_STOP_ID, MOCK_STOP_NAME, ["3_Nyby"], 60
    )
    coordinator.data = {"3_Nyby": []}
    coordinator.board_keys = ["3_Nyby", "5_Stenhagen"]
    entry = add_stop(hass, coordinator)
    hass.config_entries.async_update_entry(
        entry,
        options={
            CONF_GTFS_STATIC_KEY: STATIC_KEY,
            CONF_GTFS_REALTIME_KEY: REALTIME_KEY,
        },
    )
    return await async_get_config_entry_diagnostics(hass, entry)


async def test_trafiklab_keys_are_redacted(hass: HomeAssistant):
    diagnostics = await _diagnostics(hass)
    assert STATIC_KEY not in str(diagnostics)
    assert REALTIME_KEY not in str(diagnostics)


async def test_reports_what_the_board_offers_against_what_was_picked(
    hass: HomeAssistant,
):
    """The usual reason a sensor is missing, so both halves have to be there."""
    diagnostics = await _diagnostics(hass)
    coordinator = diagnostics["coordinator"]
    assert coordinator["board_keys"] == ["3_Nyby", "5_Stenhagen"]
    assert coordinator["selected_lines"] == ["3_Nyby"]


async def test_reports_the_map_as_unconfigured_without_an_index(hass: HomeAssistant):
    diagnostics = await _diagnostics(hass)
    assert diagnostics["map"]["configured"] is False
    assert diagnostics["map"]["index"] is None
    assert diagnostics["quota"]["realtime"]["limit"] == 30000
