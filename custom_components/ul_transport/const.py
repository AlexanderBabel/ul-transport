"""Constants for UL Transport integration."""
from datetime import timedelta

DOMAIN = "ul_transport"
CONF_STOP_ID = "stop_id"
CONF_STOP_NAME = "stop_name"
CONF_SELECTED_LINES = "selected_lines"
CONF_SCAN_INTERVAL = "scan_interval"

DEFAULT_SCAN_INTERVAL = 60
MIN_SCAN_INTERVAL = 30
MAX_SCAN_INTERVAL = 600
API_TIMEOUT = 10

# API endpoints
API_STOPS_SEARCH = "https://www.ul.se/api/journey/stops"
API_STOP_DEPARTURES = "https://api.ul.se/api/v3/stop"

# --- Live map (Trafiklab / Samtrafiken GTFS) ---
CONF_GTFS_STATIC_KEY = "gtfs_static_key"
CONF_GTFS_REALTIME_KEY = "gtfs_realtime_key"

GTFS_STATIC_URL = "https://opendata.samtrafiken.se/gtfs/ul/ul.zip"
GTFS_RT_URL = "https://opendata.samtrafiken.se/gtfs-rt/ul/{feed}.pb"

# The static feed is ~26 MB and changes daily; the index is cached on disk.
GTFS_STATIC_TIMEOUT = 180
GTFS_STATIC_MAX_AGE = 24 * 3600
GTFS_CACHE_FILE = "ul_transport_gtfs.pickle"

# Realtime feeds refresh every ~3 s upstream. This TTL is what protects the
# Trafiklab quota: every viewer of the map shares one upstream fetch.
GTFS_RT_TTL = 5
GTFS_RT_TIMEOUT = 20

# route_desc values hidden unless `kinds` asks for them. 119 of 240 routes are
# school buses, which would otherwise dominate the map. Everything else is
# shown, including the trains ("Mälartåg", "SL pendeltåg") - a stop that has
# them offers them in the line picker, so filtering them out by default made
# picking one show nothing at all.
HIDDEN_ROUTE_KINDS = ["Skolbuss"]

# Regional trips calling at a hub can be 45+ stops away; beyond this they only
# stretch the map bounds.
DEFAULT_HORIZON_MINUTES = 30

# The list below the map can reach further than the map itself: a bus 50
# minutes out is worth reading, but not worth stretching the viewport for.
DEFAULT_LIST_MINUTES = 90

# A bus vanishing the second it is due looks like it never came. Keeping it a
# little longer shows it actually pull out of the stop.
DEPARTED_LINGER_SECONDS = 45

# How much of the route ahead of each bus is sent so the card can animate along
# the road instead of straight through the buildings on the corner.
PATH_AHEAD_METRES = 600

WS_OVERVIEW = f"{DOMAIN}/map/overview"
WS_LINE = f"{DOMAIN}/map/line"
WS_STOPS = f"{DOMAIN}/map/stops"

# Traffic type mapping
TRAFFIC_TYPE_MAPPING = {
    1: "BUS",
    2: "TRAM",
    3: "METRO",
    4: "TRAIN",
    5: "FERRY",
}

# Icon mapping
TRAFFIC_TYPE_ICONS = {
    1: "mdi:bus",
    2: "mdi:tram",
    3: "mdi:subway-variant",
    4: "mdi:train",
    5: "mdi:ferry",
}

DEFAULT_ICON = "mdi:transit-connection-variant"

# UL line color mapping based on official website
LINE_COLORS = {
    "1": "#ffffff",   # White (with black text)
    "2": "#af1e14",   # Red
    "3": "#008031",   # Green
    "4": "#df1995",   # Pink
    "5": "#78be20",   # Lime green
    "6": "#8b5b29",   # Brown
    "7": "#00a3e0",   # Light blue
    "8": "#fc4c02",   # Orange
    "9": "#0077c8",   # Blue
    "10": "#960a8c",  # Purple
    "11": "#97999b",  # Gray
    "12": "#f2a900",  # Yellow/gold (with black text)
    "13": "#e6cce4",  # Light pink (with black text)
    "14": "#7ccadb",  # Cyan (with black text)
    "21": "#fc4c02",  # Orange
    "22": "#0077c8",  # Blue
    "23": "#008031",  # Green
    "30": "#53565a",  # Dark gray
    "31": "#f2a900",  # Yellow/gold (with black text)
    "32": "#0077c8",  # Blue
    "33": "#008031",  # Green
    "34": "#af1e14",  # Red
}

# Lines with dark backgrounds need light text (white)
# Lines with light backgrounds need dark text (black)
LIGHT_TEXT_LINES = {"1", "12", "13", "14", "31"}

# Attribute keys
ATTR_LINE_NAME = "line_name"
ATTR_LINE_ID = "line_id"
ATTR_TRANSPORT = "transport"
ATTR_DIRECTION = "direction"
ATTR_LATITUDE = "latitude"
ATTR_LONGITUDE = "longitude"
ATTR_AREA = "area"
ATTR_STOP_NAME = "stop_name"
ATTR_LINE_COLOR = "line_color"
ATTR_TEXT_COLOR = "text_color"
