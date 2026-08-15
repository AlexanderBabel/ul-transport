/**
 * UL Transport live map card.
 *
 * Pulls vehicle positions over the websocket only while the card is actually
 * on screen, so nothing is fetched when nobody is looking. No entities are
 * involved; see live.py for why.
 *
 * ponytail: Leaflet is loaded from a CDN rather than vendored. Self-host by
 * dropping leaflet.js/leaflet.css next to this file and pointing LEAFLET_JS /
 * LEAFLET_CSS at /ul_transport/leaflet.js and /ul_transport/leaflet.css.
 */

const LEAFLET_JS = "https://unpkg.com/leaflet@1.9.4/dist/leaflet.js";
const LEAFLET_CSS = "https://unpkg.com/leaflet@1.9.4/dist/leaflet.css";

let leafletPromise = null;

/**
 * `a ?? b`, spelled out.
 *
 * Home Assistant still ships a legacy bundle and picks it from a strict
 * user-agent test - anything below roughly Chrome 109 or iOS 18.5 gets it, and
 * a wall tablet is exactly that. HA renders fine there; a card written in
 * syntax those engines cannot parse does not, and the failure is silent: the
 * module never runs, the element is never defined, and the dashboard shows a
 * bare error box. So this file stays on ES2017 - no `??`, no `?.`.
 */
function or(value, fallback) {
  return value === undefined || value === null ? fallback : value;
}

function loadLeaflet() {
  if (window.L) return Promise.resolve(window.L);
  if (leafletPromise) return leafletPromise;
  leafletPromise = new Promise((resolve, reject) => {
    const css = document.createElement("link");
    css.rel = "stylesheet";
    css.href = LEAFLET_CSS;
    document.head.appendChild(css);
    const script = document.createElement("script");
    script.src = LEAFLET_JS;
    script.onload = () => resolve(window.L);
    script.onerror = () => reject(new Error("Could not load Leaflet"));
    document.head.appendChild(script);
  });
  return leafletPromise;
}

/**
 * How long until it leaves, or when.
 *
 * A countdown is what you want for the next bus and useless for the one after
 * lunch - nobody plans around "83 min". Past `opts.after` minutes it becomes a
 * clock time, in the user's own Home Assistant language.
 */
function minutesLabel(vehicle, opts = {}) {
  if (vehicle.eta_minutes === undefined || vehicle.eta_minutes === null) return "–";
  // The line view keeps buses that have already called at your stop.
  if (vehicle.passed || vehicle.departed) return "departed";
  if (opts.after !== undefined && vehicle.eta_minutes >= opts.after) {
    return new Date(vehicle.eta * 1000).toLocaleTimeString(opts.locale || undefined, {
      hour: "2-digit",
      minute: "2-digit",
      // "language" and "system" mean "whatever the locale does"; the other two
      // are the user overriding it in their Home Assistant profile.
      ...(opts.hour12 === undefined ? {} : { hour12: opts.hour12 }),
    });
  }
  if (vehicle.eta_minutes < 0.5) return "now";
  return `${Math.round(vehicle.eta_minutes)} min`;
}

function delayLabel(vehicle) {
  if (!vehicle.delay) return "on time";
  const minutes = Math.round(Math.abs(vehicle.delay) / 60);
  if (minutes < 1) return "on time";
  return vehicle.delay > 0 ? `${minutes} min late` : `${minutes} min early`;
}

/** Signed minutes off schedule, as shown next to the ETA. */
function delayChip(vehicle) {
  if (vehicle.delay === undefined || vehicle.delay === null) return null;
  const minutes = Math.round(vehicle.delay / 60);
  if (minutes === 0) return { text: "on time", cls: "ontime" };
  if (minutes < 0) return { text: `${minutes}`, cls: "early" };
  return { text: `+${minutes}`, cls: minutes >= 5 ? "verylate" : "late" };
}

function agoLabel(seconds) {
  if (!(seconds >= 0)) return "";
  if (seconds < 60) return `${Math.round(seconds)}s ago`;
  return `${Math.round(seconds / 60)} min ago`;
}

function stopsLabel(vehicle) {
  // A timetabled departure with no vehicle behind it means one of two things,
  // and "not yet running" is only one of them.
  if (vehicle.scheduled && !vehicle.live) {
    if (!vehicle.started) return "not yet running";
    // null, not false: the positions feed was never fetched, so having no
    // position is our own doing rather than the bus's. See live.py.
    return vehicle.live === null ? "timetabled arrival" : "no live data";
  }
  // Timetabled time, live position: on the road but not predicting arrivals.
  // Where it has got to comes from that position and is as live as any other
  // row, so it reads the same - the caveat about where the time came from is in
  // the row's tooltip, not shouted at someone watching the bus move.
  if (vehicle.scheduled) return awayLabel(vehicle) || "timetabled arrival";
  if (vehicle.departed) return "leaving the stop";
  // Predicting arrivals but not reporting a position - roughly a third of them.
  if (vehicle.live === false) return "running · no position";
  return awayLabel(vehicle);
}

/** Derived, not published by the feed - see _stops_away in live.py. */
function awayLabel(vehicle) {
  if (vehicle.stops_away === undefined || vehicle.stops_away === null) return "";
  if (vehicle.stops_away === 0) return "next stop";
  if (vehicle.stops_away === 1) return "1 stop away";
  return `${vehicle.stops_away} stops away`;
}

/**
 * Where the bus is right now, in its own words.
 *
 * Both timestamps come from the server, so this never compares clocks across
 * machines - the answer is as of the data, not as of the browser.
 */
function nextStopLabel(vehicle, generated) {
  if (!vehicle.next_stop) return "";
  const seconds = vehicle.next_stop_eta - generated;
  if (seconds <= 20) return `At ${vehicle.next_stop}`;
  if (seconds < 60) return `Next stop ${vehicle.next_stop} · ${Math.round(seconds)}s`;
  return `Next stop ${vehicle.next_stop} · ${Math.round(seconds / 60)} min`;
}

