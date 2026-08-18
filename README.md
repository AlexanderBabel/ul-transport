# UL Transport for Home Assistant

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/custom-components/hacs)
[![GitHub release](https://img.shields.io/github/release/AlexanderBabel/ul-transport.svg)](https://github.com/AlexanderBabel/ul-transport/releases)

A Home Assistant integration for real-time public transport departures from UL
(Uppsala Läns Trafik): sensors shaped for automations, and a live map card that
draws the buses actually heading to your stop.

## Features

- 🔍 **UI-based setup** - Search and select stops directly in Home Assistant
- 🚌 **Real-time data** - Live departures with delays
- ⏱️ **"Next bus in N minutes"** - a plain number, ready for a numeric_state trigger
- 🗺️ **Live map card** - positions, arrival times and the whole day's timetable
- 🗣️ **Assist tool** - ask a voice assistant when the next bus goes
- 📍 **Line filtering** - Monitor all or specific lines per stop
- 🔄 **Configurable refresh** - Set update interval (30-600 seconds)

## Requirements

Home Assistant 2025.2 or newer. The live map additionally needs two free
[Trafiklab](https://www.trafiklab.se/) keys — see [Live Map](#live-map).

## Installation

### HACS (Recommended)

1. Open HACS in Home Assistant
2. Click on "Integrations"
3. Click the three dots in the top right corner
4. Select "Custom repositories"
5. Add this repository URL: `https://github.com/AlexanderBabel/ul-transport`
6. Select category: "Integration"
7. Click "Add"
8. Search for "UL Transport" and install
9. Restart Home Assistant

### Manual Installation

1. Copy the `custom_components/ul_transport` folder to your Home Assistant's `custom_components` directory
2. Restart Home Assistant

## Configuration

1. **Settings** → **Devices & Services** → **+ Add Integration**
2. Search for **UL Transport**
3. Enter your stop name (e.g., "Centralstationen")
4. Select the stop from search results
5. Choose lines to monitor - leave empty for all, which also covers lines that
   are not running at the moment you add the stop
6. Set update interval in seconds (default: 60, range: 30-600)

To modify settings later, click **Configure** on the integration.

## Sensors

Each stop is a **device**, with its entities grouped under it — rename the
device and its entities follow.

Per stop:

| Sensor | State |
| --- | --- |
| `sensor.<stop>_line_<line>_to_<direction>` | Minutes until that line's next departure |
| `sensor.<stop>_next_departure` | Minutes until the next bus, whichever line |
| `sensor.<stop>_last_update` | When the departure board was last fetched (diagnostic) |
| `button.<stop>_refresh` | Pulls the departure board by hand |

One per install:

| Sensor | State |
| --- | --- |
| `sensor.ul_transport_api_requests` | Upstream requests made since Home Assistant started |

The request counter is there to watch the Trafiklab quota, so it counts
calls rather than successes — a 304 or a 429 is spent quota too. Its
attributes break the total down per feed, with `trafiklab_total` and
`trafiklab_per_hour` as the numbers the quota actually sees (UL's own
departure API is separate and unmetered).

The state is a **number of minutes**, floored, so a numeric_state trigger is all
an automation needs:

```yaml
trigger:
  - platform: numeric_state
    entity_id: sensor.sommarro_uppsala_next_departure
    below: 6
action:
  - service: notify.mobile_app
    data:
      message: >
        Line {{ state_attr('sensor.sommarro_uppsala_next_departure', 'line') }}
        leaves in {{ states('sensor.sommarro_uppsala_next_departure') }} minutes.
```

**Attributes**: `line`, `direction`, `transport`, `stop_name`, `departure` (the
real-time timestamp, or the planned one when there is no live report),
`scheduled_departure`, `delay_minutes` (positive is late, `null` when the bus
has no real-time data at all), `is_realtime`, and `next_departures` /
`next_departures_in` for the four after it.

The state only moves when the board is polled, so it is up to
`scan_interval` seconds behind. For a live countdown use the map card, which
refreshes on its own while it is on screen.

Departures-card sensors from earlier versions - ISO-timestamp states with
`planned_departure_time_*` attributes - are removed on upgrade. This integration
now stands on its own; use the live map card below to display departures.

## Live Map

Shows the buses actually heading to your configured stops, with real-time
position, arrival time and how many stops away they are.

**No entities are created.** The map fetches only while a card is on screen, so
nothing is polled when nobody is looking, and no vehicle data reaches the state
machine or the recorder.

### Setup

1. Register at [trafiklab.se](https://www.trafiklab.se/) and create a project
2. Add **two** datasets to it and copy a key for each:
   - **GTFS Regional** (static) — line names, stop names, route geometry
   - **GTFS Regional Realtime** — vehicle positions and arrival predictions
3. In Home Assistant, click **Configure** on any UL Transport stop and paste
   both keys. They are account-wide, so you only enter them once: every other
   stop then says which stop holds them instead of asking again.

The first load downloads a 26 MB static feed and builds an index; that takes a
few seconds and then refreshes in the background every 6 hours.

### Cards

Add **UL Transport Live Map** from the card picker — it shows a live preview
there and has a visual editor, so no YAML is needed. Pick the stop, tick the
lines you care about, drag the radius slider until the map frames what you want.

The YAML equivalent, for an overview of everything inbound to one stop:

```yaml
type: custom:ul-transport-map
stop_id: 3700600
```

A single line with its route drawn:

```yaml
type: custom:ul-transport-map
stop_id: 3700600
line: "2"
```

Clicking a bus or an arrival row in the overview switches to that line's view.

| Option | Default | Meaning |
| --- | --- | --- |
| `stop_id` | required | The UL stop id, same one the integration was configured with |
| `lines` | all | Only show these lines, e.g. `["2", "7"]` |
| `destinations` | all | Only show departures going to these, e.g. `["Stockholm city"]` |
| `line` | – | Pin the card to one line's route view instead of the overview |
| `content` | `both` | `both`, `map` or `list` |
| `include_positions` | `true` | Fetch the vehicle positions feed. `false` halves the requests, at the cost of not knowing whether a timetabled departure is already on the road |
| `layout` | `auto` | `auto`, `stacked` or `wide` |
| `refresh` | `5` (`20` for a list) | Seconds between polls while the card is visible |
| `horizon_minutes` | `30` | Arrivals further out than this are listed but not plotted |
| `list_minutes` | `90` | How far ahead the list looks, up to `1440` for a whole day |
| `list_count` | `8` | Rows in the list, and the cap on what the server sends |
| `linger` | `45` | Seconds a departed bus stays on the map; `0` removes it at once |
| `kinds` | all but `Skolbuss` | `route_desc` values to include, e.g. `["Stadsbuss"]` for city buses only |
| `radius` | `800` | Metres around the stop the map frames initially |
| `show_header` | `true` | Hide the header for a bare map |
| `show_track` | `false` | Show the platform or track each departure leaves from |
| `absolute_after` | `20` | Minutes past which a row shows a clock time instead of a countdown; `0` for clock times throughout |
| `title` | stop name | Header title override |
| `animate` | `true` | Move the buses between refreshes, along their route |
| `tap_action` | open the line | `navigate` or `url`; what tapping an arrival does |

The card has no height setting: it fills whatever the dashboard gives it, so
size it with **Layout** in the card editor (or `grid_options` in YAML) like any
built-in card. The map takes what the list leaves.

`layout: auto` puts the arrival list beside the map once the card is wider than
620 px. That is measured on the card, not the window, so a card in a
three-column dashboard stays stacked — give it the full width of a section to
get the side-by-side layout, or force it either way with `stacked` / `wide`.

Each arrival shows how far off schedule it is: `+2` two minutes late (amber,
red past five), `-1` early (blue), `on time` within a minute (green). Hovering
a bus - on the map or in the list - says which stop it is heading for and when
it gets there.

### A list on the overview, the map behind it

`content: list` drops the map and defaults to a 20-second refresh, since nothing
on screen moves between stops — four times cheaper than an open map.

It still reads the positions feed. It has nothing to plot with it, but positions
are the only thing that can tell a departure the feed is silent about ("no live
data") from one that is out on the road right now, and a list that calls a bus
you could watch move "not yet running" is wrong in the way people notice. Set
`include_positions: false` to trade that back for half the requests.

That makes a cheap card for a dashboard overview, with the full map on a
subview one tap away:

```yaml
type: custom:ul-transport-map
stop_id: 3700600
content: list
tap_action:
  action: navigate
  navigation_path: /dashboard-ul/map
```

The overview frames the map by distance around your stop rather than by a zoom
level, because fitting to the buses zooms out to wherever the furthest one
happens to be. `radius: 300` is street level, `800` is the neighbourhood, `3000`
is most of the city. The line view ignores it and frames the whole route.

`horizon_minutes` and `list_minutes` are separate on purpose: a bus 50 minutes
out is worth reading in the list but would drag the viewport across the county
if it were plotted. Rows further ahead in time than `horizon_minutes` are dimmed
— by time, not by whether a bus has been assigned yet, or a terminus like
Uppsala Centralstation would grey out its whole board.

### The whole day, not just what is moving

The realtime feeds only describe buses that are already running - UL publishes
around 65 trip updates for the whole county - so past roughly forty minutes the
list would otherwise run dry no matter how high `list_minutes` goes.

Beyond that it continues from the static timetable, which covers every line
calling at your stop for the whole service day, including the ones that only
run in the morning. `list_minutes: 1440` with a high `list_count` turns the card
into a departure board for the day. It costs no request - the timetable is the
same index the map already builds - and the server only sends `list_count` rows,
so a hub with two thousand daily departures stays cheap.

Each row says what is behind it:

| Row | Meaning |
| --- | --- |
| *3 stops away* | A bus with a live position, plotted on the map |
| *running · no position* | Predicting arrivals but not reporting where it is |
| *no live data* | Timetabled, should be under way, nothing in the realtime feed |
| *not yet running* | Timetabled, has not left its first stop yet |
A bus that is running is never listed as *not yet running*: rows are matched to
realtime data by trip id, not by comparing line names and times.

Some trips report a position but publish no arrival prediction at all — every
line 7 towards Årsta Fyrislund, for one. Those rows count the stops from the
position like any other and take their time from the timetable; hover the row
to see that. They are not marked out in the list, because to someone watching
the bus move there is nothing second-hand about them.

### How live is "live"

The header says how old the data on screen is, not how long ago the card
asked — measured as `data_age` on the server plus the browser's own elapsed
time, so the two clocks never have to agree.

Measured against UL's feed: a position is about **2-3 seconds old** by the time
the feed serves it. On top of that sits the 5-second server-side cache and the
card's 5-second refresh, which are not in phase, so what you see is typically
**3-9 seconds behind reality and at worst about 13**.

That is what `animate` is for. Every UL vehicle reports speed, and the static
feed knows the road it is driving, so each bus is drawn where it has probably
got to by now: its reported point carried **along its own route** for the age of
that position plus the time since the refresh. Moving it four times a second is
the same calculation ticked over. Where the route is unknown — a diverted bus,
or one more than 200 m off its shape — it falls back to its reported bearing,
which is a straight line through the corner.

It is still a guess: it stops extrapolating after 25 seconds, and a bus that
pulls into a stop will run on until the next position corrects it. When a
correction arrives the marker eases into it rather than jumping, unless the
error is bigger than 150 m, which is a different bus rather than a bad guess.
Set `animate: false` for raw reported positions.

A bus is kept on the map after it has called at your stop, dimmed and marked
*departed · leaving the stop*, so you see it pull away instead of blinking out
at the kerb. `linger` sets how long, in seconds; `linger: 0` removes it the
moment it is due away.

*Departed* means gone, not overdue: where there is a position, the bus has to
be past your stop on its own route before the row says so. A prediction running
out while the bus is still at the kerb — doors open, a queue boarding — is not
evidence that it left.

### Notes and limits

- **Direction names match the departure board.** GTFS calls line 7's directions
  by their last physical stop ("Uppsala Jenny Linds väg"); UL calls the same
  thing "Norby Gottsunda". The map pairs each GTFS `direction_id` with UL's own
  name by matching place names along the route, so both halves of the dashboard
  agree. Every line on UL's board is used, including ones you did not select
  for the sensors. A direction that cannot be matched falls back to the GTFS
  terminus - which is what the trains fall back to, since a route like Mälartåg
  runs to four different places in the same GTFS direction.
- **Services that end at your stop are not listed.** A quarter of the trips
  calling at Uppsala Centralstation terminate there; as departures they say
  they are going to the stop you are standing at. A line that loops back to
  your stop is still listed, at the call you can board it.
- **"N stops away" is derived, not published.** UL's feed carries no
  `current_stop_sequence`, so position along the trip is inferred from the next
  predicted call - or, for a bus nothing is predicting for, from which of its
  own calls it is nearest to. It is usually right and occasionally reads low.
  Arrival times come straight from the feed and are reliable, except on the
  trips below that publish none.
- **Some directions get no predictions at all.** Line 7 towards Årsta Fyrislund
  runs three buses that report their positions and nothing else - no arrival
  predictions are published for that direction. Those rows fall back to the
  timetable for the time and to the position for the distance, rather than
  showing a dash. Watched against the feed, one such bus counted down 1.6 min →
  0.2 min and was past the stop 84 seconds later, so the timetable is a decent
  stand-in on this line; there is no delay figure for them either way.
- **Filtering by destination** is the useful cut where one line splits or where
  a station is served by trains going several ways: `destinations: ["Stockholm
  city"]` keeps the Stockholm trains and drops the ones to Gävle. The names are
  the same ones the rows show, and combine with `lines` as an *and*.
- Only vehicles on trips that call at one of your configured stops are shown —
  that is the point, but it does mean the map is not a county-wide view.
- Around 40% of UL vehicles run without a trip assignment (deadheading, out of
  service). They have positions but nothing can be said about them, so they are
  omitted.
- School buses are the only thing hidden by default; 119 of UL's 240 routes are
  `Skolbuss` and they otherwise swamp the map. Everything else your stop is
  offered in the line picker - trains included - is shown.
- Leaflet is loaded from a CDN. To self-host, drop `leaflet.js`/`leaflet.css`
  into `custom_components/ul_transport/www/` and repoint the two constants at
  the top of `ul-transport-map.js`.

### Quota

Trafiklab's free Bronze tier allows 30,000 requests per 30 days. Each refresh
costs 2 requests (positions + predictions) shared across every viewer, so at the
default 5-second refresh an open map costs ~24 requests/minute — roughly 20
hours of map-watching per month. A `content: list` card refreshes every 20
seconds instead, so it costs 6 requests/minute, or 3 with
`include_positions: false`. `sensor.ul_transport_api_requests` reports what has
actually been spent.
Polling stops when the card is removed from the DOM or the browser tab is
hidden. If you keep the map open on a wall panel, ask Trafiklab for a free quota
upgrade.

## Assist

The integration exposes **UL Transport departures** as an Assist LLM API, so a
conversation agent can answer "when does the next bus go?" out loud.

**Settings** → **Devices & Services** → your conversation agent → **Configure**,
and tick *UL Transport departures* under the exposed APIs.

The agent gets one tool, `get_next_departures`, with an optional stop and an
optional line. Stop names are matched loosely, because speech-to-text gives
"Vaksala torg" for a stop configured as "Uppsala Vaksala torg"; with a single
stop configured, a name that matches nothing is treated as a mishearing rather
than as a different stop.

The result also carries a `card` key holding a live map config for the stop it
just answered about. Assistants without a screen ignore it.

[Voice Satellite](https://github.com/jxlarrea/voice-satellite-card-integration)
**2026.8.10 or later** draws it: any tool result with a `card` key is mounted in
its media panel while the answer is spoken, no configuration on either side. It
resolves `custom:` cards from **Settings → Dashboards → Resources**, which is
where this integration registers its card already — so nothing extra is needed
unless your Lovelace runs in YAML mode, where the card has to be listed in
`resources:` by hand. Size it with the satellite's own **Text Scale** slider.

## API & Data

**Endpoints**:
- Stop search: `https://www.ul.se/api/journey/stops`
- Departures: `https://api.ul.se/api/v3/stop/{stop_id}`

**Update interval**: configurable, 30-600 seconds (default 60)

## Troubleshooting

**No stops found**: Check spelling or try a shorter search term

**Rate limit errors**: Increase the update interval or wait a few minutes between configuration changes

**Missing real-time data**: Not all departures have real-time updates (`estimated_departure_time` will be `null`)

**The map card shows "custom element doesn't exist"**: the card is registered as
a dashboard resource, which a browser picks up the next time it opens a
dashboard. A tab left open across the upgrade — a wall panel, usually — needs one
hard reload. If it still does not appear, check that
**Settings → Dashboards → Resources** lists `/ul_transport/ul-transport-map.js`.

## Development

```bash
pip install -r requirements_test.txt
```

```bash
pytest
```

```bash
ruff check custom_components tests
```

```bash
ruff format custom_components tests
```

Every push runs `hassfest`, HACS validation, `ruff` and the test suite — see
[.github/workflows/validate.yml](.github/workflows/validate.yml).

## Support

[GitHub Issues](https://github.com/AlexanderBabel/ul-transport/issues)

## Credits

Developed by [@AlexanderBabel](https://github.com/AlexanderBabel) • Data by UL (Uppsala Läns Trafik) • MIT License
