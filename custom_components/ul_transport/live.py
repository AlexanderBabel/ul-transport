"""Live vehicle data for the map, served on demand over the websocket.

Deliberately entity-free. Plotting 300+ buses as entities would write a
state-machine update per vehicle per refresh, firing `state_changed` and
filling the recorder for data nobody looks at when the map is closed. Instead
the cards pull from here while they are on screen, and a short TTL means N
viewers still cost one upstream request.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import logging
import time
from typing import Any

import aiohttp

# At module level, not inside the fetch: protobuf pulls in a C extension on
# first use, and importing that from the event loop is a blocking call Home
# Assistant warns about. Integration modules are imported in an executor.
from google.transit import gtfs_realtime_pb2
from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
import voluptuous as vol

from .const import (
    CONF_STOP_ID,
    CONF_STOP_NAME,
    DARK_TEXT_LINES,
    DEFAULT_HORIZON_MINUTES,
    DEFAULT_LIST_MINUTES,
    DEPARTED_LINGER_SECONDS,
    DOMAIN,
    GTFS_RT_TIMEOUT,
    GTFS_RT_TTL,
    GTFS_RT_URL,
    HIDDEN_ROUTE_KINDS,
    LINE_COLORS,
    PATH_AHEAD_METRES,
    WS_LINE,
    WS_OVERVIEW,
    WS_STOPS,
    count_request,
)
from .coordinator import async_coordinators
from .gtfs import GTFSIndex, tokenize, ul_stop_to_gtfs_area

_LOGGER = logging.getLogger(__name__)


@dataclass
class _Cached:
    """One realtime feed, with the ETag needed to make refetches cheap."""

    fetched: float = 0.0
    etag: str | None = None
    payload: Any = None


class LiveFeed:
    """Fetches and caches the two realtime feeds."""

    def __init__(self, hass: HomeAssistant, realtime_key: str) -> None:
        """Hold the key and the per-feed cache; the session is shared."""
        self.hass = hass
        self.realtime_key = realtime_key
        self._cache: dict[str, _Cached] = {}

    async def _async_fetch(self, feed: str) -> Any:
        """Return a parsed feed, honouring the TTL and the upstream ETag."""
        cached = self._cache.setdefault(feed, _Cached())
        if cached.payload is not None and time.time() - cached.fetched < GTFS_RT_TTL:
            return cached.payload

        session = async_get_clientsession(self.hass)
        headers = {"Accept-Encoding": "gzip, deflate"}
        if cached.etag:
            headers["If-None-Match"] = cached.etag

        try:
            async with asyncio.timeout(GTFS_RT_TIMEOUT):
                async with session.get(
                    GTFS_RT_URL.format(feed=feed),
                    params={"key": self.realtime_key},
                    headers=headers,
                ) as response:
                    # Counted here rather than on success: a 304 or a 429 is
                    # still a request against the quota.
                    count_request(self.hass, f"rt_{feed}")
                    if response.status == 304 and cached.payload is not None:
                        count_request(self.hass, "rt_not_modified")
                        cached.fetched = time.time()
                        return cached.payload
                    if response.status == 429:
                        raise LiveFeedError(
                            "Trafiklab quota exceeded. The map polls only while "
                            "it is on screen; if this keeps happening, request a "
                            "free quota upgrade from Trafiklab."
                        )
                    if response.status != 200:
                        raise LiveFeedError(f"{feed} returned HTTP {response.status}")
                    raw = await response.read()
                    etag = response.headers.get("ETag")
        except TimeoutError as err:
            raise LiveFeedError(f"Timed out reading the {feed} feed") from err
        except aiohttp.ClientError as err:
            raise LiveFeedError(f"Could not read the {feed} feed: {err}") from err

        message = gtfs_realtime_pb2.FeedMessage()
        message.ParseFromString(raw)
        cached.fetched = time.time()
        cached.etag = etag
        cached.payload = message
        return message

    async def async_positions(self) -> Any:
        """Where the vehicles are right now."""
        return await self._async_fetch("VehiclePositions")

    async def async_trip_updates(self) -> Any:
        """Predicted arrival times per trip."""
        return await self._async_fetch("TripUpdates")


def _predictions(trip_updates: Any) -> dict[str, dict[int, tuple[int, int]]]:
    """trip_id -> {stop_sequence: (epoch_seconds, delay)}.

    Only entries carrying a real time are kept, so an absent key means "no
    prediction" rather than "predicted at epoch 0".
    """
    out: dict[str, dict[int, tuple[int, int]]] = {}
    for entity in trip_updates.entity:
        update = entity.trip_update
        calls: dict[int, tuple[int, int]] = {}
        for stop_time in update.stop_time_update:
            event = None
            if stop_time.HasField("arrival") and stop_time.arrival.time:
                event = stop_time.arrival
            elif stop_time.HasField("departure") and stop_time.departure.time:
                event = stop_time.departure
            if event is not None:
                calls[stop_time.stop_sequence] = (event.time, event.delay)
        if calls:
            out[update.trip.trip_id] = calls
    return out


def _stops_away(calls: dict[int, tuple[int, int]], my_seq: int, now: float) -> int | None:
    """How many stops the vehicle still has to make before mine.

    VehiclePositions carries no ``current_stop_sequence`` for UL (0 of 319
    vehicles), so position in the trip is inferred from the earliest call still
    in the future. That inference is approximate: when a trip's prediction
    window happens to begin at my stop this reads 0 while the bus is still
    several minutes out. Callers should treat it as a hint, not a promise --
    the ETA beside it comes straight from the feed and is reliable.
    """
    upcoming = [seq for seq, (when, _) in calls.items() if when > now]
    if not upcoming:
        return None
    next_seq = min(upcoming)
    return my_seq - next_seq if my_seq >= next_seq else None


def _stops_from_position(
    index: GTFSIndex, trip_id: str, payload: dict[str, Any], my_seq: int | None
) -> int | None:
    """Stops still to go, worked out from where the vehicle actually is.

    The fallback for a bus that is out there and reporting a position but not
    predicting arrivals - saying "timetabled arrival" about a bus you can watch
    move across the map is the one thing it plainly is not.
    """
    if my_seq is None or payload.get("lat") is None:
        return None
    seq = index.seq_near(trip_id, payload["lat"], payload["lon"])
    if seq is None or seq > my_seq:
        return None
    return my_seq - seq


def _departed(
    index: GTFSIndex,
    trip_id: str,
    payload: dict[str, Any],
    my_seq: int | None,
    eta: float,
    now: float,
) -> bool:
    """Whether it has actually pulled away, rather than merely being overdue.

    A prediction that has run out is not evidence of departure - the bus may be
    standing at the kerb, or held two stops back with nothing updating it. Where
    there is a position, where the bus is answers this outright.
    """
    if payload.get("lat") is not None and my_seq is not None:
        seq = index.seq_near(trip_id, payload["lat"], payload["lon"])
        if seq is not None:
            return seq > my_seq
    return eta < now


def _next_stop(
    index: GTFSIndex, trip: dict[str, Any], calls: dict[int, tuple[int, int]], now: float
) -> tuple[str, int] | tuple[None, None]:
    """Name and predicted time of the call the vehicle is heading for."""
    upcoming = [seq for seq, (when, _) in calls.items() if when > now]
    if not upcoming:
        return None, None
    seq = min(upcoming)
    for trip_seq, stop_id in trip["stops"]:
        if trip_seq == seq:
            return index.stops.get(stop_id, ("", 0, 0))[0], calls[seq][0]
    return None, None


def _line_payload(trip: dict[str, Any]) -> dict[str, Any]:
    """The bits of a row that need no vehicle - a scheduled trip has these too."""
    line = trip["line"]
    return {
        "line": line,
        "kind": trip["kind"],
        # Sent from here so the card does not duplicate the colour table.
        "color": LINE_COLORS.get(line, "#5f6368"),
        "text_color": "#000000" if line in DARK_TEXT_LINES else "#ffffff",
    }


def _vehicle_payload(vehicle: Any, trip: dict[str, Any], now: float) -> dict[str, Any]:
    position = vehicle.position
    return {
        **_line_payload(trip),
        "id": vehicle.vehicle.id,
        "trip_id": vehicle.trip.trip_id,
        "lat": round(position.latitude, 6),
        "lon": round(position.longitude, 6),
        "bearing": round(position.bearing) if position.HasField("bearing") else None,
        "speed": round(position.speed * 3.6, 1) if position.HasField("speed") else None,
        # Seconds since the vehicle reported this position, computed here so the
        # card can extrapolate without trusting the browser clock to agree with
        # ours. Typically 2-3 s on UL's feed.
        "age": round(now - vehicle.timestamp, 1) if vehicle.timestamp else None,
    }


def _feed_age(feed: Any, now: float) -> float:
    """How stale the feed already was when we read it, in seconds.

    The card adds its own time-since-fetch to this, so "updated 4s ago" means
    the data is four seconds old rather than four seconds since we asked.
    """
    stamp = feed.header.timestamp if feed is not None else 0
    return round(max(now - stamp, 0), 1) if stamp else 0.0


def _kind_filter(kinds: list[str] | None):
    """Which route_desc values to list.

    An explicit list is an allow-list; the default hides only school buses, so
    a stop's trains and regional coaches are there when the line picker offers
    them.
    """
    if kinds:
        allowed = set(kinds)
        return lambda kind: kind in allowed
    return lambda kind: kind not in HIDDEN_ROUTE_KINDS


def timetable_arrivals(
    index: GTFSIndex,
    area_id: str,
    now: float,
    cutoff: float,
    wanted_lines: set[str] | None,
    keep_kind,
    names: dict[tuple[str, str], str],
    seen: set[str],
    wanted_destinations: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Departures the timetable knows about and the realtime feeds do not.

    The realtime feeds only describe vehicles that are already running - UL
    publishes ~65 trip updates for the whole county - so a list asked to look
    two hours ahead runs dry after about forty minutes. The static feed already
    holds the whole day, so the rest of the list costs no request at all.

    ``seen`` is every trip the realtime feeds already accounted for; matching on
    trip id makes the duplicate impossible rather than merely unlikely.
    """
    out: list[dict[str, Any]] = []
    for when, trip_id, started in index.departures_at(area_id, now, cutoff):
        if trip_id in seen:
            continue
        trip = index.trips.get(trip_id)
        if trip is None:
            continue
        if wanted_lines is not None and trip["line"] not in wanted_lines:
            continue
        if not keep_kind(trip["kind"]):
            continue
        if index.terminates_at(trip_id, area_id):
            continue  # ends here, so it is an arrival rather than a departure
        destination = _direction(index, trip, names)
        if wanted_destinations is not None and destination not in wanted_destinations:
            continue
        seen.add(trip_id)  # a trip calling twice at one stop is listed once
        out.append(
            {
                **_line_payload(trip),
                "id": f"sched-{trip_id}",
                "trip_id": trip_id,
                "destination": destination,
                "track": index.trip_platform(trip_id, area_id),
                "eta": when,
                "eta_minutes": round((when - now) / 60, 1),
                # Timetabled, so there is nothing to be late against yet.
                "delay": None,
                # No vehicle assigned, so nothing to plot and no position to
                # count stops from.
                "scheduled": True,
                # Whether the bus should already be out on the road. If it is,
                # this is a gap in the realtime feed rather than a bus that has
                # not set off.
                "started": started <= now,
                "on_map": False,
            }
        )
    return out


