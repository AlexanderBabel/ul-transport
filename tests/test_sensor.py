"""Tests for the UL Transport sensors.

The states are minutes until the next bus, so the fixtures are built relative
to now rather than pinned to a date - a departure board fixed in 2026 tests
nothing about a countdown.
"""

from datetime import UTC, datetime, timedelta
import json
import pathlib

from homeassistant.util import dt as dt_util
import pytest

from custom_components.ul_transport.button import ULTransportRefreshButton
from custom_components.ul_transport.const import DOMAIN
from custom_components.ul_transport.coordinator import ULTransportDataUpdateCoordinator
from custom_components.ul_transport.quota import (
    QUOTA_DAYS,
    QUOTA_REALTIME,
    QUOTA_STATIC,
    count_request,
    request_quota,
)
from custom_components.ul_transport.sensor import (
    ULLineDepartureSensor,
    ULNextDepartureSensor,
    ULQuotaSensor,
    ULTransportLastUpdateSensor,
)

from .conftest import MOCK_STOP_ID, MOCK_STOP_NAME

LINE_2 = "2_Uppsala Central"
LINE_8 = "8_Gottsunda"


def _stamp(minutes: float) -> str:
    """UL's own format: ISO in UTC with a trailing Z."""
    when = datetime.now(UTC) + timedelta(minutes=minutes)
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


