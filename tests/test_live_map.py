"""Tests for the live map: GTFS indexing and the two map views."""

from __future__ import annotations

from datetime import datetime, time as dtime, timedelta
import io
import time
import zipfile
from zoneinfo import ZoneInfo

from google.transit import gtfs_realtime_pb2
import pytest

from custom_components.ul_transport.gtfs import (
    GTFSIndex,
    build_index,
    ul_stop_to_gtfs_area,
)
from custom_components.ul_transport.live import (
    _predictions,
    _stops_away,
    direction_names,
    line_view,
    overview,
    ul_directions,
)

# A tiny synthetic feed: one stop with two platforms, two lines, three trips.
MY_UL_ID = 3700600
MY_AREA = "9021003700600000"

STOPS = """stop_id,stop_name,stop_lat,stop_lon,location_type,parent_station,platform_code
9021003700600000,Uppsala Centralstation,59.858,17.646,1,,
9022003700600001,Uppsala Centralstation,59.8581,17.6461,0,9021003700600000,A
9022003700600002,Uppsala Centralstation,59.8582,17.6462,0,9021003700600000,B
9021003700141000,Granbystaden,59.8759,17.6763,1,,
9022003700141001,Granbystaden,59.8755,17.6756,0,9021003700141000,A
9021003700999000,Elsewhere,59.900,17.700,1,,
9022003700999001,Elsewhere,59.9001,17.7001,0,9021003700999000,A
9021003700888000,Terminus,59.910,17.710,1,,
9022003700888001,Terminus,59.9101,17.7101,0,9021003700888000,A
"""

ROUTES = """route_id,agency_id,route_short_name,route_long_name,route_type,route_desc
R2,A1,2,,700,Stadsbuss
R9,A1,9,,700,Skolbuss
RT,A1,Mälartåg,,100,Mälartåg
"""

TRIPS = """route_id,service_id,trip_id,trip_headsign,trip_short_name,direction_id,shape_id,x
R2,1,T_INBOUND,,,0,S2,1
R2,1,T_PASSED,,,0,S2,1
R9,1,T_SCHOOL,,,0,S9,1
R2,1,T_OTHER,,,0,S2,1
R2,1,T_REVERSE,,,1,S2,1
R2,1,T_TERMINATES,,,0,S2,1
RT,1,T_TRAIN,,,0,S2,1
R2,1,T_LOOP,,,0,S2,1
"""

# T_OTHER never calls at my stop, so it must not appear anywhere. Every other
# trip carries on past my stop to Terminus: one that ends at my stop is an
# arrival, not a departure, and is deliberately not listed - see T_TERMINATES.
STOP_TIMES = """trip_id,arrival_time,departure_time,stop_id,stop_sequence
T_INBOUND,12:00:00,12:00:00,9022003700141001,1
T_INBOUND,12:05:00,12:05:00,9022003700999001,2
T_INBOUND,12:10:00,12:10:00,9022003700600001,3
T_INBOUND,12:25:00,12:25:00,9022003700888001,4
T_PASSED,11:00:00,11:00:00,9022003700141001,1
T_PASSED,11:10:00,11:10:00,9022003700600001,2
T_PASSED,11:25:00,11:25:00,9022003700888001,3
T_SCHOOL,12:00:00,12:00:00,9022003700141001,1
T_SCHOOL,12:20:00,12:20:00,9022003700600002,2
T_SCHOOL,12:35:00,12:35:00,9022003700888001,3
T_OTHER,12:00:00,12:00:00,9022003700141001,1
T_OTHER,12:30:00,12:30:00,9022003700999001,2
T_REVERSE,12:00:00,12:00:00,9022003700600001,1
T_REVERSE,12:15:00,12:15:00,9022003700141001,2
T_TERMINATES,12:00:00,12:00:00,9022003700141001,1
T_TERMINATES,12:12:00,12:12:00,9022003700600001,2
T_TRAIN,12:02:00,12:02:00,9022003700141001,1
T_TRAIN,12:18:00,12:18:00,9022003700600001,2
T_TRAIN,12:40:00,12:40:00,9022003700888001,3
T_LOOP,12:04:00,12:04:00,9022003700600001,1
T_LOOP,12:20:00,12:20:00,9022003700141001,2
T_LOOP,12:34:00,12:34:00,9022003700600002,3
"""

SHAPES = """shape_id,shape_pt_lat,shape_pt_lon,shape_pt_sequence
S2,59.870,17.670,2
S2,59.876,17.676,1
S2,59.858,17.646,3
S9,59.880,17.680,1
"""


def _zip() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        zf.writestr("stops.txt", STOPS)
        zf.writestr("routes.txt", ROUTES)
        zf.writestr("trips.txt", TRIPS)
        zf.writestr("stop_times.txt", STOP_TIMES)
        zf.writestr("shapes.txt", SHAPES)
    return buffer.getvalue()


@pytest.fixture
def index() -> GTFSIndex:
    return build_index(_zip(), [MY_UL_ID])


# --- A feed with a calendar, for the whole-day timetable ---
STOCKHOLM = ZoneInfo("Europe/Stockholm")

AGENCY = """agency_id,agency_name,agency_url,agency_timezone
A1,UL,https://ul.se,Europe/Stockholm
"""


def _clock(now: float, offset_minutes: float) -> str:
    """A GTFS clock time this many minutes from ``now``, in agency-local time.

    Hours run past 24 rather than wrapping: in GTFS a trip leaving at half past
    midnight belongs to the previous day's service, and pretending otherwise
    makes the tests pass only in the afternoon.
    """
    date = datetime.fromtimestamp(now, STOCKHOLM).date()
    midnight = datetime.combine(date, dtime(12), STOCKHOLM).timestamp() - 12 * 3600
    seconds = int(now + offset_minutes * 60 - midnight)
    return f"{seconds // 3600:02d}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"


