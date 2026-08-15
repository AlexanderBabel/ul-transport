"""Departure sensors for UL Transport.

Shaped for automations rather than for a departure board: the state of every
sensor is "minutes until the next bus", which a numeric_state trigger can use
directly, with the timestamps alongside it as attributes. The live map card
draws its own data over the websocket and needs none of these.
"""

from __future__ import annotations

from datetime import datetime
import logging
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import EntityCategory, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .const import (
    DEFAULT_ICON,
    DOMAIN,
    REQUEST_COUNTS,
    TRAFFIC_TYPE_ICONS,
    TRAFFIC_TYPE_MAPPING,
)
from .coordinator import ULTransportConfigEntry, ULTransportDataUpdateCoordinator

_LOGGER = logging.getLogger(__name__)

# Suffixes this integration currently creates. Anything else on the entry is a
# leftover from the departures-card sensors, which no longer exist.
CURRENT_SUFFIXES = ("_in", "_next_departure", "_last_update", "_api_requests")


def _parse(value: str | None) -> datetime | None:
    """One of UL's timestamps as an aware datetime."""
    if not value:
        return None
    parsed = dt_util.parse_datetime(value)
    # UL sends UTC with a trailing Z; a naive one is read as local time.
    return dt_util.as_utc(parsed) if parsed else None


def _departure(departure: dict[str, Any]) -> datetime | None:
    """When this bus actually leaves: real time if there is any, else planned."""
    return _parse(departure.get("realTimeDepartureDateTime")) or _parse(
        departure.get("departureDateTime")
    )


