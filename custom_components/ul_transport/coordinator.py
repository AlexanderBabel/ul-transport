"""Data update coordinator for UL Transport."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
import logging
from typing import Any

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .const import API_STOP_DEPARTURES, API_TIMEOUT, DOMAIN, count_request

_LOGGER = logging.getLogger(__name__)

type ULTransportConfigEntry = ConfigEntry[ULTransportDataUpdateCoordinator]

# Enough for "and the one after that?" without keeping a timetable in memory.
MAX_DEPARTURES_PER_LINE = 5


class ULTransportDataUpdateCoordinator(DataUpdateCoordinator[dict[str, list[dict[str, Any]]]]):
    """Class to manage fetching UL Transport data."""

    def __init__(
        self,
        hass: HomeAssistant,
        stop_id: int,
        stop_name: str,
        selected_lines: list[str],
        scan_interval: int,
    ) -> None:
        """Initialize."""
        self.stop_id = stop_id
        self.stop_name = stop_name
        self.selected_lines = selected_lines
        self.last_successful_update: datetime | None = None
        # Every "line_towards" UL advertises here, before the selection filter:
        # the map names its directions from these and shows lines the sensors
        # were never asked about.
        self.board_keys: list[str] = []

        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_{stop_id}",
            update_interval=timedelta(seconds=scan_interval),
        )

    @property
    def device_info(self) -> DeviceInfo:
        """The stop, as the device every entity for it hangs off."""
        return DeviceInfo(
            identifiers={(DOMAIN, str(self.stop_id))},
            name=self.stop_name,
            manufacturer="Upplands Lokaltrafik",
            model="Departure board",
            entry_type=DeviceEntryType.SERVICE,
            configuration_url=f"https://www.ul.se/hallplats/{self.stop_id}",
        )

    async def _async_update_data(self) -> dict[str, list[dict[str, Any]]]:
        """Fetch data from API."""
        url = f"{API_STOP_DEPARTURES}/{self.stop_id}"
        session = async_get_clientsession(self.hass)

        try:
            async with asyncio.timeout(API_TIMEOUT):
                async with session.get(url) as response:
                    count_request(self.hass, "ul_departures")
                    if response.status == 429:
                        raise UpdateFailed(
                            "UL API rate limit exceeded. Please wait a few "
                            "minutes before retrying."
                        )
                    if response.status != 200:
                        raise UpdateFailed(
                            f"Error communicating with API: {response.status}"
                        )
                    data = await response.json()
        except TimeoutError as err:
            raise UpdateFailed(f"Timeout fetching departures for {self.stop_name}") from err
        except aiohttp.ClientError as err:
            raise UpdateFailed(f"Error communicating with API: {err}") from err

        return self._group(data)

    def _group(self, data: Any) -> dict[str, list[dict[str, Any]]]:
        """Group the response by line+direction, keeping only selected lines.

        Separate from the fetch so a malformed payload fails as UpdateFailed
        rather than as a KeyError escaping the coordinator: this is the trust
        boundary with a third-party API that has no schema guarantee.
        """
        grouped: dict[str, list[dict[str, Any]]] = {}
        all_keys: list[str] = []

        try:
            for dep in data.get("departures", []):
                line_name = str(dep["line"]["name"]).strip()
                direction = dep["line"]["towards"].strip()
                key = f"{line_name}_{direction}"
                all_keys.append(key)

                if self.selected_lines and key not in self.selected_lines:
                    continue

                grouped.setdefault(key, []).append(dep)

            self.board_keys = sorted(set(all_keys))

            # Sort each group by departure time and keep the soonest few.
            for key, departures in grouped.items():
                grouped[key] = sorted(
                    departures,
                    key=lambda x: x.get("realTimeDepartureDateTime")
                    or x["departureDateTime"],
                )[:MAX_DEPARTURES_PER_LINE]
        except (AttributeError, KeyError, TypeError) as err:
            raise UpdateFailed(f"Unexpected API response shape: {err}") from err

        if self.selected_lines:
            _LOGGER.debug(
                "Stop %s: found %d unique lines from API, filtered to %d based on "
                "selection. API keys: %s, selected: %s",
                self.stop_name,
                len(set(all_keys)),
                len(grouped),
                self.board_keys,
                sorted(self.selected_lines),
            )
        else:
            _LOGGER.debug(
                "Grouped %d line combinations for stop %s", len(grouped), self.stop_name
            )

        self.last_successful_update = dt_util.utcnow()
        return grouped


def async_coordinators(hass: HomeAssistant) -> list[ULTransportDataUpdateCoordinator]:
    """Every stop that is currently set up.

    Config entries are the register of what exists; hass.data holds only the
    state the stops share (the map runtime, the request tally).
    """
    return [
        entry.runtime_data
        for entry in hass.config_entries.async_loaded_entries(DOMAIN)
    ]
