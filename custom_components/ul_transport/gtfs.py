"""Static GTFS index backing the live map.

The realtime feeds carry positions but no line numbers, no stop names and no
geometry (``route_id`` is empty in both VehiclePositions and TripUpdates), so
everything human-readable has to come from the static feed. That feed is 143 MB
unpacked, which is why this module builds a small index once a day and pickles
it, rather than parsing on demand.
"""
from __future__ import annotations

import bisect
import collections
import csv
import datetime as dt
import io
import logging
import math
import re
import pickle
import time
import zipfile
from dataclasses import dataclass, field
from typing import Any, Iterator

from zoneinfo import ZoneInfo

import aiohttp
import async_timeout

from homeassistant.core import HomeAssistant

from .const import (
    GTFS_CACHE_FILE,
    GTFS_STATIC_MAX_AGE,
    GTFS_STATIC_TIMEOUT,
    GTFS_STATIC_URL,
)

_LOGGER = logging.getLogger(__name__)

# Bump when the shape of GTFSIndex changes so stale pickles are discarded.
INDEX_VERSION = 7

# A token in more than this share of stop names carries no information about
# which way a bus is going.
COMMON_TOKEN_SHARE = 0.15

METRES_PER_DEGREE = 111320


def tokenize(text: str) -> set[str]:
    """Words worth matching on: lowercase, no punctuation, no short filler."""
    return {word.lower() for word in re.split(r"\W+", text) if len(word) > 2}


def ul_stop_to_gtfs_area(ul_stop_id: int | str) -> str:
    """Map a UL stop id onto its GTFS parent-station id.

    UL's own ids are embedded verbatim in the Samtrafiken national ids::

        3700600  ->  9021003700600000
        ^^^^^^^         ^^^^^^^

    Verified against stops.txt for every type-0 (actual stop) result the UL
    search API returns; addresses and POIs have no GTFS counterpart, and the
    config flow already filters those out.
    """
    return f"902100{ul_stop_id}000"