def _path(index: GTFSIndex, trip_id: str, payload: dict[str, Any]) -> list[list[float]]:
    """The road ahead of this bus, for the card to move it along.

    Empty when the vehicle is not near its own shape - the card falls back to
    the reported bearing, which is what it used before there were any paths.
    """
    if payload.get("lat") is None:
        return []
    return [
        [round(lat, 6), round(lon, 6)]
        for lat, lon in index.path_ahead(
            trip_id, payload["lat"], payload["lon"], PATH_AHEAD_METRES
        )
    ]


def _terminus(index: GTFSIndex, trip: dict[str, Any]) -> str:
    """Last call of the trip. UL leaves trip_headsign empty on all 22k trips."""
    if trip["headsign"]:
        return trip["headsign"]
    if not trip["stops"]:
        return ""
    last_stop = trip["stops"][-1][1]
    name, _, _ = index.stops.get(last_stop, ("", 0.0, 0.0))
    return name


def ul_directions(board: Any) -> list[tuple]:
    """Distinct (line, towards) pairs UL advertises at this stop.

    Takes anything iterating "line_towards" keys - the coordinator's grouped
    departures or its raw key list. Reuses what the departure sensors already
    fetch, so putting UL's own direction names on the map costs no extra
    requests against either API. Only the names matter - see direction_names
    for why the times do not.
    """
    pairs = set()
    for key in board or {}:
        line, _, towards = key.partition("_")
        if line and towards:
            pairs.add((line, towards))
    return sorted(pairs)