def _timetable_zip(now: float, service_dates: str | None = None) -> bytes:
    """The same stops, with three line-2 trips timed against ``now``."""

    def at(offset: float) -> str:
        return _clock(now, offset)

    stop_times = (
        "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
        # Sets off in two minutes, due at my stop in ten.
        f"T_SOON,{at(2)},{at(2)},9022003700141001,1\n"
        f"T_SOON,{at(10)},{at(10)},9022003700600001,2\n"
        f"T_SOON,{at(25)},{at(25)},9022003700888001,3\n"
        # Left twenty minutes ago; still nothing about it in the realtime feed.
        f"T_RUNNING,{at(-20)},{at(-20)},9022003700141001,1\n"
        f"T_RUNNING,{at(15)},{at(15)},9022003700600001,2\n"
        f"T_RUNNING,{at(30)},{at(30)},9022003700888001,3\n"
        f"T_LATER,{at(60)},{at(60)},9022003700141001,1\n"
        f"T_LATER,{at(75)},{at(75)},9022003700600001,2\n"
        f"T_LATER,{at(90)},{at(90)},9022003700888001,3\n"
        # A school bus, to prove the kind filter reaches the timetable too.
        f"T_SCHOOL_LATER,{at(90)},{at(90)},9022003700141001,1\n"
        f"T_SCHOOL_LATER,{at(100)},{at(100)},9022003700600002,2\n"
        f"T_SCHOOL_LATER,{at(115)},{at(115)},9022003700888001,3\n"
        # Ends at my stop, so it is an arrival rather than a departure.
        f"T_ENDS_HERE,{at(20)},{at(20)},9022003700141001,1\n"
        f"T_ENDS_HERE,{at(35)},{at(35)},9022003700600001,2\n"
    )
    trips = (
        "route_id,service_id,trip_id,trip_headsign,direction_id,shape_id\n"
        "R2,S1,T_SOON,,0,S2\n"
        "R2,S1,T_RUNNING,,0,S2\n"
        "R2,S1,T_LATER,,0,S2\n"
        "R9,S1,T_SCHOOL_LATER,,0,S9\n"
        "R2,S1,T_ENDS_HERE,,0,S2\n"
    )
    today = datetime.fromtimestamp(now, STOCKHOLM).date()
    calendar = (
        "service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,"
        "start_date,end_date\n"
        f"S1,1,1,1,1,1,1,1,{(today - timedelta(days=2)):%Y%m%d},"
        f"{(today + timedelta(days=2)):%Y%m%d}\n"
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        zf.writestr("agency.txt", AGENCY)
        zf.writestr("stops.txt", STOPS)
        zf.writestr("routes.txt", ROUTES)
        zf.writestr("trips.txt", trips)
        zf.writestr("stop_times.txt", stop_times)
        zf.writestr("shapes.txt", SHAPES)
        zf.writestr("calendar.txt", calendar)
        if service_dates is not None:
            zf.writestr("calendar_dates.txt", service_dates)
    return buffer.getvalue()


@pytest.fixture
def timetabled() -> tuple[GTFSIndex, float]:
    now = time.time()
    return build_index(_timetable_zip(now), [MY_UL_ID]), now


@pytest.fixture
def timetabled_no_service() -> tuple[GTFSIndex, float]:
    """Today cancelled by an exception - a public holiday, say."""
    now = time.time()
    today = datetime.fromtimestamp(now, STOCKHOLM).date()
    removals = "service_id,date,exception_type\n" + "".join(
        f"S1,{(today + timedelta(days=offset)):%Y%m%d},2\n" for offset in (-1, 0, 1)
    )
    return build_index(_timetable_zip(now, removals), [MY_UL_ID]), now


def _positions(*trips: tuple[str, float, float]) -> gtfs_realtime_pb2.FeedMessage:
    feed = gtfs_realtime_pb2.FeedMessage()
    feed.header.gtfs_realtime_version = "2.0"
    for trip_id, lat, lon in trips:
        entity = feed.entity.add()
        entity.id = trip_id
        entity.vehicle.trip.trip_id = trip_id
        entity.vehicle.vehicle.id = f"veh_{trip_id}"
        entity.vehicle.position.latitude = lat
        entity.vehicle.position.longitude = lon
        entity.vehicle.position.bearing = 180
        entity.vehicle.position.speed = 10.0
    return feed


def _updates(spec: dict[str, list[tuple[int, float, int]]]):
    """Build a TripUpdates feed from trip_id -> [(stop_sequence, epoch, delay)]."""
    feed = gtfs_realtime_pb2.FeedMessage()
    feed.header.gtfs_realtime_version = "2.0"
    for trip_id, calls in spec.items():
        entity = feed.entity.add()
        entity.id = trip_id
        entity.trip_update.trip.trip_id = trip_id
        for seq, when, delay in calls:
            update = entity.trip_update.stop_time_update.add()
            update.stop_sequence = seq
            update.arrival.time = int(when)
            update.arrival.delay = delay
    return feed


class TestStopIdMapping:
    def test_embeds_ul_id(self):
        # The whole map hinges on this: UL's ids sit inside the national ids.
        assert ul_stop_to_gtfs_area(3700600) == "9021003700600000"
        assert ul_stop_to_gtfs_area("3700141") == "9021003700141000"


class TestBuildIndex:
    def test_keeps_only_trips_calling_at_configured_stops(self, index):
        assert set(index.trips) == {
            "T_INBOUND",
            "T_PASSED",
            "T_SCHOOL",
            "T_REVERSE",
            "T_TERMINATES",
            "T_TRAIN",
            "T_LOOP",
        }

    def test_resolves_line_and_kind(self, index):
        assert index.trips["T_INBOUND"]["line"] == "2"
        assert index.trips["T_SCHOOL"]["kind"] == "Skolbuss"

    def test_maps_platforms_to_area(self, index):
        assert index.platforms[MY_AREA] == {
            "9022003700600001",
            "9022003700600002",
        }

    def test_finds_my_sequence_via_any_platform(self, index):
        assert index.trip_seq_at("T_INBOUND", MY_AREA) == 3
        # T_SCHOOL calls at platform B, not A.
        assert index.trip_seq_at("T_SCHOOL", MY_AREA) == 2

    def test_unknown_trip_has_no_sequence(self, index):
        assert index.trip_seq_at("T_MISSING", MY_AREA) is None

    def test_platform_code_per_trip(self, index):
        assert index.trip_platform("T_INBOUND", MY_AREA) == "A"
        assert index.trip_platform("T_SCHOOL", MY_AREA) == "B"
        assert index.trip_platform("T_MISSING", MY_AREA) == ""

    def test_nearest_call_to_a_position(self, index):
        # Sitting on top of Granbystaden, which T_INBOUND calls at first.
        assert index.seq_near("T_INBOUND", 59.8755, 17.6756) == 1
        # And by my stop, two calls later.
        assert index.seq_near("T_INBOUND", 59.8581, 17.6461) == 3

    def test_shapes_sorted_by_point_sequence(self, index):
        # Source rows are deliberately out of order.
        assert index.shapes["S2"] == [
            (59.876, 17.676),
            (59.870, 17.670),
            (59.858, 17.646),
        ]

    def test_stop_names_resolved(self, index):
        assert index.stops["9022003700141001"][0] == "Granbystaden"

    def test_empty_when_no_stops_match(self):
        assert build_index(_zip(), [9999999]).trips == {}


class TestStopsAway:
    def test_counts_remaining_calls(self):
        now = 1000.0
        calls = {1: (500, 0), 2: (1100, 0), 3: (1200, 0)}
        assert _stops_away(calls, 3, now) == 1

    def test_zero_when_mine_is_next(self):
        calls = {2: (1100, 0), 3: (1200, 0)}
        assert _stops_away(calls, 2, 1000.0) == 0

    def test_none_when_nothing_upcoming(self):
        assert _stops_away({1: (500, 0)}, 3, 1000.0) is None


class TestPredictions:
    def test_falls_back_to_departure(self):
        feed = gtfs_realtime_pb2.FeedMessage()
        entity = feed.entity.add()
        entity.trip_update.trip.trip_id = "T"
        update = entity.trip_update.stop_time_update.add()
        update.stop_sequence = 4
        update.departure.time = 1234
        update.departure.delay = 60
        assert _predictions(feed)["T"] == {4: (1234, 60)}

    def test_skips_entries_without_a_time(self):
        feed = gtfs_realtime_pb2.FeedMessage()
        entity = feed.entity.add()
        entity.trip_update.trip.trip_id = "T"
        entity.trip_update.stop_time_update.add().stop_sequence = 4
        assert "T" not in _predictions(feed)


class TestOverview:
    def _run(self, index, now, kinds=None):
        positions = _positions(
            ("T_INBOUND", 59.870, 17.670),
            ("T_PASSED", 59.860, 17.650),
            ("T_SCHOOL", 59.874, 17.674),
            ("T_OTHER", 59.900, 17.700),
        )
        updates = _updates(
            {
                "T_INBOUND": [(2, now + 120, -30), (3, now + 420, -30)],
                "T_PASSED": [(2, now - 300, 0)],
                "T_SCHOOL": [(1, now + 60, 0), (2, now + 600, 120)],
            }
        )
        return overview(index, positions, updates, MY_UL_ID, kinds=kinds, now=now)

    def test_shows_inbound_vehicle_with_eta(self, index):
        now = time.time()
        result = self._run(index, now)
        assert [v["trip_id"] for v in result["vehicles"]] == ["T_INBOUND"]
        vehicle = result["vehicles"][0]
        assert vehicle["line"] == "2"
        assert vehicle["eta_minutes"] == 7.0
        assert vehicle["delay"] == -30
        assert vehicle["stops_away"] == 1

    def test_reports_how_stale_the_data_is(self, index):
        # The card adds its own time-since-fetch to these, so both are ages
        # measured on this side of the clock rather than absolute timestamps.
        # Whole seconds: the feed's timestamps are integers, and a fractional
        # `now` would leave the ages a coin flip either side of the bound.
        now = float(int(time.time()))
        positions = _positions(("T_INBOUND", 59.870, 17.670))
        positions.header.timestamp = int(now) - 4
        positions.entity[0].vehicle.timestamp = int(now) - 9
        updates = _updates({"T_INBOUND": [(3, now + 420, 0)]})
        result = overview(index, positions, updates, MY_UL_ID, now=now)
        assert 3.5 <= result["data_age"] <= 4.5
        assert 8.5 <= result["vehicles"][0]["age"] <= 9.5

    def test_freshness_absent_when_the_feed_omits_timestamps(self, index):
        result = self._run(index, time.time())
        assert result["data_age"] == 0.0
        assert result["vehicles"][0]["age"] is None

    def test_excludes_vehicle_that_already_passed(self, index):
        assert all(
            v["trip_id"] != "T_PASSED"
            for v in self._run(index, time.time())["vehicles"]
        )

    def test_excludes_trips_that_never_call_at_my_stop(self, index):
        assert all(
            v["trip_id"] != "T_OTHER" for v in self._run(index, time.time())["vehicles"]
        )

    def test_school_buses_filtered_by_default(self, index):
        assert all(
            v["kind"] != "Skolbuss" for v in self._run(index, time.time())["vehicles"]
        )

    def test_school_buses_included_when_requested(self, index):
        result = self._run(index, time.time(), kinds=["Stadsbuss", "Skolbuss"])
        assert {v["trip_id"] for v in result["vehicles"]} == {"T_INBOUND", "T_SCHOOL"}

    def test_trains_are_listed_by_default(self, index):
        """Only school buses are hidden: the line picker offers the trains."""
        now = time.time()
        result = overview(
            index,
            _positions(("T_TRAIN", 59.87, 17.67)),
            _updates({"T_TRAIN": [(2, now + 300, 0)]}),
            MY_UL_ID,
            now=now,
        )
        assert [v["line"] for v in result["vehicles"]] == ["Mälartåg"]

    def test_trip_ending_at_my_stop_is_not_a_departure(self, index):
        """Nobody boards a bus whose last call is the stop you are standing at."""
        now = time.time()
        result = overview(
            index,
            _positions(("T_TERMINATES", 59.87, 17.67)),
            _updates({"T_TERMINATES": [(2, now + 300, 0)]}),
            MY_UL_ID,
            now=now,
        )
        assert result["vehicles"] == []

    def test_a_loop_back_to_my_stop_is_still_a_departure(self, index):
        """It ends here, but it also starts here - you board it on the way out."""
        now = time.time()
        result = overview(
            index,
            _positions(("T_LOOP", 59.858, 17.646)),
            _updates({"T_LOOP": [(1, now + 300, 0)]}),
            MY_UL_ID,
            now=now,
        )
        assert [v["trip_id"] for v in result["vehicles"]] == ["T_LOOP"]

    def test_destination_falls_back_to_last_call(self, index):
        # UL leaves trip_headsign empty, so this must come from stop_times.
        assert self._run(index, time.time())["vehicles"][0]["destination"] == (
            "Terminus"
        )

    def test_line_filter(self, index):
        now = time.time()
        positions = _positions(
            ("T_INBOUND", 59.87, 17.67), ("T_SCHOOL", 59.874, 17.674)
        )
        updates = _updates(
            {"T_INBOUND": [(3, now + 300, 0)], "T_SCHOOL": [(2, now + 300, 0)]}
        )
        both = ["Stadsbuss", "Skolbuss"]
        filtered = overview(
            index, positions, updates, MY_UL_ID, kinds=both, lines=["9"], now=now
        )
        assert {v["line"] for v in filtered["vehicles"]} == {"9"}
        unfiltered = overview(index, positions, updates, MY_UL_ID, kinds=both, now=now)
        assert len(unfiltered["vehicles"]) == 2

    def test_destination_filter(self, index):
        """Where it goes, not which number it wears - see wanted_destinations."""
        now = time.time()
        positions = _positions(("T_INBOUND", 59.87, 17.67), ("T_REVERSE", 59.86, 17.65))
        updates = _updates(
            {"T_INBOUND": [(3, now + 300, 0)], "T_REVERSE": [(1, now + 600, 0)]}
        )
        rows = overview(
            index, positions, updates, MY_UL_ID, destinations=["Granbystaden"], now=now
        )["vehicles"]
        # Same line, same stop, opposite ways: only one of them is wanted.
        assert [v["trip_id"] for v in rows] == ["T_REVERSE"]

    def test_destination_filter_reaches_the_timetable_too(self, timetabled):
        index, now = timetabled
        rows = overview(
            index,
            _positions(),
            _updates({}),
            MY_UL_ID,
            destinations=["Nowhere at all"],
            list_minutes=120,
            now=now,
        )["vehicles"]
        assert rows == []

    def test_platform_is_sent_with_every_row(self, index):
        now = time.time()
        rows = overview(
            index,
            _positions(("T_INBOUND", 59.87, 17.67)),
            _updates({"T_INBOUND": [(3, now + 300, 0)]}),
            MY_UL_ID,
            now=now,
        )["vehicles"]
        assert rows[0]["track"] == "A"

    def test_horizon_keeps_distant_arrivals_off_the_map(self, index):
        # An hour out: worth reading in the list, not worth stretching the
        # viewport for.
        now = time.time()
        positions = _positions(("T_INBOUND", 59.87, 17.67))
        updates = _updates({"T_INBOUND": [(3, now + 3600, 0)]})
        listed = overview(index, positions, updates, MY_UL_ID, now=now)
        assert [v["on_map"] for v in listed["vehicles"]] == [False]
        # ...and its approach path is not plotted either, only my own stop.
        assert {s["id"] for s in listed["stops"]} == {MY_AREA}
        kept = overview(
            index, positions, updates, MY_UL_ID, horizon_minutes=120, now=now
        )
        assert [v["on_map"] for v in kept["vehicles"]] == [True]

    def test_horizon_is_sent_so_the_card_can_dim_by_time(self, index):
        """The card greys the rows the map does not reach, and only those.

        It cannot work that out from ``on_map``: at a terminus every departure
        starts there, has no vehicle yet, and would grey the whole board.
        """
        now = time.time()
        result = overview(
            index,
            _positions(("T_INBOUND", 59.87, 17.67)),
            _updates({"T_INBOUND": [(3, now + 300, 0)]}),
            MY_UL_ID,
            horizon_minutes=25,
            now=now,
        )
        assert result["horizon"] == 25

    def test_list_horizon_drops_the_truly_distant(self, index):
        now = time.time()
        positions = _positions(("T_INBOUND", 59.87, 17.67))
        updates = _updates({"T_INBOUND": [(3, now + 3600, 0)]})
        assert (
            overview(index, positions, updates, MY_UL_ID, list_minutes=20, now=now)[
                "vehicles"
            ]
            == []
        )

    def test_stops_cover_approach_path_only(self, index):
        # The bus is one stop out, so the map should show that stop and mine -
        # not everything the trip serves after my stop.
        now = time.time()
        positions = _positions(("T_INBOUND", 59.87, 17.67))
        updates = _updates({"T_INBOUND": [(2, now + 120, 0), (3, now + 420, 0)]})
        result = overview(index, positions, updates, MY_UL_ID, now=now)
        assert {s["id"] for s in result["stops"]} == {
            "9022003700999001",  # the call still ahead of the bus
            MY_AREA,  # my stop, as the parent station
        }

    def test_my_stop_is_one_marker_not_one_per_platform(self, index):
        """Platforms sit metres apart and stack into a blob if each is drawn."""
        now = time.time()
        result = self._run(index, now)
        mine = [s for s in result["stops"] if s["mine"]]
        assert len(mine) == 1
        assert mine[0]["id"] == MY_AREA
        # The individual platforms must not also appear.
        ids = {s["id"] for s in result["stops"]}
        assert not ids & {"9022003700600001", "9022003700600002"}

    def test_marks_my_stop(self, index):
        stops = self._run(index, time.time())["stops"]
        assert any(s["mine"] for s in stops)
        assert all(s["lat"] and s["lon"] for s in stops)

    def test_line_colour_included(self, index):
        vehicle = self._run(index, time.time())["vehicles"][0]
        assert vehicle["color"] == "#af1e14"  # line 2 is red
        assert vehicle["text_color"] == "#ffffff"

    def test_vehicle_without_trip_is_listed_but_not_plotted(self, index):
        # An unassigned position says nothing about where that bus is going, so
        # the arrival stays in the list on the strength of its prediction alone.
        now = time.time()
        positions = _positions(("T_INBOUND", 59.87, 17.67))
        positions.entity[0].vehicle.trip.trip_id = ""
        updates = _updates({"T_INBOUND": [(3, now + 300, 0)]})
        vehicle = overview(index, positions, updates, MY_UL_ID, now=now)["vehicles"][0]
        assert vehicle["on_map"] is False
        assert "lat" not in vehicle

    def test_vehicle_without_prediction_is_skipped(self, index):
        now = time.time()
        positions = _positions(("T_INBOUND", 59.87, 17.67))
        assert (
            overview(index, positions, _updates({}), MY_UL_ID, now=now)["vehicles"]
            == []
        )

    def test_sorted_by_arrival(self, index):
        now = time.time()
        positions = _positions(
            ("T_INBOUND", 59.87, 17.67), ("T_SCHOOL", 59.874, 17.674)
        )
        updates = _updates(
            {
                "T_INBOUND": [(3, now + 600, 0)],
                "T_SCHOOL": [(2, now + 120, 0)],
            }
        )
        result = overview(
            index,
            positions,
            updates,
            MY_UL_ID,
            kinds=["Stadsbuss", "Skolbuss"],
            now=now,
        )
        assert [v["trip_id"] for v in result["vehicles"]] == ["T_SCHOOL", "T_INBOUND"]


class TestDirectionNames:
    """UL names directions by place ("Norby Gottsunda"), GTFS by last stop."""

    # T_INBOUND (dir 0) ends at Uppsala Centralstation.
    # T_REVERSE (dir 1) ends at Granbystaden.
    def test_resolves_both_directions_by_place_name(self, index):
        names = direction_names(index, [("2", "Centralstation"), ("2", "Granbystaden")])
        assert names[("2", "0")] == "Centralstation"
        assert names[("2", "1")] == "Granbystaden"

    def test_resolves_the_last_one_by_elimination(self, index):
        # "Gamla Uppsala" matches nothing in the feed's stop names - exactly the
        # real line 2 case - but it is the only name left once dir 1 is taken.
        names = direction_names(index, [("2", "Granbystaden"), ("2", "Gamla Uppsala")])
        assert names[("2", "1")] == "Granbystaden"
        assert names[("2", "0")] == "Gamla Uppsala"

    def test_no_guess_when_nothing_matches(self, index):
        # One unmatchable name and two directions: elimination cannot decide.
        assert direction_names(index, [("2", "Totally Elsewhere")]) == {}

    def test_ignores_tokens_common_to_every_stop(self, index):
        # "Uppsala" prefixes every stop name, so it must not decide anything.
        assert direction_names(index, [("2", "Uppsala")]) == {}

    def test_applied_to_vehicles(self, index):
        now = time.time()
        result = overview(
            index,
            _positions(("T_INBOUND", 59.87, 17.67)),
            _updates({"T_INBOUND": [(3, now + 300, 0)]}),
            MY_UL_ID,
            directions=[("2", "Centralstation"), ("2", "Granbystaden")],
            now=now,
        )
        assert result["vehicles"][0]["destination"] == "Centralstation"

    def test_falls_back_to_terminus_without_ul_names(self, index):
        now = time.time()
        result = overview(
            index,
            _positions(("T_INBOUND", 59.87, 17.67)),
            _updates({"T_INBOUND": [(3, now + 300, 0)]}),
            MY_UL_ID,
            now=now,
        )
        assert result["vehicles"][0]["destination"] == "Terminus"

    def test_labels_vehicles_with_no_prediction_of_their_own(self, index):
        """Why this is resolved per direction and not per vehicle."""
        now = time.time()
        result = line_view(
            index,
            _positions(("T_INBOUND", 59.87, 17.67), ("T_PASSED", 59.86, 17.65)),
            _updates({}),  # no predictions at all
            "2",
            MY_UL_ID,
            directions=[("2", "Centralstation"), ("2", "Granbystaden")],
            now=now,
        )
        assert {v["destination"] for v in result["vehicles"]} == {"Centralstation"}

    def test_opposite_directions_never_share_a_name(self, index):
        names = direction_names(index, [("2", "Centralstation"), ("2", "Granbystaden")])
        assert names[("2", "0")] != names[("2", "1")]


class TestULDirections:
    def test_extracts_distinct_line_and_towards(self):
        assert ul_directions(
            {"2_Gamla Uppsala": [{}, {}], "2_Håga": [{}], "7_Norby Gottsunda": [{}]}
        ) == [("2", "Gamla Uppsala"), ("2", "Håga"), ("7", "Norby Gottsunda")]

    def test_tolerates_empty_and_malformed(self):
        assert ul_directions(None) == []
        assert ul_directions({"nounderscore": [{}]}) == []


class TestNameCacheAccumulates:
    """UL only advertises directions that have upcoming departures."""

    def test_second_refresh_completes_the_pairing(self, index):
        cache = {}
        # Late evening: only one direction still running.
        first = direction_names(index, [("2", "Granbystaden")], cache)
        assert first.get(("2", "0")) is None  # cannot eliminate yet

        # Next refresh brings the other one into view.
        second = direction_names(index, [("2", "Gamla Uppsala")], cache)
        assert second[("2", "1")] == "Granbystaden"
        assert second[("2", "0")] == "Gamla Uppsala"

    def test_without_cache_the_second_refresh_forgets(self, index):
        assert direction_names(index, [("2", "Gamla Uppsala")]) == {}


class TestLineView:
    def test_returns_shape_and_vehicles(self, index):
        now = time.time()
        positions = _positions(("T_INBOUND", 59.870, 17.670))
        updates = _updates({"T_INBOUND": [(3, now + 300, 0)]})
        result = line_view(index, positions, updates, "2", MY_UL_ID, now=now)
        assert [v["trip_id"] for v in result["vehicles"]] == ["T_INBOUND"]
        assert result["shape"][0] == {"lat": 59.876, "lon": 17.676}

    def test_excludes_other_lines(self, index):
        now = time.time()
        positions = _positions(("T_SCHOOL", 59.874, 17.674))
        result = line_view(index, positions, _updates({}), "2", MY_UL_ID, now=now)
        assert result["vehicles"] == []

    def test_keeps_passed_vehicles_but_flags_them(self, index):
        now = time.time()
        # Away towards Terminus: due five minutes ago and demonstrably gone.
        positions = _positions(("T_PASSED", 59.905, 17.705))
        updates = _updates({"T_PASSED": [(2, now - 300, 0)]})
        result = line_view(index, positions, updates, "2", MY_UL_ID, now=now)
        assert result["vehicles"][0]["passed"] is True

    def test_a_late_bus_short_of_the_stop_has_not_passed(self, index):
        now = time.time()
        positions = _positions(("T_PASSED", 59.8755, 17.6756))
        updates = _updates({"T_PASSED": [(2, now - 300, 0)]})
        result = line_view(index, positions, updates, "2", MY_UL_ID, now=now)
        assert result["vehicles"][0]["passed"] is False

    def test_uses_clicked_trip_shape(self, index):
        now = time.time()
        result = line_view(
            index,
            _positions(),
            _updates({}),
            "2",
            MY_UL_ID,
            trip_id="T_INBOUND",
            now=now,
        )
        assert len(result["shape"]) == 3

    def test_falls_back_to_the_timetable_when_nothing_is_predicting(self, timetabled):
        """Line 7 towards Årsta Fyrislund: three buses running, no trip updates.

        A dash for the departure time of a bus you can watch move across the
        map is the least useful thing the row could say.
        """
        index, now = timetabled
        # Parked on its first call, one stop short of mine.
        result = line_view(
            index,
            _positions(("T_RUNNING", 59.8755, 17.6756)),
            _updates({}),
            "2",
            MY_UL_ID,
            now=now,
        )
        row = result["vehicles"][0]
        assert row["eta"] == pytest.approx(now + 15 * 60, abs=60)
        assert row["scheduled"] is True
        assert row["delay"] is None  # timetabled, so nothing to be late against
        assert row["stops_away"] == 1
        assert row["track"] == "A"

    def test_no_invented_time_for_a_bus_that_does_not_call_here(self, index):
        now = time.time()
        result = line_view(
            index,
            _positions(("T_OTHER", 59.87, 17.67)),
            _updates({}),
            "2",
            MY_UL_ID,
            now=now,
        )
        assert all("eta" not in v for v in result["vehicles"])

    def test_unknown_line_is_empty_not_an_error(self, index):
        result = line_view(index, _positions(), _updates({}), "999", MY_UL_ID)
        assert result["vehicles"] == []
        assert result["shape"] == []


class TestLinger:
    """A bus that has just called is kept briefly so you see it pull away."""

    # Past my stop and on its way to Terminus: the flag follows the vehicle, so
    # a bus that has genuinely pulled away has to be somewhere else by now.
    GONE = (59.911, 17.711)

    def _at(self, index, offset, where=GONE):
        now = time.time()
        positions = _positions(("T_INBOUND", *where))
        updates = _updates({"T_INBOUND": [(3, now + offset, 0)]})
        return overview(index, positions, updates, MY_UL_ID, now=now)["vehicles"]

    def test_still_on_the_map_just_after_arriving(self, index):
        vehicle = self._at(index, -20)[0]
        assert vehicle["departed"] is True
        assert vehicle["on_map"] is True

    def test_overdue_at_the_kerb_has_not_departed(self, index):
        """Overdue is not gone.

        The prediction runs out while the bus is still standing there - doors
        open, a queue boarding - and "departed" on the row above your head is
        the one thing that would make you stop running for it.
        """
        vehicle = self._at(index, -20, where=(59.8581, 17.6461))[0]
        assert vehicle["departed"] is False

    def test_gone_once_the_linger_expires(self, index):
        assert self._at(index, -120) == []

    def test_how_long_is_configurable(self, index):
        now = time.time()
        positions = _positions(("T_INBOUND", *self.GONE))
        updates = _updates({"T_INBOUND": [(3, now - 20, 0)]})
        kept = overview(
            index, positions, updates, MY_UL_ID, linger_seconds=120, now=now
        )
        assert kept["vehicles"][0]["departed"] is True
        # Zero: off the map the moment it is due away.
        gone = overview(index, positions, updates, MY_UL_ID, linger_seconds=0, now=now)
        assert gone["vehicles"] == []


class TestNextStop:
    def test_names_the_call_it_is_heading_for(self, index):
        now = time.time()
        positions = _positions(("T_INBOUND", 59.870, 17.670))
        updates = _updates({"T_INBOUND": [(2, now + 60, 0), (3, now + 420, 0)]})
        vehicle = overview(index, positions, updates, MY_UL_ID, now=now)["vehicles"][0]
        assert vehicle["next_stop"] == "Elsewhere"
        assert vehicle["next_stop_eta"] == int(now + 60)

    def test_absent_when_nothing_is_still_ahead(self, index):
        now = time.time()
        positions = _positions(("T_INBOUND", 59.858, 17.647))
        updates = _updates({"T_INBOUND": [(3, now - 10, 0)]})
        vehicle = overview(index, positions, updates, MY_UL_ID, now=now)["vehicles"][0]
        assert vehicle["next_stop"] is None


class TestPathAhead:
    def test_follows_the_route_from_where_the_bus_is(self, index):
        # The bus sits on the middle shape point, so the road ahead is the run
        # down to the terminus - not the point behind it.
        assert index.path_ahead("T_INBOUND", 59.870, 17.670, 600) == [
            (59.870, 17.670),
            (59.858, 17.646),
        ]

    def test_empty_when_the_bus_is_nowhere_near_its_shape(self, index):
        assert index.path_ahead("T_INBOUND", 59.900, 17.900, 600) == []

    def test_sent_only_with_plotted_vehicles(self, index):
        now = time.time()
        positions = _positions(("T_INBOUND", 59.870, 17.670))
        updates = _updates({"T_INBOUND": [(3, now + 420, 0)]})
        result = overview(index, positions, updates, MY_UL_ID, now=now)
        # Starts at the bus, ends at the next shape point down the line.
        path = result["vehicles"][0]["path"]
        assert len(path) == 2
        assert path[1] == [59.858, 17.646]


class TestTimetable:
    """The realtime feeds only know about buses already running.

    Everything past that comes from the static feed, which holds the whole day.
    """

    def test_fills_the_list_past_where_the_feed_runs_dry(self, timetabled):
        index, now = timetabled
        positions = _positions(("T_SOON", 59.870, 17.670))
        updates = _updates({"T_SOON": [(2, now + 600, 0)]})
        rows = overview(index, positions, updates, MY_UL_ID, list_minutes=120, now=now)[
            "vehicles"
        ]
        assert [v["trip_id"] for v in rows] == ["T_SOON", "T_RUNNING", "T_LATER"]
        assert [v.get("scheduled", False) for v in rows] == [False, True, True]

    def test_service_ending_at_my_stop_is_not_listed(self, timetabled):
        index, now = timetabled
        rows = overview(
            index, _positions(), _updates({}), MY_UL_ID, list_minutes=120, now=now
        )["vehicles"]
        assert all(v["trip_id"] != "T_ENDS_HERE" for v in rows)

    def test_scheduled_rows_are_not_plotted(self, timetabled):
        index, now = timetabled
        rows = overview(index, _positions(), _updates({}), MY_UL_ID, now=now)[
            "vehicles"
        ]
        assert all(v["on_map"] is False for v in rows)
        assert all("lat" not in v for v in rows)

    def test_bus_already_running_is_not_listed_twice(self, timetabled):
        """Matched on trip id, so a delayed bus cannot slip past as a duplicate."""
        index, now = timetabled
        positions = _positions(("T_SOON", 59.870, 17.670))
        # Running eleven minutes late: no time-based match would find this.
        updates = _updates({"T_SOON": [(2, now + 1260, 660)]})
        rows = overview(index, positions, updates, MY_UL_ID, now=now)["vehicles"]
        soon = [v for v in rows if v["trip_id"] == "T_SOON"]
        assert len(soon) == 1
        assert "scheduled" not in soon[0]  # the live one won, not the timetable
        assert soon[0]["delay"] == 660

    def test_a_running_bus_with_no_prediction_is_plotted_not_called_missing(
        self, timetabled
    ):
        """The other half of the "not yet running" bug: position, no trip update."""
        index, now = timetabled
        positions = _positions(("T_RUNNING", 59.870, 17.670))
        row = next(
            v
            for v in overview(index, positions, _updates({}), MY_UL_ID, now=now)[
                "vehicles"
            ]
            if v["trip_id"] == "T_RUNNING"
        )
        assert row["on_map"] is True
        assert row["live"] is True
        assert row["started"] is True
        assert row["lat"] == pytest.approx(59.87)  # protobuf floats
        assert row["path"]

    def test_a_running_bus_says_how_far_off_it_is(self, timetabled):
        """Its position is the only evidence there is, so use it."""
        index, now = timetabled
        # On its first call; mine is the next one.
        positions = _positions(("T_RUNNING", 59.8755, 17.6756))
        row = next(
            v
            for v in overview(index, positions, _updates({}), MY_UL_ID, now=now)[
                "vehicles"
            ]
            if v["trip_id"] == "T_RUNNING"
        )
        assert row["stops_away"] == 1

    def test_says_whether_the_bus_should_be_out_there_yet(self, timetabled):
        # T_RUNNING left its first stop twenty minutes ago and is still not in
        # the realtime feed; T_LATER has not set off at all. Same empty map,
        # different reason.
        index, now = timetabled
        rows = {
            v["trip_id"]: v
            for v in overview(
                index, _positions(), _updates({}), MY_UL_ID, list_minutes=120, now=now
            )["vehicles"]
        }
        assert rows["T_RUNNING"]["started"] is True
        assert rows["T_LATER"]["started"] is False

    def test_beyond_the_list_window_is_dropped(self, timetabled):
        index, now = timetabled
        rows = overview(
            index, _positions(), _updates({}), MY_UL_ID, list_minutes=30, now=now
        )["vehicles"]
        assert [v["trip_id"] for v in rows] == ["T_SOON", "T_RUNNING"]

    def test_other_lines_filtered_out(self, timetabled):
        index, now = timetabled
        rows = overview(
            index, _positions(), _updates({}), MY_UL_ID, lines=["9"], now=now
        )["vehicles"]
        assert rows == []

    def test_school_buses_filtered_out(self, timetabled):
        index, now = timetabled
        rows = overview(
            index, _positions(), _updates({}), MY_UL_ID, list_minutes=180, now=now
        )["vehicles"]
        assert all(v["line"] != "9" for v in rows)

    def test_day_off_is_not_listed(self, timetabled_no_service):
        index, now = timetabled_no_service
        assert (
            overview(
                index, _positions(), _updates({}), MY_UL_ID, list_minutes=180, now=now
            )["vehicles"]
            == []
        )

    def test_agency_timezone_is_read_from_the_feed(self, timetabled):
        index, _ = timetabled
        assert index.timezone == "Europe/Stockholm"

    def test_a_stop_with_no_timetable_is_empty_not_an_error(self, timetabled):
        index, now = timetabled
        assert index.departures_at("9021003700999000", now, now + 3600) == []


class TestListLimit:
    def test_extra_rows_are_dropped_but_never_a_plotted_bus(self, timetabled):
        index, now = timetabled
        positions = _positions(("T_SOON", 59.870, 17.670))
        updates = _updates({"T_SOON": [(2, now + 600, 0)]})
        rows = overview(
            index, positions, updates, MY_UL_ID, list_minutes=120, limit=1, now=now
        )["vehicles"]
        assert [v["trip_id"] for v in rows] == ["T_SOON"]
        assert rows[0]["on_map"] is True


class TestLiveFlag:
    """Not in the realtime feed and not reporting a position are different."""

    def test_true_with_a_position(self, index):
        now = time.time()
        positions = _positions(("T_INBOUND", 59.870, 17.670))
        updates = _updates({"T_INBOUND": [(3, now + 300, 0)]})
        assert (
            overview(index, positions, updates, MY_UL_ID, now=now)["vehicles"][0][
                "live"
            ]
            is True
        )

    def test_false_when_only_a_prediction_arrives(self, index):
        now = time.time()
        positions = _positions(("T_SCHOOL", 59.874, 17.674))
        updates = _updates({"T_INBOUND": [(3, now + 300, 0)]})
        assert (
            overview(index, positions, updates, MY_UL_ID, now=now)["vehicles"][0][
                "live"
            ]
            is False
        )

    def test_unknown_when_positions_were_never_fetched(self, index):
        """A list-only card skips that feed, so it cannot say either way."""
        now = time.time()
        updates = _updates({"T_INBOUND": [(3, now + 300, 0)]})
        assert (
            overview(index, None, updates, MY_UL_ID, now=now)["vehicles"][0]["live"]
            is None
        )


class TestListOnly:
    """No map means no positions feed, which halves the upstream requests."""

    def test_arrivals_survive_without_positions(self, index):
        now = time.time()
        updates = _updates({"T_INBOUND": [(2, now + 120, 0), (3, now + 420, 0)]})
        result = overview(index, None, updates, MY_UL_ID, now=now)
        vehicle = result["vehicles"][0]
        assert vehicle["eta_minutes"] == 7.0
        assert vehicle["stops_away"] == 1
        assert vehicle["on_map"] is False
        assert "path" not in vehicle

    def test_timetabled_rows_do_not_claim_a_missing_position(self, timetabled):
        """Null, not absent: without the feed, "no live data" is our own doing."""
        index, now = timetabled
        rows = overview(index, None, _updates({}), MY_UL_ID, list_minutes=120, now=now)[
            "vehicles"
        ]
        assert rows and all(row["scheduled"] for row in rows)
        assert all(row["live"] is None for row in rows)

    def test_freshness_comes_from_the_feed_it_did_read(self, index):
        now = float(int(time.time()))
        updates = _updates({"T_INBOUND": [(3, now + 420, 0)]})
        updates.header.timestamp = int(now) - 6
        assert (
            5.5 <= overview(index, None, updates, MY_UL_ID, now=now)["data_age"] <= 6.5
        )


class TestIndexCoverage:
    def test_adding_a_stop_invalidates_the_cached_index(self, index):
        # The pickle is only rebuilt daily, so without this a stop added today
        # has no trips and the map is empty for it.
        assert index.covers([MY_UL_ID])
        assert not index.covers([MY_UL_ID, 3700141])