// Dead reckoning is a guess that gets worse the longer it runs: a bus turns a
// corner, or stops, and the marker keeps going. Past this it just holds still.
const MAX_EXTRAPOLATION = 25;
const METRES_PER_DEGREE = 111320;
const TICK_MS = 250;
// A wrong guess is corrected by gliding, not by teleporting - except when the
// correction is so large that gliding would drag the marker across the map.
const EASE = 0.22;
const SNAP_METRES = 150;
// Swinging the arrow straight onto every step makes it twitch on each refresh;
// a bus does not turn 40 degrees in a quarter of a second.
const TURN_EASE = 0.25;

function metres(a, b) {
  const scale = Math.cos((a[0] * Math.PI) / 180);
  return (
    Math.hypot(b[0] - a[0], (b[1] - a[1]) * scale) * METRES_PER_DEGREE
  );
}

/** Compass degrees from a to b, to match the feed's own bearing. */
function bearing(a, b) {
  const scale = Math.cos((a[0] * Math.PI) / 180);
  return (Math.atan2((b[1] - a[1]) * scale, b[0] - a[0]) * 180) / Math.PI;
}

/** Signed degrees from heading a to heading b, the short way round. */
function turn(a, b) {
  return ((b - a + 540) % 360) - 180;
}

/** Walk `distance` metres along a polyline, stopping at its end. */
function along(path, distance) {
  let left = distance;
  for (let i = 1; i < path.length; i++) {
    const leg = metres(path[i - 1], path[i]);
    if (leg >= left) {
      const share = leg ? left / leg : 0;
      return [
        path[i - 1][0] + (path[i][0] - path[i - 1][0]) * share,
        path[i - 1][1] + (path[i][1] - path[i - 1][1]) * share,
      ];
    }
    left -= leg;
  }
  return path[path.length - 1];
}

function navigate(path) {
  history.pushState(null, "", path);
  window.dispatchEvent(new CustomEvent("location-changed", { bubbles: true, composed: true }));
}

