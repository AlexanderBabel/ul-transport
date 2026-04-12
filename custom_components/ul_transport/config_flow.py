"""Config flow for UL Transport integration."""
from __future__ import annotations

import logging
from typing import Any

import aiohttp
import async_timeout
import voluptuous as vol

from homeassistant import config_entries
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import selector
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResult
from homeassistant.exceptions import HomeAssistantError

from .const import (
    DOMAIN,
    CONF_STOP_ID,
    CONF_STOP_NAME,
    CONF_SELECTED_LINES,
    CONF_SCAN_INTERVAL,
    DEFAULT_SCAN_INTERVAL,
    MIN_SCAN_INTERVAL,
    MAX_SCAN_INTERVAL,
    API_STOPS_SEARCH,
    API_STOP_DEPARTURES,
    API_TIMEOUT,
)

_LOGGER = logging.getLogger(__name__)


async def validate_stop(hass: HomeAssistant, stop_id: int) -> dict[str, Any]:
    """Validate the stop ID by fetching data from the API."""
    url = f"{API_STOP_DEPARTURES}/{stop_id}"
    
    try:
        async with async_timeout.timeout(API_TIMEOUT):
            async with aiohttp.ClientSession() as session:
                async with session.get(url) as response:
                    if response.status != 200:
                        raise CannotConnect
                    data = await response.json()
                    return {"stop_name": data.get("name", "Unknown Stop")}
    except Exception as err:
        _LOGGER.error(f"Error validating stop {stop_id}: {err}")
        raise CannotConnect


async def search_stops(hass: HomeAssistant, query: str) -> list[dict[str, Any]]:
    """Search for stops using the UL API."""
    url = f"{API_STOPS_SEARCH}?query={query}"
    
    try:
        async with async_timeout.timeout(API_TIMEOUT):
            async with aiohttp.ClientSession() as session:
                async with session.get(url) as response:
                    if response.status != 200:
                        raise CannotConnect
                    # Stop search API returns text/html but is actually JSON
                    data = await response.json(content_type=None)
                    
                    # Extract stops from response
                    stops = []
                    if isinstance(data, list):
                        for stop in data:
                            name = stop.get("name", "Unknown")
                            stop_type = stop.get("type", 0)
                            
                            # Type 0 = stop, 1 = address, 2 = POI
                            # Only include actual stops, not addresses or POIs
                            if stop_type != 0:
                                continue
                            
                            stops.append({
                                "id": stop.get("id"),
                                "name": name,
                            })
                    
                    return stops
    except Exception as err:
        _LOGGER.error(f"Error searching stops with query '{query}': {err}")
        raise CannotConnect


class ULTransportConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for UL Transport."""

    VERSION = 1

    @staticmethod
    def async_get_options_flow(config_entry: config_entries.ConfigEntry):
        """Get the options flow for this handler."""
        return ULTransportOptionsFlowHandler()

    def __init__(self):
        """Initialize the config flow."""
        self._stops = []
        self._stop_query = None
        self._stop_id = None
        self._stop_name = None
        self._available_lines = []

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the initial step where user enters stop name."""
        errors = {}

        if user_input is not None:
            self._stop_query = user_input["stop_query"]
            
            try:
                self._stops = await search_stops(self.hass, self._stop_query)
                
                if not self._stops:
                    errors["base"] = "no_stops_found"
                else:
                    return await self.async_step_select_stop()
                    
            except CannotConnect:
                errors["base"] = "cannot_connect"
            except Exception:  # pylint: disable=broad-except
                _LOGGER.exception("Unexpected exception")
                errors["base"] = "unknown"

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required("stop_query", default=""): str,
                }
            ),
            errors=errors,
        )

    async def async_step_select_stop(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the step where user selects a stop from search results."""
        errors = {}

        if user_input is not None:
            self._stop_id = int(user_input[CONF_STOP_ID])
            
            # Find the selected stop name
            self._stop_name = next(
                (stop["name"] for stop in self._stops if stop["id"] == self._stop_id),
                "Unknown Stop"
            )
            
            # Check if already configured
            await self.async_set_unique_id(str(self._stop_id))
            self._abort_if_unique_id_configured()
            
            try:
                # Fetch departures to get available lines
                url = f"{API_STOP_DEPARTURES}/{self._stop_id}"
                async with async_timeout.timeout(API_TIMEOUT):
                    async with aiohttp.ClientSession() as session:
                        async with session.get(url) as response:
                            if response.status != 200:
                                raise CannotConnect
                            data = await response.json()
                            self._stop_name = data.get("name", self._stop_name)
                            
                            # Extract unique line + direction combinations
                            lines_set = set()
                            for dep in data.get("departures", []):
                                line_name = str(dep["line"]["name"]).strip()
                                direction = dep["line"]["towards"].strip()
                                lines_set.add(f"{line_name}_{direction}")
                            
                            self._available_lines = sorted(list(lines_set))
                            
                            if not self._available_lines:
                                errors["base"] = "no_departures_found"
                            else:
                                return await self.async_step_select_lines()
                                
            except CannotConnect:
                errors["base"] = "cannot_connect"
            except Exception:  # pylint: disable=broad-except
                _LOGGER.exception("Unexpected exception")
                errors["base"] = "unknown"

        # Create dropdown options
        stop_options = {
            str(stop["id"]): stop['name']
            for stop in self._stops
        }

        return self.async_show_form(
            step_id="select_stop",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_STOP_ID): vol.In(stop_options),
                }
            ),
            errors=errors,
        )

    async def async_step_select_lines(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the step where user selects which lines to monitor."""
        errors = {}

        if user_input is not None:
            selected_lines = user_input.get("selected_lines", [])
            scan_interval = user_input.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
            
            # If empty list, monitor all lines
            if not selected_lines:
                selected_lines = self._available_lines
            
            return self.async_create_entry(
                title=self._stop_name,
                data={
                    CONF_STOP_ID: self._stop_id,
                    CONF_STOP_NAME: self._stop_name,
                    CONF_SELECTED_LINES: selected_lines,
                    CONF_SCAN_INTERVAL: scan_interval,
                },
            )

        # Create line options with readable names
        line_options = []
        for line_key in self._available_lines:
            parts = line_key.split("_", 1)
            if len(parts) == 2:
                line_name, direction = parts
                line_options.append(
                    selector.SelectOptionDict(
                        value=line_key,
                        label=f"Line {line_name} → {direction}"
                    )
                )
            else:
                line_options.append(
                    selector.SelectOptionDict(value=line_key, label=line_key)
                )

        return self.async_show_form(
            step_id="select_lines",
            data_schema=vol.Schema(
                {
                    vol.Optional("selected_lines", default=[]): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=line_options,
                            multiple=True,
                            mode=selector.SelectSelectorMode.DROPDOWN,
                        )
                    ),
                    vol.Optional(
                        CONF_SCAN_INTERVAL,
                        default=DEFAULT_SCAN_INTERVAL
                    ): vol.All(
                        vol.Coerce(int),
                        vol.Range(min=MIN_SCAN_INTERVAL, max=MAX_SCAN_INTERVAL)
                    ),
                }
            ),
            errors=errors,
        )


