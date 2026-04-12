"""Tests for UL Transport coordinator."""
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest
from homeassistant.helpers.update_coordinator import UpdateFailed

from custom_components.ul_transport.coordinator import ULTransportDataUpdateCoordinator

from .conftest import MOCK_DEPARTURES_RESPONSE, MOCK_STOP_ID, MOCK_STOP_NAME


def _make_coordinator(hass, selected_lines=None, scan_interval=60):
    return ULTransportDataUpdateCoordinator(
        hass,
        MOCK_STOP_ID,
        MOCK_STOP_NAME,
        selected_lines or [],
        scan_interval,
    )


def _mock_response(status=200, json_data=None):
    response = AsyncMock()
    response.status = status
    response.json = AsyncMock(return_value=json_data or MOCK_DEPARTURES_RESPONSE)
    return response


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
            "custom_components.ul_transport.coordinator.aiohttp.ClientSession"
        ) as mock_session_cls:
            mock_session_cls.return_value.__aenter__ = AsyncMock(
                return_value=_build_session_mock(200, MOCK_DEPARTURES_RESPONSE)
            )
            mock_session_cls.return_value.__aexit__ = AsyncMock(return_value=False)

            data = await coordinator._async_update_data()

        assert "2_Uppsala Central" in data
        assert "8_Gottsunda" in data
        assert len(data["2_Uppsala Central"]) == 2
        assert len(data["8_Gottsunda"]) == 1

    async def test_filters_by_selected_lines(self, hass):
        coord = _make_coordinator(hass, selected_lines=["2_Uppsala Central"])
        with patch(
            "custom_components.ul_transport.coordinator.aiohttp.ClientSession"
        ) as mock_session_cls:
            mock_session_cls.return_value.__aenter__ = AsyncMock(
                return_value=_build_session_mock(200, MOCK_DEPARTURES_RESPONSE)
            )
            mock_session_cls.return_value.__aexit__ = AsyncMock(return_value=False)

            data = await coord._async_update_data()

        assert "2_Uppsala Central" in data
        assert "8_Gottsunda" not in data

    async def test_raises_on_rate_limit(self, coordinator):
        with patch(
            "custom_components.ul_transport.coordinator.aiohttp.ClientSession"
        ) as mock_session_cls:
            mock_session_cls.return_value.__aenter__ = AsyncMock(
                return_value=_build_session_mock(429, {})
            )
            mock_session_cls.return_value.__aexit__ = AsyncMock(return_value=False)

            with pytest.raises(UpdateFailed, match="rate limit"):
                await coordinator._async_update_data()

    async def test_raises_on_non_200(self, coordinator):
        with patch(
            "custom_components.ul_transport.coordinator.aiohttp.ClientSession"
        ) as mock_session_cls:
            mock_session_cls.return_value.__aenter__ = AsyncMock(
                return_value=_build_session_mock(500, {})
            )
            mock_session_cls.return_value.__aexit__ = AsyncMock(return_value=False)

            with pytest.raises(UpdateFailed, match="500"):
                await coordinator._async_update_data()

    async def test_raises_on_client_error(self, coordinator):
        with patch(
            "custom_components.ul_transport.coordinator.aiohttp.ClientSession"
        ) as mock_session_cls:
            session_mock = MagicMock()
            session_mock.__aenter__ = AsyncMock(side_effect=aiohttp.ClientError("connection refused"))
            session_mock.__aexit__ = AsyncMock(return_value=False)
            mock_session_cls.return_value = session_mock

            with pytest.raises(UpdateFailed, match="connection refused"):
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
            "custom_components.ul_transport.coordinator.aiohttp.ClientSession"
        ) as mock_session_cls:
            mock_session_cls.return_value.__aenter__ = AsyncMock(
                return_value=_build_session_mock(200, many_deps)
            )
            mock_session_cls.return_value.__aexit__ = AsyncMock(return_value=False)

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
            "custom_components.ul_transport.coordinator.aiohttp.ClientSession"
        ) as mock_session_cls:
            mock_session_cls.return_value.__aenter__ = AsyncMock(
                return_value=_build_session_mock(200, unsorted_deps)
            )
            mock_session_cls.return_value.__aexit__ = AsyncMock(return_value=False)

            data = await coord._async_update_data()

        deps = data["2_Dest"]
        first_time = deps[0].get("realTimeDepartureDateTime") or deps[0]["departureDateTime"]
        second_time = deps[1].get("realTimeDepartureDateTime") or deps[1]["departureDateTime"]
        assert first_time <= second_time


# --- helpers ---

def _build_session_mock(status, json_data):
    """Build a mock that acts as the session returned by __aenter__."""
    response = AsyncMock()
    response.status = status
    response.json = AsyncMock(return_value=json_data)

    cm = AsyncMock()
    cm.__aenter__ = AsyncMock(return_value=response)
    cm.__aexit__ = AsyncMock(return_value=False)

    session = MagicMock()
    session.get = MagicMock(return_value=cm)
    return session