class ULTransportMap extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._map = null;
    this._layers = null;
    this._buses = new Map();
    this._timer = null;
    this._tick = null;
    this._line = null;
    this._tripId = null;
    this._data = null;
    this._error = null;
    this._onVisibility = () => {
      if (document.hidden) this._stopPolling();
      else this._startPolling();
    };
  }

  static getConfigElement() {
    return document.createElement("ul-transport-map-editor");
  }

  /** Default config for the card picker; picks the first configured stop. */
  static async getStubConfig(hass) {
    try {
      const { stops } = await hass.callWS({ type: "ul_transport/map/stops" });
      if (stops && stops.length) return { stop_id: stops[0].stop_id };
    } catch (err) {
      // No keys configured yet - the card will render its own error, which is
      // more useful in the picker than a card that silently fails to appear.
    }
    return { stop_id: 0 };
  }

  setConfig(config) {
    if (!config.stop_id) {
      throw new Error("ul-transport-map: stop_id is required");
    }
    const previous = this._config;
    this._config = {
      content: "both",
      layout: "auto",
      // A list has no positions to animate, so it does not need five-second
      // freshness - and every refresh it skips is a request saved.
      refresh: config.content === "list" ? 20 : 5,
      radius: 800,
      list_count: 8,
      linger: 45,
      show_header: true,
      animate: true,
      // Past twenty minutes a countdown stops being something you act on.
      absolute_after: 20,
      show_track: false,
      ...config,
    };
    // Otherwise dragging the radius slider in the editor changes nothing
    // visible, since the map is deliberately framed only once.
    if (previous && previous.radius !== this._config.radius) this._fitted = false;
    this._line = config.line || null;
    this._render();
    // Leaving the dashboard editor re-configures the card in place, and can do
    // it before the element is connected. Restarting here rather than only in
    // connectedCallback is what keeps it refreshing afterwards.
    if (previous && previous.refresh !== this._config.refresh) this._stopPolling();
    if (this.isConnected) this._startPolling();
    // Switching content in the editor changes which feeds are wanted.
    if (previous && previous.content !== this._config.content) this._load();
  }

  set hass(hass) {
    const first = !this._hass;
    this._hass = hass;
    if (first) this._load();
    // Watchdog. Home Assistant pushes hass constantly, and _startPolling is a
    // no-op while polling is healthy - so a card that lost its timer somewhere
    // in the dashboard editor's teardown picks it back up within a second.
    else this._startPolling();
  }

  getCardSize() {
    return this._config.content === "list" ? 5 : 9;
  }

  /** Sections view: let the dashboard size the card, and the map follow. */
  getGridOptions() {
    const list = this._config.content === "list";
    return {
      rows: list ? "auto" : 8,
      min_rows: list ? 1 : 3,
      columns: 12,
      min_columns: 6,
    };
  }

  connectedCallback() {
    document.addEventListener("visibilitychange", this._onVisibility);
    this._startPolling();
  }

  disconnectedCallback() {
    // The whole point: navigating away stops the upstream requests.
    document.removeEventListener("visibilitychange", this._onVisibility);
    this._stopPolling();
  }

  _startPolling() {
    if (this._timer || !this._config) return;
    if (!this.isConnected || document.hidden) return;
    // A half-cleared pair from a previous life would otherwise keep ticking
    // for the lifetime of the page.
    this._stopPolling();
    // The card picker mounts every card at once; polling them all at 5s would
    // burn the Trafiklab quota just by opening the dialog.
    const seconds = this.preview ? 60 : this._config.refresh;
    this._timer = setInterval(() => this._load(), seconds * 1000);
    // Moves the buses between refreshes and ages the "x ago" in the header.
    // Local work only - it never touches the network.
    this._tick = setInterval(() => {
      this._buses.forEach((bus) => this._place(bus));
      if (this._data) this._renderAge();
    }, TICK_MS);
    this._load();
  }

  _stopPolling() {
    clearInterval(this._timer);
    clearInterval(this._tick);
    clearTimeout(this._retry);
    this._timer = null;
    this._tick = null;
  }

  get _hasMap() {
    return this._config.content !== "list";
  }

  /** Clock times in the language and clock Home Assistant is already using. */
  get _timeOpts() {
    const locale = (this._hass && this._hass.locale) || {};
    return {
      after: this._config.absolute_after,
      locale: locale.language,
      hour12:
        locale.time_format === "12"
          ? true
          : locale.time_format === "24"
          ? false
          : undefined,
    };
  }

  async _load() {
    if (!this._hass || !this._config) return;
    const message = this._line
      ? {
          type: "ul_transport/map/line",
          stop_id: this._config.stop_id,
          line: this._line,
          ...(this._tripId ? { trip_id: this._tripId } : {}),
        }
      : {
          type: "ul_transport/map/overview",
          stop_id: this._config.stop_id,
          // Positions cost a second upstream request and a list has nothing to
          // plot with them - but they are also the only thing that can say a
          // timetabled departure is actually out on the road, so they are worth
          // it. `include_positions: false` in YAML buys the request back.
          ...(this._config.include_positions === false
            ? { include_positions: false }
            : {}),
          // A whole-day list at a hub is thousands of departures; only the
          // rows that will be drawn are worth sending.
          limit: Math.min(this._config.list_count, 200),
          ...(this._config.linger !== undefined
            ? { linger_seconds: this._config.linger }
            : {}),
          ...(this._config.kinds ? { kinds: this._config.kinds } : {}),
          ...(this._config.lines && this._config.lines.length
            ? { lines: this._config.lines }
            : {}),
          ...(this._config.destinations && this._config.destinations.length
            ? { destinations: this._config.destinations }
            : {}),
          ...(this._config.horizon_minutes
            ? { horizon_minutes: this._config.horizon_minutes }
            : {}),
          ...(this._config.list_minutes
            ? { list_minutes: this._config.list_minutes }
            : {}),
        };
    try {
      this._data = await this._hass.callWS(message);
      // Wall clock, not data_age: the browser's clock may disagree with the
      // server's, so age is only ever measured as a delta on each side.
      this._received = Date.now();
      this._error = null;
      this._retries = 0;
    } catch (err) {
      this._error = err.message || String(err);
      // Home Assistant is still setting the integration up: a card that first
      // rendered during a restart should fill itself in, not sit on an error
      // until the next scheduled refresh.
      if (!this._data && (this._retries || 0) < 5) {
        this._retries = (this._retries || 0) + 1;
        clearTimeout(this._retry);
        this._retry = setTimeout(() => this._load(), 2000);
      }
    }
    this._render();
  }

  _showLine(line, tripId) {
    if (!this._hasMap) return;  // a route view with no map is just the list
    this._line = line;
    this._tripId = tripId || null;
    this._data = null;
    this._fitted = false;  // the two views frame different things
    this._load();
  }

  _showOverview() {
    this._line = this._config.line || null;
    this._tripId = null;
    this._data = null;
    this._fitted = false;
    this._load();
  }

  /** The subset of tap_action worth having on a transport card. */
  _act() {
    const action = this._config.tap_action;
    if (!action || action.action === "none") return false;
    if (action.action === "navigate" && action.navigation_path) {
      navigate(action.navigation_path);
      return true;
    }
    if (action.action === "url" && action.url_path) {
      window.open(action.url_path, "_blank", "noreferrer");
      return true;
    }
    return false;
  }

  _render() {
    if (!this.shadowRoot.querySelector("ha-card")) this._scaffold();
    const card = this.shadowRoot.querySelector("ha-card");
    const status = this.shadowRoot.querySelector(".status");

    card.dataset.content = this._config.content;
    this._applyLayout();
    this._renderHeader();

    if (this._error) {
      status.textContent = this._error;
      status.style.display = "block";
    } else {
      status.style.display = "none";
    }

    if (!this._data) return;
    this._renderList(this.shadowRoot.querySelector(".arrivals"));
    if (this._hasMap) this._renderMap();
  }

  /**
   * Stacked or side by side.
   *
   * Decided on the width of the card, not of the window: a card in a
   * three-column dashboard is narrow even on a wide screen.
   */
  _applyLayout(width) {
    const card = this.shadowRoot.querySelector("ha-card");
    if (!card) return;
    const layout = this._config.layout;
    const measured = or(width, card.getBoundingClientRect().width);
    const wide =
      layout === "wide" || (layout !== "stacked" && measured >= 620);
    card.toggleAttribute("data-wide", wide && this._config.content === "both");
  }

  _scaffold() {
    // Leaflet's stylesheet has to be inside the shadow root as well as in
    // document.head - style in the document does not cross the shadow
    // boundary, and without it the tiles are positioned as static images and
    // the map renders as scattered fragments.
    const leafletCss = document.createElement("link");
    leafletCss.rel = "stylesheet";
    leafletCss.href = LEAFLET_CSS;

    const style = document.createElement("style");
    style.textContent = `
      /* The dashboard decides how tall the card is - grid rows in a sections
         view, content in a masonry one - and the map takes what is left. */
      :host { display: block; height: 100%; }
      ha-card {
        overflow: hidden; display: flex; flex-direction: column; height: 100%;
      }
      .header {
        display: flex; align-items: center; gap: 12px;
        padding: 12px 16px;
      }
      .back {
        flex: none; width: 34px; height: 34px; padding: 0;
        display: inline-flex; align-items: center; justify-content: center;
        cursor: pointer; border: none; border-radius: 50%;
        background: var(--secondary-background-color);
        color: var(--primary-text-color);
      }
      .back:hover { background: var(--divider-color); }
      .back svg { width: 22px; height: 22px; fill: currentColor; }
      .titles { flex: 1; min-width: 0; }
      .title {
        font-size: 1.05rem; font-weight: 500; line-height: 1.25;
        overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
      }
      .subtitle {
        font-size: .75rem; color: var(--secondary-text-color);
        overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
      }
      .updated {
        flex: none; font-size: .72rem; color: var(--secondary-text-color);
        text-align: right; line-height: 1.3;
      }
      .chip {
        flex: none; min-width: 34px; height: 34px; box-sizing: border-box;
        display: inline-flex; align-items: center; justify-content: center;
        border-radius: 8px; padding: 0 8px; font-weight: 700; font-size: .95rem;
      }
      .status { padding: 8px 16px; color: var(--error-color); font-size: .85rem; }
      .body { flex: 1 1 auto; min-height: 0; display: flex; flex-direction: column; }
      .map { flex: 1 1 auto; min-height: 220px; }
      /* Shrinkable: in a short card the list gives way and scrolls rather
         than pushing itself out through the bottom edge. */
      .arrivals {
        flex: 0 1 auto; min-height: 0; max-height: 240px;
        overflow-y: auto; padding: 4px 8px 0;
      }
      /* The trailing gap as an element rather than as padding: WebKit drops
         bottom padding inside a scroll container, which cuts the last row. */
      .arrivals::after { content: ""; display: block; height: 12px; }
      ha-card[data-wide] .body { flex-direction: row; }
      ha-card[data-wide] .arrivals {
        flex: 0 0 34%; min-width: 210px; max-height: none; padding: 4px 8px 0;
        border-left: 1px solid var(--divider-color);
      }
      ha-card[data-content="map"] .arrivals { display: none; }
      /* No map to fill, so the card is as tall as its rows. */
      ha-card[data-content="list"] .map { display: none; }
      ha-card[data-content="list"] .arrivals { max-height: none; }
      .row {
        display: flex; align-items: center; gap: 10px;
        padding: 6px 8px; border-radius: 8px; cursor: pointer;
      }
      .row:hover { background: var(--secondary-background-color); }
      /* Listed but further ahead than the map reaches - see horizon in live.py. */
      .row.far { opacity: .62; }
      /* Been and gone: still there for a moment, but not something to run for. */
      .row.departed .eta { color: var(--secondary-text-color); font-weight: 500; }
      .badge {
        min-width: 30px; text-align: center; border-radius: 6px;
        padding: 3px 6px; font-weight: 600; font-size: .85rem;
      }
      .dest { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
      /* Which platform to stand on. Outlined rather than filled - it is where
         the bus is, not what the bus is, and must not read as a line badge. */
      .track {
        flex: none; font-size: .72rem; font-weight: 600; padding: 2px 6px;
        border-radius: 4px; border: 1px solid var(--divider-color);
        color: var(--secondary-text-color);
      }
      .meta { font-size: .75rem; color: var(--secondary-text-color); }
      .eta { font-weight: 600; min-width: 54px; text-align: right; line-height: 1.2; }
      .delay { font-size: .72rem; font-weight: 600; }
      .delay.ontime { color: var(--success-color, #2e7d32); }
      .delay.late { color: var(--warning-color, #ef6c00); }
      .delay.verylate { color: var(--error-color, #c62828); }
      .delay.early { color: var(--info-color, #0288d1); }
      .empty { padding: 12px 16px; color: var(--secondary-text-color); font-size: .9rem; }
      .bus-wrap { position: relative; width: 36px; height: 36px; }
      .dir { position: absolute; inset: 0; }
      .tip {
        position: absolute; left: 50%; top: 0; margin-left: -5px;
        width: 0; height: 0;
        border-left: 5px solid transparent; border-right: 5px solid transparent;
        border-bottom: 8px solid currentColor;
        filter: drop-shadow(0 0 1px rgba(255,255,255,.9));
      }
      .bus {
        position: absolute; left: 5px; top: 5px; width: 26px; height: 26px;
        border-radius: 50%; border: 2px solid #fff; box-shadow: 0 0 3px rgba(0,0,0,.5);
        display: flex; align-items: center; justify-content: center;
        font-size: 11px; font-weight: 700; box-sizing: border-box;
      }
      /* Called at your stop already; kept a moment so you see it pull away. */
      .bus-wrap.departed { opacity: .55; }
    `;
    const card = document.createElement("ha-card");
    card.innerHTML = `
      <div class="header">
        <button class="back" title="Back to all lines" aria-label="Back to all lines">
          <svg viewBox="0 0 24 24"><path d="M15.4 7.4 14 6l-6 6 6 6 1.4-1.4-4.6-4.6z"/></svg>
        </button>
        <span class="chip"></span>
        <span class="titles">
          <div class="title"></div>
          <div class="subtitle"></div>
        </span>
        <span class="updated"></span>
      </div>
      <div class="status"></div>
      <div class="body">
        <div class="map"></div>
        <div class="arrivals"></div>
      </div>
    `;
    this.shadowRoot.innerHTML = "";
    this.shadowRoot.appendChild(leafletCss);
    this.shadowRoot.appendChild(style);
    this.shadowRoot.appendChild(card);
    card.querySelector(".back").addEventListener("click", () => this._showOverview());
    // One observer for both jobs: pick the layout, and tell Leaflet its box
    // moved - it never notices on its own.
    new ResizeObserver((entries) => {
      this._applyLayout(entries[0].contentRect.width);
      if (this._map) this._map.invalidateSize();
    }).observe(card);
  }

  _renderHeader() {
    const header = this.shadowRoot.querySelector(".header");
    header.style.display = this._config.show_header === false ? "none" : "flex";

    const data = this._data || {};
    const count = (data.vehicles || []).length;
    const chip = this.shadowRoot.querySelector(".chip");
    if (this._line) {
      chip.textContent = this._line;
      chip.style.display = "inline-flex";
      chip.style.background = data.color || "var(--secondary-background-color)";
      chip.style.color = data.text_color || "var(--primary-text-color)";
    } else {
      chip.style.display = "none";
    }

    this.shadowRoot.querySelector(".title").textContent =
      or(this._config.title, this._line ? `Line ${this._line}` : data.stop_name || "Live buses");
    this.shadowRoot.querySelector(".subtitle").textContent = this._data
      ? this._line
        ? `${count} on this line · ${data.stop_name || ""}`.replace(/ · $/, "")
        : `${count} inbound`
      : "loading…";
    this._renderAge();

    // Hidden when the card is pinned to a line - there is nothing to go back to.
    this.shadowRoot.querySelector(".back").style.display =
      this._line && !this._config.line ? "inline-flex" : "none";
  }

  /** Age of the data on screen, not time since the request. */
  _renderAge() {
    const updated = this.shadowRoot.querySelector(".updated");
    if (!updated) return;
    if (!this._data || !this._received) {
      updated.textContent = "";
      return;
    }
    const age = (this._data.data_age || 0) + (Date.now() - this._received) / 1000;
    updated.textContent = agoLabel(age);
    updated.title = `Positions recorded ${agoLabel(age)}, refreshed every ${
      this._config.refresh
    }s`;
  }

  _renderList(list) {
    const vehicles = this._data.vehicles || [];
    if (!vehicles.length) {
      list.innerHTML = `<div class="empty">No vehicles inbound right now.</div>`;
      return;
    }
    list.innerHTML = "";
    vehicles.slice(0, this._config.list_count).forEach((vehicle) => {
      const row = document.createElement("div");
      row.className = "row";
      // Dimmed for being further ahead than the map reaches - not for having no
      // bus assigned yet. At a terminus every departure starts there, and a
      // whole board greyed out reads as nothing happening when in fact
      // everything is.
      if (this._data.horizon && vehicle.eta_minutes > this._data.horizon) {
        row.classList.add("far");
      }
      if (vehicle.departed || vehicle.passed) row.classList.add("departed");
      const chip = delayChip(vehicle);
      row.title =
        nextStopLabel(vehicle, this._data.generated) ||
        (vehicle.scheduled && vehicle.live
          ? "This trip publishes no arrival predictions: the position is live, the time is the timetable."
          : "");
      row.innerHTML = `
        <span class="badge" style="background:${vehicle.color};color:${vehicle.text_color}">
          ${vehicle.line}
        </span>
        <span class="dest">
          ${vehicle.destination || ""}
          <div class="meta">${stopsLabel(vehicle)}</div>
        </span>
        ${
          this._config.show_track && vehicle.track
            ? `<span class="track" title="Platform ${vehicle.track}">${vehicle.track}</span>`
            : ""
        }
        <span class="eta">
          ${minutesLabel(vehicle, this._timeOpts)}
          ${chip ? `<div class="delay ${chip.cls}" title="${delayLabel(vehicle)}">${chip.text}</div>` : ""}
        </span>
      `;
      row.addEventListener("click", () => {
        // A configured tap action wins: on a dashboard overview the point of
        // the row is usually to open the full map somewhere else.
        if (!this._act()) this._showLine(vehicle.line, vehicle.trip_id);
      });
      list.appendChild(row);
    });
  }

  /**
   * Where the bus has probably got to since it reported.
   *
   * The reported position is already a few seconds old when it reaches us and
   * another refresh interval passes before the next one. Speed comes with
   * every UL position, and the route it is driving comes from the static feed,
   * so the gap is closed by walking it along its own road - falling back to
   * its heading where the route is unknown.
   */
  _target(bus) {
    const seconds = bus.age + (Date.now() - bus.at) / 1000;
    if (this._config.animate === false || !bus.speed || seconds <= 0) {
      return [bus.lat, bus.lon];
    }
    const distance = (bus.speed / 3.6) * Math.min(seconds, MAX_EXTRAPOLATION);
    if (bus.path && bus.path.length > 1) return along(bus.path, distance);
    if (bus.bearing === null || bus.bearing === undefined) return [bus.lat, bus.lon];
    const radians = (bus.bearing * Math.PI) / 180;
    return [
      bus.lat + (distance * Math.cos(radians)) / METRES_PER_DEGREE,
      bus.lon +
        (distance * Math.sin(radians)) /
          (METRES_PER_DEGREE * Math.cos((bus.lat * Math.PI) / 180)),
    ];
  }

  /** Draw it, easing towards the guess so a correction glides rather than jumps. */
  _place(bus) {
    const target = this._target(bus);
    const from = bus.shown;
    if (!from || metres(from, target) > SNAP_METRES) {
      bus.shown = target;
    } else {
      bus.shown = [
        from[0] + (target[0] - from[0]) * EASE,
        from[1] + (target[1] - from[1]) * EASE,
      ];
      // The marker follows the road it is being carried along; the reported
      // bearing is from wherever the bus was when it last spoke, which is a
      // corner ago. Point the arrow where the dot is actually going.
      if (metres(from, bus.shown) > 2) {
        const step = bearing(from, bus.shown);
        const current = or(or(bus.heading, bus.bearing), step);
        const delta = turn(current, step);
        // A step pointing backwards is a correction landing - a fresh position
        // behind where the dead reckoning had run to, or a bus put back at the
        // stop it is standing at. The bus has not turned round, so nor does the
        // arrow; anything gentler than that it eases into.
        if (Math.abs(delta) < 90) bus.heading = current + delta * TURN_EASE;
      }
    }
    bus.marker.setLatLng(bus.shown);
    const heading = or(bus.heading, bus.bearing);
    const element = bus.marker.getElement();
    const dir =
      heading === null || heading === undefined || !element
        ? null
        : element.querySelector(".dir");
    if (dir) dir.style.transform = `rotate(${heading}deg)`;
  }

  async _renderMap() {
    const L = await loadLeaflet();
    const container = this.shadowRoot.querySelector(".map");
    if (!this._map) {
      this._map = L.map(container, { attributionControl: true });
      L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
        maxZoom: 19,
        attribution: "© OpenStreetMap",
      }).addTo(this._map);
      this._layers = L.layerGroup().addTo(this._map);
      // Buses live in their own group: the static layers are wiped and rebuilt
      // on every refresh, while the markers have to survive so they can keep
      // moving between refreshes.
      this._busLayer = L.layerGroup().addTo(this._map);
      this._fitted = false;
    }
    this._layers.clearLayers();

    const bounds = [];

    (this._data.shape || []).length &&
      L.polyline(
        this._data.shape.map((p) => [p.lat, p.lon]),
        { color: "#0077c8", weight: 4, opacity: 0.6 }
      ).addTo(this._layers);

    (this._data.stops || []).forEach((stop) => {
      L.circleMarker([stop.lat, stop.lon], {
        radius: stop.mine ? 8 : 4,
        // Not UL red - that is line 2's colour, and a red dot next to a red
        // line-2 badge reads as another bus.
        color: stop.mine ? "#111" : "#666",
        fillColor: stop.mine ? "#fff" : "#fff",
        fillOpacity: 1,
        weight: stop.mine ? 4 : 2,
      })
        .bindTooltip(stop.name)
        .addTo(this._layers);
      if (stop.mine) bounds.push([stop.lat, stop.lon]);
    });

    const seen = new Set();
    (this._data.vehicles || []).forEach((vehicle) => {
      // Listed but outside the map horizon: no marker, so the viewport is not
      // dragged across the county by a bus that is 50 minutes out.
      if (vehicle.on_map === false) return;
      seen.add(vehicle.id);
      // The heading is a triangle riding the rim of the badge, rotated as a
      // whole. Rotating a glyph inside the badge tilts the line number with it.
      // The angle is set in _place rather than here: baked into the icon it
      // would only ever be as current as the last refresh, while the dot itself
      // keeps moving between them.
      const heading =
        vehicle.bearing === null || vehicle.bearing === undefined
          ? ""
          : `<div class="dir"><i class="tip" style="border-bottom-color:${vehicle.color}"></i></div>`;
      const html = `<div class="bus-wrap${vehicle.departed ? " departed" : ""}">
                 ${heading}
                 <div class="bus" style="background:${vehicle.color};color:${vehicle.text_color}">${vehicle.line}</div>
               </div>`;
      let bus = this._buses.get(vehicle.id);
      if (!bus) {
        const marker = L.marker([vehicle.lat, vehicle.lon], {
          icon: L.divIcon({ className: "", html, iconSize: [36, 36], iconAnchor: [18, 18] }),
        }).addTo(this._busLayer);
        marker.on("click", () => this._showLine(bus.line, bus.trip));
        bus = { marker, html };
        this._buses.set(vehicle.id, bus);
      } else if (bus.html !== html) {
        // Only when the bus actually turned - replacing the icon rebuilds its
        // DOM node, and doing that every refresh makes the markers flicker.
        bus.marker.setIcon(
          L.divIcon({ className: "", html, iconSize: [36, 36], iconAnchor: [18, 18] })
        );
        bus.html = html;
      }
      Object.assign(bus, {
        lat: vehicle.lat,
        lon: vehicle.lon,
        bearing: vehicle.bearing,
        speed: vehicle.speed,
        path: vehicle.path,
        age: vehicle.age || 0,
        at: Date.now(),
        line: vehicle.line,
        trip: vehicle.trip_id,
      });
      const where = nextStopLabel(vehicle, this._data.generated);
      bus.marker.bindTooltip(
        `Line ${vehicle.line} → ${vehicle.destination || "?"}<br>` +
          (where ? `${where}<br>` : "") +
          `${minutesLabel(vehicle, this._timeOpts)} · ${delayLabel(vehicle)}`
      );
      this._place(bus);
      bounds.push([vehicle.lat, vehicle.lon]);
    });

    this._buses.forEach((bus, id) => {
      if (seen.has(id)) return;
      this._busLayer.removeLayer(bus.marker);
      this._buses.delete(id);
    });

    // Fit once, then leave the viewport alone so refreshes don't yank the map
    // out from under someone who has panned or zoomed.
    if (!this._fitted) {
      const mine = (this._data.stops || []).find((s) => s.mine);
      if (this._line) {
        // The route is the point of this view, so frame the whole thing.
        const route = (this._data.shape || []).map((p) => [p.lat, p.lon]);
        const all = route.length ? route : bounds;
        if (all.length) this._map.fitBounds(all, { padding: [20, 20] });
      } else if (mine) {
        // Framed by distance around the stop rather than by a zoom level: what
        // people want here is "show me the streets around my stop", and fitting
        // to the buses instead zooms out to whichever one is furthest away.
        this._map.fitBounds(
          L.latLng(mine.lat, mine.lon).toBounds(this._config.radius * 2)
        );
      } else if (bounds.length) {
        this._map.fitBounds(bounds, { padding: [30, 30] });
      }
      this._fitted = bounds.length > 0 || !!mine;
    }
    setTimeout(() => this._map.invalidateSize(), 0);
  }
}