@dataclass
class GTFSIndex:
    """Everything the map needs, reduced to the configured stops."""

    built: float = 0.0
    version: int = INDEX_VERSION
    # UL stop ids this index was reduced to. Adding a stop has to rebuild even
    # when the cache is fresh, or the new stop has no trips and shows nothing.
    stop_ids: list[str] = field(default_factory=list)
    # Timetable timezone from agency.txt. GTFS clock times are local to it, so
    # it decides what "today" and "12:05" mean.
    timezone: str = "Europe/Stockholm"
    # trip_id -> {"line", "kind", "headsign", "direction", "shape", "stops",
    #             "service", "start"}
    trips: dict[str, dict[str, Any]] = field(default_factory=dict)
    # gtfs area id -> [(seconds after midnight, trip_id)], sorted: the whole
    # day's timetable at each configured stop.
    board: dict[str, list[tuple[int, str]]] = field(default_factory=dict)
    # service_id -> the YYYYMMDD dates it runs on
    service_dates: dict[str, set[str]] = field(default_factory=dict)
    # stop_id -> (name, lat, lon)
    stops: dict[str, tuple[str, float, float]] = field(default_factory=dict)
    # gtfs area id -> platform stop_ids belonging to it
    platforms: dict[str, set[str]] = field(default_factory=dict)
    # platform stop_id -> its platform_code ("A", "B4", "2"). Only the ones at
    # configured stops are kept - it is the platform you are standing on that
    # matters, not the one the bus leaves some other town from.
    platform_codes: dict[str, str] = field(default_factory=dict)
    # gtfs area id -> (name, lat, lon). Areas are parents, so they never appear
    # in stop_times and would otherwise be pruned along with unused stops.
    areas: dict[str, tuple[str, float, float]] = field(default_factory=dict)
    # shape_id -> [(lat, lon), ...]
    shapes: dict[str, list[tuple[float, float]]] = field(default_factory=dict)
    # line short name -> shape_ids, longest first
    line_shapes: dict[str, list[str]] = field(default_factory=dict)
    # tokens too common in stop names to identify a direction ("uppsala" is in
    # 100% of them)
    common_tokens: set[str] = field(default_factory=set)

    @property
    def stale(self) -> bool:
        """Whether the index is old enough to warrant a rebuild."""
        return (
            self.version != INDEX_VERSION
            or time.time() - self.built > GTFS_STATIC_MAX_AGE
        )

    def covers(self, ul_stop_ids: list[int | str]) -> bool:
        """Whether this index was built for exactly these stops."""
        return self.stop_ids == sorted(str(s) for s in ul_stop_ids)

    def departures_at(
        self, area_id: str, start: float, end: float
    ) -> list[tuple[float, str, float]]:
        """Scheduled departures from a stop as (when, trip_id, trip_started).

        ``trip_started`` is when the trip leaves its first stop, which is how a
        listed departure can tell "no bus assigned yet" from "should be out
        there but is not reporting".

        The realtime feeds only describe buses that are already moving, so
        anything further out than about forty minutes has to come from the
        timetable. Neighbouring service dates are walked rather than just
        today's: a trip leaving at 25:10 belongs to yesterday's date, and one
        leaving at 00:10 tomorrow is what a late-evening list is asking for.
        """
        board = self.board.get(area_id)
        if not board:
            return []
        zone = ZoneInfo(self.timezone)
        today = dt.datetime.fromtimestamp(start, zone).date()

        out: list[tuple[float, str, float]] = []
        for offset in (-1, 0, 1):
            date = today + dt.timedelta(days=offset)
            # GTFS clock times are noon-minus-twelve-hours, not midnight, which
            # is the same thing on every day except the two the clocks change.
            midnight = (
                dt.datetime.combine(date, dt.time(12), zone).timestamp() - 12 * 3600
            )
            key = date.strftime("%Y%m%d")
            first = bisect.bisect_left(board, (int(start - midnight), ""))
            for seconds, trip_id in board[first:]:
                when = midnight + seconds
                if when > end:
                    break
                trip = self.trips.get(trip_id)
                if trip and key in self.service_dates.get(trip["service"], ()):
                    out.append((when, trip_id, midnight + trip["start"]))
        out.sort()
        return out

    def path_ahead(
        self, trip_id: str, lat: float, lon: float, metres: float
    ) -> list[tuple[float, float]]:
        """The next ``metres`` of this trip's route, starting beside (lat, lon).

        A bus follows the road, so extrapolating along its bearing sends it
        through the buildings on every corner. The card walks this instead.
        Direction is resolved by nearest point rather than by stop sequence:
        shape points carry no sequence link to stop_times.
        """
        trip = self.trips.get(trip_id)
        points = self.shapes.get(trip["shape"]) if trip else None
        if not points:
            return []
        scale = math.cos(math.radians(lat))
        best, start = None, 0
        for i, (plat, plon) in enumerate(points):
            gap = (plat - lat) ** 2 + ((plon - lon) * scale) ** 2
            if best is None or gap < best:
                best, start = gap, i
        # More than ~200 m off the route means this is not the shape the bus is
        # actually on (a diverted or mismatched variant); bearing is safer.
        if best is not None and math.sqrt(best) * METRES_PER_DEGREE > 200:
            return []

        out = [(lat, lon)]
        travelled = 0.0
        previous = (lat, lon)
        for point in points[start + 1 :]:
            travelled += math.dist(
                (previous[0], previous[1] * scale), (point[0], point[1] * scale)
            ) * METRES_PER_DEGREE
            out.append(point)
            previous = point
            if travelled >= metres:
                break
        return out if len(out) > 1 else []

    def terminates_at(self, trip_id: str, area_id: str) -> bool:
        """Whether the trip's last call is this stop.

        A hub is the end of the line for a lot of trips - 3451 of the 12868
        calling at Uppsala Centralstation - and they are listed as departures
        towards the stop you are already standing at. Nobody boards those.

        A trip that also *starts* here is a loop rather than an arrival, and its
        first call is a departure like any other, so it stays.
        """
        trip = self.trips.get(trip_id)
        if not trip or not trip["stops"]:
            return False
        mine = self.platforms.get(area_id, set())
        here = lambda stop_id: stop_id == area_id or stop_id in mine
        return here(trip["stops"][-1][1]) and not here(trip["stops"][0][1])

    def seq_near(self, trip_id: str, lat: float, lon: float) -> int | None:
        """Sequence of the call this vehicle is closest to.

        For the trips the realtime feed sends no predictions for - all of line
        7 towards Årsta Fyrislund, and a third of the buses on the road at any
        moment - the reported position is the only evidence of how far along
        the route they have got. Good to a stop either way: a bus between two
        calls is nearest whichever it is nearer.
        """
        trip = self.trips.get(trip_id)
        if not trip:
            return None
        scale = math.cos(math.radians(lat))
        best: tuple[float, int] | None = None
        for seq, stop_id in trip["stops"]:
            entry = self.stops.get(stop_id)
            if entry is None:
                continue
            gap = (entry[1] - lat) ** 2 + ((entry[2] - lon) * scale) ** 2
            if best is None or gap < best[0]:
                best = (gap, seq)
        return best[1] if best else None

    def trip_platform(self, trip_id: str, area_id: str) -> str:
        """The platform code this trip calls at my stop, "" when unknown."""
        trip = self.trips.get(trip_id)
        platforms = self.platforms.get(area_id) or set()
        for _, stop_id in trip["stops"] if trip else ():
            if stop_id in platforms:
                return self.platform_codes.get(stop_id, "")
        return ""

    def trip_seq_at(self, trip_id: str, area_id: str) -> int | None:
        """Return the stop_sequence at which ``trip_id`` calls at ``area_id``."""
        trip = self.trips.get(trip_id)
        if not trip:
            return None
        platforms = self.platforms.get(area_id)
        if not platforms:
            return None
        for seq, stop_id in trip["stops"]:
            if stop_id in platforms:
                return seq
        return None


