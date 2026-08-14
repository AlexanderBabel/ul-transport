# Changelog

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
