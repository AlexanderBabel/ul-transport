"""The UL Transport integration."""

from __future__ import annotations

import asyncio
from datetime import timedelta
import logging
from pathlib import Path

from homeassistant.components.frontend import add_extra_js_url
from homeassistant.components.http import StaticPathConfig
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.event import async_track_time_interval

from .const import (
    CONF_GTFS_REALTIME_KEY,
    CONF_GTFS_STATIC_KEY,
    CONF_SCAN_INTERVAL,
    CONF_SELECTED_LINES,
    CONF_STOP_ID,
    CONF_STOP_NAME,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
)
from .coordinator import ULTransportConfigEntry, ULTransportDataUpdateCoordinator
from .gtfs import async_load_index
from .live import LiveFeed, async_register_websocket
from .llm_tool import (
    async_register as async_register_llm_api,
    async_unregister as async_unregister_llm_api,
)

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
    except Exception as err:  # noqa: BLE001 - the map is additive, sensors go on
        _LOGGER.error("Live map unavailable, GTFS index failed: %s", err)
        return

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
    url = f"/{DOMAIN}/ul-transport-map.js?v={stamp}"
    if not await _async_register_resource(hass, url):
        # Only as a fallback. The index routes fetch this file while the page is
        # still parsing, before the frontend has finished setting itself up, and
        # on a legacy-bundle browser the definition it performs there does not
        # survive - the module runs to its last line and the element is still
        # missing. Dashboard resources load after the app is up, which is when
        # every working HACS card on that same tablet defines itself.
        add_extra_js_url(hass, url)
        add_extra_js_url(hass, url, es5=True)


async def _async_register_resource(hass: HomeAssistant, url: str) -> bool:
    """List the card as a dashboard resource. True if it is now listed.

    The route every HACS card uses, and the one that demonstrably works on a
    legacy-bundle browser: resources arrive over the websocket once the frontend
    is running, rather than out of index.html while the page is still parsing.

    The caller falls back to add_extra_js_url when this returns False.
    """
    data = hass.data.get("lovelace")
    # YAML-mode resources are the user's file to edit; add_extra_js_url covers
    # that case on its own.
    if data is None or getattr(data, "resource_mode", None) != "storage":
        return False
    resources = data.resources
    # "module" is the only type the frontend still offers for new resources;
    # "js" survives for existing ones but is on its way out.
    try:
        await resources.async_get_info()  # loads the collection from storage
        base = url.split("?", maxsplit=1)[0]
        for item in resources.async_items():
            if item.get("url", "").split("?", maxsplit=1)[0] != base:
                continue
            if item["url"] != url or item.get("type") != "module":
                await resources.async_update_item(
                    item["id"], {"url": url, "res_type": "module"}
                )
            return True
        await resources.async_create_item({"res_type": "module", "url": url})
    except Exception as err:  # noqa: BLE001 - falls back to add_extra_js_url
        _LOGGER.warning("Could not register the card as a dashboard resource: %s", err)
        return False
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ULTransportConfigEntry) -> bool:
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
        CONF_SELECTED_LINES, entry.data.get(CONF_SELECTED_LINES, [])
    )
    scan_interval = entry.options.get(
        CONF_SCAN_INTERVAL, entry.data.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
    )

    coordinator = ULTransportDataUpdateCoordinator(
        hass, stop_id, stop_name, selected_lines, scan_interval
    )
    await coordinator.async_config_entry_first_refresh()

    entry.runtime_data = coordinator

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
            except Exception as err:  # noqa: BLE001 - keep the index we have
                _LOGGER.warning("GTFS index refresh failed: %s", err)

        hass.data[DOMAIN]["index_timer"] = async_track_time_interval(
            hass, _refresh_index, INDEX_REFRESH
        )

    entry.async_on_unload(entry.add_update_listener(_async_options_updated))

    return True


async def _async_options_updated(
    hass: HomeAssistant, entry: ULTransportConfigEntry
) -> None:
    """Reload when options change so new keys or stop selections take effect."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_remove_entry(
    hass: HomeAssistant, entry: ULTransportConfigEntry
) -> None:
    """Take the card back out of the dashboard resources on uninstall.

    Left behind it is a resource pointing at a 404, which the frontend complains
    about on every dashboard load.
    """
    if hass.config_entries.async_entries(DOMAIN):
        return  # other stops still use it
    data = hass.data.get("lovelace")
    if data is None or getattr(data, "resource_mode", None) != "storage":
        return
    try:
        await data.resources.async_get_info()
        for item in list(data.resources.async_items()):
            if item.get("url", "").startswith(f"/{DOMAIN}/"):
                await data.resources.async_delete_item(item["id"])
    except Exception as err:  # noqa: BLE001 - uninstall must not fail on this
        _LOGGER.warning("Could not remove the card's dashboard resource: %s", err)


async def async_unload_entry(
    hass: HomeAssistant, entry: ULTransportConfigEntry
) -> bool:
    """Unload a config entry."""
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        # Hand the request-counter sensor to whichever entry sets up next.
        if hass.data[DOMAIN].get("counter_entry") == entry.entry_id:
            hass.data[DOMAIN].pop("counter_entry")

        # Last entry out tears down the shared map runtime. The HTTP session is
        # Home Assistant's own and is deliberately left alone.
        if not hass.config_entries.async_loaded_entries(DOMAIN):
            async_unregister_llm_api(hass)
            hass.data[DOMAIN].pop("map", None)
            if timer := hass.data[DOMAIN].pop("index_timer", None):
                timer()

    return unload_ok