def _rows(zf: zipfile.ZipFile, name: str) -> Iterator[dict[str, str]]:
    """Stream one CSV member without materialising it.

    Optional members are common in GTFS - a feed with calendar.txt often has no
    calendar_dates.txt and vice versa - so a missing one reads as empty.
    """
    try:
        handle = zf.open(name)
    except KeyError:
        return
    with handle as raw:
        yield from csv.DictReader(io.TextIOWrapper(raw, encoding="utf-8-sig"))


def _seconds(clock: str) -> int | None:
    """GTFS ``HH:MM:SS`` as seconds after midnight. Hours can exceed 24."""
    try:
        hours, minutes, secs = (int(part) for part in clock.split(":"))
    except ValueError:
        return None
    return hours * 3600 + minutes * 60 + secs


_WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")


def _service_dates(
    zf: zipfile.ZipFile, wanted: set[str]
) -> dict[str, set[str]]:
    """Which dates each service runs on, expanded from calendar + exceptions.

    Expanded once at build time so answering "is this trip running today" at
    request time is a set lookup rather than a weekday-and-range calculation.
    """
    dates: dict[str, set[str]] = {}
    for row in _rows(zf, "calendar.txt"):
        service = row["service_id"]
        if service not in wanted:
            continue
        try:
            day = dt.datetime.strptime(row["start_date"], "%Y%m%d").date()
            end = dt.datetime.strptime(row["end_date"], "%Y%m%d").date()
        except ValueError:
            continue
        runs = {i for i, name in enumerate(_WEEKDAYS) if row.get(name) == "1"}
        while day <= end:
            if day.weekday() in runs:
                dates.setdefault(service, set()).add(day.strftime("%Y%m%d"))
            day += dt.timedelta(days=1)

    for row in _rows(zf, "calendar_dates.txt"):
        service = row["service_id"]
        if service not in wanted:
            continue
        if row.get("exception_type") == "1":
            dates.setdefault(service, set()).add(row["date"])
        else:
            dates.get(service, set()).discard(row["date"])
    return dates


