"""Departure sensors for UL Transport.

Shaped for automations rather than for a departure board: the state of every
sensor is "minutes until the next bus", which a numeric_state trigger can use
directly, with the timestamps alongside it as attributes. The live map card
draws its own data over the websocket and needs none of these.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .const import (
    DOMAIN,
    CONF_STOP_NAME,
    TRAFFIC_TYPE_MAPPING,
    TRAFFIC_TYPE_ICONS,
    DEFAULT_ICON,
)
from .coordinator import ULTransportDataUpdateCoordinator

_LOGGER = logging.getLogger(__name__)

# Suffixes this integration currently creates. Anything else on the entry is a
# leftover from the departures-card sensors, which no longer exist.
CURRENT_SUFFIXES = ("_in", "_next_departure", "_last_update")


def _parse(value: str | None) -> datetime | None:
    """One of UL's timestamps as an aware datetime."""
    if not value:
        return None
    parsed = dt_util.parse_datetime(value)
    # UL sends UTC with a trailing Z; a naive one is read as local time.
    return dt_util.as_utc(parsed) if parsed else None


def _departure(departure: dict[str, Any]) -> datetime | None:
    """When this bus actually leaves: real time if there is any, else planned."""
    return _parse(
        departure.get("realTimeDepartureDateTime")
    ) or _parse(departure.get("departureDateTime"))


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
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up UL Transport sensors from a config entry."""
    coordinator: ULTransportDataUpdateCoordinator = hass.data[DOMAIN][config_entry.entry_id]
    stop_name = config_entry.data[CONF_STOP_NAME]

    sensors: list[SensorEntity] = [
        ULNextDepartureSensor(coordinator, stop_name),
        ULTransportLastUpdateSensor(coordinator, stop_name),
    ]
    sensors += [
        ULLineDepartureSensor(coordinator, key, stop_name)
        for key, departures in coordinator.data.items()
        if departures
    ]

    _prune_removed_entities(hass, config_entry)
    async_add_entities(sensors, True)

    config_entry.async_on_unload(config_entry.add_update_listener(update_listener))


def _prune_removed_entities(hass: HomeAssistant, config_entry: ConfigEntry) -> None:
    """Drop registry entries for sensors this integration no longer creates.

    Matched on the id shape rather than on what exists right now, so a line that
    happens not to be running at restart keeps its entity and its history.
    """
    registry = er.async_get(hass)
    for entry in er.async_entries_for_config_entry(registry, config_entry.entry_id):
        if entry.domain == "sensor" and not entry.unique_id.endswith(CURRENT_SUFFIXES):
            _LOGGER.debug("Removing obsolete sensor %s", entry.entity_id)
            registry.async_remove(entry.entity_id)


async def update_listener(hass: HomeAssistant, config_entry: ConfigEntry) -> None:
    """Handle options update."""
    await hass.config_entries.async_reload(config_entry.entry_id)


class _ULDepartureSensor(CoordinatorEntity, SensorEntity):
    """Minutes until the next departure, with the times as attributes."""

    _attr_device_class = SensorDeviceClass.DURATION
    _attr_native_unit_of_measurement = UnitOfTime.MINUTES

    def _departures(self) -> list[dict[str, Any]]:
        """Upcoming departures for this sensor, earliest first."""
        raise NotImplementedError

    @property
    def native_value(self) -> int | None:
        """Minutes until the next departure."""
        upcoming = self._departures()
        return _minutes_until(_departure(upcoming[0])) if upcoming else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
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
        upcoming = self._departures()
        if not upcoming:
            return DEFAULT_ICON
        traffic_type = upcoming[0]["line"].get("trafficType", 0)
        return TRAFFIC_TYPE_ICONS.get(traffic_type, DEFAULT_ICON)


class ULLineDepartureSensor(_ULDepartureSensor):
    """One line in one direction from one stop."""

    def __init__(
        self,
        coordinator: ULTransportDataUpdateCoordinator,
        key: str,
        stop_name: str,
    ) -> None:
        super().__init__(coordinator)
        self._key = key
        self._stop_name = stop_name

        line_name, _, direction = key.partition("_")
        self._attr_name = f"{stop_name} Line {line_name} to {direction}"
        self._attr_unique_id = f"{coordinator.stop_id}_{key}_in"

    def _departures(self) -> list[dict[str, Any]]:
        return self.coordinator.data.get(self._key) or []

    @property
    def available(self) -> bool:
        return self.coordinator.last_update_success and bool(self._departures())


class ULNextDepartureSensor(_ULDepartureSensor):
    """The next bus from this stop, whichever line it is.

    The one sensor most automations want: "leave now" does not care which line
    turns up, only that something does.
    """

    def __init__(
        self,
        coordinator: ULTransportDataUpdateCoordinator,
        stop_name: str,
    ) -> None:
        super().__init__(coordinator)
        self._stop_name = stop_name
        self._attr_name = f"{stop_name} Next departure"
        self._attr_unique_id = f"{coordinator.stop_id}_next_departure"

    def _departures(self) -> list[dict[str, Any]]:
        merged = [
            departure
            for group in (self.coordinator.data or {}).values()
            for departure in group
        ]
        return sorted(
            (d for d in merged if _departure(d) is not None), key=_departure
        )

    @property
    def available(self) -> bool:
        return self.coordinator.last_update_success and bool(self._departures())


class ULTransportLastUpdateSensor(CoordinatorEntity, SensorEntity):
    """Sensor reporting the timestamp of the last successful API fetch."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_icon = "mdi:clock-check-outline"

    def __init__(
        self,
        coordinator: ULTransportDataUpdateCoordinator,
        stop_name: str,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self._attr_name = f"{stop_name} Last Update"
        self._attr_unique_id = f"{coordinator.stop_id}_last_update"

    @property
    def native_value(self):
        """Return the last successful update time."""
        return self.coordinator.last_successful_update

    @property
    def available(self) -> bool:
        """Return True once at least one fetch has succeeded."""
        return self.coordinator.last_successful_update is not None