def _departure(
    planned: float, estimated: float | None = None, line="2", towards="Uppsala Central"
) -> dict:
    return {
        "line": {
            "name": line,
            "towards": towards,
            "lineNo": int(line),
            "trafficType": 1,
        },
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
    coord.last_successful_update = datetime.now(UTC)
    return coord


class TestLineSensor:
    def test_name_and_unique_id(self, coordinator):
        sensor = ULLineDepartureSensor(coordinator, LINE_2)
        # has_entity_name: the stop is the device, so the entity carries only
        # the part after it. Home Assistant composes the friendly name.
        assert sensor.has_entity_name is True
        assert sensor.name == "Line 2 to Uppsala Central"
        assert sensor.unique_id == f"{MOCK_STOP_ID}_{LINE_2}_in"

    def test_state_is_minutes_until_the_next_bus(self, coordinator):
        sensor = ULLineDepartureSensor(coordinator, LINE_2)
        assert sensor.native_value == 6

    def test_minutes_are_floored_not_rounded(self, coordinator):
        """'In 2 minutes' has to stop being true before the bus goes."""
        coordinator.data[LINE_2] = [_departure(2.9, 2.9)]
        assert ULLineDepartureSensor(coordinator, LINE_2).native_value == 2

    def test_a_bus_at_the_kerb_reads_zero_not_negative(self, coordinator):
        coordinator.data[LINE_2] = [_departure(-3, -3)]
        assert ULLineDepartureSensor(coordinator, LINE_2).native_value == 0

    def test_falls_back_to_the_timetable_without_realtime(self, coordinator):
        coordinator.data[LINE_2] = [_departure(9.05)]
        sensor = ULLineDepartureSensor(coordinator, LINE_2)
        assert sensor.native_value == 9
        assert sensor.extra_state_attributes["is_realtime"] is False
        assert sensor.extra_state_attributes["delay_minutes"] is None

    def test_none_when_nothing_is_running(self, coordinator):
        coordinator.data[LINE_2] = []
        sensor = ULLineDepartureSensor(coordinator, LINE_2)
        assert sensor.native_value is None
        assert sensor.extra_state_attributes == {}
        assert sensor.available is False


class TestAttributes:
    def test_the_bits_an_automation_needs(self, coordinator):
        attrs = ULLineDepartureSensor(coordinator, LINE_2).extra_state_attributes
        assert attrs["line"] == "2"
        assert attrs["direction"] == "Uppsala Central"
        assert attrs["transport"] == "BUS"
        assert attrs["stop_name"] == MOCK_STOP_NAME
        assert attrs["is_realtime"] is True
        assert attrs["delay_minutes"] == 2
        assert attrs["departure"] > attrs["scheduled_departure"]

    def test_the_ones_after_it(self, coordinator):
        attrs = ULLineDepartureSensor(coordinator, LINE_2).extra_state_attributes
        assert attrs["next_departures_in"] == [20]
        assert len(attrs["next_departures"]) == 1

    def test_icon_follows_the_traffic_type(self, coordinator):
        assert ULLineDepartureSensor(coordinator, LINE_2).icon == "mdi:bus"


class TestNextDepartureSensor:
    """One sensor for "is anything leaving soon", whichever line it is."""

    def test_picks_the_earliest_across_lines(self, coordinator):
        sensor = ULNextDepartureSensor(coordinator)
        assert sensor.native_value == 2
        assert sensor.extra_state_attributes["line"] == "8"

    def test_unique_id_and_name(self, coordinator):
        sensor = ULNextDepartureSensor(coordinator)
        assert sensor.unique_id == f"{MOCK_STOP_ID}_next_departure"
        assert sensor.translation_key == "next_departure"

    def test_unavailable_with_an_empty_board(self, coordinator):
        coordinator.data = {}
        assert ULNextDepartureSensor(coordinator).available is False


class TestLastUpdateSensor:
    def test_name_and_unique_id(self, coordinator):
        sensor = ULTransportLastUpdateSensor(coordinator)
        assert sensor.translation_key == "last_update"
        assert sensor.unique_id == f"{MOCK_STOP_ID}_last_update"

    def test_icon(self, coordinator):
        sensor = ULTransportLastUpdateSensor(coordinator)
        assert sensor.icon == "mdi:clock-check-outline"

    def test_native_value_none_before_first_fetch(self, coordinator):
        coordinator.last_successful_update = None
        sensor = ULTransportLastUpdateSensor(coordinator)
        assert sensor.native_value is None
        assert sensor.available is False

    def test_native_value_after_successful_fetch(self, coordinator):
        now = datetime.now(UTC)
        coordinator.last_successful_update = now
        sensor = ULTransportLastUpdateSensor(coordinator)
        assert sensor.native_value == now
        assert sensor.available is True


class TestQuotaSensors:
    """The quota meters: what has been spent of each, and on what."""

    def test_zero_before_anything_is_fetched(self, hass):
        sensor = ULQuotaSensor(hass, "realtime", QUOTA_REALTIME)
        assert sensor.native_value == 0
        assert sensor.extra_state_attributes["remaining"] == QUOTA_REALTIME

    def test_realtime_counts_only_what_that_quota_sees(self, hass):
        for _ in range(3):
            count_request(hass, "rt_TripUpdates")
        count_request(hass, "rt_VehiclePositions")
        count_request(hass, "rt_not_modified")
        count_request(hass, "gtfs_static")
        count_request(hass, "ul_departures")

        sensor = ULQuotaSensor(hass, "realtime", QUOTA_REALTIME)
        # UL is a different API, and not_modified is a tally of calls already
        # counted, so neither belongs in what the Trafiklab quota sees.
        assert sensor.native_value == 4
        attributes = sensor.extra_state_attributes
        assert attributes["remaining"] == QUOTA_REALTIME - 4
        assert attributes["limit"] == QUOTA_REALTIME
        # The breakdown still carries everything, quota or not.
        assert attributes["requests"]["rt_TripUpdates"] == 3
        assert attributes["requests"]["ul_departures"] == 1

    def test_static_counts_only_the_static_downloads(self, hass):
        count_request(hass, "gtfs_static")
        count_request(hass, "rt_TripUpdates")

        sensor = ULQuotaSensor(hass, "static", QUOTA_STATIC)
        assert sensor.native_value == 1
        assert sensor.extra_state_attributes["remaining"] == QUOTA_STATIC - 1

    def test_days_outside_the_window_are_forgotten(self, hass):
        quota = request_quota(hass)
        # A day older than the window; the quota it spent has expired with it.
        stale = (dt_util.now().date() - timedelta(days=QUOTA_DAYS)).isoformat()
        quota._days[stale] = {"rt_TripUpdates": 500}
        count_request(hass, "rt_TripUpdates")

        assert quota.realtime() == 1


class TestDeviceAndTranslations:
    """Entities hang off the stop, and every translation_key has a string."""

    def test_every_entity_is_on_the_stop_device(self, coordinator):
        for sensor in (
            ULLineDepartureSensor(coordinator, LINE_2),
            ULNextDepartureSensor(coordinator),
            ULTransportLastUpdateSensor(coordinator),
        ):
            assert sensor.device_info["identifiers"] == {(DOMAIN, str(MOCK_STOP_ID))}
            assert sensor.device_info["name"] == MOCK_STOP_NAME

    def test_translation_keys_exist_in_every_language(self, coordinator):
        """A translation_key with no string renders as a blank name in the UI."""
        component = (
            pathlib.Path(__file__).parent.parent / "custom_components/ul_transport"
        )
        wanted = {
            "sensor": {
                ULNextDepartureSensor(coordinator).translation_key,
                ULTransportLastUpdateSensor(coordinator).translation_key,
            },
            "button": {ULTransportRefreshButton(coordinator).translation_key},
        }
        files = [
            component / "strings.json",
            *(component / "translations").glob("*.json"),
        ]
        assert len(files) >= 3
        for path in files:
            entities = json.loads(path.read_text())["entity"]
            for platform, keys in wanted.items():
                for key in keys:
                    assert entities[platform][key]["name"], (
                        f"{path.name}: {platform}.{key}"
                    )