def build_index(raw: bytes, ul_stop_ids: list[int | str]) -> GTFSIndex:
    """Build the index from a downloaded ul.zip. Blocking - run in an executor.

    Scoped to trips that actually call at the configured stops, which is what
    keeps this tractable: the full feed has 22k trips and 1.4M stop_times rows.
    """
    started = time.monotonic()
    zf = zipfile.ZipFile(io.BytesIO(raw))
    index = GTFSIndex(
        built=time.time(), stop_ids=sorted(str(s) for s in ul_stop_ids)
    )

    for row in _rows(zf, "agency.txt"):
        if row.get("agency_timezone"):
            index.timezone = row["agency_timezone"]
            break

    areas = {ul_stop_to_gtfs_area(s) for s in ul_stop_ids}
    all_stops: dict[str, tuple[str, float, float]] = {}
    mine: set[str] = set()
    # platform stop_id -> the area it belongs to, for the timetable board
    area_of: dict[str, str] = {}

    for row in _rows(zf, "stops.txt"):
        stop_id = row["stop_id"]
        try:
            all_stops[stop_id] = (
                row["stop_name"],
                float(row["stop_lat"]),
                float(row["stop_lon"]),
            )
        except ValueError:  # a handful of feed rows carry empty coordinates
            continue
        parent = row["parent_station"]
        if stop_id in areas:
            index.platforms.setdefault(stop_id, set())
            index.areas[stop_id] = all_stops[stop_id]
            mine.add(stop_id)
            area_of[stop_id] = stop_id
        elif parent in areas:
            index.platforms.setdefault(parent, set()).add(stop_id)
            mine.add(stop_id)
            area_of[stop_id] = parent
            if row.get("platform_code"):
                index.platform_codes[stop_id] = row["platform_code"]

    if not mine:
        _LOGGER.warning("No GTFS stops matched configured stops %s", ul_stop_ids)
        return index

    # Pass 1: which trips touch our stops. Two passes over the 56 MB
    # stop_times.txt beats one pass that holds every trip in memory.
    keep: set[str] = set()
    for row in _rows(zf, "stop_times.txt"):
        if row["stop_id"] in mine:
            keep.add(row["trip_id"])

    # Pass 2: full call sequence for those trips only, plus the clock times the
    # timetable view needs - when the trip starts, and when it is due at each of
    # my stops.
    seqs: dict[str, list[tuple[int, str]]] = {t: [] for t in keep}
    starts: dict[str, tuple[int, int]] = {}
    board: dict[str, list[tuple[int, str]]] = {}
    for row in _rows(zf, "stop_times.txt"):
        trip_id = row["trip_id"]
        seq = seqs.get(trip_id)
        if seq is None:
            continue
        sequence = int(row["stop_sequence"])
        stop_id = row["stop_id"]
        seq.append((sequence, stop_id))
        when = _seconds(row.get("departure_time") or row.get("arrival_time") or "")
        if when is None:
            continue
        first = starts.get(trip_id)
        if first is None or sequence < first[0]:
            starts[trip_id] = (sequence, when)
        if stop_id in mine:
            board.setdefault(area_of[stop_id], []).append((when, trip_id))
    for calls in seqs.values():
        calls.sort()
    for entries in board.values():
        entries.sort()
    index.board = board

    routes = {
        row["route_id"]: (row["route_short_name"], row["route_desc"])
        for row in _rows(zf, "routes.txt")
    }

    wanted_shapes: set[str] = set()
    for row in _rows(zf, "trips.txt"):
        trip_id = row["trip_id"]
        if trip_id not in keep:
            continue
        line, kind = routes.get(row["route_id"], ("?", ""))
        shape = row["shape_id"]
        index.trips[trip_id] = {
            "line": line,
            "kind": kind,
            # UL leaves trip_headsign empty, so the destination is the last call.
            "headsign": row.get("trip_headsign") or "",
            # GTFS direction_id. UL has no headsign, so this is the only stable
            # handle on "which way round" a trip runs.
            "direction": row.get("direction_id", ""),
            "shape": shape,
            "stops": seqs[trip_id],
            "service": row.get("service_id", ""),
            # When the trip leaves its first stop, so a scheduled arrival can
            # say whether the bus should already be out there.
            "start": starts.get(trip_id, (0, 0))[1],
        }
        if shape:
            wanted_shapes.add(shape)
            index.line_shapes.setdefault(line, [])
            if shape not in index.line_shapes[line]:
                index.line_shapes[line].append(shape)

    index.service_dates = _service_dates(
        zf, {trip["service"] for trip in index.trips.values()}
    )
    # A trip nobody kept leaves its board entry pointing at nothing.
    for entries in index.board.values():
        entries[:] = [e for e in entries if e[1] in index.trips]

    # shapes.txt is 82 MB of the 143 MB; only pull the shapes we reference.
    for row in _rows(zf, "shapes.txt"):
        shape_id = row["shape_id"]
        if shape_id in wanted_shapes:
            index.shapes.setdefault(shape_id, []).append(
                (
                    int(row["shape_pt_sequence"]),
                    float(row["shape_pt_lat"]),
                    float(row["shape_pt_lon"]),
                )
            )
    for shape_id, points in index.shapes.items():
        points.sort()
        index.shapes[shape_id] = [(lat, lon) for _, lat, lon in points]

    # A line has several shape variants (line 1 has 15); longest first so the
    # line view can default to the most representative one.
    for line, shape_ids in index.line_shapes.items():
        shape_ids.sort(key=lambda s: len(index.shapes.get(s, ())), reverse=True)

    # Keep only stops actually referenced by the trips we kept.
    referenced = {
        stop_id for trip in index.trips.values() for _, stop_id in trip["stops"]
    }
    index.stops = {s: all_stops[s] for s in referenced if s in all_stops}

    frequency: collections.Counter = collections.Counter()
    for name, _, _ in index.stops.values():
        frequency.update(tokenize(name))
    threshold = max(1, len(index.stops) * COMMON_TOKEN_SHARE)
    index.common_tokens = {t for t, n in frequency.items() if n > threshold}

    _LOGGER.info(
        "Built GTFS index in %.1fs: %d trips, %d stops, %d shapes, %d timetabled calls",
        time.monotonic() - started,
        len(index.trips),
        len(index.stops),
        len(index.shapes),
        sum(len(e) for e in index.board.values()),
    )
    return index


