# Changelog

## 2.3.0 - 2026-08-25

The Trafiklab keys move into a config entry of their own. Before this they
lived on whichever stop happened to be set up first, so removing that stop took
the live map down for every other stop on the install.

Nothing to do on upgrade: the keys are moved automatically the first time Home
Assistant restarts, and the quota sensors keep their entity ids and their
history.

### Added

- Setup now opens by offering the live map and saying what it needs and why,
  with a **Skip** that goes straight to adding a stop. Departure sensors have
  never needed the keys, and the flow no longer implies otherwise.
- The keys are checked against Trafiklab before they are saved, and a refused
  key is reported on the field it belongs to - so the commonest mistake, the
  two keys swapped between the boxes, is caught at setup rather than by the map
  silently never appearing.

### Changed

- **The Trafiklab keys live in their own entry.** One place to enter them, one
  place to edit them, and stops can be added or removed without touching them.
- The realtime and static quota sensors hang off that entry instead of moving
  house whenever a stop was removed. Their entity ids and history are kept.
- The per-stop options no longer show the key fields; they carry only lines and
  the refresh interval.
- Once the keys are set up the first question stops being asked, so adding a
  second stop goes straight to the search.

## 2.2.0 - 2026-08-25

Housekeeping against Home Assistant's integration quality scale, a proper
documentation set, and the logo redrawn from UL's own vector artwork.

### Added

- A **diagnostics** download on the integration's menu. It reports what UL
  advertises against what you selected - the usual reason a sensor is missing -
  alongside the GTFS index and quota state. The Trafiklab keys are redacted.
- Entity icons now come from `icons.json` (icon translations) rather than being
  set in code, so they can be overridden the way any core integration's can.
- [`docs/live-map.md`](docs/live-map.md): every card option, the quota
  arithmetic and the known limitations, in one reference.
- Screenshots of the card in the README.

### Changed

- The logo is UL's round profile mark, rendered from the official vector at
  Pantone 116 C (`#FFD300`). The previous bitmap carried a yellow gradient.
- The coordinator is handed its config entry explicitly instead of relying on
  the context variable the base class falls back on.
- Both platforms declare `PARALLEL_UPDATES = 0`: the coordinator owns the
  polling and the entities only read what it already has.

## 2.1.0 - 2026-08-25

Quota work, and the direction names the map shows. The Trafiklab free tier
allows 30,000 realtime requests per 30 days and 60 static downloads, and nothing
in the integration could tell you where you stood against either.

### Added

- `sensor.trafiklab_api_realtime_requests_30_days` and
  `sensor.trafiklab_api_static_requests_30_days`, on their own **Trafiklab API**
  device because the key is account-wide. Each reports what its quota has seen
  over the same trailing 30 days the quota is measured over, with `limit`,
  `remaining` and `percent_used` alongside the per-feed breakdown. Counts are
  persisted, so a restart no longer resets them.
- A hard stop before Trafiklab's: once the 30-day realtime count reaches 30,000
  the integration serves the data it already has instead of collecting 429s.

### Changed

- A `content: list` card now refreshes every 90 seconds instead of 20, and skips
  the vehicle positions feed by default. That is one request per refresh rather
  than three per 20 seconds - the difference between a list left up on a wall
  panel fitting inside the quota and spending it in four days. Set
  `include_positions: true` to get the "already on the road" marker back.
- Arrival countdowns are worked out in the browser from the departure time, so
  they tick down by the second between polls rather than sitting still and
  jumping. This is what makes the slower list refresh readable.
- The card editor's refresh slider now reaches 300 seconds.

### Fixed

- Direction names fell back to their GTFS terminus - the route's last physical
  stop, rather than the districts the UL app names - for two separate reasons. The learned-name cache and the coordinator lookup were keyed by stop,
  so a stop advertising only one of a line's two names could pair neither; they
  are now pooled across every configured stop, and another stop on the same line
  supplies the missing half. Separately, the first and last runs of a line carry
  a compound name (the downtown origin prepended to the destination, on the
  morning trip that starts there), leaving three names for two directions;
  the variants are now folded onto the destination they share.

### Removed

- `sensor.ul_transport_api_requests`, which counted only since the last restart
  and so could not answer the one question worth asking. It is removed from the
  entity registry automatically on upgrade; the two quota sensors replace it.
  Any automation or template referencing it needs repointing at
  `sensor.trafiklab_api_realtime_requests_30_days`.

## 2.0.3 - 2026-08-18

Voice Satellite 2026.8.10 shipped Lovelace cards from tool results as a
supported feature, so the local patch this repository carried is gone.

### Changed

- The `card` key in the Assist tool result is now plain Lovelace config. The
  `card_module` and `card_scale` keys it used to carry were the patch's own
  extensions and upstream does not read them: it resolves `custom:` cards from
  the dashboard resource registry — where `__init__.py` already registers this
  card — and sizes them with the satellite's Text Scale slider.

### Removed

- `contrib/`, which held the patch teaching the Voice Satellite card to draw
  tool result cards, and the instructions for building and deploying it.
  Install Voice Satellite 2026.8.10 or later from HACS instead.

## 2.0.2 - 2026-08-15

Maintenance release. Nothing about the integration's behaviour changed; 2.0.1
users gain the packaging and repository fixes below.

### Fixed

- `manifest.json` keys are ordered the way `hassfest` requires (domain, name,
  then alphabetical). 2.0.1 shipped with `after_dependencies` in the wrong
  place, which failed validation.
- The integration now asks for `gtfs-realtime-bindings>=2.2.0` rather than
  `>=1.0.0`. 1.0.0 declares no dependencies at all, so it could be installed
  without the `protobuf` the live map's realtime feeds need; 2.2.0 declares it
  and leaves the version unpinned, so nothing else in Home Assistant is
  disturbed. This is also the version CI tests against.

### Added

- Brand assets under `custom_components/ul_transport/brand/`, so HACS shows an
  icon rather than falling back to the core-only brands repository.
- A `LICENSE` file. The README and `pyproject.toml` already said MIT; now the
  repository does too.

### Changed

- Repository housekeeping with no effect on the installed integration: CI
  actions pinned to commit SHAs and updated, Dependabot configured to keep
  them moving, `ruff format` applied and enforced, and the test suite given
  the `home-assistant-frontend` wheel it needs to run outside a full Home
  Assistant install.

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
