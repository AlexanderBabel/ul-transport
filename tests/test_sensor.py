"""Tests for the UL Transport sensors.

The states are minutes until the next bus, so the fixtures are built relative
to now rather than pinned to a date - a departure board fixed in 2026 tests
nothing about a countdown.
"""
from datetime import datetime, timedelta, timezone

import pytest

from custom_components.ul_transport.coordinator import ULTransportDataUpdateCoordinator
from custom_components.ul_transport.sensor import (
    ULLineDepartureSensor,
    ULNextDepartureSensor,
    ULTransportLastUpdateSensor,
)

from .conftest import MOCK_STOP_ID, MOCK_STOP_NAME

LINE_2 = "2_Uppsala Central"
LINE_8 = "8_Gottsunda"


def _stamp(minutes: float) -> str:
    """UL's own format: ISO in UTC with a trailing Z."""
    when = datetime.now(timezone.utc) + timedelta(minutes=minutes)
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


def _departure(planned: float, estimated: float | None = None, line="2",
               towards="Uppsala Central") -> dict:
    return {
        "line": {"name": line, "towards": towards, "lineNo": int(line), "trafficType": 1},
        "departureDateTime": _stamp(planned),
        "realTimeDepartureDateTime": None if estimated is None else _stamp(estimated),
        "coordinate": {"latitude": 59.858, "longitude": 17.645},
        "area": "Uppsala",
    }


@pytest.fixture
def coordinator(hass):
    coord = ULTransportDataUpdateCoordinator(hass, MOCK_STOP_ID, MOCK_STOP_NAME, [], 60)
    coord.data = {
        # Six minutes out and running two minutes late.
        LINE_2: [_departure(4.05, 6.05), _departure(20.05)],
        LINE_8: [_departure(2.05, 2.05, line="8", towards="Gottsunda")],
    }
    coord.last_update_success = True
    coord.last_successful_update = datetime.now(timezone.utc)
    return coord


class TestLineSensor:
    def test_name_and_unique_id(self, coordinator):
        sensor = ULLineDepartureSensor(coordinator, LINE_2, MOCK_STOP_NAME)
        assert sensor.name == f"{MOCK_STOP_NAME} Line 2 to Uppsala Central"
        assert sensor.unique_id == f"{MOCK_STOP_ID}_{LINE_2}_in"

    def test_state_is_minutes_until_the_next_bus(self, coordinator):
        sensor = ULLineDepartureSensor(coordinator, LINE_2, MOCK_STOP_NAME)
        assert sensor.native_value == 6

    def test_minutes_are_floored_not_rounded(self, coordinator):
        """"In 2 minutes" has to stop being true before the bus goes."""
        coordinator.data[LINE_2] = [_departure(2.9, 2.9)]
        assert ULLineDepartureSensor(coordinator, LINE_2, MOCK_STOP_NAME).native_value == 2

    def test_a_bus_at_the_kerb_reads_zero_not_negative(self, coordinator):
        coordinator.data[LINE_2] = [_departure(-3, -3)]
        assert ULLineDepartureSensor(coordinator, LINE_2, MOCK_STOP_NAME).native_value == 0

    def test_falls_back_to_the_timetable_without_realtime(self, coordinator):
        coordinator.data[LINE_2] = [_departure(9.05)]
        sensor = ULLineDepartureSensor(coordinator, LINE_2, MOCK_STOP_NAME)
        assert sensor.native_value == 9
        assert sensor.extra_state_attributes["is_realtime"] is False
        assert sensor.extra_state_attributes["delay_minutes"] is None

    def test_none_when_nothing_is_running(self, coordinator):
        coordinator.data[LINE_2] = []
        sensor = ULLineDepartureSensor(coordinator, LINE_2, MOCK_STOP_NAME)
        assert sensor.native_value is None
        assert sensor.extra_state_attributes == {}
        assert sensor.available is False


class TestAttributes:
    def test_the_bits_an_automation_needs(self, coordinator):
        attrs = ULLineDepartureSensor(
            coordinator, LINE_2, MOCK_STOP_NAME
        ).extra_state_attributes
        assert attrs["line"] == "2"
        assert attrs["direction"] == "Uppsala Central"
        assert attrs["transport"] == "BUS"
        assert attrs["stop_name"] == MOCK_STOP_NAME
        assert attrs["is_realtime"] is True
        assert attrs["delay_minutes"] == 2
        assert attrs["departure"] > attrs["scheduled_departure"]

    def test_the_ones_after_it(self, coordinator):
        attrs = ULLineDepartureSensor(
            coordinator, LINE_2, MOCK_STOP_NAME
        ).extra_state_attributes
        assert attrs["next_departures_in"] == [20]
        assert len(attrs["next_departures"]) == 1

    def test_icon_follows_the_traffic_type(self, coordinator):
        assert ULLineDepartureSensor(coordinator, LINE_2, MOCK_STOP_NAME).icon == "mdi:bus"


class TestNextDepartureSensor:
    """One sensor for "is anything leaving soon", whichever line it is."""

    def test_picks_the_earliest_across_lines(self, coordinator):
        sensor = ULNextDepartureSensor(coordinator, MOCK_STOP_NAME)
        assert sensor.native_value == 2
        assert sensor.extra_state_attributes["line"] == "8"

    def test_unique_id_and_name(self, coordinator):
        sensor = ULNextDepartureSensor(coordinator, MOCK_STOP_NAME)
        assert sensor.unique_id == f"{MOCK_STOP_ID}_next_departure"
        assert sensor.name == f"{MOCK_STOP_NAME} Next departure"

    def test_unavailable_with_an_empty_board(self, coordinator):
        coordinator.data = {}
        assert ULNextDepartureSensor(coordinator, MOCK_STOP_NAME).available is False


class TestLastUpdateSensor:
    def test_name_and_unique_id(self, coordinator):
        sensor = ULTransportLastUpdateSensor(coordinator, MOCK_STOP_NAME)
        assert sensor.name == f"{MOCK_STOP_NAME} Last Update"
        assert sensor.unique_id == f"{MOCK_STOP_ID}_last_update"

    def test_icon(self, coordinator):
        sensor = ULTransportLastUpdateSensor(coordinator, MOCK_STOP_NAME)
        assert sensor.icon == "mdi:clock-check-outline"

    def test_native_value_none_before_first_fetch(self, coordinator):
        coordinator.last_successful_update = None
        sensor = ULTransportLastUpdateSensor(coordinator, MOCK_STOP_NAME)
        assert sensor.native_value is None
        assert sensor.available is False

    def test_native_value_after_successful_fetch(self, coordinator):
        now = datetime.now(timezone.utc)
        coordinator.last_successful_update = now
        sensor = ULTransportLastUpdateSensor(coordinator, MOCK_STOP_NAME)
        assert sensor.native_value == now
        assert sensor.available is True
