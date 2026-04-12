"""Data update coordinator for UL Transport."""
from __future__ import annotations

from datetime import timedelta
import logging
from typing import Any

import aiohttp
import async_timeout

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import API_STOP_DEPARTURES, API_TIMEOUT, DOMAIN

_LOGGER = logging.getLogger(__name__)


class ULTransportDataUpdateCoordinator(DataUpdateCoordinator):
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

        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_{stop_id}",
            update_interval=timedelta(seconds=scan_interval),
        )

    async def _async_update_data(self) -> dict[str, list[dict[str, Any]]]:
        """Fetch data from API."""
        url = f"{API_STOP_DEPARTURES}/{self.stop_id}"

        try:
            async with async_timeout.timeout(API_TIMEOUT):
                async with aiohttp.ClientSession() as session:
                    async with session.get(url) as response:
                        if response.status == 429:
                            raise UpdateFailed(
                                "UL API rate limit exceeded. Please wait a few minutes before retrying."
                            )
                        if response.status != 200:
                            raise UpdateFailed(
                                f"Error communicating with API: {response.status}"
                            )
                        data = await response.json()

            # Group departures by line+direction
            grouped: dict[str, list[dict[str, Any]]] = {}
            all_keys = []

            for dep in data.get("departures", []):
                line_name = str(dep["line"]["name"]).strip()
                direction = dep["line"]["towards"].strip()
                key = f"{line_name}_{direction}"
                all_keys.append(key)

                # Filter by selected lines if configured
                if self.selected_lines and key not in self.selected_lines:
                    continue

                if key not in grouped:
                    grouped[key] = []

                grouped[key].append(dep)

            if self.selected_lines:
                _LOGGER.debug(
                    f"Stop {self.stop_name}: Found {len(set(all_keys))} unique lines from API, "
                    f"filtered to {len(grouped)} based on selection. "
                    f"API keys: {sorted(set(all_keys))}, "
                    f"Selected: {sorted(self.selected_lines)}"
                )
            else:
                _LOGGER.debug(f"Grouped {len(grouped)} line combinations for stop {self.stop_name}")

            # Sort each group by departure time and limit to 5
            for key in grouped:
                grouped[key] = sorted(
                    grouped[key],
                    key=lambda x: x.get("realTimeDepartureDateTime") or x["departureDateTime"],
                )[:5]

            return grouped

        except aiohttp.ClientError as err:
            raise UpdateFailed(f"Error communicating with API: {err}") from err
        except Exception as err:
            raise UpdateFailed(f"Unexpected error: {err}") from err