// Always attempt the definition, never gate it on a lookup. Home Assistant
// hands this file to the browser three ways, so a second run is normal - but a
// guard that asks "already defined?" first and gets a wrong answer skips the
// definition silently, and the card is then missing with nothing logged. A
// redefine throws, which is the harmless half of the trade.
try {
  customElements.define("ul-transport-map", ULTransportMap);
} catch (err) {
  // A card that cannot register itself is simply absent from the dashboard,
  // and all Home Assistant can say is that the element does not exist. Leave
  // the reason somewhere reachable rather than swallowing it.
  window.__ulTransportDefineError = String(err);
  console.error("ul-transport-map: could not define the card", err);
}

/** Visual editor, built on HA's own <ha-form> so it matches every other card. */
const LABELS = {
  stop_id: "Stop",
  lines: "Lines to show",
  destinations: "Destinations to show",
  line: "Pin to a single line (route view)",
  content: "Show",
  layout: "Layout",
  title: "Header title",
  show_header: "Show header",
  show_track: "Show platform",
  animate: "Move buses between refreshes",
  absolute_after: "Show clock time from",
  radius: "View around stop",
  refresh: "Refresh",
  horizon_minutes: "Plot on map up to",
  list_minutes: "List arrivals up to",
  list_count: "Rows in list",
  linger: "Keep departed buses",
  tap_action: "Tap behaviour",
};

