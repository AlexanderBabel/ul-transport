"""Support for UL Transport buttons."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .coordinator import ULTransportConfigEntry, ULTransportDataUpdateCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ULTransportConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up UL Transport button from a config entry."""
    async_add_entities([ULTransportRefreshButton(config_entry.runtime_data)])


class ULTransportRefreshButton(
    CoordinatorEntity[ULTransportDataUpdateCoordinator], ButtonEntity
):
    """Button to manually trigger a data refresh."""

    _attr_has_entity_name = True
    _attr_icon = "mdi:refresh"
    _attr_translation_key = "refresh"

    def __init__(self, coordinator: ULTransportDataUpdateCoordinator) -> None:
        """Initialize the button."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.stop_id}_refresh"
        self._attr_device_info = coordinator.device_info

    async def async_press(self) -> None:
        """Handle the button press."""
        await self.coordinator.async_request_refresh()
