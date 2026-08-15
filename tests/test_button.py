"""Tests for UL Transport button entity."""

from unittest.mock import AsyncMock, patch

import pytest

from custom_components.ul_transport.button import ULTransportRefreshButton
from custom_components.ul_transport.const import DOMAIN
from custom_components.ul_transport.coordinator import ULTransportDataUpdateCoordinator

from .conftest import MOCK_STOP_ID, MOCK_STOP_NAME, build_session


@pytest.fixture
async def coordinator(hass):
    coord = ULTransportDataUpdateCoordinator(hass, MOCK_STOP_ID, MOCK_STOP_NAME, [], 60)
    with patch(
        "custom_components.ul_transport.coordinator.async_get_clientsession",
        return_value=build_session(),
    ):
        await coord.async_refresh()
    return coord


class TestRefreshButton:
    def test_unique_id(self, coordinator):
        button = ULTransportRefreshButton(coordinator)
        assert button.unique_id == f"{MOCK_STOP_ID}_refresh"

    def test_icon(self, coordinator):
        assert ULTransportRefreshButton(coordinator).icon == "mdi:refresh"

    def test_named_from_the_stop_device(self, coordinator):
        """The stop is the device, so the button is just "Refresh" on it."""
        button = ULTransportRefreshButton(coordinator)
        assert button.has_entity_name is True
        assert button.translation_key == "refresh"
        assert button.device_info["identifiers"] == {(DOMAIN, str(MOCK_STOP_ID))}
        assert button.device_info["name"] == MOCK_STOP_NAME

    async def test_press_triggers_coordinator_refresh(self, coordinator):
        button = ULTransportRefreshButton(coordinator)
        coordinator.async_request_refresh = AsyncMock()

        await button.async_press()

        coordinator.async_request_refresh.assert_called_once()