const HELPERS = {
  lines: "Leave empty to show every line serving the stop.",
  destinations:
    "Leave empty for every direction. Useful where one line splits - trains to Stockholm but not to Gävle.",
  line: "Leave empty for the overview of all inbound buses.",
  title: "Leave empty to use the stop name.",
  show_track: "The platform or track the departure leaves from, where the timetable names one.",
  absolute_after:
    "Departures further out than this show a clock time instead of a countdown. Zero shows clock times for everything.",
  radius: "How far around the stop is framed when the map opens.",
  refresh: "Only polls while the card is on screen. A list costs one request per refresh, a map two.",
  horizon_minutes: "Arrivals later than this are still listed, just not plotted.",
  list_minutes: "Beyond what the live feed knows, the list continues from the timetable - up to a whole day.",
  list_count: "Also caps how much the server sends, so a whole-day list stays cheap.",
  linger: "How long a bus stays on the map after it has called at your stop. Zero removes it as it leaves.",
  animate: "Carries each bus along its route at its reported speed, which is a guess between refreshes.",
  tap_action: "What tapping an arrival does. Default opens that line's route on the map.",
};

/** Wireframes for the box selectors, in the style of the built-in cards. */
function preview(draw) {
  return (
    "data:image/svg+xml," +
    encodeURIComponent(
      `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 96 64">
         <rect x="1" y="1" width="94" height="62" rx="6" fill="none"
               stroke="#9e9e9e" stroke-width="2"/>${draw}</svg>`
    )
  );
}
const MAP_BLOCK = (x, y, w, h) =>
  `<rect x="${x}" y="${y}" width="${w}" height="${h}" rx="3" fill="#9e9e9e" opacity=".35"/>
   <circle cx="${x + w / 2}" cy="${y + h / 2}" r="4" fill="#9e9e9e"/>`;