class ULTransportOptionsFlowHandler(config_entries.OptionsFlow):
    """Handle options flow for UL Transport."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Manage the options."""
        errors = {}

        if user_input is not None:
            selected_lines = user_input.get("selected_lines", [])
            scan_interval = user_input.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
            
            # Save empty list to monitor all lines (don't fetch specific lines)
            # This allows new lines to appear automatically
            return self.async_create_entry(
                title="",
                data={
                    CONF_SELECTED_LINES: selected_lines,
                    CONF_SCAN_INTERVAL: scan_interval,
                }
            )

        # Fetch available lines
        stop_id = self.config_entry.data[CONF_STOP_ID]
        try:
            url = f"{API_STOP_DEPARTURES}/{stop_id}"
            async with async_timeout.timeout(API_TIMEOUT):
                async with aiohttp.ClientSession() as session:
                    async with session.get(url) as response:
                        if response.status != 200:
                            raise CannotConnect
                        data = await response.json()
                        
                        lines_set = set()
                        for dep in data.get("departures", []):
                            line_name = str(dep["line"]["name"]).strip()
                            direction = dep["line"]["towards"].strip()
                            lines_set.add(f"{line_name}_{direction}")
                        
                        available_lines = sorted(list(lines_set))
        except CannotConnect:
            errors["base"] = "cannot_connect"
            available_lines = []
        except Exception:
            _LOGGER.exception("Unexpected exception")
            errors["base"] = "unknown"
            available_lines = []

        if not available_lines and not errors:
            errors["base"] = "no_departures_found"

        # Get current selection from options or data
        current_selection = self.config_entry.options.get(
            CONF_SELECTED_LINES, 
            self.config_entry.data.get(CONF_SELECTED_LINES, [])
        )
        
        # Merge current selection with available lines to allow deselecting old lines
        all_lines = set(available_lines)
        all_lines.update(current_selection)
        all_lines = sorted(list(all_lines))

        # Create line options with readable names
        line_options = []
        for line_key in all_lines:
            parts = line_key.split("_", 1)
            if len(parts) == 2:
                line_name, direction = parts
                # Mark lines that are no longer available
                if line_key not in available_lines:
                    label = f"Line {line_name} → {direction} (no departures)"
                else:
                    label = f"Line {line_name} → {direction}"
                line_options.append(
                    selector.SelectOptionDict(value=line_key, label=label)
                )
            else:
                line_options.append(
                    selector.SelectOptionDict(value=line_key, label=line_key)
                )

        # Get current scan interval
        current_scan_interval = self.config_entry.options.get(
            CONF_SCAN_INTERVAL,
            self.config_entry.data.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
        )

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        "selected_lines", 
                        default=current_selection
                    ): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=line_options,
                            multiple=True,
                            mode=selector.SelectSelectorMode.DROPDOWN,
                        )
                    ),
                    vol.Optional(
                        CONF_SCAN_INTERVAL,
                        default=current_scan_interval
                    ): vol.All(
                        vol.Coerce(int),
                        vol.Range(min=MIN_SCAN_INTERVAL, max=MAX_SCAN_INTERVAL)
                    ),
                }
            ),
            errors=errors,
        )


class CannotConnect(HomeAssistantError):
    """Error to indicate we cannot connect."""
