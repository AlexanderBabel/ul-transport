"""Config flow for UL Transport integration."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import aiohttp
from homeassistant import config_entries
from homeassistant.config_entries import ConfigFlowResult
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession
import voluptuous as vol

from .const import (
    API_STOP_DEPARTURES,
    API_STOPS_SEARCH,
    API_TIMEOUT,
    CONF_GTFS_REALTIME_KEY,
    CONF_GTFS_STATIC_KEY,
    CONF_SCAN_INTERVAL,
    CONF_SELECTED_LINES,
    CONF_STOP_ID,
    CONF_STOP_NAME,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    MAX_SCAN_INTERVAL,
    MIN_SCAN_INTERVAL,
)

_LOGGER = logging.getLogger(__name__)


async def _async_get_json(hass: HomeAssistant, url: str, **kwargs: Any) -> Any:
    """One UL API call. Anything that is not a usable body is CannotConnect."""
    session = async_get_clientsession(hass)
    try:
        async with asyncio.timeout(API_TIMEOUT):
            async with session.get(url, **kwargs) as response:
                if response.status != 200:
                    raise CannotConnect(f"HTTP {response.status} from {url}")
                # The stop search returns text/html but is actually JSON.
                return await response.json(content_type=None)
    except CannotConnect:
        raise
    except (TimeoutError, aiohttp.ClientError, ValueError) as err:
        raise CannotConnect(f"Could not reach the UL API: {err}") from err


async def async_search_stops(hass: HomeAssistant, query: str) -> list[dict[str, Any]]:
    """Search for stops using the UL API.

    Type 0 is an actual stop; 1 is an address and 2 a point of interest, and
    neither of those has a departure board or a GTFS counterpart.
    """
    data = await _async_get_json(hass, API_STOPS_SEARCH, params={"query": query})
    if not isinstance(data, list):
        return []
    return [
        {"id": stop.get("id"), "name": stop.get("name", "Unknown")}
        for stop in data
        if stop.get("type", 0) == 0
    ]


async def async_get_board(hass: HomeAssistant, stop_id: int) -> tuple[str, list[str]]:
    """The stop's name and the "line_towards" keys currently departing from it."""
    data = await _async_get_json(hass, f"{API_STOP_DEPARTURES}/{stop_id}")
    keys = {
        f"{str(dep['line']['name']).strip()}_{dep['line']['towards'].strip()}"
        for dep in data.get("departures", [])
    }
    return data.get("name", ""), sorted(keys)


def _line_options(
    keys: list[str], unavailable: set[str] | None = None
) -> list[selector.SelectOptionDict]:
    """Readable labels for "line_towards" keys, marking ones with no departures."""
    options = []
    for key in keys:
        line_name, separator, direction = key.partition("_")
        if not separator:
            options.append(selector.SelectOptionDict(value=key, label=key))
            continue
        label = f"Line {line_name} → {direction}"
        if unavailable and key in unavailable:
            label = f"{label} (no departures)"
        options.append(selector.SelectOptionDict(value=key, label=label))
    return options


def _scan_interval_selector() -> Any:
    """Validator for the poll interval, shared by both flows."""
    return vol.All(
        vol.Coerce(int), vol.Range(min=MIN_SCAN_INTERVAL, max=MAX_SCAN_INTERVAL)
    )


class ULTransportConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for UL Transport."""

    VERSION = 1

    @staticmethod
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> ULTransportOptionsFlowHandler:
        """Get the options flow for this handler."""
        return ULTransportOptionsFlowHandler()

    def __init__(self) -> None:
        """Initialize the config flow."""
        self._stops: list[dict[str, Any]] = []
        self._stop_id: int | None = None
        self._stop_name: str = ""
        self._available_lines: list[str] = []

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the initial step where user enters stop name."""
        errors: dict[str, str] = {}

        if user_input is not None:
            try:
                self._stops = await async_search_stops(self.hass, user_input["stop_query"])
            except CannotConnect as err:
                _LOGGER.debug("Stop search failed: %s", err)
                errors["base"] = "cannot_connect"
            except Exception:
                _LOGGER.exception("Unexpected error searching for stops")
                errors["base"] = "unknown"
            else:
                if not self._stops:
                    errors["base"] = "no_stops_found"
                else:
                    return await self.async_step_select_stop()

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({vol.Required("stop_query", default=""): str}),
            errors=errors,
        )

    async def async_step_select_stop(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the step where user selects a stop from search results."""
        errors: dict[str, str] = {}

        if user_input is not None:
            self._stop_id = int(user_input[CONF_STOP_ID])
            self._stop_name = next(
                (stop["name"] for stop in self._stops if stop["id"] == self._stop_id),
                "Unknown Stop",
            )

            await self.async_set_unique_id(str(self._stop_id))
            self._abort_if_unique_id_configured()

            try:
                name, self._available_lines = await async_get_board(
                    self.hass, self._stop_id
                )
            except CannotConnect as err:
                _LOGGER.debug("Could not read the board for %s: %s", self._stop_id, err)
                errors["base"] = "cannot_connect"
            except Exception:
                _LOGGER.exception("Unexpected error reading the departure board")
                errors["base"] = "unknown"
            else:
                self._stop_name = name or self._stop_name
                if not self._available_lines:
                    errors["base"] = "no_departures_found"
                else:
                    return await self.async_step_select_lines()

        return self.async_show_form(
            step_id="select_stop",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_STOP_ID): vol.In(
                        {str(stop["id"]): stop["name"] for stop in self._stops}
                    )
                }
            ),
            errors=errors,
        )

    async def async_step_select_lines(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the step where user selects which lines to monitor."""
        if user_input is not None:
            # Empty means every line, and stays empty: pinning it to the lines
            # running at setup time means a stop added at midnight never gets a
            # sensor for the line that only runs in the morning.
            return self.async_create_entry(
                title=self._stop_name,
                data={
                    CONF_STOP_ID: self._stop_id,
                    CONF_STOP_NAME: self._stop_name,
                    CONF_SELECTED_LINES: user_input.get(CONF_SELECTED_LINES, []),
                    CONF_SCAN_INTERVAL: user_input.get(
                        CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL
                    ),
                },
            )

        return self.async_show_form(
            step_id="select_lines",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        CONF_SELECTED_LINES, default=[]
                    ): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=_line_options(self._available_lines),
                            multiple=True,
                            mode=selector.SelectSelectorMode.DROPDOWN,
                        )
                    ),
                    vol.Optional(
                        CONF_SCAN_INTERVAL, default=DEFAULT_SCAN_INTERVAL
                    ): _scan_interval_selector(),
                }
            ),
        )