const ROWS = (x, y, w, n) =>
  Array.from({ length: n }, (_, i) =>
    `<rect x="${x}" y="${y + i * 9}" width="${w}" height="4" rx="2" fill="#9e9e9e"/>`
  ).join("");

const PREVIEWS = {
  stacked: preview(MAP_BLOCK(8, 8, 80, 28) + ROWS(8, 42, 60, 2)),
  wide: preview(MAP_BLOCK(8, 8, 48, 48) + ROWS(62, 12, 26, 4)),
  auto: preview(
    MAP_BLOCK(8, 8, 34, 48) +
      ROWS(46, 12, 18, 4) +
      `<path d="M70 32h18m0 0-5-5m5 5-5 5" stroke="#9e9e9e" stroke-width="2" fill="none"/>`
  ),
  both: preview(MAP_BLOCK(8, 8, 80, 28) + ROWS(8, 42, 60, 2)),
  map: preview(MAP_BLOCK(8, 8, 80, 48)),
  list: preview(ROWS(8, 12, 80, 5)),
};

const ICONS = {
  appearance:
    "M17.5,12A1.5,1.5 0 0,1 16,10.5A1.5,1.5 0 0,1 17.5,9A1.5,1.5 0 0,1 19,10.5A1.5,1.5 0 0,1 17.5,12M14.5,8A1.5,1.5 0 0,1 13,6.5A1.5,1.5 0 0,1 14.5,5A1.5,1.5 0 0,1 16,6.5A1.5,1.5 0 0,1 14.5,8M9.5,8A1.5,1.5 0 0,1 8,6.5A1.5,1.5 0 0,1 9.5,5A1.5,1.5 0 0,1 11,6.5A1.5,1.5 0 0,1 9.5,8M6.5,12A1.5,1.5 0 0,1 5,10.5A1.5,1.5 0 0,1 6.5,9A1.5,1.5 0 0,1 8,10.5A1.5,1.5 0 0,1 6.5,12M12,3A9,9 0 0,0 3,12A9,9 0 0,0 12,21A1.5,1.5 0 0,0 13.5,19.5C13.5,19.11 13.35,18.76 13.11,18.5C12.88,18.23 12.73,17.88 12.73,17.5A1.5,1.5 0 0,1 14.23,16H16A5,5 0 0,0 21,11C21,6.58 16.97,3 12,3Z",
  data: "M3,17V19H9V17H3M3,5V7H13V5H3M13,21V19H21V17H13V15H11V21H13M7,9V11H3V13H7V15H9V9H7M21,13V11H11V13H21M15,9H17V7H21V5H17V3H15V9Z",
  interactions:
    "M10,9A1,1 0 0,1 11,8A1,1 0 0,1 12,9V13.47L13.21,13.6L18.15,15.79C18.68,16.03 19,16.56 19,17.14V21.5C18.97,22.32 18.32,22.97 17.5,23H11C10.62,23 10.26,22.85 10,22.57L5.1,18.37L5.84,17.6C6.03,17.39 6.3,17.28 6.58,17.28H6.8L10,19V9M11,5A4,4 0 0,1 15,9C15,10.5 14.2,11.77 13,12.46V11.24C13.61,10.69 14,9.89 14,9A3,3 0 0,0 11,6A3,3 0 0,0 8,9C8,9.89 8.39,10.69 9,11.24V12.46C7.8,11.77 7,10.5 7,9A4,4 0 0,1 11,5Z",
};

