# Live map card — full reference

The short version lives in the [README](../README.md#live-map-card). This page
is everything else: every option, what the rows mean, how far behind reality
the map is, and what the free Trafiklab quota actually buys you.

- [Card options](#card-options)
- [Sizing and layout](#sizing-and-layout)
- [The arrival list](#the-arrival-list)
- [A whole day, not just what is moving](#a-whole-day-not-just-what-is-moving)
- [How live is "live"](#how-live-is-live)
- [Quota](#quota)
- [Known limitations](#known-limitations)

## Card options

Every option has a control in the visual editor, so you only need this table if
you write YAML by hand.

| Option | Default | Meaning |
| --- | --- | --- |
| `stop_id` | required | The UL stop id, the same one the integration was configured with |
| `lines` | all | Only show these lines, e.g. `["3", "5"]` |
| `destinations` | all | Only show departures going to these, e.g. `["Stockholm city"]` |
| `line` | – | Pin the card to one line's route view instead of the overview |
| `content` | `both` | `both`, `map` or `list` |
| `include_positions` | `true` (`false` for a list) | Fetch the vehicle positions feed. `false` halves the requests, at the cost of not knowing whether a timetabled departure is already on the road |
| `layout` | `auto` | `auto`, `stacked` or `wide` |
| `refresh` | `5` (`90` for a list) | Seconds between polls while the card is visible, up to `300` |
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

`lines` and `destinations` combine as an *and*: both have to match.
`destinations` is the useful cut where one line splits, or where a station is
served by trains going several ways — `destinations: ["Stockholm city"]` keeps
the Stockholm trains and drops the ones to Gävle. The names are the same ones
the rows show.

## Sizing and layout

The card has no height setting. It fills whatever the dashboard gives it, so
size it with **Layout** in the card editor (or `grid_options` in YAML) like any
built-in card. The map takes what the list leaves.

`layout: auto` puts the arrival list beside the map once the card is wider than
620 px. That is measured on the *card*, not the window, so a card in a
three-column dashboard stays stacked — give it the full width of a section to
get the side-by-side layout, or force it either way with `stacked` / `wide`.

The overview frames the map by distance around your stop rather than by a zoom
level, because fitting to the buses zooms out to wherever the furthest one
happens to be. `radius: 300` is street level, `800` is the neighbourhood, `3000`
is most of the city. The line view ignores `radius` and frames the whole route.

`horizon_minutes` and `list_minutes` are separate on purpose: a bus 50 minutes
out is worth reading in the list but would drag the viewport across the county
if it were plotted. Rows further ahead in time than `horizon_minutes` are dimmed
— by time, not by whether a bus has been assigned yet, or a terminus like
Uppsala Centralstation would grey out its whole board.

## The arrival list

Each arrival shows how far off schedule it is: `+2` two minutes late (amber, red
past five), `-1` early (blue), `on time` within a minute (green). Hovering a bus
— on the map or in the list — says which stop it is heading for and when it gets
there.

Each row also says what is behind it:

| Row | Meaning |
| --- | --- |
| *3 stops away* | A bus with a live position, plotted on the map |
| *running · no position* | Predicting arrivals but not reporting where it is |
| *no live data* | Timetabled, should be under way, nothing in the realtime feed |
| *not yet running* | Timetabled, has not left its first stop yet |

A bus that is running is never listed as *not yet running*: rows are matched to
realtime data by trip id, not by comparing line names and times.

Some trips report a position but publish no arrival prediction at all — on a
few lines that is true of a whole direction. Those rows count the stops from the
position like any other and take their time from the timetable; hover the row to
see that. They are not marked out in the list, because to someone watching the
bus move there is nothing second-hand about them.

A bus is kept on the map after it has called at your stop, dimmed and marked
*departed · leaving the stop*, so you see it pull away instead of blinking out at
the kerb. `linger` sets how long, in seconds; `linger: 0` removes it the moment
it is due away.

*Departed* means gone, not overdue: where there is a position, the bus has to be
past your stop on its own route before the row says so. A prediction running out
while the bus is still at the kerb — doors open, a queue boarding — is not
evidence that it left.

### A cheap list card, with the map one tap away

`content: list` drops the map and defaults to one request every 90 seconds — 36×
cheaper than an open map, and inside the quota even left up all day on a wall
panel. Nothing on screen moves between stops, and the countdowns are worked out
in the browser from the departure times, so they keep ticking down by the second
between polls.

It also skips the positions feed by default, which is the second of the two
requests. The cost is that a departure the realtime feed says nothing about reads
as *no live data* rather than as a bus already on the road. Set
`include_positions: true` to buy that back at double the requests.

That makes a cheap card for a dashboard overview, with the full map on a subview
one tap away:

```yaml
type: custom:ul-transport-map
stop_id: 3700062
content: list
tap_action:
  action: navigate
  navigation_path: /dashboard-ul/map
```

## A whole day, not just what is moving

The realtime feeds only describe buses that are already running — UL publishes
around 65 trip updates for the whole county — so past roughly forty minutes the
list would otherwise run dry no matter how high `list_minutes` goes.

Beyond that it continues from the static timetable, which covers every line
calling at your stop for the whole service day, including the ones that only run
in the morning. `list_minutes: 1440` with a high `list_count` turns the card into
a departure board for the day. It costs no extra request — the timetable is the
same index the map already builds — and the server only sends `list_count` rows,
so a hub with two thousand daily departures stays cheap.

```yaml
type: custom:ul-transport-map
stop_id: 3700062
content: list
list_minutes: 1440
list_count: 30
absolute_after: 0
```

## How live is "live"

The header says how old the data on screen is, not how long ago the card asked —
measured as `data_age` on the server plus the browser's own elapsed time, so the
two clocks never have to agree.

Measured against UL's feed: a position is about **2–3 seconds old** by the time
the feed serves it. On top of that sits the 5-second server-side cache and the
card's 5-second refresh, which are not in phase, so what you see is typically
**3–9 seconds behind reality and at worst about 13**.

That is what `animate` is for. Every UL vehicle reports speed, and the static
feed knows the road it is driving, so each bus is drawn where it has probably got
to by now: its reported point carried **along its own route** for the age of that
position plus the time since the refresh. Moving it four times a second is the
same calculation ticked over. Where the route is unknown — a diverted bus, or one
more than 200 m off its shape — it falls back to its reported bearing, which is a
straight line through the corner.

It is still a guess: it stops extrapolating after 25 seconds, and a bus that
pulls into a stop will run on until the next position corrects it. When a
correction arrives the marker eases into it rather than jumping, unless the error
is bigger than 150 m, which is a different bus rather than a bad guess. Set
`animate: false` for raw reported positions.

## Quota

Trafiklab's free Regional tier allows **30,000 realtime requests per 30 days** —
about 1,000 a day — and **60 static downloads**. Every viewer of a stop shares one
upstream fetch, so the cost is per card configuration, not per person:

| Card | Requests/refresh | Cost while on screen | Left up 24/7 |
| --- | --- | --- | --- |
| `content: map` or `both`, 5 s | 2 | ~1,440/hour | far over |
| `content: list`, 90 s (default) | 1 | 40/hour | ~960/day |
| `content: list`, 90 s, `include_positions: true` | 2 | 80/hour | ~1,920/day |

So an open map costs roughly a day of budget per hour: it is for looking at, not
for leaving up. A default list card fits inside the quota even on a wall panel.
`refresh` accepts up to 300 seconds if you want more headroom.

Polling stops when the card is removed from the DOM or the browser tab is hidden,
and the integration refuses to call out at all once the 30-day count reaches
30,000 — the card goes stale rather than the key getting blocked. Watch
`sensor.trafiklab_api_realtime_requests_30_days` for where you actually stand,
and ask Trafiklab for a free quota upgrade if a map really does need to stay
open.

The static feed is downloaded at most once a day (60 per 30 days allows two), so
the only way to run that one out is repeatedly reloading the integration.

## Known limitations

- **Direction names match the departure board.** GTFS names a direction after
  the route's last physical stop; UL names it after the districts it serves, and
  the two are rarely the same string. The map pairs each GTFS `direction_id`
  with UL's own name by matching place names along the route, so both halves of
  the dashboard agree. Every line on UL's board is used, including ones you did not select for
  the sensors. A direction that cannot be matched falls back to the GTFS
  terminus — which is what the trains fall back to, since a route like Mälartåg
  runs to four different places in the same GTFS direction.
- **Services that end at your stop are not listed.** A quarter of the trips
  calling at Uppsala Centralstation terminate there; as departures they say they
  are going to the stop you are standing at. A line that loops back to your stop
  is still listed, at the call you can board it.
- **"N stops away" is derived, not published.** UL's feed carries no
  `current_stop_sequence`, so position along the trip is inferred from the next
  predicted call — or, for a bus nothing is predicting for, from which of its own
  calls it is nearest to. It is usually right and occasionally reads low. Arrival
  times come straight from the feed and are reliable, except on the trips below
  that publish none.
- **Some directions get no predictions at all.** A handful of directions run
  buses that report their positions and nothing else. Those rows fall back to
  the timetable for the time and to the position for the distance, rather than
  showing a dash. Watched against the feed, one such bus counted down
  1.6 min → 0.2 min and was past the stop 84 seconds later, so the timetable is
  a decent stand-in; there is no delay figure for them either way.
- **Only vehicles calling at one of your configured stops are shown.** That is
  the point, but it does mean the map is not a county-wide view.
- **Unassigned vehicles are omitted.** Around 40% of UL vehicles run without a
  trip assignment (deadheading, out of service). They have positions but nothing
  can be said about them.
- **School buses are hidden by default.** 119 of UL's 240 routes are `Skolbuss`
  and they otherwise swamp the map. Everything else your stop is offered in the
  line picker — trains included — is shown. Add them back with
  `kinds: ["Skolbuss", "Stadsbuss"]` or whatever your stop serves.
- **Leaflet is loaded from a CDN.** To self-host, drop `leaflet.js` and
  `leaflet.css` into `custom_components/ul_transport/www/` and repoint the two
  constants at the top of `ul-transport-map.js`.
