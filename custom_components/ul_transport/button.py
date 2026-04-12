"""Support for UL Transport buttons."""
from __future__ import annotations

import logging

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_STOP_NAME, DOMAIN
from .coordinator import ULTransportDataUpdateCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up UL Transport button from a config entry."""
    coordinator: ULTransportDataUpdateCoordinator = hass.data[DOMAIN][config_entry.entry_id]
    stop_name = config_entry.data[CONF_STOP_NAME]

    async_add_entities([ULTransportRefreshButton(coordinator, stop_name)])


class ULTransportRefreshButton(CoordinatorEntity, ButtonEntity):
    """Button to manually trigger a data refresh."""

    def __init__(
        self,
        coordinator: ULTransportDataUpdateCoordinator,
        stop_name: str,
    ) -> None:
        """Initialize the button."""
        super().__init__(coordinator)
        self._attr_name = f"{stop_name} Refresh"
        self._attr_unique_id = f"{coordinator.stop_id}_refresh"
        self._attr_icon = "mdi:refresh"

    async def async_press(self) -> None:
        """Handle the button press."""
        await self.coordinator.async_request_refresh()
