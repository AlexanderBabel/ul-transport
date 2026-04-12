"""Test fixtures for UL Transport."""
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import HomeAssistant

from custom_components.ul_transport.const import DOMAIN

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


@pytest.fixture
def mock_api_response():
    """Return a mock aiohttp response."""
    return MOCK_DEPARTURES_RESPONSE


@pytest.fixture
def mock_config_entry(hass: HomeAssistant):
    """Return a mock config entry."""
    from homeassistant.config_entries import ConfigEntry
    from unittest.mock import MagicMock

    entry = MagicMock(spec=ConfigEntry)
    entry.entry_id = "test_entry_id"
    entry.data = {
        "stop_id": MOCK_STOP_ID,
        "stop_name": MOCK_STOP_NAME,
        "selected_lines": [],
        "scan_interval": 60,
    }
    entry.options = {}
    entry.async_on_unload = MagicMock()
    entry.add_update_listener = MagicMock(return_value=lambda: None)
    return entry
