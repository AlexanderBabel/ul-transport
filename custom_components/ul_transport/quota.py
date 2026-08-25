"""Rolling 30-day tally of upstream requests, against Trafiklab's quotas.

Trafiklab counts over a rolling 30 days and the quota belongs to the API key,
not to the process, so an in-memory counter that starts at zero on every
restart cannot answer the only question worth asking: how much is left. Counts
are bucketed per day and persisted - thirty integers per feed is small enough
to keep the whole window on disk and read the answer straight off it.

ponytail: whole days, not a true rolling 30x24h window. It over-counts by at
most the oldest day still in the bucket, which errs towards saying you have
less quota than you do. Store timestamps per request if that ever matters.
"""

from __future__ import annotations

from collections import Counter
from datetime import timedelta

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import DOMAIN

DATA_QUOTA = "quota"
STORAGE_KEY = f"{DOMAIN}_requests"
STORAGE_VERSION = 1

# Both quotas on Trafiklab's free Regional tier, over a rolling 30 days.
QUOTA_DAYS = 30
QUOTA_REALTIME = 30000
QUOTA_STATIC = 60

# Saving on every request would write to disk every few seconds while the map
# is open. Five minutes of counts is an acceptable loss on a hard kill.
SAVE_DELAY = 300

# Counted alongside the feed call it belongs to rather than instead of it, so
# it is a sub-tally of requests already counted and must not be summed again.
NOT_MODIFIED = "rt_not_modified"


class RequestQuota:
    """Per-day request counts for the last QUOTA_DAYS days."""

    def __init__(self, hass: HomeAssistant) -> None:
        """Set up the store. Call async_load before trusting the counts."""
        self._store: Store[dict[str, dict[str, int]]] = Store(
            hass, STORAGE_VERSION, STORAGE_KEY
        )
        self._days: dict[str, dict[str, int]] = {}
        self.loaded = False

    async def async_load(self) -> None:
        """Read the window back off disk, dropping whatever has aged out."""
        self._days = await self._store.async_load() or {}
        self.loaded = True
        self._trim()

    def count(self, key: str) -> None:
        """Tally one upstream request against today."""
        today = dt_util.now().date().isoformat()
        counts = self._days.setdefault(today, {})
        counts[key] = counts.get(key, 0) + 1
        self._trim()
        self._store.async_delay_save(lambda: self._days, SAVE_DELAY)

    def totals(self) -> dict[str, int]:
        """Requests per key over the window."""
        totals: Counter[str] = Counter()
        for counts in self._days.values():
            totals.update(counts)
        return dict(totals)

    def realtime(self) -> int:
        """What the realtime quota has actually seen.

        Calls, not successes: a 304 or a 429 is spent quota too. UL's own
        departure API is not Trafiklab's and has no published quota.
        """
        return sum(
            value
            for key, value in self.totals().items()
            if key.startswith("rt_") and key != NOT_MODIFIED
        )

    def static(self) -> int:
        """Static feed downloads over the window."""
        return self.totals().get("gtfs_static", 0)

    def _trim(self) -> None:
        """Forget days that have fallen out of the window."""
        cutoff = (dt_util.now().date() - timedelta(days=QUOTA_DAYS - 1)).isoformat()
        for day in [day for day in self._days if day < cutoff]:
            del self._days[day]


async def async_setup_quota(hass: HomeAssistant) -> RequestQuota:
    """The one tally for this install, read back off disk exactly once.

    Called before the first counted request of the session, so there is never
    anything in memory for the load to overwrite.
    """
    quota = request_quota(hass)
    if not quota.loaded:
        await quota.async_load()
    return quota


def request_quota(hass: HomeAssistant) -> RequestQuota:
    """The tally, creating an unloaded one if setup has not run yet.

    Four modules make HTTP calls and none of them owns the others, so the tally
    lives in hass.data rather than on a class.
    """
    data = hass.data.setdefault(DOMAIN, {})
    if DATA_QUOTA not in data:
        data[DATA_QUOTA] = RequestQuota(hass)
    return data[DATA_QUOTA]


def count_request(hass: HomeAssistant, key: str) -> None:
    """Tally one upstream HTTP request."""
    request_quota(hass).count(key)
