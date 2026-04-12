"""Tests for UL Transport button entity."""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.ul_transport.button import ULTransportRefreshButton
from custom_components.ul_transport.coordinator import ULTransportDataUpdateCoordinator

from .conftest import MOCK_DEPARTURES_RESPONSE, MOCK_STOP_ID, MOCK_STOP_NAME


def _build_session_mock(status, json_data):
    response = AsyncMock()
    response.status = status
    response.json = AsyncMock(return_value=json_data)
    cm = AsyncMock()
    cm.__aenter__ = AsyncMock(return_value=response)
    cm.__aexit__ = AsyncMock(return_value=False)
    session = MagicMock()
    session.get = MagicMock(return_value=cm)
    return session


@pytest.fixture
async def coordinator(hass):
    coord = ULTransportDataUpdateCoordinator(
        hass, MOCK_STOP_ID, MOCK_STOP_NAME, [], 60
    )
    with patch(
        "custom_components.ul_transport.coordinator.aiohttp.ClientSession"
    ) as mock_session_cls:
        mock_session_cls.return_value.__aenter__ = AsyncMock(
            return_value=_build_session_mock(200, MOCK_DEPARTURES_RESPONSE)
        )
        mock_session_cls.return_value.__aexit__ = AsyncMock(return_value=False)
        await coord.async_refresh()
    return coord


class TestRefreshButton:
    def test_name(self, coordinator):
        button = ULTransportRefreshButton(coordinator, MOCK_STOP_NAME)
        assert button.name == f"{MOCK_STOP_NAME} Refresh"

    def test_unique_id(self, coordinator):
        button = ULTransportRefreshButton(coordinator, MOCK_STOP_NAME)
        assert button.unique_id == f"{MOCK_STOP_ID}_refresh"

    def test_icon(self, coordinator):
        button = ULTransportRefreshButton(coordinator, MOCK_STOP_NAME)
        assert button.icon == "mdi:refresh"

    async def test_press_triggers_coordinator_refresh(self, coordinator):
        button = ULTransportRefreshButton(coordinator, MOCK_STOP_NAME)
        coordinator.async_request_refresh = AsyncMock()

        await button.async_press()

        coordinator.async_request_refresh.assert_called_once()