def _map_keys_entry(hass: HomeAssistant) -> config_entries.ConfigEntry | None:
    """The one stop that holds the Trafiklab keys, if any stop does.

    They unlock the same account-wide feeds whichever stop they are typed on,
    so they are asked for once and edited where they were entered rather than
    copied onto every stop. The first entry holding them keeps them, so two
    stops that both have keys today do not hide the fields from each other.
    """
    for entry in hass.config_entries.async_entries(DOMAIN):
        static = entry.options.get(CONF_GTFS_STATIC_KEY) or entry.data.get(
            CONF_GTFS_STATIC_KEY
        )
        realtime = entry.options.get(CONF_GTFS_REALTIME_KEY) or entry.data.get(
            CONF_GTFS_REALTIME_KEY
        )
        if static and realtime:
            return entry
    return None


class ULTransportOptionsFlowHandler(config_entries.OptionsFlow):
    """Handle options flow for UL Transport."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage the options."""
        errors: dict[str, str] = {}

        holder = _map_keys_entry(self.hass)
        elsewhere = (
            holder if holder and holder.entry_id != self.config_entry.entry_id else None
        )

        def current(key: str, fallback: Any) -> Any:
            return self.config_entry.options.get(
                key, self.config_entry.data.get(key, fallback)
            )

        if user_input is not None:
            options = {
                CONF_SELECTED_LINES: user_input.get(CONF_SELECTED_LINES, []),
                CONF_SCAN_INTERVAL: user_input.get(
                    CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL
                ),
            }
            # Only this entry's own keys are editable here; when another entry
            # holds them the fields are not shown, and writing "" would blank
            # what that entry stored.
            if elsewhere is None:
                options[CONF_GTFS_STATIC_KEY] = user_input.get(CONF_GTFS_STATIC_KEY, "")
                options[CONF_GTFS_REALTIME_KEY] = user_input.get(
                    CONF_GTFS_REALTIME_KEY, ""
                )
            return self.async_create_entry(title="", data=options)

        try:
            _, available_lines = await async_get_board(
                self.hass, self.config_entry.data[CONF_STOP_ID]
            )
        except CannotConnect as err:
            _LOGGER.debug("Could not read the board: %s", err)
            errors["base"] = "cannot_connect"
            available_lines = []
        except Exception:
            _LOGGER.exception("Unexpected error reading the departure board")
            errors["base"] = "unknown"
            available_lines = []

        if not available_lines and not errors:
            errors["base"] = "no_departures_found"

        current_selection = current(CONF_SELECTED_LINES, [])
        # Merged so a line that has stopped running can still be deselected.
        all_lines = sorted(set(available_lines) | set(current_selection))

        schema: dict[Any, Any] = {
            vol.Optional(
                CONF_SELECTED_LINES, default=current_selection
            ): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=_line_options(
                        all_lines, unavailable=set(all_lines) - set(available_lines)
                    ),
                    multiple=True,
                    mode=selector.SelectSelectorMode.DROPDOWN,
                )
            ),
            vol.Optional(
                CONF_SCAN_INTERVAL,
                default=current(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
            ): _scan_interval_selector(),
        }
        # Both keys are needed for the live map; leave blank to skip it and keep
        # departure sensors only. They are account-wide, so they are asked for
        # on one stop only - whichever one has them is where they are edited.
        if elsewhere is None:
            schema[
                vol.Optional(
                    CONF_GTFS_STATIC_KEY, default=current(CONF_GTFS_STATIC_KEY, "")
                )
            ] = str
            schema[
                vol.Optional(
                    CONF_GTFS_REALTIME_KEY, default=current(CONF_GTFS_REALTIME_KEY, "")
                )
            ] = str

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(schema),
            errors=errors,
            description_placeholders={
                "keys": (
                    f"The live map uses the Trafiklab keys set on {elsewhere.title}."
                    if elsewhere is not None
                    else ""
                )
            },
        )


class CannotConnect(HomeAssistantError):
    """Error to indicate we cannot connect."""
