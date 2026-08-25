"""Config flow for UL Transport integration.

Two kinds of entry live in this domain. One holds the Trafiklab keys for the
whole install; the rest are stops. Home Assistant creates one entry per flow, so
the keys and a stop cannot be set up in a single pass - instead the first run
offers a menu, and once the keys have an entry of their own the menu stops
appearing and every later run goes straight to the stop search.
"""

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
    CONF_ENTRY_TYPE,
    CONF_GTFS_REALTIME_KEY,
    CONF_GTFS_STATIC_KEY,
    CONF_SCAN_INTERVAL,
    CONF_SELECTED_LINES,
    CONF_STOP_ID,
    CONF_STOP_NAME,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    ENTRY_TYPE_STOP,
    ENTRY_TYPE_TRAFIKLAB,
    GTFS_RT_URL,
    GTFS_STATIC_URL,
    MAX_SCAN_INTERVAL,
    MIN_SCAN_INTERVAL,
    TRAFIKLAB_TITLE,
    TRAFIKLAB_UNIQUE_ID,
)
from .coordinator import trafiklab_entry
from .quota import count_request

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


async def async_validate_keys(hass: HomeAssistant, static: str, realtime: str) -> None:
    """Check both keys against Trafiklab before writing them to an entry.

    A key typed wrong otherwise fails much later and much more quietly - the
    sensors carry on working and the map simply never appears.

    The static feed is ~26 MB, so it is checked on the response status alone and
    the body is never read. Both calls are counted: Trafiklab counts them.
    """
    session = async_get_clientsession(hass)

    async def check(url: str, key: str, field: str, counter: str) -> None:
        try:
            async with asyncio.timeout(API_TIMEOUT):
                async with session.get(url, params={"key": key}) as response:
                    count_request(hass, counter)
                    # Trafiklab answers a bad or unsubscribed key with 401/403.
                    if response.status in (401, 403):
                        raise InvalidKey(field)
                    if response.status >= 400:
                        raise CannotConnect(f"HTTP {response.status} from {url}")
        except (TimeoutError, aiohttp.ClientError) as err:
            raise CannotConnect(f"Could not reach Trafiklab: {err}") from err

    await check(GTFS_STATIC_URL, static, CONF_GTFS_STATIC_KEY, "gtfs_static")
    await check(
        GTFS_RT_URL.format(feed="TripUpdates"),
        realtime,
        CONF_GTFS_REALTIME_KEY,
        "rt_TripUpdates",
    )


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


def _keys_schema(static: str = "", realtime: str = "") -> vol.Schema:
    """The two Trafiklab keys, asked for in one place and only one place."""
    return vol.Schema(
        {
            vol.Required(CONF_GTFS_STATIC_KEY, default=static): str,
            vol.Required(CONF_GTFS_REALTIME_KEY, default=realtime): str,
        }
    )


class ULTransportConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for UL Transport."""

    VERSION = 1

    @staticmethod
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> config_entries.OptionsFlow:
        """Keys and stops are configured separately, so they get their own flows."""
        if config_entry.data.get(CONF_ENTRY_TYPE) == ENTRY_TYPE_TRAFIKLAB:
            return ULTrafiklabOptionsFlowHandler()
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
        """Offer the live map first, but only while it has nowhere to live.

        Once the keys have an entry, the only thing left to add is a stop, so
        asking again would be a dead end.
        """
        if trafiklab_entry(self.hass) is not None:
            return await self.async_step_stop()
        return self.async_show_menu(step_id="user", menu_options=["trafiklab", "stop"])

    async def async_step_trafiklab(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Set up the live map: two Trafiklab keys, entered once for the install."""
        await self.async_set_unique_id(TRAFIKLAB_UNIQUE_ID)
        self._abort_if_unique_id_configured(error="already_configured_trafiklab")

        errors: dict[str, str] = {}
        if user_input is not None:
            static = user_input[CONF_GTFS_STATIC_KEY].strip()
            realtime = user_input[CONF_GTFS_REALTIME_KEY].strip()
            try:
                await async_validate_keys(self.hass, static, realtime)
            except InvalidKey as err:
                errors[err.field] = "invalid_key"
            except CannotConnect as err:
                _LOGGER.debug("Trafiklab key check failed: %s", err)
                errors["base"] = "cannot_connect"
            except Exception:
                _LOGGER.exception("Unexpected error checking the Trafiklab keys")
                errors["base"] = "unknown"
            else:
                return self.async_create_entry(
                    title=TRAFIKLAB_TITLE,
                    data={
                        CONF_ENTRY_TYPE: ENTRY_TYPE_TRAFIKLAB,
                        CONF_GTFS_STATIC_KEY: static,
                        CONF_GTFS_REALTIME_KEY: realtime,
                    },
                )

        return self.async_show_form(
            step_id="trafiklab",
            data_schema=_keys_schema(
                (user_input or {}).get(CONF_GTFS_STATIC_KEY, ""),
                (user_input or {}).get(CONF_GTFS_REALTIME_KEY, ""),
            ),
            errors=errors,
        )

    async def async_step_import(self, import_data: dict[str, Any]) -> ConfigFlowResult:
        """Adopt keys that an older version left on a stop entry.

        Not validated: they were working a moment ago, and a Trafiklab outage
        during a restart must not be what loses them.
        """
        await self.async_set_unique_id(TRAFIKLAB_UNIQUE_ID)
        self._abort_if_unique_id_configured()
        return self.async_create_entry(
            title=TRAFIKLAB_TITLE,
            data={
                CONF_ENTRY_TYPE: ENTRY_TYPE_TRAFIKLAB,
                CONF_GTFS_STATIC_KEY: import_data[CONF_GTFS_STATIC_KEY],
                CONF_GTFS_REALTIME_KEY: import_data[CONF_GTFS_REALTIME_KEY],
            },
        )

    async def async_step_stop(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the step where user enters stop name."""
        errors: dict[str, str] = {}

        if user_input is not None:
            try:
                self._stops = await async_search_stops(
                    self.hass, user_input["stop_query"]
                )
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
            step_id="stop",
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
                    CONF_ENTRY_TYPE: ENTRY_TYPE_STOP,
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


class ULTrafiklabOptionsFlowHandler(config_entries.OptionsFlow):
    """Edit the Trafiklab keys - the one place they are ever entered."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Hand straight over to the keys step.

        Options flows have to start at ``init``, and the stop flow already uses
        that id for its own strings; a separate step keeps the two sets apart.
        """
        return await self.async_step_keys(user_input)

    async def async_step_keys(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage the keys."""
        errors: dict[str, str] = {}

        def current(key: str) -> str:
            return self.config_entry.options.get(
                key, self.config_entry.data.get(key, "")
            )

        if user_input is not None:
            static = user_input[CONF_GTFS_STATIC_KEY].strip()
            realtime = user_input[CONF_GTFS_REALTIME_KEY].strip()
            try:
                await async_validate_keys(self.hass, static, realtime)
            except InvalidKey as err:
                errors[err.field] = "invalid_key"
            except CannotConnect as err:
                _LOGGER.debug("Trafiklab key check failed: %s", err)
                errors["base"] = "cannot_connect"
            except Exception:
                _LOGGER.exception("Unexpected error checking the Trafiklab keys")
                errors["base"] = "unknown"
            else:
                return self.async_create_entry(
                    title="",
                    data={
                        CONF_GTFS_STATIC_KEY: static,
                        CONF_GTFS_REALTIME_KEY: realtime,
                    },
                )

        return self.async_show_form(
            step_id="keys",
            data_schema=_keys_schema(
                current(CONF_GTFS_STATIC_KEY), current(CONF_GTFS_REALTIME_KEY)
            ),
            errors=errors,
        )


class ULTransportOptionsFlowHandler(config_entries.OptionsFlow):
    """Handle options flow for one stop."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage the options."""
        errors: dict[str, str] = {}

        def current(key: str, fallback: Any) -> Any:
            return self.config_entry.options.get(
                key, self.config_entry.data.get(key, fallback)
            )

        if user_input is not None:
            return self.async_create_entry(
                title="",
                data={
                    CONF_SELECTED_LINES: user_input.get(CONF_SELECTED_LINES, []),
                    CONF_SCAN_INTERVAL: user_input.get(
                        CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL
                    ),
                },
            )

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

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        CONF_SELECTED_LINES, default=current_selection
                    ): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=_line_options(
                                all_lines,
                                unavailable=set(all_lines) - set(available_lines),
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
            ),
            errors=errors,
        )


class CannotConnect(HomeAssistantError):
    """Error to indicate we cannot connect."""


class InvalidKey(HomeAssistantError):
    """A Trafiklab key the API refused. ``field`` names which of the two."""

    def __init__(self, field: str) -> None:
        """Remember which field to hang the error on."""
        super().__init__(f"Trafiklab rejected {field}")
        self.field = field
