"""Test fixtures for UL Transport."""

from unittest.mock import AsyncMock, MagicMock

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ul_transport.const import (
    CONF_SCAN_INTERVAL,
    CONF_SELECTED_LINES,
    CONF_STOP_ID,
    CONF_STOP_NAME,
    DOMAIN,
)

MOCK_STOP_ID = 740020565
MOCK_STOP_NAME = "Centralstationen"

MOCK_DEPARTURES_RESPONSE = {
    "departures": [
        {
            "line": {
                "name": "2",
                "towards": "Uppsala Central",
                "lineNo": 2,
                "trafficType": 1,
            },
            "departureDateTime": "2026-04-12T12:00:00",
            "realTimeDepartureDateTime": "2026-04-12T12:02:00",
            "coordinate": {"latitude": 59.858, "longitude": 17.645},
            "area": "Uppsala",
        },
        {
            "line": {
                "name": "2",
                "towards": "Uppsala Central",
                "lineNo": 2,
                "trafficType": 1,
            },
            "departureDateTime": "2026-04-12T12:10:00",
            "realTimeDepartureDateTime": None,
            "coordinate": {"latitude": 59.858, "longitude": 17.645},
            "area": "Uppsala",
        },
        {
            "line": {
                "name": "8",
                "towards": "Gottsunda",
                "lineNo": 8,
                "trafficType": 1,
            },
            "departureDateTime": "2026-04-12T12:05:00",
            "realTimeDepartureDateTime": "2026-04-12T12:05:00",
            "coordinate": {"latitude": 59.858, "longitude": 17.645},
            "area": "Uppsala",
        },
    ]
}


def build_session(status: int = 200, json_data=None, error: Exception | None = None):
    """A stand-in for the shared aiohttp session.

    Matches what ``async_get_clientsession`` hands back - a session object whose
    ``get`` is an async context manager - rather than the class, so the tests
    exercise the same call shape the integration uses.
    """
    session = MagicMock()
    if error is not None:
        session.get = MagicMock(side_effect=error)
        return session
    response = AsyncMock()
    response.status = status
    response.json = AsyncMock(return_value=MOCK_DEPARTURES_RESPONSE if json_data is None else json_data)
    context = AsyncMock()
    context.__aenter__ = AsyncMock(return_value=response)
    context.__aexit__ = AsyncMock(return_value=False)
    session.get = MagicMock(return_value=context)
    return session


def add_stop(hass: HomeAssistant, coordinator, stop_id: int = MOCK_STOP_ID) -> MockConfigEntry:
    """Register a stop the way async_setup_entry leaves it.

    The coordinator lives on the config entry's runtime_data, so anything that
    looks up "which stops exist" has to go through the entry register.
    """
    hass.data.setdefault(DOMAIN, {})
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=str(stop_id),
        title=coordinator.stop_name,
        data={
            CONF_STOP_ID: stop_id,
            CONF_STOP_NAME: coordinator.stop_name,
            CONF_SELECTED_LINES: [],
            CONF_SCAN_INTERVAL: 60,
        },
    )
    entry.add_to_hass(hass)
    entry.runtime_data = coordinator
    entry.mock_state(hass, ConfigEntryState.LOADED)
    return entry


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Let Home Assistant load custom_components/ul_transport in tests."""
    return