def _minutes_until(when: datetime | None) -> int | None:
    """Whole minutes from now, floored, never negative.

    Floored because "in 2 minutes" has to stop being true before the bus goes,
    not a rounded thirty seconds after it has.
    """
    if when is None:
        return None
    return max(0, int((when - dt_util.utcnow()).total_seconds() // 60))


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ULTransportConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up UL Transport sensors from a config entry."""
    coordinator = config_entry.runtime_data

    sensors: list[SensorEntity] = [
        ULNextDepartureSensor(coordinator),
        ULTransportLastUpdateSensor(coordinator),
    ]
    sensors += [
        ULLineDepartureSensor(coordinator, key)
        for key, departures in coordinator.data.items()
        if departures
    ]

    # One per install, not per stop - the quota it watches is account-wide. The
    # first entry set up owns it; see async_unload_entry for the handover.
    if (
        hass.data[DOMAIN].setdefault("counter_entry", config_entry.entry_id)
        == config_entry.entry_id
    ):
        sensors.append(ULApiRequestsSensor(hass))

    _prune_removed_entities(hass, config_entry)
    async_add_entities(sensors)


def _prune_removed_entities(
    hass: HomeAssistant, config_entry: ULTransportConfigEntry
) -> None:
    """Drop registry entries for sensors this integration no longer creates.

    Matched on the id shape rather than on what exists right now, so a line that
    happens not to be running at restart keeps its entity and its history.
    """
    registry = er.async_get(hass)
    for entry in er.async_entries_for_config_entry(registry, config_entry.entry_id):
        if entry.domain == "sensor" and not entry.unique_id.endswith(CURRENT_SUFFIXES):
            _LOGGER.debug("Removing obsolete sensor %s", entry.entity_id)
            registry.async_remove(entry.entity_id)


class _ULDepartureSensor(
    CoordinatorEntity[ULTransportDataUpdateCoordinator], SensorEntity
):
    """Minutes until the next departure, with the times as attributes."""

    _attr_has_entity_name = True
    _attr_device_class = SensorDeviceClass.DURATION
    _attr_native_unit_of_measurement = UnitOfTime.MINUTES

    def __init__(self, coordinator: ULTransportDataUpdateCoordinator) -> None:
        """Attach the sensor to its stop."""
        super().__init__(coordinator)
        self._attr_device_info = coordinator.device_info

    @property
    def _stop_name(self) -> str:
        return self.coordinator.stop_name

    def _departures(self) -> list[dict[str, Any]]:
        """Upcoming departures for this sensor, earliest first."""
        raise NotImplementedError

    @property
    def native_value(self) -> int | None:
        """Minutes until the next departure."""
        upcoming = self._departures()
        return _minutes_until(_departure(upcoming[0])) if upcoming else None

    @property
    def available(self) -> bool:
        """Available while the poll is succeeding and something is due."""
        return self.coordinator.last_update_success and bool(self._departures())

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """The next departure in full, plus the ones after it."""
        upcoming = self._departures()
        if not upcoming:
            return {}
        first = upcoming[0]
        line = first["line"]
        planned = _parse(first.get("departureDateTime"))
        estimated = _parse(first.get("realTimeDepartureDateTime"))
        return {
            "line": str(line["name"]).strip(),
            "direction": line["towards"].strip(),
            "transport": TRAFFIC_TYPE_MAPPING.get(line.get("trafficType", 0), "UNKNOWN"),
            "stop_name": self._stop_name,
            "departure": estimated or planned,
            "scheduled_departure": planned,
            # Positive is late. Absent when the bus has no real-time report at
            # all, which is not the same as being exactly on time.
            "delay_minutes": (
                round((estimated - planned).total_seconds() / 60)
                if estimated and planned
                else None
            ),
            "is_realtime": estimated is not None,
            # The ones after it, so an automation can look past a bus it missed.
            "next_departures": [
                when for dep in upcoming[1:] if (when := _departure(dep))
            ],
            "next_departures_in": [
                minutes
                for dep in upcoming[1:]
                if (minutes := _minutes_until(_departure(dep))) is not None
            ],
        }

    @property
    def icon(self) -> str:
        """Match the icon to what is actually turning up."""
        upcoming = self._departures()
        if not upcoming:
            return DEFAULT_ICON
        traffic_type = upcoming[0]["line"].get("trafficType", 0)
        return TRAFFIC_TYPE_ICONS.get(traffic_type, DEFAULT_ICON)


class ULLineDepartureSensor(_ULDepartureSensor):
    """One line in one direction from one stop."""

    def __init__(
        self, coordinator: ULTransportDataUpdateCoordinator, key: str
    ) -> None:
        """Initialize the sensor for one "line_towards" key."""
        super().__init__(coordinator)
        self._key = key

        line_name, _, direction = key.partition("_")
        # Not a translation key: the name is built from live API data, so there
        # is nothing to translate beyond the word "Line".
        self._attr_name = f"Line {line_name} to {direction}"
        self._attr_unique_id = f"{coordinator.stop_id}_{key}_in"

    def _departures(self) -> list[dict[str, Any]]:
        return self.coordinator.data.get(self._key) or []


class ULNextDepartureSensor(_ULDepartureSensor):
    """The next bus from this stop, whichever line it is.

    The one sensor most automations want: "leave now" does not care which line
    turns up, only that something does.
    """

    _attr_translation_key = "next_departure"

    def __init__(self, coordinator: ULTransportDataUpdateCoordinator) -> None:
        """Initialize the whole-stop sensor."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.stop_id}_next_departure"

    def _departures(self) -> list[dict[str, Any]]:
        merged = [
            departure
            for group in (self.coordinator.data or {}).values()
            for departure in group
        ]
        return sorted((d for d in merged if _departure(d) is not None), key=_departure)


class ULTransportLastUpdateSensor(
    CoordinatorEntity[ULTransportDataUpdateCoordinator], SensorEntity
):
    """Sensor reporting the timestamp of the last successful API fetch."""

    _attr_has_entity_name = True
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:clock-check-outline"
    _attr_translation_key = "last_update"

    def __init__(self, coordinator: ULTransportDataUpdateCoordinator) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.stop_id}_last_update"
        self._attr_device_info = coordinator.device_info

    @property
    def native_value(self) -> datetime | None:
        """Return the last successful update time."""
        return self.coordinator.last_successful_update

    @property
    def available(self) -> bool:
        """Return True once at least one fetch has succeeded."""
        return self.coordinator.last_successful_update is not None


class ULApiRequestsSensor(SensorEntity):
    """Upstream HTTP requests made since Home Assistant started.

    Exists to answer "am I anywhere near the Trafiklab quota", so it counts
    calls rather than successes: a 304 or a 429 is spent quota too. Account-wide
    rather than per stop, so it belongs to no single stop's device.
    """

    _attr_name = "UL Transport API requests"
    _attr_unique_id = f"{DOMAIN}_api_requests"
    _attr_icon = "mdi:api"
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_native_unit_of_measurement = "requests"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_should_poll = False

    def __init__(self, hass: HomeAssistant) -> None:
        """Read the tally straight out of hass.data.

        Held privately rather than on ``self.hass``, which the entity platform
        owns, so the counter can also be read before the entity is added.
        """
        self._hass = hass
        self._since = dt_util.utcnow()

    @property
    def _counts(self) -> dict[str, int]:
        return self._hass.data.get(DOMAIN, {}).get(REQUEST_COUNTS, {})

    @property
    def native_value(self) -> int:
        """Every upstream call this process has made."""
        return sum(self._counts.values())

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """The tally per feed, plus what the Trafiklab quota actually sees."""
        counts = self._counts
        # ul_departures goes to UL's own API, which has no published quota;
        # everything else is Trafiklab. not_modified is a tally of the rt_ calls
        # that came back empty, not a request of its own, so it is not summed.
        trafiklab = sum(
            value
            for key, value in counts.items()
            if key not in ("rt_not_modified", "ul_departures")
        )
        hours = max((dt_util.utcnow() - self._since).total_seconds() / 3600, 1 / 60)
        return {
            **counts,
            "trafiklab_total": trafiklab,
            "trafiklab_per_hour": round(trafiklab / hours, 1),
            "since": self._since,
        }