class ULTransportMapEditor extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._config = {};
    this._stops = [];
  }

  setConfig(config) {
    this._config = { ...config };
    this._render();
  }

  set hass(hass) {
    const first = !this._hass;
    this._hass = hass;
    if (this._form) this._form.hass = hass;
    if (first) this._loadStops();
  }

  async _loadStops() {
    try {
      const result = await this._hass.callWS({ type: "ul_transport/map/stops" });
      this._stops = result.stops || [];
      this._configured = result.configured;
    } catch (err) {
      this._stops = [];
      this._error = err.message || String(err);
    }
    this._render();
  }

  get _stop() {
    return this._stops.find(
      (s) => String(s.stop_id) === String(this._config.stop_id)
    );
  }

  get _lines() {
    return this._stop ? this._stop.lines : [];
  }

  _schema() {
    const lineOptions = this._lines.map((l) => ({ value: l, label: `Line ${l}` }));
    const destinations = (this._stop && this._stop.destinations) || [];
    const box = (name, options) => ({
      name,
      selector: { select: { mode: "box", box_max_columns: 3, options } },
    });
    return [
      {
        name: "stop_id",
        required: true,
        selector: {
          select: {
            mode: "dropdown",
            options: this._stops.map((s) => ({
              // ha-form select values must be strings; coerced back on change.
              value: String(s.stop_id),
              label: s.name,
            })),
          },
        },
      },
      {
        name: "lines",
        selector: {
          select: {
            multiple: true,
            // A hub is served by thirty-odd lines, and thirty checkboxes push
            // every other setting off the screen.
            mode: lineOptions.length > 8 ? "dropdown" : "list",
            options: lineOptions,
          },
        },
      },
      {
        name: "destinations",
        selector: {
          select: {
            multiple: true,
            mode: "dropdown",
            options: destinations.map((d) => ({ value: d, label: d })),
          },
        },
      },
      box("content", [
        { value: "both", label: "Map and list", image: PREVIEWS.both },
        { value: "map", label: "Map only", image: PREVIEWS.map },
        {
          value: "list",
          label: "List only",
          description: "Half the API requests",
          image: PREVIEWS.list,
        },
      ]),
      // Nothing to arrange when only one of the two is on screen.
      ...(this._config.content && this._config.content !== "both"
        ? []
        : [
            box("layout", [
              { value: "auto", label: "Auto", description: "Wide when the card is", image: PREVIEWS.auto },
              { value: "stacked", label: "Stacked", image: PREVIEWS.stacked },
              { value: "wide", label: "Side by side", image: PREVIEWS.wide },
            ]),
          ]),
      {
        name: "",
        type: "expandable",
        flatten: true,
        title: "Appearance",
        iconPath: ICONS.appearance,
        schema: [
          { name: "show_header", selector: { boolean: {} } },
          { name: "title", selector: { text: {} } },
          { name: "show_track", selector: { boolean: {} } },
          {
            name: "absolute_after",
            selector: {
              number: { min: 0, max: 120, step: 5, mode: "slider", unit_of_measurement: "min" },
            },
          },
          {
            name: "radius",
            selector: {
              number: { min: 200, max: 5000, step: 100, mode: "slider", unit_of_measurement: "m" },
            },
          },
          { name: "animate", selector: { boolean: {} } },
        ],
      },
      {
        name: "",
        type: "expandable",
        flatten: true,
        title: "Data",
        iconPath: ICONS.data,
        schema: [
          {
            name: "refresh",
            selector: { number: { min: 3, max: 120, mode: "slider", unit_of_measurement: "s" } },
          },
          {
            name: "horizon_minutes",
            selector: { number: { min: 1, max: 180, mode: "slider", unit_of_measurement: "min" } },
          },
          {
            name: "list_minutes",
            selector: { number: { min: 5, max: 1440, step: 5, mode: "slider", unit_of_measurement: "min" } },
          },
          {
            name: "list_count",
            selector: { number: { min: 1, max: 60, mode: "slider" } },
          },
          {
            name: "linger",
            selector: { number: { min: 0, max: 300, step: 5, mode: "slider", unit_of_measurement: "s" } },
          },
          { name: "line", selector: { select: { mode: "dropdown", options: lineOptions } } },
        ],
      },
      {
        name: "",
        type: "expandable",
        flatten: true,
        title: "Interactions",
        iconPath: ICONS.interactions,
        schema: [
          {
            name: "tap_action",
            selector: { ui_action: { actions: ["navigate", "url", "none"] } },
          },
        ],
      },
    ];
  }

  _render() {
    if (!this._form) {
      this.shadowRoot.innerHTML = `
        <style>
          .warn { color: var(--error-color, #c62828); font-size: .9rem; margin-bottom: 12px; }
        </style>
        <div class="warn" id="warn" hidden></div>
      `;
      this._form = document.createElement("ha-form");
      this._form.computeLabel = (schema) => or(LABELS[schema.name], schema.name);
      this._form.computeHelper = (schema) => or(HELPERS[schema.name], "");
      this._form.addEventListener("value-changed", (event) => {
        const next = { ...event.detail.value };
        if (next.stop_id !== undefined) next.stop_id = Number(next.stop_id);
        // ha-form emits "" for cleared selects; drop them so the card falls
        // back to its defaults instead of failing validation on an empty line.
        for (const key of ["line", "lines", "destinations", "title"]) {
          if (next[key] === "" || (Array.isArray(next[key]) && !next[key].length)) {
            delete next[key];
          }
        }
        this._config = next;
        this.dispatchEvent(
          new CustomEvent("config-changed", {
            detail: { config: next },
            bubbles: true,
            composed: true,
          })
        );
        this._render();
      });
      this.shadowRoot.appendChild(this._form);
    }

    const warn = this.shadowRoot.getElementById("warn");
    if (warn) {
      const message =
        this._error ||
        (this._configured === false
          ? "Add your Trafiklab API keys in the integration options before using this card."
          : "");
      warn.textContent = message;
      warn.hidden = !message;
    }

    this._form.hass = this._hass;
    this._form.schema = this._schema();
    // stop_id round-trips as a string through the select selector. The card's
    // defaults are shown so the sliders and toggles start where the card is.
    this._form.data = {
      content: "both",
      layout: "auto",
      radius: 800,
      refresh: 5,
      list_count: 8,
      linger: 45,
      show_header: true,
      show_track: false,
      animate: true,
      absolute_after: 20,
      ...this._config,
      stop_id: this._config.stop_id === undefined ? undefined : String(this._config.stop_id),
    };
  }
}

try {
  customElements.define("ul-transport-map-editor", ULTransportMapEditor);
} catch (err) {
  window.__ulTransportEditorDefineError = String(err);
  console.error("ul-transport-map-editor: could not define the editor", err);
}

window.customCards = window.customCards || [];
// Guarded like the definitions above: a page that loaded the module twice
// would otherwise list the card twice in the picker.
if (!window.customCards.some((card) => card.type === "ul-transport-map")) {
  window.customCards.push({
    type: "ul-transport-map",
    name: "UL Transport Live Map",
    description: "Live UL buses inbound to your configured stops.",
    documentationURL: "https://github.com/AlexanderBabel/ul-transport#live-map",
    // Renders the real card in the picker rather than a static screenshot.
    preview: true,
  });
}
