"""Support for UL Transport sensors."""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    DOMAIN,
    CONF_STOP_NAME,
    TRAFFIC_TYPE_MAPPING,
    TRAFFIC_TYPE_ICONS,
    DEFAULT_ICON,
    ATTR_LINE_NAME,
    ATTR_LINE_ID,
    ATTR_TRANSPORT,
    ATTR_DIRECTION,
    ATTR_LATITUDE,
    ATTR_LONGITUDE,
    ATTR_AREA,
    ATTR_STOP_NAME,
    ATTR_LINE_COLOR,
    ATTR_TEXT_COLOR,
    LINE_COLORS,
    LIGHT_TEXT_LINES,
)
from .coordinator import ULTransportDataUpdateCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up UL Transport sensors from a config entry."""
    coordinator: ULTransportDataUpdateCoordinator = hass.data[DOMAIN][config_entry.entry_id]
    stop_name = config_entry.data[CONF_STOP_NAME]

    _LOGGER.debug(
        "Setting up sensors for stop %s (ID: %s)",
        stop_name,
        coordinator.stop_id,
    )

    # Create sensors for each unique line+direction combination
    sensors = []
    for key, departures in coordinator.data.items():
        if departures:
            sensors.append(ULTransportSensor(coordinator, key, stop_name))

    async_add_entities(sensors, True)

    # Register update listener to reload when options change
    config_entry.async_on_unload(config_entry.add_update_listener(update_listener))


async def update_listener(hass: HomeAssistant, config_entry: ConfigEntry) -> None:
    """Handle options update."""
    await hass.config_entries.async_reload(config_entry.entry_id)


class ULTransportSensor(CoordinatorEntity, SensorEntity):
    """Representation of a UL Transport sensor."""

    def __init__(
        self,
        coordinator: ULTransportDataUpdateCoordinator,
        key: str,
        stop_name: str,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self._key = key
        self._stop_name = stop_name

        # Get initial departure info for naming
        departures = coordinator.data.get(key, [])
        if departures:
            first_dep = departures[0]
            line_name = str(first_dep["line"]["name"])
            direction = first_dep["line"]["towards"]
            
            self._attr_name = f"{stop_name} Line {line_name} to {direction}"
            self._attr_unique_id = f"{coordinator.stop_id}_{key}"

    @property
    def state(self) -> str | None:
        """Return the state of the sensor."""
        departures = self.coordinator.data.get(self._key, [])
        if not departures:
            return None

        first_dep = departures[0]
        # Return real-time departure if available, otherwise planned
        return first_dep.get("realTimeDepartureDateTime") or first_dep["departureDateTime"]

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the state attributes."""
        departures = self.coordinator.data.get(self._key, [])
        if not departures:
            return {}

        first_dep = departures[0]
        line_info = first_dep["line"]
        
        # Map traffic type
        traffic_type = line_info.get("trafficType", 0)
        transport_mode = TRAFFIC_TYPE_MAPPING.get(traffic_type, "UNKNOWN")
        
        # Get line color from mapping
        line_name = str(line_info["name"])
        line_color = LINE_COLORS.get(line_name, "#53565a")  # Default to dark gray
        text_color = "#000000" if line_name in LIGHT_TEXT_LINES else "#ffffff"

        attributes = {
            ATTR_LINE_NAME: line_name,
            ATTR_LINE_ID: f"{line_info.get('lineNo', 0)}",
            ATTR_TRANSPORT: transport_mode,
            ATTR_DIRECTION: line_info["towards"],
            ATTR_LATITUDE: first_dep["coordinate"]["latitude"],
            ATTR_LONGITUDE: first_dep["coordinate"]["longitude"],
            ATTR_AREA: first_dep.get("area", ""),
            ATTR_STOP_NAME: self._stop_name,
            ATTR_LINE_COLOR: line_color,
            ATTR_TEXT_COLOR: text_color,
        }

        # Add up to 5 departure times
        for i, dep in enumerate(departures[:5]):
            suffix = "" if i == 0 else f"_{i}"
            attributes[f"planned_departure_time{suffix}"] = dep["departureDateTime"]
            attributes[f"estimated_departure_time{suffix}"] = dep.get("realTimeDepartureDateTime")

        # Fill remaining slots with None if less than 5 departures
        for i in range(len(departures), 5):
            if i > 0:  # Skip the first one as it's the state
                suffix = f"_{i}"
                attributes[f"planned_departure_time{suffix}"] = None
                attributes[f"estimated_departure_time{suffix}"] = None

        return attributes

    @property
    def icon(self) -> str:
        """Return the icon to use in the frontend."""
        departures = self.coordinator.data.get(self._key, [])
        if not departures:
            return DEFAULT_ICON

        traffic_type = departures[0]["line"].get("trafficType", 0)
        return TRAFFIC_TYPE_ICONS.get(traffic_type, DEFAULT_ICON)

    @property
    def available(self) -> bool:
        """Return True if entity is available."""
        return self.coordinator.last_update_success and bool(
            self.coordinator.data.get(self._key)
        )