def _name_score(index: GTFSIndex, trip: dict[str, Any], towards: str) -> int:
    """How strongly a UL direction name matches where this trip actually goes.

    Counted over the last third of the trip, where the distinctive place names
    live, and ignoring tokens common to most stop names.
    """
    tokens = tokenize(towards) - index.common_tokens
    if not tokens:
        return 0
    calls = trip["stops"]
    tail = calls[len(calls) * 2 // 3 :] or calls
    text = " ".join(index.stops.get(s, ("", 0, 0))[0].lower() for _, s in tail)
    return sum(1 for token in tokens if token in text)


def direction_names(
    index: GTFSIndex, directions: list[tuple], name_cache: dict | None = None
) -> dict[tuple[str, str], str]:
    """Map (line, direction_id) -> UL's own name for that direction.

    GTFS termini are physical last stops ("Uppsala Jenny Linds väg"); UL
    markets the same direction as "Norby Gottsunda", which is what the
    departure board and the app show.

    Matching on arrival time looks obvious and is wrong: UL lists the same
    direction twice in one response, and opposite directions pass a mid-route
    stop within seconds of each other, so a time match happily gives both
    directions the same name and changes its mind on the next refresh. Matching
    on place names instead is decided entirely by static data, so it is stable
    across refreshes. A direction that scores nothing is resolved by
    elimination when it is the only one left.
    """
    seen: dict[str, set[str]] = {}
    for line, towards in directions:
        seen.setdefault(line, set()).add(towards)
    if name_cache is not None:
        for line, names in seen.items():
            name_cache.setdefault(line, set()).update(names)
        seen = name_cache

    resolved: dict[tuple[str, str], str] = {}
    for line, names in seen.items():
        longest: dict[str, dict[str, Any]] = {}
        for trip in index.trips.values():
            if trip["line"] != line:
                continue
            current = longest.get(trip["direction"])
            if current is None or len(trip["stops"]) > len(current["stops"]):
                longest[trip["direction"]] = trip
        if not longest:
            continue

        free_dirs, free_names = set(longest), set(names)
        scores = {
            (direction, name): _name_score(index, trip, name)
            for direction, trip in longest.items()
            for name in names
        }
        for (direction, name), score in sorted(scores.items(), key=lambda kv: -kv[1]):
            if score <= 0 or direction not in free_dirs or name not in free_names:
                continue
            rivals = [
                s
                for (d, n), s in scores.items()
                if d == direction and n in free_names and n != name
            ]
            if rivals and max(rivals) >= score:
                continue  # tied - let elimination decide instead of guessing
            resolved[(line, direction)] = name
            free_dirs.discard(direction)
            free_names.discard(name)

        if len(free_dirs) == 1 and len(free_names) == 1:
            resolved[(line, free_dirs.pop())] = free_names.pop()
    return resolved


def _direction(
    index: GTFSIndex, trip: dict[str, Any], names: dict[tuple[str, str], str]
) -> str:
    """UL's name for this trip's direction, or the GTFS terminus."""
    return names.get((trip["line"], trip["direction"])) or _terminus(index, trip)


def overview(
    index: GTFSIndex,
    positions: Any,
    trip_updates: Any,
    ul_stop_id: int | str,
    kinds: list[str] | None = None,
    lines: list[str] | None = None,
    destinations: list[str] | None = None,
    directions: list[tuple] | None = None,
    name_cache: dict | None = None,
    horizon_minutes: float = DEFAULT_HORIZON_MINUTES,
    list_minutes: float | None = None,
    linger_seconds: float = DEPARTED_LINGER_SECONDS,
    limit: int | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    """View one: vehicles still inbound to a configured stop, plus their stops.

    Driven by the trip updates rather than the positions, so a stop still lists
    its arrivals when ``positions`` is None - which is what a list-only card
    asks for, and halves the upstream requests.
    """
    now = time.time() if now is None else now
    keep_kind = _kind_filter(kinds)
    wanted_lines = {str(line) for line in lines} if lines else None
    # Where it goes, not which number is on the front: at a station "line 7 but
    # only towards Årsta Fyrislund" and "trains, but not the ones to Gävle" are
    # the useful cuts, and neither is expressible as a set of line numbers.
    wanted_destinations = set(destinations) if destinations else None
    directions = directions or []
    horizon = now + horizon_minutes * 60
    # Everything up to here is listed; only what is inside the horizon is
    # plotted, so the list can look further ahead than the viewport.
    if list_minutes is None:
        list_minutes = DEFAULT_LIST_MINUTES
    cutoff = now + max(list_minutes, horizon_minutes) * 60
    area_id = ul_stop_to_gtfs_area(ul_stop_id)
    calls_by_trip = _predictions(trip_updates)
    names = direction_names(index, directions, name_cache)

    vehicles: list[dict[str, Any]] = []
    # Always present, so the card can frame the map on the stop even when
    # nothing is inbound.
    referenced: set[str] = {area_id}
    # ~40% of vehicles run without a trip assignment (deadheading and out of
    # service); nothing useful can be said about them, so they never key in.
    running = {
        entity.vehicle.trip.trip_id: entity.vehicle
        for entity in (positions.entity if positions is not None else [])
        if entity.vehicle.trip.trip_id
    }

    # Trips the realtime feeds have already spoken for, so the timetable does
    # not list them a second time.
    seen: set[str] = set(calls_by_trip)

    for trip_id, calls in calls_by_trip.items():
        trip = index.trips.get(trip_id)
        if trip is None:
            continue  # does not call at any configured stop
        if wanted_lines is not None and trip["line"] not in wanted_lines:
            continue
        if not keep_kind(trip["kind"]):
            continue
        if index.terminates_at(trip_id, area_id):
            continue  # ends here: nobody is waiting to board it
        destination = _direction(index, trip, names)
        if wanted_destinations is not None and destination not in wanted_destinations:
            continue
        my_seq = index.trip_seq_at(trip_id, area_id)
        if my_seq is None or my_seq not in calls:
            continue

        eta, delay = calls[my_seq]
        if eta < now - linger_seconds:
            continue  # been and gone long enough ago to stop caring
        if eta > cutoff:
            continue

        away = _stops_away(calls, my_seq, now)
        vehicle = running.get(trip_id)
        payload = (
            _vehicle_payload(vehicle, trip, now)
            if vehicle is not None
            else {**_line_payload(trip), "id": trip_id, "trip_id": trip_id}
        )
        next_stop, next_eta = _next_stop(index, trip, calls, now)
        payload.update(
            {
                "destination": destination,
                "track": index.trip_platform(trip_id, area_id),
                "eta": eta,
                "eta_minutes": round((eta - now) / 60, 1),
                "delay": delay,
                "stops_away": away,
                "next_stop": next_stop,
                "next_stop_eta": next_eta,
                # The trip is running - it is sending arrival predictions - but
                # this one is not reporting where it is. About a third of them.
                # Null rather than False when the positions feed was not read at
                # all: a list-only card must not claim a bus has no position.
                "live": None if positions is None else vehicle is not None,
                # Held on the map for a moment after it has gone, so you see it
                # pull away instead of blinking out at the kerb.
                "departed": _departed(index, trip_id, payload, my_seq, eta, now),
                # Regional trips can be 45+ stops out. Plotting them stretches
                # the map across the county for a bus nobody is waiting for yet.
                "on_map": vehicle is not None and eta <= horizon,
            }
        )
        if payload["on_map"]:
            payload["path"] = _path(index, trip_id, payload)
        vehicles.append(payload)
        if not payload["on_map"]:
            continue
        # Only the approach path: the calls still between the bus and my stop.
        # Everything downstream of my stop is noise here, and the union across
        # every inbound vehicle runs to hundreds of markers if left unbounded.
        first_seq = my_seq - away if away is not None else my_seq
        referenced.update(
            stop_id
            for seq, stop_id in trip["stops"]
            if first_seq <= seq <= my_seq
        )

    scheduled = timetable_arrivals(
        index,
        area_id,
        now,
        cutoff,
        wanted_lines,
        keep_kind,
        names,
        seen,
        wanted_destinations,
    )
    for row in scheduled:
        if positions is None:
            # Not fetched, so "no live data" would be our own doing rather than
            # the feed's. Null means unknown, as it does on the rows above.
            row["live"] = None
        vehicle = running.get(row["trip_id"])
        if vehicle is None:
            continue
        # Out on the road but sending no arrival predictions. The timetable
        # supplies the time, the vehicle supplies the place - listing this as
        # "not yet running" while it sits on the map is the one thing it is not.
        row.update(_vehicle_payload(vehicle, index.trips[row["trip_id"]], now))
        row["live"] = True
        row["started"] = True
        row["stops_away"] = _stops_from_position(
            index, row["trip_id"], row, index.trip_seq_at(row["trip_id"], area_id)
        )
        row["on_map"] = row["eta"] <= horizon
        if row["on_map"]:
            row["path"] = _path(index, row["trip_id"], row)
    vehicles.extend(scheduled)
    vehicles.sort(key=lambda v: v["eta"])
    if limit is not None:
        # A whole-day list at a hub is a couple of thousand departures. Cut it
        # to what the card will draw - but never drop a bus off the map to do it.
        vehicles = [v for i, v in enumerate(vehicles) if i < limit or v["on_map"]]

    return {
        "stop_id": ul_stop_id,
        "area_id": area_id,
        "stop_name": index.areas.get(area_id, ("", 0, 0))[0],
        "generated": now,
        # How far ahead the map reaches, so the card can tell a departure that is
        # further out than the map from one that simply has no bus assigned yet.
        "horizon": horizon_minutes,
        "data_age": _feed_age(positions or trip_updates, now),
        "vehicles": vehicles,
        "stops": _stop_payload(index, referenced, area_id),
    }


def line_view(
    index: GTFSIndex,
    positions: Any,
    trip_updates: Any,
    line: str,
    ul_stop_id: int | str,
    trip_id: str | None = None,
    directions: list[tuple] | None = None,
    name_cache: dict | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    """View two: one line, its route geometry, its stops and its vehicles."""
    now = time.time() if now is None else now
    directions = directions or []
    area_id = ul_stop_to_gtfs_area(ul_stop_id)
    calls_by_trip = _predictions(trip_updates)
    names = direction_names(index, directions, name_cache)
    # Scheduled calls at my stop, for the buses nothing is predicting for. A
    # window rather than a point: one of them may be running late and already
    # past its slot.
    timetabled: dict[str, float] = {}
    for when, tid, _ in index.departures_at(area_id, now - 1800, now + 7200):
        timetabled.setdefault(tid, when)

    vehicles: list[dict[str, Any]] = []
    for entity in positions.entity:
        vehicle = entity.vehicle
        vehicle_trip = vehicle.trip.trip_id
        trip = index.trips.get(vehicle_trip)
        if trip is None or trip["line"] != line:
            continue
        payload = _vehicle_payload(vehicle, trip, now)
        payload["destination"] = _direction(index, trip, names)
        payload["track"] = index.trip_platform(vehicle_trip, area_id)
        payload["path"] = _path(index, vehicle_trip, payload)
        my_seq = index.trip_seq_at(vehicle_trip, area_id)
        calls = calls_by_trip.get(vehicle_trip, {})
        next_stop, next_eta = _next_stop(index, trip, calls, now)
        payload["next_stop"], payload["next_stop_eta"] = next_stop, next_eta
        if my_seq is not None and my_seq in calls:
            eta, delay = calls[my_seq]
            payload.update(
                {
                    "eta": eta,
                    "eta_minutes": round((eta - now) / 60, 1),
                    "delay": delay,
                    "stops_away": _stops_away(calls, my_seq, now),
                    "passed": _departed(index, vehicle_trip, payload, my_seq, eta, now),
                }
            )
        elif my_seq is not None and vehicle_trip in timetabled:
            # A bus you can watch move, with nothing predicting for it - all of
            # line 7 towards Årsta Fyrislund. It called at your stop at some
            # point today, so the timetable answers when, and the position
            # answers how far off it is; a dash answers neither.
            eta = timetabled[vehicle_trip]
            payload.update(
                {
                    "eta": eta,
                    "eta_minutes": round((eta - now) / 60, 1),
                    "delay": None,
                    "scheduled": True,
                    # It is on the map by definition here, so the row must not
                    # read "not yet running".
                    "live": True,
                    "stops_away": _stops_from_position(
                        index, vehicle_trip, payload, my_seq
                    ),
                    "passed": _departed(index, vehicle_trip, payload, my_seq, eta, now),
                }
            )
        vehicles.append(payload)

    # Prefer the geometry of the trip the user actually clicked; fall back to
    # the line's longest variant (line 1 has 15 of them).
    shape_id = None
    if trip_id and trip_id in index.trips:
        shape_id = index.trips[trip_id]["shape"]
    if not shape_id:
        variants = index.line_shapes.get(line) or []
        shape_id = variants[0] if variants else None

    if trip_id and trip_id in index.trips:
        route_stops = {stop_id for _, stop_id in index.trips[trip_id]["stops"]}
    else:
        route_stops = {
            stop_id
            for trip in index.trips.values()
            if trip["line"] == line
            for _, stop_id in trip["stops"]
        }

    return {
        "line": line,
        "color": LINE_COLORS.get(line, "#5f6368"),
        "text_color": "#000000" if line in DARK_TEXT_LINES else "#ffffff",
        "stop_name": index.areas.get(area_id, ("", 0, 0))[0],
        "generated": now,
        "data_age": _feed_age(positions, now),
        "vehicles": sorted(vehicles, key=lambda v: v.get("eta") or float("inf")),
        "shape": [
            {"lat": round(lat, 6), "lon": round(lon, 6)}
            for lat, lon in index.shapes.get(shape_id, [])
        ],
        "stops": _stop_payload(index, route_stops, area_id),
    }


def _stop_payload(
    index: GTFSIndex, stop_ids: set[str], area_id: str
) -> list[dict[str, Any]]:
    """Stop markers, with the configured stop collapsed to a single point.

    A stop's platforms are separate GTFS entries - Sommarro's sit ~170 m apart
    on opposite sides of the road - so drawing each as "my stop" stacks several
    highlighted markers into one blob. The parent station is the thing the user
    thinks of as their stop.
    """
    mine = index.platforms.get(area_id, set())
    out = []
    for stop_id in stop_ids:
        entry = index.stops.get(stop_id)
        if entry is None or stop_id in mine or stop_id == area_id:
            continue
        name, lat, lon = entry
        out.append(
            {
                "id": stop_id,
                "name": name,
                "lat": round(lat, 6),
                "lon": round(lon, 6),
                "mine": False,
            }
        )
    out.sort(key=lambda s: s["name"])

    area = index.areas.get(area_id)
    if area is not None and (stop_ids & mine or area_id in stop_ids):
        name, lat, lon = area
        out.append(
            {
                "id": area_id,
                "name": name,
                "lat": round(lat, 6),
                "lon": round(lon, 6),
                "mine": True,
            }
        )
    return out


@callback
def async_register_websocket(hass: HomeAssistant) -> None:
    """Register the map commands. Safe to call more than once."""
    if hass.data[DOMAIN].get("ws_registered"):
        return
    hass.data[DOMAIN]["ws_registered"] = True
    websocket_api.async_register_command(hass, ws_overview)
    websocket_api.async_register_command(hass, ws_line)
    websocket_api.async_register_command(hass, ws_stops)


async def _async_context(hass: HomeAssistant) -> tuple[GTFSIndex, LiveFeed]:
    data = hass.data.get(DOMAIN, {})
    runtime = data.get("map")
    if runtime is None:
        # A card on the dashboard renders before the integration has finished
        # starting - downloading and indexing the static feed takes a few
        # seconds - and "not configured" is the wrong thing to say then.
        raise LiveFeedError(
            "The live map is still starting up."
            if data.get("map_loading")
            else "The live map is not configured. Add your Trafiklab GTFS "
            "Regional static and realtime API keys in the integration options."
        )
    return runtime["index"], runtime["feed"]


def _name_cache(hass: HomeAssistant, ul_stop_id: int | str) -> dict:
    """Per-stop store of learned direction names, kept for the process lifetime."""
    runtime = hass.data.get(DOMAIN, {}).get("map")
    if runtime is None:
        return {}
    return runtime.setdefault("names", {}).setdefault(str(ul_stop_id), {})


def _coordinator_directions(hass: HomeAssistant, ul_stop_id: int | str) -> list[tuple]:
    """UL's own direction names for a stop, from the sensor coordinator.

    Read from the unfiltered board rather than from ``data``: the sensors keep
    only the lines you picked, and a line missing from here falls back to its
    GTFS terminus - "Uppsala Naturstensvägen" where the app says "Flogsta
    Stenhagen".
    """
    for coordinator in async_coordinators(hass):
        if coordinator.stop_id == int(ul_stop_id):
            return ul_directions(coordinator.board_keys)
    return []


@websocket_api.websocket_command({vol.Required("type"): WS_STOPS})
@websocket_api.async_response
async def ws_stops(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """List configured stops and the lines serving them, for the card editor."""
    index = hass.data.get(DOMAIN, {}).get("map", {}).get("index")
    stops = []
    for entry in hass.config_entries.async_entries(DOMAIN):
        stop_id = entry.data.get(CONF_STOP_ID)
        if stop_id is None:
            continue
        lines: set[str] = set()
        destinations: set[str] = set()
        if index is not None:
            area_id = ul_stop_to_gtfs_area(stop_id)
            names = direction_names(
                index,
                _coordinator_directions(hass, stop_id),
                _name_cache(hass, stop_id),
            )
            for trip_id, trip in index.trips.items():
                # Only what the card would actually draw: offering a line the
                # default filters drop makes picking it show nothing.
                if trip["kind"] in HIDDEN_ROUTE_KINDS:
                    continue
                if index.terminates_at(trip_id, area_id):
                    continue
                if index.trip_seq_at(trip_id, area_id) is not None:
                    lines.add(trip["line"])
                    destinations.add(_direction(index, trip, names))
        stops.append(
            {
                "stop_id": stop_id,
                "name": entry.data.get(CONF_STOP_NAME, str(stop_id)),
                "lines": sorted(lines, key=lambda x: (len(x), x)),
                # The same strings the rows show, so picking one here and
                # reading it off a row are the same thing.
                "destinations": sorted(d for d in destinations if d),
            }
        )
    connection.send_result(msg["id"], {"stops": stops, "configured": index is not None})


@websocket_api.websocket_command(
    {
        vol.Required("type"): WS_OVERVIEW,
        vol.Required("stop_id"): vol.Coerce(int),
        vol.Optional("kinds"): [str],
        vol.Optional("lines"): [str],
        vol.Optional("destinations"): [str],
        vol.Optional("horizon_minutes"): vol.All(
            vol.Coerce(float), vol.Range(min=1, max=180)
        ),
        # A whole day, for a stop used as a timetable rather than a map.
        vol.Optional("list_minutes"): vol.All(
            vol.Coerce(float), vol.Range(min=1, max=1440)
        ),
        vol.Optional("linger_seconds"): vol.All(
            vol.Coerce(float), vol.Range(min=0, max=600)
        ),
        vol.Optional("limit"): vol.All(vol.Coerce(int), vol.Range(min=1, max=200)),
        vol.Optional("include_positions"): bool,
    }
)
@websocket_api.async_response
async def ws_overview(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Serve view one."""
    try:
        index, feed = await _async_context(hass)
        # A card showing only the list has nothing to plot, so it does not pay
        # for the positions feed - half the upstream requests.
        positions = (
            await feed.async_positions()
            if msg.get("include_positions", True)
            else None
        )
        trip_updates = await feed.async_trip_updates()
        result = overview(
            index,
            positions,
            trip_updates,
            msg["stop_id"],
            kinds=msg.get("kinds"),
            lines=msg.get("lines"),
            destinations=msg.get("destinations"),
            directions=_coordinator_directions(hass, msg["stop_id"]),
            name_cache=_name_cache(hass, msg["stop_id"]),
            horizon_minutes=msg.get("horizon_minutes", DEFAULT_HORIZON_MINUTES),
            list_minutes=msg.get("list_minutes"),
            linger_seconds=msg.get("linger_seconds", DEPARTED_LINGER_SECONDS),
            limit=msg.get("limit"),
        )
    except LiveFeedError as err:
        connection.send_error(msg["id"], "live_feed_error", str(err))
        return
    except Exception as err:
        _LOGGER.exception("Live map overview failed")
        connection.send_error(msg["id"], "unknown_error", str(err))
        return
    connection.send_result(msg["id"], result)


@websocket_api.websocket_command(
    {
        vol.Required("type"): WS_LINE,
        vol.Required("stop_id"): vol.Coerce(int),
        vol.Required("line"): str,
        vol.Optional("trip_id"): str,
    }
)
@websocket_api.async_response
async def ws_line(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Serve view two."""
    try:
        index, feed = await _async_context(hass)
        positions = await feed.async_positions()
        trip_updates = await feed.async_trip_updates()
        result = line_view(
            index,
            positions,
            trip_updates,
            msg["line"],
            msg["stop_id"],
            msg.get("trip_id"),
            directions=_coordinator_directions(hass, msg["stop_id"]),
            name_cache=_name_cache(hass, msg["stop_id"]),
        )
    except LiveFeedError as err:
        connection.send_error(msg["id"], "live_feed_error", str(err))
        return
    except Exception as err:
        _LOGGER.exception("Live map line view failed")
        connection.send_error(msg["id"], "unknown_error", str(err))
        return
    connection.send_result(msg["id"], result)


class LiveFeedError(Exception):
    """Raised when the realtime feeds cannot be served."""
