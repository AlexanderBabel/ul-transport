# Changelog

## 2.0.1 - 2026-08-15

Hardening release: no new features, and every entity keeps its id, its history
and its friendly name.

### Fixed

- **Saving options reloaded the integration twice.** Both `__init__` and the
  sensor platform registered an update listener, so every change to lines,
  interval or API keys kicked off two concurrent reloads of the same entry.
- **Every API call opened its own HTTP session.** The coordinator, the config
  flow and the static GTFS download each built an `aiohttp.ClientSession` per
  request and the live feed held one of its own. They now share Home
  Assistant's session, which is what keeps connection pooling, the shared DNS
  cache and clean shutdown working.
- **A rate-limited poll reported the wrong error.** A catch-all handler in the
  coordinator re-wrapped its own `UpdateFailed`, so "UL API rate limit
  exceeded" reached the log as "Unexpected error: ...". Timeouts said nothing
  useful either; both now report themselves.
- A malformed departure payload raised `KeyError` out of the coordinator
  instead of failing the update cleanly.
- The stop search sent its query unencoded, so a stop name containing `&` or
  `#` searched for the wrong thing.
- The card is now registered as a dashboard resource as well as an extra module
  URL. `add_extra_js_url` only reaches a browser through a freshly rendered
  `index.html`, so a session that loaded the page before the integration set up
  showed a configuration error until a hard reload - and a kiosk tablet serving
  its index from the service-worker cache never recovered at all. Resources are
  fetched over the websocket every time a dashboard opens.
- `content: list` cards no longer label a bus that is out on the road "no live
  data". They read the positions feed too; `include_positions: false` restores
  the cheaper behaviour, and now says "timetabled arrival" rather than claiming
  a position is missing.

### Changed

- **Entities now belong to a device per stop**, named after the stop, with the
  refresh button and every sensor grouped under it. Renaming the device renames
  its entities together. Friendly names are unchanged
  (`Centralstationen Line 2 to Uppsala Central`), and unique ids are untouched,
  so history carries over.
- `Last Update` moved to the **diagnostic** category, where a
  last-successful-fetch timestamp belongs. It keeps its entity id.
- The `Next departure` and `Last Update` sensors and the refresh button are now
  named from `strings.json`, so they follow the Home Assistant language setting
  (Swedish translations included).
- Dropped the undeclared `async_timeout` dependency in favour of
  `asyncio.timeout`, and stopped listing `aiohttp` as a requirement - Home
  Assistant provides it, and declaring it fails `hassfest`.
- The coordinator is handed to the platforms through `entry.runtime_data`
  rather than `hass.data`, so "which stops exist" is answered by the config
  entry register instead of by type-sniffing a shared dict.

### Added

- **`sensor.ul_transport_api_requests`.** Upstream requests since Home Assistant
  started, broken down per feed, with `trafiklab_total` and `trafiklab_per_hour`
  as the numbers the quota sees. Counts calls rather than successes: a 304 or a
  429 is spent quota too.
- CI on every push: `hassfest`, HACS validation, `ruff` and the test suite.
- `ruff` configuration in `pyproject.toml`, and tests for the config and
  options flows and for end-to-end entry setup, device creation, entity naming
  and unload (155 tests, 81% coverage).

## 2.0.0 - 2026-08-14

The integration no longer leans on an external departures card. Departures
are drawn by the live map card that ships with it, and the sensors changed
shape to suit automations rather than that card - see **Removed**.

### Added

- **Live map card** (`custom:ul-transport-map`). Shows the buses actually
  heading to your configured stops, with real-time position, arrival time and
  how many stops away they are. Has a visual editor and a live preview in the
  card picker. Needs two Trafiklab keys - GTFS Regional and GTFS Regional
  Realtime - entered once under **Configure** on any stop.
- The card's list continues from the static timetable once the realtime feed
  runs dry, so `list_minutes: 1440` turns it into a departure board for the
  whole service day at no extra request.
- **Assist tool.** `get_next_departures` is exposed as the *UL Transport
  departures* LLM API, so a conversation agent can answer "when does the next
  bus go?". Its result carries a live map config for a satellite with a screen
  to draw.
- **Refresh button** per stop, for pulling the departure board by hand.

### Changed

- Sensor states are now the **number of minutes** until departure, floored, so
  a `numeric_state` trigger is all an automation needs. Timestamps moved to the
  `departure` / `scheduled_departure` attributes.
- Polling moved to a `DataUpdateCoordinator`, so all sensors for a stop share
  one request.

### Removed

- **Breaking:** the departures-card sensors - ISO-timestamp states with
  `planned_departure_time_*` attributes - are gone, along with support for
  `ha-departure-map`. They are removed on upgrade. Use the live map card
  instead.
