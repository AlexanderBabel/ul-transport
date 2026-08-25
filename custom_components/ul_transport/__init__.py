"""The UL Transport integration.

Two kinds of config entry share this domain. One holds the Trafiklab keys and
owns the live map and the quota sensors; the rest are stops, each with its own
coordinator and departure sensors. Stops work with no Trafiklab entry at all -
the map is additive - and the keys survive any stop being removed, which is the
whole reason they have an entry of their own.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
import logging
from pathlib import Path

from homeassistant.components.frontend import add_extra_js_url
from homeassistant.components.http import StaticPathConfig
from homeassistant.config_entries import SOURCE_IMPORT, ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.typing import ConfigType

from .const import (
    CONF_ENTRY_TYPE,
    CONF_GTFS_REALTIME_KEY,
    CONF_GTFS_STATIC_KEY,
    CONF_SCAN_INTERVAL,
    CONF_SELECTED_LINES,
    CONF_STOP_ID,
    CONF_STOP_NAME,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    ENTRY_TYPE_TRAFIKLAB,
)
from .coordinator import (
    ULTransportConfigEntry,
    ULTransportDataUpdateCoordinator,
    is_stop_entry,
    trafiklab_entry,
    trafiklab_keys,
)
from .gtfs import async_load_index
from .live import LiveFeed, async_register_websocket
from .llm_tool import (
    async_register as async_register_llm_api,
    async_unregister as async_unregister_llm_api,
)
from .quota import async_setup_quota

_LOGGER = logging.getLogger(__name__)

STOP_PLATFORMS: list[Platform] = [Platform.SENSOR, Platform.BUTTON]
# The quota sensors watch the Trafiklab key, so they belong to its entry.
TRAFIKLAB_PLATFORMS: list[Platform] = [Platform.SENSOR]

CARD_PATH = Path(__file__).parent / "www" / "ul-transport-map.js"
INDEX_REFRESH = timedelta(hours=6)


def _configured_stop_ids(hass: HomeAssistant) -> list[int]:
    return [
        entry.data[CONF_STOP_ID]
        for entry in hass.config_entries.async_entries(DOMAIN)
        if is_stop_entry(entry) and CONF_STOP_ID in entry.data
    ]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Move pre-2.3 keys off whichever stop happened to be holding them.

    Runs before any entry is set up, so the rest of the integration only ever
    sees keys in their own entry. Removing the stop that held them used to take
    the live map down for every other stop; this is that bug's one-way fix.
    """
    hass.data.setdefault(DOMAIN, {})
    _async_migrate_legacy_keys(hass)
    return True


def _async_migrate_legacy_keys(hass: HomeAssistant) -> None:
    """Adopt keys stored on a stop entry, then strip them from every stop."""
    # Only the adoption is skipped when the keys already have an entry. The
    # stripping below still runs, so a stale copy left on a stop by a
    # half-finished upgrade cannot become a second place to edit them.
    for entry in _stops_holding_keys(hass) if trafiklab_entry(hass) is None else ():
        static, realtime = _legacy_keys(entry)
        _LOGGER.info(
            "Moving the Trafiklab keys off %s and onto their own entry", entry.title
        )
        hass.async_create_task(
            hass.config_entries.flow.async_init(
                DOMAIN,
                context={"source": SOURCE_IMPORT},
                data={
                    CONF_GTFS_STATIC_KEY: static,
                    CONF_GTFS_REALTIME_KEY: realtime,
                },
            )
        )
        break

    # Whether or not that stop is the one adopted: a leftover copy is a second
    # place to edit and a second place to leak from.
    for entry in hass.config_entries.async_entries(DOMAIN):
        if not _has_legacy_keys(entry):
            continue
        hass.config_entries.async_update_entry(
            entry,
            data=_without_keys(entry.data),
            options=_without_keys(entry.options),
        )


def _legacy_keys(entry: ConfigEntry) -> tuple[str | None, str | None]:
    """Keys an older version wrote onto this entry, from options or data."""
    return (
        entry.options.get(CONF_GTFS_STATIC_KEY) or entry.data.get(CONF_GTFS_STATIC_KEY),
        entry.options.get(CONF_GTFS_REALTIME_KEY)
        or entry.data.get(CONF_GTFS_REALTIME_KEY),
    )


def _stops_holding_keys(hass: HomeAssistant) -> list[ConfigEntry]:
    """Stops carrying a usable pair of keys, oldest first."""
    return [
        entry
        for entry in hass.config_entries.async_entries(DOMAIN)
        if is_stop_entry(entry) and all(_legacy_keys(entry))
    ]


def _has_legacy_keys(entry: ConfigEntry) -> bool:
    if entry.data.get(CONF_ENTRY_TYPE) == ENTRY_TYPE_TRAFIKLAB:
        return False
    keys = (CONF_GTFS_STATIC_KEY, CONF_GTFS_REALTIME_KEY)
    return any(key in entry.data or key in entry.options for key in keys)


