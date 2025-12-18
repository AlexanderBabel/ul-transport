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