def _cache_path(hass: HomeAssistant) -> str:
    return hass.config.path(".storage", GTFS_CACHE_FILE)


def _load_cache(path: str) -> GTFSIndex | None:
    """Read a previously built index. Blocking."""
    try:
        with open(path, "rb") as handle:
            index = pickle.load(handle)
    except FileNotFoundError:
        return None
    except Exception as err:  # a corrupt cache is rebuildable, never fatal
        _LOGGER.warning("Discarding unreadable GTFS cache: %s", err)
        return None
    return index if isinstance(index, GTFSIndex) else None


def _save_cache(path: str, index: GTFSIndex) -> None:
    """Persist the index. Blocking."""
    try:
        with open(path, "wb") as handle:
            pickle.dump(index, handle, protocol=pickle.HIGHEST_PROTOCOL)
    except OSError as err:
        _LOGGER.warning("Could not write GTFS cache: %s", err)


async def async_load_index(
    hass: HomeAssistant,
    static_key: str,
    ul_stop_ids: list[int | str],
    force: bool = False,
) -> GTFSIndex:
    """Return a usable index, downloading and rebuilding it when stale.

    A stale-but-present index is returned if the rebuild fails, so a Trafiklab
    outage degrades the map rather than emptying it.
    """
    path = _cache_path(hass)
    index = await hass.async_add_executor_job(_load_cache, path)

    if index is not None and not index.stale and index.covers(ul_stop_ids) and not force:
        return index

    session = aiohttp.ClientSession()
    try:
        async with async_timeout.timeout(GTFS_STATIC_TIMEOUT):
            # This endpoint 406s without an explicit Accept-Encoding.
            async with session.get(
                GTFS_STATIC_URL,
                params={"key": static_key},
                headers={"Accept-Encoding": "gzip, deflate"},
            ) as response:
                if response.status != 200:
                    raise GTFSError(
                        f"Static GTFS download failed: HTTP {response.status}"
                    )
                raw = await response.read()
    except Exception as err:
        if index is not None:
            _LOGGER.warning("Using stale GTFS index; refresh failed: %s", err)
            return index
        raise GTFSError(f"Could not download static GTFS: {err}") from err
    finally:
        await session.close()

    fresh = await hass.async_add_executor_job(build_index, raw, ul_stop_ids)
    if not fresh.trips and index is not None:
        _LOGGER.warning("Rebuilt GTFS index was empty; keeping previous one")
        return index

    await hass.async_add_executor_job(_save_cache, path, fresh)
    return fresh


class GTFSError(Exception):
    """Raised when the static feed cannot be turned into an index."""