def _without_keys(mapping: dict) -> dict:
    return {
        key: value
        for key, value in mapping.items()
        if key not in (CONF_GTFS_STATIC_KEY, CONF_GTFS_REALTIME_KEY)
    }


async def _async_setup_map(hass: HomeAssistant) -> None:
    """Build the GTFS index and expose the map, if the keys are configured.

    Failure here never blocks the departure sensors - the map is additive.
    """
    keys = trafiklab_keys(hass)
    if keys is None:
        return
    static_key, realtime_key = keys
    stop_ids = _configured_stop_ids(hass)
    if not stop_ids:
        # Keys but no stops yet. The index is reduced to the configured stops,
        # so there is nothing to build until one exists.
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


async def _async_common_setup(hass: HomeAssistant) -> None:
    """The parts both kinds of entry need, whichever is set up first."""
    hass.data.setdefault(DOMAIN, {})
    # Before the first coordinator refresh, which is itself a counted request.
    await async_setup_quota(hass)

    # Registered even without keys, so the card picker and its editor can list
    # stops and explain what is missing rather than erroring out - and before
    # the coordinator's first refresh, because a dashboard is on screen while
    # this runs and a card asking too early should be told to wait rather than
    # that the map is unconfigured or the command unknown.
    if trafiklab_keys(hass) is not None and "map" not in hass.data[DOMAIN]:
        hass.data[DOMAIN]["map_loading"] = True
    async_register_websocket(hass)
    async_register_llm_api(hass)
    await _async_serve_card(hass)


async def async_setup_entry(hass: HomeAssistant, entry: ULTransportConfigEntry) -> bool:
    """Set up either the Trafiklab keys or one stop."""
    await _async_common_setup(hass)

    if entry.data.get(CONF_ENTRY_TYPE) == ENTRY_TYPE_TRAFIKLAB:
        return await _async_setup_trafiklab(hass, entry)
    return await _async_setup_stop(hass, entry)


async def _async_setup_trafiklab(
    hass: HomeAssistant, entry: ULTransportConfigEntry
) -> bool:
    """Set up the live map from the entry holding the keys."""
    await hass.config_entries.async_forward_entry_setups(entry, TRAFIKLAB_PLATFORMS)

    await _async_setup_map(hass)

    if not hass.data[DOMAIN].get("index_timer"):

        async def _refresh_index(_now) -> None:
            keys = trafiklab_keys(hass)
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


async def _async_setup_stop(hass: HomeAssistant, entry: ULTransportConfigEntry) -> bool:
    """Set up one stop's coordinator, sensors and button."""
    stop_id = entry.data[CONF_STOP_ID]
    stop_name = entry.data[CONF_STOP_NAME]
    selected_lines = entry.options.get(
        CONF_SELECTED_LINES, entry.data.get(CONF_SELECTED_LINES, [])
    )
    scan_interval = entry.options.get(
        CONF_SCAN_INTERVAL, entry.data.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
    )

    coordinator = ULTransportDataUpdateCoordinator(
        hass, entry, stop_id, stop_name, selected_lines, scan_interval
    )
    await coordinator.async_config_entry_first_refresh()

    entry.runtime_data = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, STOP_PLATFORMS)

    # The index is reduced to the configured stops, so a new stop means the map
    # has to be rebuilt or it shows nothing for this one.
    await _async_setup_map(hass)

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
    """Rebuild the map without this stop, and clean up on the last entry out."""
    if hass.config_entries.async_entries(DOMAIN):
        # The index is reduced to the configured stops, so it has to be rebuilt
        # once a stop is genuinely gone. Here rather than in async_unload_entry,
        # which runs while the entry is still in the register.
        if is_stop_entry(entry):
            await _async_setup_map(hass)
        return  # other stops, or the keys, still use the card

    # Take the card back out of the dashboard resources: left behind it is a
    # resource pointing at a 404, which the frontend complains about on every
    # dashboard load.
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
    is_trafiklab = entry.data.get(CONF_ENTRY_TYPE) == ENTRY_TYPE_TRAFIKLAB
    platforms = TRAFIKLAB_PLATFORMS if is_trafiklab else STOP_PLATFORMS

    if not await hass.config_entries.async_unload_platforms(entry, platforms):
        return False

    # Either way the index is now describing a set of stops that is about to
    # change, so it is dropped rather than trusted. Rebuilding is left to
    # whatever happens next: a reload sets the entry up again, and a removal
    # goes through async_remove_entry, which is the only point where the entry
    # is genuinely gone from the register.
    hass.data[DOMAIN].pop("map", None)
    if is_trafiklab and (timer := hass.data[DOMAIN].pop("index_timer", None)):
        timer()

    if not hass.config_entries.async_loaded_entries(DOMAIN):
        async_unregister_llm_api(hass)

    return True
