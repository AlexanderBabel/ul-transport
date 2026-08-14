"""The UL Transport integration."""
from __future__ import annotations

import asyncio
from datetime import timedelta
import logging
from pathlib import Path

from homeassistant.components.frontend import add_extra_js_url
from homeassistant.components.http import StaticPathConfig
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.event import async_track_time_interval

from .const import (
    DOMAIN,
    CONF_GTFS_REALTIME_KEY,
    CONF_GTFS_STATIC_KEY,
    CONF_STOP_ID,
    CONF_STOP_NAME,
    CONF_SELECTED_LINES,
    CONF_SCAN_INTERVAL,
    DEFAULT_SCAN_INTERVAL,
)
from .coordinator import ULTransportDataUpdateCoordinator
from .gtfs import async_load_index
from .llm_tool import (
    async_register as async_register_llm_api,
    async_unregister as async_unregister_llm_api,
)
from .live import LiveFeed, async_register_websocket

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SENSOR, Platform.BUTTON]

CARD_PATH = Path(__file__).parent / "www" / "ul-transport-map.js"
INDEX_REFRESH = timedelta(hours=6)


def _map_keys(hass: HomeAssistant) -> tuple[str, str] | None:
    """Find the Trafiklab keys.

    They are account-wide but config entries are per stop, so they are read from
    whichever entry has them set rather than duplicated across every entry.
    """
    for entry in hass.config_entries.async_entries(DOMAIN):
        static = entry.options.get(CONF_GTFS_STATIC_KEY) or entry.data.get(
            CONF_GTFS_STATIC_KEY
        )
        realtime = entry.options.get(CONF_GTFS_REALTIME_KEY) or entry.data.get(
            CONF_GTFS_REALTIME_KEY
        )
        if static and realtime:
            return static, realtime
    return None


def _configured_stop_ids(hass: HomeAssistant) -> list[int]:
    return [
        entry.data[CONF_STOP_ID]
        for entry in hass.config_entries.async_entries(DOMAIN)
        if CONF_STOP_ID in entry.data
    ]


async def _async_setup_map(hass: HomeAssistant) -> None:
    """Build the GTFS index and expose the map, if keys are configured.

    Failure here never blocks the departure sensors - the map is additive.
    """
    keys = _map_keys(hass)
    if keys is None:
        return
    static_key, realtime_key = keys
    stop_ids = _configured_stop_ids(hass)
    if not stop_ids:
        return

    # Entries are set up concurrently and each one wants the index. Without the
    # lock they all download and rebuild it at the same time.
    lock = hass.data[DOMAIN].setdefault("map_lock", asyncio.Lock())
    try:
        async with lock:
            await _async_build_map(hass, static_key, realtime_key, stop_ids)
    finally:
        hass.data[DOMAIN]["map_loading"] = False


async def _async_build_map(
    hass: HomeAssistant, static_key: str, realtime_key: str, stop_ids: list[int]
) -> None:
    runtime = hass.data[DOMAIN].get("map")
    if runtime is not None and runtime["stops"] == sorted(stop_ids):
        return

    try:
        index = await async_load_index(hass, static_key, stop_ids)
    except Exception as err:  # pylint: disable=broad-except
        _LOGGER.error("Live map unavailable, GTFS index failed: %s", err)
        return

    if runtime is not None:
        await runtime["feed"].async_close()

    hass.data[DOMAIN]["map"] = {
        "index": index,
        "feed": LiveFeed(hass, realtime_key),
        "stops": sorted(stop_ids),
    }


async def _async_serve_card(hass: HomeAssistant) -> None:
    """Serve the map card from the integration so users add no manual resource."""
    if hass.data[DOMAIN].get("card_registered"):
        return
    hass.data[DOMAIN]["card_registered"] = True
    await hass.http.async_register_static_paths(
        [
            StaticPathConfig(
                f"/{DOMAIN}",
                str(Path(__file__).parent / "www"),
                cache_headers=False,
            )
        ]
    )
    # Without cache headers browsers cache the card heuristically and keep
    # running an old copy after an update; the mtime makes each version its own
    # URL, so a restart is enough to pick up a new card.
    stamp = await hass.async_add_executor_job(lambda: int(CARD_PATH.stat().st_mtime))
    add_extra_js_url(hass, f"/{DOMAIN}/ul-transport-map.js?v={stamp}")


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up UL Transport from a config entry."""
    hass.data.setdefault(DOMAIN, {})

    # Both registered even without API keys, so the card picker and its editor
    # can list stops and explain what is missing rather than erroring out - and
    # registered before the coordinator's first refresh, because a dashboard is
    # on screen while this runs and a card asking too early should be told to
    # wait rather than that the map is unconfigured or the command unknown.
    if _map_keys(hass) is not None and "map" not in hass.data[DOMAIN]:
        hass.data[DOMAIN]["map_loading"] = True
    async_register_websocket(hass)
    async_register_llm_api(hass)
    await _async_serve_card(hass)

    stop_id = entry.data[CONF_STOP_ID]
    stop_name = entry.data[CONF_STOP_NAME]
    selected_lines = entry.options.get(
        CONF_SELECTED_LINES,
        entry.data.get(CONF_SELECTED_LINES, [])
    )
    scan_interval = entry.options.get(
        CONF_SCAN_INTERVAL,
        entry.data.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
    )

    coordinator = ULTransportDataUpdateCoordinator(
        hass, stop_id, stop_name, selected_lines, scan_interval
    )
    await coordinator.async_config_entry_first_refresh()

    hass.data[DOMAIN][entry.entry_id] = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    await _async_setup_map(hass)

    if not hass.data[DOMAIN].get("index_timer"):

        async def _refresh_index(_now) -> None:
            keys = _map_keys(hass)
            runtime = hass.data[DOMAIN].get("map")
            if keys is None or runtime is None:
                return
            try:
                runtime["index"] = await async_load_index(
                    hass, keys[0], _configured_stop_ids(hass)
                )
            except Exception as err:  # pylint: disable=broad-except
                _LOGGER.warning("GTFS index refresh failed: %s", err)

        hass.data[DOMAIN]["index_timer"] = async_track_time_interval(
            hass, _refresh_index, INDEX_REFRESH
        )

    entry.async_on_unload(entry.add_update_listener(_async_options_updated))

    return True


async def _async_options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload when options change so new keys or stop selections take effect."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        hass.data[DOMAIN].pop(entry.entry_id)

        # Last entry out tears down the shared map runtime.
        if not hass.config_entries.async_loaded_entries(DOMAIN):
            async_unregister_llm_api(hass)
            runtime = hass.data[DOMAIN].pop("map", None)
            if runtime is not None:
                await runtime["feed"].async_close()
            if timer := hass.data[DOMAIN].pop("index_timer", None):
                timer()

    return unload_ok
