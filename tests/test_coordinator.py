"""Tests for UL Transport coordinator."""
from datetime import UTC
from unittest.mock import patch

import aiohttp
from homeassistant.helpers.update_coordinator import UpdateFailed
import pytest

from custom_components.ul_transport.coordinator import ULTransportDataUpdateCoordinator

from .conftest import (
    MOCK_DEPARTURES_RESPONSE,
    MOCK_STOP_ID,
    MOCK_STOP_NAME,
    build_session,
)


def _make_coordinator(hass, selected_lines=None, scan_interval=60):
    return ULTransportDataUpdateCoordinator(
        hass,
        MOCK_STOP_ID,
        MOCK_STOP_NAME,
        selected_lines or [],
        scan_interval,
    )


@pytest.fixture
def coordinator(hass):
    return _make_coordinator(hass)


class TestCoordinatorInit:
    def test_stores_stop_info(self, hass):
        coord = _make_coordinator(hass, selected_lines=["2_Uppsala Central"], scan_interval=120)
        assert coord.stop_id == MOCK_STOP_ID
        assert coord.stop_name == MOCK_STOP_NAME
        assert coord.selected_lines == ["2_Uppsala Central"]


class TestCoordinatorFetch:
    async def test_groups_departures_by_line_direction(self, coordinator):
        with patch(
    "custom_components.ul_transport.coordinator.async_get_clientsession",
    return_value=build_session(200, MOCK_DEPARTURES_RESPONSE),
):

            data = await coordinator._async_update_data()

        assert "2_Uppsala Central" in data
        assert "8_Gottsunda" in data
        assert len(data["2_Uppsala Central"]) == 2
        assert len(data["8_Gottsunda"]) == 1

    async def test_sets_last_successful_update_on_success(self, coordinator):
        with patch(
    "custom_components.ul_transport.coordinator.async_get_clientsession",
    return_value=build_session(200, MOCK_DEPARTURES_RESPONSE),
):

            assert coordinator.last_successful_update is None
            await coordinator._async_update_data()

        assert coordinator.last_successful_update is not None
        assert coordinator.last_successful_update.tzinfo == UTC

    async def test_does_not_update_last_successful_update_on_failure(self, coordinator):
        with patch(
    "custom_components.ul_transport.coordinator.async_get_clientsession",
    return_value=build_session(500, {}),
), pytest.raises(UpdateFailed):
            await coordinator._async_update_data()

        assert coordinator.last_successful_update is None

    async def test_filters_by_selected_lines(self, hass):
        coord = _make_coordinator(hass, selected_lines=["2_Uppsala Central"])
        with patch(
    "custom_components.ul_transport.coordinator.async_get_clientsession",
    return_value=build_session(200, MOCK_DEPARTURES_RESPONSE),
):

            data = await coord._async_update_data()

        assert "2_Uppsala Central" in data
        assert "8_Gottsunda" not in data

    async def test_raises_on_rate_limit(self, coordinator):
        with patch(
    "custom_components.ul_transport.coordinator.async_get_clientsession",
    return_value=build_session(429, {}),
), pytest.raises(UpdateFailed, match="rate limit"):
            await coordinator._async_update_data()

    async def test_raises_on_non_200(self, coordinator):
        with patch(
    "custom_components.ul_transport.coordinator.async_get_clientsession",
    return_value=build_session(500, {}),
), pytest.raises(UpdateFailed, match="500"):
            await coordinator._async_update_data()

    async def test_raises_on_client_error(self, coordinator):
        with patch(
            "custom_components.ul_transport.coordinator.async_get_clientsession",
            return_value=build_session(error=aiohttp.ClientError("connection refused")),
        ), pytest.raises(UpdateFailed, match="connection refused"):
            await coordinator._async_update_data()

    async def test_limits_to_five_departures(self, hass):
        """Ensure more than 5 departures are capped at 5."""
        many_deps = {
            "departures": [
                {
                    "line": {"name": "2", "towards": "Uppsala Central", "lineNo": 2, "trafficType": 1},
                    "departureDateTime": f"2026-04-12T12:0{i}:00",
                    "realTimeDepartureDateTime": None,
                    "coordinate": {"latitude": 59.858, "longitude": 17.645},
                    "area": "Uppsala",
                }
                for i in range(7)
            ]
        }
        coord = _make_coordinator(hass)
        with patch(
    "custom_components.ul_transport.coordinator.async_get_clientsession",
    return_value=build_session(200, many_deps),
):

            data = await coord._async_update_data()

        assert len(data["2_Uppsala Central"]) == 5

    async def test_sorts_departures_by_realtime_then_planned(self, hass):
        unsorted_deps = {
            "departures": [
                {
                    "line": {"name": "2", "towards": "Dest", "lineNo": 2, "trafficType": 1},
                    "departureDateTime": "2026-04-12T12:05:00",
                    "realTimeDepartureDateTime": "2026-04-12T12:07:00",
                    "coordinate": {"latitude": 0, "longitude": 0},
                    "area": "",
                },
                {
                    "line": {"name": "2", "towards": "Dest", "lineNo": 2, "trafficType": 1},
                    "departureDateTime": "2026-04-12T12:01:00",
                    "realTimeDepartureDateTime": None,
                    "coordinate": {"latitude": 0, "longitude": 0},
                    "area": "",
                },
            ]
        }
        coord = _make_coordinator(hass)
        with patch(
    "custom_components.ul_transport.coordinator.async_get_clientsession",
    return_value=build_session(200, unsorted_deps),
):

            data = await coord._async_update_data()

        deps = data["2_Dest"]
        first_time = deps[0].get("realTimeDepartureDateTime") or deps[0]["departureDateTime"]
        second_time = deps[1].get("realTimeDepartureDateTime") or deps[1]["departureDateTime"]
        assert first_time <= second_time
