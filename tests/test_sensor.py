"""Tests for UL Transport sensor entity."""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.ul_transport.coordinator import ULTransportDataUpdateCoordinator
from custom_components.ul_transport.sensor import ULTransportSensor

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


class TestSensorInit:
    def test_name_and_unique_id(self, coordinator):
        sensor = ULTransportSensor(coordinator, "2_Uppsala Central", MOCK_STOP_NAME)
        assert sensor.name == f"{MOCK_STOP_NAME} Line 2 to Uppsala Central"
        assert sensor.unique_id == f"{MOCK_STOP_ID}_2_Uppsala Central"


class TestSensorState:
    def test_returns_realtime_when_available(self, coordinator):
        sensor = ULTransportSensor(coordinator, "2_Uppsala Central", MOCK_STOP_NAME)
        assert sensor.state == "2026-04-12T12:02:00"

    def test_returns_planned_when_no_realtime(self, coordinator):
        # Patch coordinator data with no realtime
        coordinator.data["2_Uppsala Central"][0]["realTimeDepartureDateTime"] = None
        sensor = ULTransportSensor(coordinator, "2_Uppsala Central", MOCK_STOP_NAME)
        assert sensor.state == "2026-04-12T12:00:00"

    def test_returns_none_when_no_departures(self, coordinator):
        coordinator.data["2_Uppsala Central"] = []
        sensor = ULTransportSensor(coordinator, "2_Uppsala Central", MOCK_STOP_NAME)
        assert sensor.state is None


class TestSensorAttributes:
    def test_core_attributes_present(self, coordinator):
        sensor = ULTransportSensor(coordinator, "2_Uppsala Central", MOCK_STOP_NAME)
        attrs = sensor.extra_state_attributes

        assert attrs["line_name"] == "2"
        assert attrs["transport"] == "BUS"
        assert attrs["direction"] == "Uppsala Central"
        assert attrs["stop_name"] == MOCK_STOP_NAME
        assert attrs["line_color"] == "#af1e14"  # Line 2 = red
        assert attrs["text_color"] == "#ffffff"

    def test_departure_slots_filled(self, coordinator):
        sensor = ULTransportSensor(coordinator, "2_Uppsala Central", MOCK_STOP_NAME)
        attrs = sensor.extra_state_attributes

        assert "planned_departure_time" in attrs
        assert "planned_departure_time_1" in attrs
        # Slots beyond available departures filled with None
        assert attrs["planned_departure_time_2"] is None

    def test_empty_when_no_departures(self, coordinator):
        coordinator.data["2_Uppsala Central"] = []
        sensor = ULTransportSensor(coordinator, "2_Uppsala Central", MOCK_STOP_NAME)
        assert sensor.extra_state_attributes == {}


class TestSensorIcon:
    def test_bus_icon(self, coordinator):
        sensor = ULTransportSensor(coordinator, "2_Uppsala Central", MOCK_STOP_NAME)
        assert sensor.icon == "mdi:bus"

    def test_default_icon_when_no_departures(self, coordinator):
        coordinator.data["2_Uppsala Central"] = []
        sensor = ULTransportSensor(coordinator, "2_Uppsala Central", MOCK_STOP_NAME)
        assert sensor.icon == "mdi:transit-connection-variant"


class TestSensorAvailability:
    def test_available_with_data(self, coordinator):
        sensor = ULTransportSensor(coordinator, "2_Uppsala Central", MOCK_STOP_NAME)
        assert sensor.available is True

    def test_unavailable_when_coordinator_failed(self, coordinator):
        coordinator.last_update_success = False
        sensor = ULTransportSensor(coordinator, "2_Uppsala Central", MOCK_STOP_NAME)
        assert sensor.available is False

    def test_unavailable_when_empty_data(self, coordinator):
        coordinator.data["2_Uppsala Central"] = []
        sensor = ULTransportSensor(coordinator, "2_Uppsala Central", MOCK_STOP_NAME)
        assert sensor.available is False
