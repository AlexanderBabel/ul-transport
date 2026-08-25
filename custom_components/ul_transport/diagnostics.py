"""Diagnostics for UL Transport.

Shaped for the two things that actually go wrong: the departure board is empty
because the selection no longer matches what UL advertises, or the live map is
missing because the keys, the index or the quota is not what the user thinks.
The raw departures are included as-is - they are a public timetable, and the
line/direction spelling is exactly what a mismatch turns on.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from .const import CONF_GTFS_REALTIME_KEY, CONF_GTFS_STATIC_KEY, DOMAIN
from .coordinator import ULTransportConfigEntry
from .quota import QUOTA_DAYS, QUOTA_REALTIME, QUOTA_STATIC, request_quota

TO_REDACT = {CONF_GTFS_STATIC_KEY, CONF_GTFS_REALTIME_KEY}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ULTransportConfigEntry
) -> dict[str, Any]:
    """Diagnostics for one stop, plus the shared map and quota state."""
    coordinator = entry.runtime_data
    quota = request_quota(hass)
    runtime = hass.data.get(DOMAIN, {}).get("map")
    index = runtime["index"] if runtime else None

    return {
        "entry": {
            "data": async_redact_data(dict(entry.data), TO_REDACT),
            "options": async_redact_data(dict(entry.options), TO_REDACT),
        },
        "coordinator": {
            "stop_id": coordinator.stop_id,
            "stop_name": coordinator.stop_name,
            "last_update_success": coordinator.last_update_success,
            "last_successful_update": coordinator.last_successful_update,
            "update_interval": str(coordinator.update_interval),
            # What UL advertises versus what the user picked: a selection that
            # no longer matches is the usual reason a sensor went missing.
            "board_keys": coordinator.board_keys,
            "selected_lines": coordinator.selected_lines,
            "departures": coordinator.data,
        },
        "map": {
            "configured": runtime is not None,
            "loading": hass.data.get(DOMAIN, {}).get("map_loading", False),
            "stops": runtime["stops"] if runtime else [],
            "index": {
                "built": index.built,
                "stale": index.stale,
                "trips": len(index.trips),
                "stops": len(index.stops),
                "lines": sorted(index.line_shapes),
            }
            if index is not None
            else None,
        },
        "quota": {
            "window_days": QUOTA_DAYS,
            "realtime": {"used": quota.realtime(), "limit": QUOTA_REALTIME},
            "static": {"used": quota.static(), "limit": QUOTA_STATIC},
            "requests": quota.totals(),
        },
    }
