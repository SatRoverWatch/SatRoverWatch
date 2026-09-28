import io
import json
import math
import os
import random
import re
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv
from requests_oauthlib import OAuth1
from PIL import Image, ImageDraw, ImageFont
from map_generator import (
    maidenhead4,
    maidenhead6,
    map_grid4_bounds,
    generate_rover_map,
    generate_recent_track_map,
)
from pass_predictor import (
    PASS_SATELLITES,
    get_qualifying_passes,
    print_pass_context,
    public_pass_lines,
)


# ------------------------------------------------------------
# SatRoverWatch - Main Rover Watcher
# ------------------------------------------------------------

# Rover-specific settings are loaded from rover_config.json below. The private
# configuration file is intentionally excluded from Git; rover_config.example.json
# documents the public schema without publishing a real rover's private settings.
X_ROVER_POST_LOOKBACK_HOURS = 6

REPORTING_GAP_HOURS = 8
MOVING_SPEED_KMH = 5.0

IDLE_RADIUS_METERS = 250.0
IDLE_CONFIRM_MINUTES = 10
POSSIBLE_ACTIVATION_REPORTS = 2

# A stopped/slow final APRS packet followed by silence can be recognized as
# quiet time when mapped lodging is very close to the final coordinate.
SILENT_STOP_CONFIRM_MINUTES = 10
# The normal idle detector still uses MOVING_SPEED_KMH (5 km/h).
# This higher threshold applies ONLY to the special case where the final
# APRS packet is followed by silence and mapped lodging is very close.
SILENT_STOP_MAX_SPEED_KMH = 20.0
LODGING_RADIUS_METERS = 150

OSM_NEARBY_RADIUS_METERS = 250
# Ordinary travel-stop POIs suppress satellite-pass context only when very close.
ORDINARY_STOP_RADIUS_METERS = 100
ROVER_DOG_RADIUS_METERS = ORDINARY_STOP_RADIUS_METERS

# Rover map image attached to location-based X posts.
MAP_OUTPUT_WIDTH = 1200
MAP_OUTPUT_HEIGHT = 675
MAP_WIDTH_KM = 1.0
MAP_TILE_SIZE = 256
MAP_TILE_URL = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
MAP_TILE_CACHE_SECONDS = 7 * 24 * 3600
MAP_USER_AGENT = (
    "SatRoverWatch-map/0.1 "
    "(https://x.com/SatRoverWatch; contact: satroverwatch@gmail.com)"
)
MAP_NOMINATIM_URL = "https://nominatim.openstreetmap.org/reverse"
MAP_NWS_POINTS_URL = "https://api.weather.gov/points/{lat},{lon}"
MAP_NWS_HEADERS = {
    "User-Agent": "SatRoverWatch/0.5 (https://x.com/SatRoverWatch)",
    "Accept": "application/geo+json",
}
PROJECT_DIR = Path(__file__).resolve().parent
STATE_FILE = PROJECT_DIR / "state.json"
MESSAGES_FILE = PROJECT_DIR / "messages.json"
ENV_FILE = PROJECT_DIR / ".env"
MAP_CACHE_DIR = PROJECT_DIR / "cache" / "osm_tiles"
MAP_OUTPUT_FILE = PROJECT_DIR / "cache" / "satroverwatch_map.png"
TRACK_MAP_OUTPUT_FILE = PROJECT_DIR / "cache" / "satroverwatch_track_map.png"
TRACK_MAP_WINDOW_MINUTES = 60
DATABASE_FILE = PROJECT_DIR / "satroverwatch.db"
ROVER_CONFIG_FILE = PROJECT_DIR / "rover_config.json"


def load_rover_config():
    """Load one enabled rover from the private rover configuration."""
    try:
        with ROVER_CONFIG_FILE.open("r", encoding="utf-8") as file:
            config = json.load(file)
    except FileNotFoundError:
        raise SystemExit(
            f"ERROR: Private rover configuration not found: {ROVER_CONFIG_FILE}\n"
            "Copy rover_config.example.json to rover_config.json and configure it."
        )
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"ERROR: Could not read {ROVER_CONFIG_FILE.name}: {exc}")

    rovers = config.get("rovers")
    if not isinstance(rovers, list) or not rovers:
        raise SystemExit("ERROR: rover_config.json must contain a non-empty 'rovers' list.")

    enabled = [rover for rover in rovers if isinstance(rover, dict) and rover.get("enabled") is True]
    if not enabled:
        print("ROVER TRACKING: No rover is enabled in rover_config.json.")
        raise SystemExit(0)
    if len(enabled) > 1:
        raise SystemExit(
            "ERROR: This watcher version processes one rover per run. "
            "Enable exactly one rover until the multi-rover loop is implemented."
        )

    rover = enabled[0]
    callsign = str(rover.get("callsign", "")).strip().upper()
    aprs_callsign = str(rover.get("aprs_callsign") or callsign).strip().upper()
    if not callsign or not aprs_callsign:
        raise SystemExit("ERROR: Enabled rover requires callsign and aprs_callsign.")
    return rover, callsign, aprs_callsign


ROVER_CONFIG, CALLSIGN, APRS_CALLSIGN = load_rover_config()
X_ROVER_HANDLE = str(ROVER_CONFIG.get("x_handle") or "").strip()
X_ROVER_USERNAME = str(ROVER_CONFIG.get("x_username") or "").strip().lstrip("@")
X_ROVER_POSTS_ENABLED = bool(ROVER_CONFIG.get("protected_x_intent_enabled", False))

HOME_CONFIG = ROVER_CONFIG.get("home_geofence") or {}
HOME_GEOFENCE_ENABLED = bool(HOME_CONFIG.get("enabled", False))
HOME_GEOFENCE_LATITUDE = HOME_CONFIG.get("latitude")
HOME_GEOFENCE_LONGITUDE = HOME_CONFIG.get("longitude")
HOME_GEOFENCE_RADIUS_KM = float(HOME_CONFIG.get("radius_km", 10.0))
HOME_ARRIVAL_MESSAGE = str(
    HOME_CONFIG.get("arrival_message")
    or f"{X_ROVER_HANDLE + ' ' if X_ROVER_HANDLE else ''}{CALLSIGN} has returned home from the rover trip. 📡🚙"
)

load_dotenv(ENV_FILE)

APRSFI_API_KEY = os.getenv("APRSFI_API_KEY")

X_CONSUMER_KEY = os.getenv("X_CONSUMER_KEY")
X_CONSUMER_SECRET = os.getenv("X_CONSUMER_SECRET")
X_ACCESS_TOKEN = os.getenv("X_ACCESS_TOKEN")
X_ACCESS_TOKEN_SECRET = os.getenv("X_ACCESS_TOKEN_SECRET")

X_POSTING_ENABLED = (
    os.getenv("X_POSTING_ENABLED", "false").strip().lower()
    in ("1", "true", "yes", "on")
)

# OpenAI is used only to classify the intent of an opted-in rover's protected X posts.
# It never decides satellite visibility or publishes a post by itself.
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_INTENT_MODEL = os.getenv("OPENAI_INTENT_MODEL", "gpt-5.6-luna")
OPENAI_RESPONSES_URL = "https://api.openai.com/v1/responses"



if not APRSFI_API_KEY:
    raise SystemExit("ERROR: APRSFI_API_KEY not found in .env")


# ------------------------------------------------------------
# Public message templates
# ------------------------------------------------------------

# These are safe built-in fallbacks. Normal public wording is loaded from
# messages.json so message pools can be edited without changing this script.
DEFAULT_MESSAGES = {
    "ROVER_RETURN": [
        "Have a great day roving and be safe!",
        "Good luck on the grids today!",
        "Have fun out there and happy roving!",
        "Safe travels and good luck on the satellites!",
        "Another day of roving — have fun out there!"
    ],
    "GRID_CHANGE": [
        "Grid change: {callsign} has moved from {old_grid} to {new_grid}.",
        "Another grid in the rearview mirror — {callsign} has crossed from {old_grid} into {new_grid}.",
        "The Rovermobile rolls on! {callsign} has moved from {old_grid} into {new_grid}.",
        "New grid! {callsign} has crossed the line from {old_grid} into {new_grid}.",
        "{callsign} is collecting grids again — {old_grid} is behind us and {new_grid} is now on the map.",
        "Grid boundary crossed: {old_grid} → {new_grid}. The rover rolls on!",
        "Another line on the map crossed — {callsign} is now in {new_grid}, after leaving {old_grid}.",
        "The Rovermobile has rolled into {new_grid}. Previous grid: {old_grid}.",
        "Onward to another grid! {callsign} has moved from {old_grid} to {new_grid}.",
        "{old_grid} → {new_grid}: another grid change for {callsign}."
    ],
    "GRID_MOVING": [
        "The rover is currently on the move. 📡🚙",
        "The Rovermobile is still rolling. 🚙📡",
        "The rover rolls on toward the next grid. 🚙🛰️",
        "Still moving — who knows which grid is next? 📡🚙"
    ],
    "GRID_SIGNOFF": [
        "Happy roving! 🛰️",
        "Keep on roving! 🚙🛰️",
        "Safe travels, rover! 📡🚙",
        "Onward to the next grid! 🛰️🚙",
        "Happy grid chasing! 📡🛰️"
    ],
    "QUIET_TIME": [
        "Rover quiet time? Even satellite rovers need to recharge. 😴🛰️",
        "Shhh... rover quiet time. 🤫 More grids tomorrow?",
        "Looks like the rover may be taking a well-earned break. 🚙💤",
        "A little rover downtime before the next adventure? 😴📡",
        "Resting the rover for a bit? There are always more grids ahead. 🚙🛰️"
    ],
    "ROVER_DOG": [
        "Rover Dog stop? 🌭 Or just fueling up for more grids? 🚙🛰️",
        "Fuel for the rover... and maybe a Rover Dog for the operator? 🌭😄",
        "A quick pit stop before chasing more grids? ⛽🚙🛰️",
        "Rover refueling stop — machine, operator, or both? 🌭⛽",
        "Pit stop time. The rover has grids to chase! 🚙📡"
    ],
    "ROVER_BREAK": [
        "Taking a rover break and enjoying the surroundings? 🚙🛰️",
        "A little rover downtime before the next satellite pass? 📡🛰️",
        "Good spot for a rover break — more grids may be waiting ahead. 🚙",
        "Stretching the legs before the next part of the rover adventure? 🛰️🚙",
        "The rover has found a place to pause for a while. 📡"
    ],
    "FOOD_STOP": [
        "A quick food stop before chasing more satellites? 🚙📡",
        "Rover meal break? There may be more grids ahead. 🚙🛰️",
        "A little operator refueling before the next part of the trip? 😄📡"
    ],
    "POSSIBLE_ACTIVATION": [
        "Possible satellite activation? 📡🛰️",
        "Could a satellite activation be getting underway? 🛰️📡",
        "The rover has stopped — maybe it is time to work some satellites! 📡🛰️",
        "No obvious pit stop nearby. Could this be a satellite operating spot? 🛰️",
        "Stationary for a while — perhaps some satellite passes are on the menu? 📡"
    ]
}


def load_message_pools():
    """Load editable public wording from messages.json with safe fallbacks."""
    pools = {key: list(value) for key, value in DEFAULT_MESSAGES.items()}
    if not MESSAGES_FILE.exists():
        print("MESSAGES: messages.json not found; using built-in fallback wording.")
        return pools
    try:
        with MESSAGES_FILE.open("r", encoding="utf-8") as file:
            external = json.load(file)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"WARNING: Could not read messages.json: {exc}")
        print("MESSAGES: Using built-in fallback wording.")
        return pools
    if not isinstance(external, dict):
        print("WARNING: messages.json must contain a JSON object.")
        return pools
    for category, messages in external.items():
        if (isinstance(category, str) and isinstance(messages, list) and messages
                and all(isinstance(message, str) and message.strip() for message in messages)):
            pools[category] = messages
    return pools


MESSAGE_POOLS = load_message_pools()


def choose_message(category, previous_message=None):
    """Choose from a message pool while avoiding the immediately previous line."""
    messages = MESSAGE_POOLS.get(category) or DEFAULT_MESSAGES.get(category) or []
    if not messages:
        return ""
    choices = [message for message in messages if message != previous_message]
    return random.choice(choices or messages)


def render_message(category, previous_message=None, **values):
    """Choose a template and safely substitute known event values."""
    template = choose_message(category, previous_message)
    try:
        return template.format(**values)
    except (KeyError, ValueError) as exc:
        print(f"WARNING: Invalid {category} message template: {exc}")
        fallback = (DEFAULT_MESSAGES.get(category) or [""])[0]
        try:
            return fallback.format(**values)
        except (KeyError, ValueError):
            return fallback


def choose_idle_message(category, previous_message=None):
    """Choose idle wording from messages.json while avoiding an immediate repeat."""
    if category not in MESSAGE_POOLS:
        category = "POSSIBLE_ACTIVATION"
    return choose_message(category, previous_message)


# ------------------------------------------------------------
# Maidenhead conversion
# ------------------------------------------------------------


# ------------------------------------------------------------
# Utility functions
# ------------------------------------------------------------

def format_duration(seconds):
    """Return a readable duration such as 13h 14m."""

    seconds = max(0, int(seconds))

    hours, remainder = divmod(seconds, 3600)
    minutes = remainder // 60

    if hours:
        return f"{hours}h {minutes}m"

    return f"{minutes}m"


def load_state():
    """Load the previously saved rover state."""

    if not STATE_FILE.exists():
        return {}

    try:
        with STATE_FILE.open("r") as file:
            return json.load(file)

    except (json.JSONDecodeError, OSError):
        print("WARNING: Could not read state.json.")
        print("Starting with a new state.")
        return {}


def save_state(state):
    """Safely save the current rover state."""

    temporary_file = STATE_FILE.with_suffix(".tmp")

    with temporary_file.open("w") as file:
        json.dump(state, file, indent=4)

    temporary_file.replace(STATE_FILE)


def utc_text(timestamp):
    """Convert Unix timestamp to readable UTC."""

    return datetime.fromtimestamp(
        timestamp,
        tz=timezone.utc,
    ).strftime("%Y-%m-%d %H:%M:%S UTC")


def distance_meters(lat1, lon1, lat2, lon2):
    """Return great-circle distance between two coordinates in meters."""

    earth_radius_m = 6371008.8

    lat1_rad = math.radians(lat1)
    lat2_rad = math.radians(lat2)

    delta_lat = math.radians(lat2 - lat1)
    delta_lon = math.radians(lon2 - lon1)

    a = (
        math.sin(delta_lat / 2) ** 2
        + math.cos(lat1_rad)
        * math.cos(lat2_rad)
        * math.sin(delta_lon / 2) ** 2
    )

    c = 2 * math.atan2(
        math.sqrt(a),
        math.sqrt(1 - a),
    )

    return earth_radius_m * c


def reverse_geocode_county(latitude, longitude):
    """Return county/state text from OpenStreetMap Nominatim, if available."""

    reverse_url = "https://nominatim.openstreetmap.org/reverse"

    reverse_params = {
        "lat": latitude,
        "lon": longitude,
        "format": "jsonv2",
        "zoom": 10,
        "addressdetails": 1,
    }

    reverse_headers = {
        "User-Agent": (
            "SatRoverWatch/0.2 "
            "(https://x.com/SatRoverWatch)"
        )
    }

    try:
        response = requests.get(
            reverse_url,
            params=reverse_params,
            headers=reverse_headers,
            timeout=10,
        )
        response.raise_for_status()
        reverse_data = response.json()

    except (requests.RequestException, ValueError) as exc:
        print(f"WARNING: Reverse geocoding failed: {exc}")
        return None

    address = reverse_data.get("address", {})

    county = address.get("county")
    state = address.get("state")

    if county and state:
        return f"{county}, {state}"

    return county or state


def nearby_osm_features(latitude, longitude):
    """Return useful OSM features near the rover using resilient Overpass lookups."""

    # Try more than one public Overpass instance. A temporary 5xx/timeout on
    # one server should not turn a normal fuel/food stop into an unknown stop.
    overpass_urls = (
        "https://overpass-api.de/api/interpreter",
        "https://overpass.kumi.systems/api/interpreter",
    )

    # Names are deliberately NOT required. SatRoverWatch publishes only a
    # generic nearby category, so an unnamed fuel station, restaurant, etc.
    # is still useful evidence for classifying the stop.
    query = f"""
[out:json][timeout:15];
(
  nwr(around:{OSM_NEARBY_RADIUS_METERS},{latitude},{longitude})[tourism~"^(hotel|motel|hostel|camp_site|caravan_site|attraction|viewpoint|museum)$"];
  nwr(around:{OSM_NEARBY_RADIUS_METERS},{latitude},{longitude})[amenity~"^(fuel|restaurant|fast_food|cafe|bar|pub|biergarten|parking)$"];
  nwr(around:{OSM_NEARBY_RADIUS_METERS},{latitude},{longitude})[shop=convenience];
  nwr(around:{OSM_NEARBY_RADIUS_METERS},{latitude},{longitude})[leisure~"^(park|nature_reserve|stadium|sports_centre)$"];
  nwr(around:{OSM_NEARBY_RADIUS_METERS},{latitude},{longitude})[natural][name];
  nwr(around:{OSM_NEARBY_RADIUS_METERS},{latitude},{longitude})[historic][name];
);
out center tags;
"""

    headers = {
        "User-Agent": (
            "SatRoverWatch/0.3 "
            "(https://x.com/SatRoverWatch)"
        )
    }

    data = None
    last_error = None

    for retry_round in range(1, 3):
        for attempt, overpass_url in enumerate(overpass_urls, start=1):
            try:
                response = requests.post(
                    overpass_url,
                    data={"data": query},
                    headers=headers,
                    timeout=20,
                )
                response.raise_for_status()
                data = response.json()
                if attempt > 1 or retry_round > 1:
                    print(
                        "OpenStreetMap nearby-feature lookup succeeded "
                        f"on retry round {retry_round}, server #{attempt}."
                    )
                break

            except (requests.RequestException, ValueError) as exc:
                last_error = exc
                print(
                    "WARNING: OpenStreetMap nearby-feature lookup failed "
                    f"on retry round {retry_round}, server #{attempt}: {exc}"
                )

        if data is not None:
            break

        if retry_round == 1:
            print(
                "OpenStreetMap nearby-feature lookup: both servers failed; "
                "waiting 10 seconds before one retry round."
            )
            time.sleep(10)

    if data is None:
        print(
            "WARNING: All OpenStreetMap Overpass nearby-feature lookups failed. "
            f"Last error: {last_error}"
        )
        return None

    features = []
    seen = set()

    for element in data.get("elements", []):
        tags = element.get("tags", {})

        feature_lat = element.get("lat")
        feature_lon = element.get("lon")

        if feature_lat is None or feature_lon is None:
            center = element.get("center", {})
            feature_lat = center.get("lat")
            feature_lon = center.get("lon")

        if feature_lat is None or feature_lon is None:
            continue

        distance = distance_meters(
            latitude,
            longitude,
            float(feature_lat),
            float(feature_lon),
        )

        if distance > OSM_NEARBY_RADIUS_METERS:
            continue

        if tags.get("tourism"):
            kind = tags["tourism"]
        elif tags.get("amenity"):
            kind = tags["amenity"]
        elif tags.get("shop"):
            kind = tags["shop"]
        elif tags.get("leisure"):
            kind = tags["leisure"]
        elif tags.get("natural"):
            kind = tags["natural"]
        elif tags.get("historic"):
            kind = tags["historic"]
        else:
            kind = "place"

        name = tags.get("name") or tags.get("brand") or f"Unnamed {kind}"

        # Element identity is more reliable than a name for unnamed POIs and
        # also prevents two different unnamed features of the same kind from
        # collapsing into one result.
        element_type = element.get("type", "unknown")
        element_id = element.get("id")
        if element_id is not None:
            key = (element_type, element_id)
        else:
            key = (name.casefold(), kind, round(distance, 1))

        if key in seen:
            continue
        seen.add(key)

        features.append(
            {
                "name": name,
                "kind": kind,
                "distance_m": distance,
                "rover_dog": (
                    tags.get("amenity") == "fuel"
                    or tags.get("shop") == "convenience"
                ),
            }
        )

    features.sort(key=lambda item: item["distance_m"])
    return features


def nearby_lodging_feature(features, radius_m=LODGING_RADIUS_METERS):
    """Return nearby lodging within the requested decision radius."""
    if features is None:
        return None

    lodging_kinds = {"hotel", "motel", "hostel"}
    return next(
        (
            feature
            for feature in features
            if feature["kind"] in lodging_kinds
            and feature["distance_m"] <= LODGING_RADIUS_METERS
        ),
        None,
    )


def ordinary_stop_feature(features):
    """Return a close POI that makes an ordinary travel stop the likely context."""
    if not features:
        return None

    ordinary_kinds = {
        "fuel", "convenience", "restaurant", "fast_food", "cafe",
        "hotel", "motel", "hostel",
    }
    return next(
        (
            feature for feature in features
            if feature["kind"] in ordinary_kinds
            and feature["distance_m"] <= ORDINARY_STOP_RADIUS_METERS
        ),
        None,
    )


def classify_idle_context(features):
    """Classify an idle stop without exposing a specific business name publicly."""
    if features is None:
        return None, None

    break_kinds = {
        "camp_site", "caravan_site", "park", "nature_reserve",
        "viewpoint", "attraction", "museum",
    }
    food_kinds = {"restaurant", "fast_food", "cafe", "bar", "pub", "biergarten"}

    rover_dog_feature = next(
        (
            feature for feature in features
            if feature["rover_dog"]
            and feature["distance_m"] <= ROVER_DOG_RADIUS_METERS
        ),
        None,
    )
    if rover_dog_feature:
        return "ROVER_DOG", "near a gas station or convenience store"

    lodging_feature = nearby_lodging_feature(features, ORDINARY_STOP_RADIUS_METERS)
    if lodging_feature:
        return "QUIET_TIME", "near a hotel or motel"

    food_feature = next(
        (
            feature for feature in features
            if feature["kind"] in food_kinds
            and feature["distance_m"] <= ORDINARY_STOP_RADIUS_METERS
        ),
        None,
    )
    if food_feature:
        return "FOOD_STOP", "near a restaurant or cafe"

    break_feature = next(
        (
            feature for feature in features
            if feature["kind"] in break_kinds
            and feature["distance_m"] <= OSM_NEARBY_RADIUS_METERS
        ),
        None,
    )
    if break_feature:
        return "ROVER_BREAK", None

    return "POSSIBLE_ACTIVATION", None



# ------------------------------------------------------------
# National Weather Service forecast for morning return posts
# ------------------------------------------------------------

NWS_HEADERS = {
    "User-Agent": "SatRoverWatch/0.4 (https://x.com/SatRoverWatch)",
    "Accept": "application/geo+json",
}


def get_nws_daytime_weather(latitude, longitude):
    """
    Return a compact U.S. NWS daytime forecast for a rover return event.

    This is deliberately fail-safe. If the coordinate is outside NWS coverage,
    the API is unavailable, or the response is unexpected, return None so the
    normal rover return post can still be published without weather.
    """

    points_url = (
        "https://api.weather.gov/points/"
        f"{latitude:.4f},{longitude:.4f}"
    )

    try:
        points_response = requests.get(
            points_url,
            headers=NWS_HEADERS,
            timeout=15,
        )

        if points_response.status_code in (400, 404):
            print(
                "NWS WEATHER: No U.S. point forecast available "
                "for this coordinate."
            )
            return None

        points_response.raise_for_status()
        points_data = points_response.json()
        properties = points_data.get("properties", {})

        forecast_url = properties.get("forecast")
        if not forecast_url:
            print("NWS WEATHER: Point response contained no forecast URL.")
            return None

        relative = properties.get("relativeLocation", {})
        relative_properties = relative.get("properties", {})
        city = relative_properties.get("city")
        state = relative_properties.get("state")
        location = ", ".join(
            item for item in (city, state) if item
        )

        forecast_response = requests.get(
            forecast_url,
            headers=NWS_HEADERS,
            timeout=15,
        )
        forecast_response.raise_for_status()
        forecast_data = forecast_response.json()

    except (requests.RequestException, ValueError) as exc:
        print(f"NWS WEATHER: Forecast lookup failed: {exc}")
        return None

    periods = (
        forecast_data.get("properties", {}).get("periods", [])
    )

    if not periods:
        print("NWS WEATHER: Forecast response contained no periods.")
        return None

    # Return events normally happen in the morning. Prefer the first daytime
    # period supplied by NWS instead of blindly using periods[0]. This skips
    # an Overnight period if APRS returns before the daytime forecast begins.
    daytime_period = next(
        (
            period
            for period in periods
            if period.get("isDaytime") is True
        ),
        None,
    )

    if daytime_period is None:
        print("NWS WEATHER: No daytime forecast period was available.")
        return None

    temperature = daytime_period.get("temperature")
    temperature_unit = daytime_period.get("temperatureUnit", "F")
    short_forecast = daytime_period.get("shortForecast")

    if temperature is None or not short_forecast:
        print("NWS WEATHER: Daytime forecast was missing required fields.")
        return None

    precip_value = (
        daytime_period.get("probabilityOfPrecipitation", {})
        .get("value")
    )

    try:
        precip_percent = (
            round(float(precip_value))
            if precip_value is not None
            else None
        )
    except (TypeError, ValueError):
        precip_percent = None

    # Keep the public line compact and source its meteorological wording
    # directly from the NWS short forecast. Omit insignificant precipitation.
    weather_line = (
        f"Rover weather: {short_forecast} today, "
        f"high near {temperature}°{temperature_unit}."
    )

    if precip_percent is not None and precip_percent >= 20:
        weather_line += f" Precipitation chance {precip_percent}%."

    print()
    print("NWS WEATHER:")
    if location:
        print(f"Forecast location: {location}")
    print(f"Forecast period:   {daytime_period.get('name', 'Daytime')}")
    print(f"Forecast:          {short_forecast}")
    print(f"High:              {temperature}°{temperature_unit}")
    if precip_percent is not None:
        print(f"Precipitation:     {precip_percent}%")
    print(f"Post weather line: {weather_line}")

    return weather_line


# ------------------------------------------------------------
# X rover-post reading and rule-based activation extraction
# ------------------------------------------------------------

SATELLITE_PATTERNS = {
    "SO-50": (r"\bSO[\s-]?50\b",),
    "RS-44": (r"\bRS[\s-]?44\b",),
    "FO-29": (r"\bFO[\s-]?29\b",),
    "ISS": (r"\bISS\b", r"\bSPACE\s+STATION\b"),
}

GRID_PATTERN = re.compile(r"\b([A-Ra-r]{2}[0-9]{2})(?:[A-Xa-x]{2})?\b")

ACTIVATION_PHRASES = (
    "going to be on",
    "gonna be on",
    "will be on",
    "i'll be on",
    "ill be on",
    "planning to be on",
    "plan to be on",
    "operating",
    "operate",
    "activation",
    "activate",
    "working",
    "work ",
)

TIMING_PATTERNS = (
    re.compile(r"\bin\s+(?:a\s+)?few\s+minutes\b", re.IGNORECASE),
    re.compile(r"\bin\s+\d{1,3}\s+(?:minutes?|mins?)\b", re.IGNORECASE),
    re.compile(r"\bin\s+\d{1,2}\s+(?:hours?|hrs?)\b", re.IGNORECASE),
    re.compile(r"\b(?:very\s+)?shortly\b", re.IGNORECASE),
    re.compile(r"\bsoon\b", re.IGNORECASE),
    re.compile(r"\bright\s+now\b", re.IGNORECASE),
    re.compile(r"\bnow\b", re.IGNORECASE),
    re.compile(r"\blater\s+(?:today|tonight)\b", re.IGNORECASE),
)


def x_oauth1():
    """Return OAuth 1.0a user-context auth for SatRoverWatch."""
    if not x_credentials_available():
        return None
    return OAuth1(
        X_CONSUMER_KEY,
        X_CONSUMER_SECRET,
        X_ACCESS_TOKEN,
        X_ACCESS_TOKEN_SECRET,
    )


def get_x_user_by_username(username):
    """Look up an X user with SatRoverWatch user-context authentication."""
    auth = x_oauth1()
    if auth is None:
        print("X ROVER POSTS: OAuth credentials are unavailable.")
        return None

    try:
        response = requests.get(
            f"https://api.x.com/2/users/by/username/{username}",
            auth=auth,
            params={"user.fields": "protected"},
            timeout=15,
        )
    except requests.RequestException as exc:
        print(f"X ROVER POSTS: User lookup failed: {exc}")
        return None

    if response.status_code != 200:
        print(
            "X ROVER POSTS: User lookup was not available. "
            f"HTTP {response.status_code}"
        )
        print(response.text)
        return None

    try:
        return response.json().get("data")
    except ValueError:
        print("X ROVER POSTS: User lookup returned invalid JSON.")
        return None


def get_recent_rover_x_posts(user_id, since_id=None):
    """Read recent posts by the configured rover using OAuth 1.0a user context."""
    auth = x_oauth1()
    if auth is None:
        return None

    params = {
        "max_results": 10,
        "exclude": "retweets",
        "tweet.fields": "created_at",
    }
    if since_id:
        params["since_id"] = str(since_id)

    try:
        response = requests.get(
            f"https://api.x.com/2/users/{user_id}/tweets",
            auth=auth,
            params=params,
            timeout=15,
        )
    except requests.RequestException as exc:
        print(f"X ROVER POSTS: Timeline lookup failed: {exc}")
        return None

    if response.status_code != 200:
        print(
            "X ROVER POSTS: Timeline lookup was not available. "
            f"HTTP {response.status_code}"
        )
        print(response.text)
        return None

    try:
        return response.json().get("data", [])
    except ValueError:
        print("X ROVER POSTS: Timeline lookup returned invalid JSON.")
        return None


def extract_rover_activation(post_text):
    """
    Extract only high-confidence satellite activation facts.

    No AI is used. A post must contain:
      1. one recognized satellite,
      2. a Maidenhead grid, and
      3. an activation/operating phrase.

    Ambiguous posts are ignored rather than guessed.
    """
    cleaned = " ".join((post_text or "").split())
    lowered = cleaned.casefold()

    satellite = None
    for satellite_name, patterns in SATELLITE_PATTERNS.items():
        if any(re.search(pattern, cleaned, re.IGNORECASE) for pattern in patterns):
            satellite = satellite_name
            break

    grid_match = GRID_PATTERN.search(cleaned)
    grid = grid_match.group(1).upper() if grid_match else None

    activation_intent = any(
        phrase in lowered for phrase in ACTIVATION_PHRASES
    )

    if not satellite or not grid or not activation_intent:
        return None

    timing = None
    for pattern in TIMING_PATTERNS:
        match = pattern.search(cleaned)
        if match:
            timing = match.group(0)
            break

    return {
        "satellite": satellite,
        "grid": grid,
        "timing": timing,
    }


OPENAI_INTENT_VALUES = [
    "FUTURE_OPERATION",
    "CURRENT_OPERATION",
    "PAST_ACTIVITY",
    "ROVING_TRAVEL_INTENT",
    "LOCATION_ONLY",
    "UNRELATED",
    "UNCERTAIN",
]

OPENAI_INTENT_SCHEMA = {
    "type": "object",
    "properties": {
        "intent": {"type": "string", "enum": OPENAI_INTENT_VALUES},
        "satellites": {"type": "array", "items": {"type": "string"}},
        "grids": {"type": "array", "items": {"type": "string"}},
        "explicit_operating_intent": {"type": "boolean"},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "reason": {"type": "string"},
    },
    "required": [
        "intent", "satellites", "grids", "explicit_operating_intent",
        "confidence", "reason",
    ],
    "additionalProperties": False,
}

OPENAI_INTENT_INSTRUCTIONS = """Classify amateur-radio satellite rover social-media posts.
Classify only what the author explicitly means; never infer an activation merely
because a satellite and Maidenhead grid appear together.

FUTURE_OPERATION = clear plan/intention to operate a satellite in the future.
CURRENT_OPERATION = clearly operating/calling on a satellite now.
PAST_ACTIVITY = satellite contact, operation, reception, or activity already happened.
ROVING_TRAVEL_INTENT = future/current rover travel or intended grids without clear satellite operation.
LOCATION_ONLY = location/grid/stopping status without clear travel or satellite operation.
UNRELATED = unrelated to rover travel/location or amateur-radio satellite operation.
UNCERTAIN = ambiguous wording where a public operating announcement would require guessing.

Be conservative. Completed-event language such as 'worked', 'a pleasure to work',
'thanks for the QSOs', 'heard', or 'sounded great' is PAST_ACTIVITY. A satellite
name or grid alone never proves operating intent. Tentative language such as
'maybe ... later if I have time' is UNCERTAIN unless there is a clear commitment.
Extract only satellite names and Maidenhead grids explicitly present. Never invent
facts. explicit_operating_intent is true only for explicit FUTURE_OPERATION or
CURRENT_OPERATION. Keep reason to one short sentence."""


def classify_rover_x_intent(post_text):
    """Return conservative structured intent from OpenAI, or None on any failure."""
    if not OPENAI_API_KEY:
        print("X INTENT: OPENAI_API_KEY unavailable; relay blocked safely.")
        return None

    payload = {
        "model": OPENAI_INTENT_MODEL,
        "store": False,
        "instructions": OPENAI_INTENT_INSTRUCTIONS,
        "input": post_text,
        "text": {"format": {
            "type": "json_schema",
            "name": "rover_intent",
            "strict": True,
            "schema": OPENAI_INTENT_SCHEMA,
        }},
    }

    try:
        response = requests.post(
            OPENAI_RESPONSES_URL,
            headers={
                "Authorization": f"Bearer {OPENAI_API_KEY}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=30,
        )
        response.raise_for_status()
        body = response.json()
    except (requests.RequestException, ValueError) as exc:
        print(f"X INTENT: OpenAI classification unavailable; relay blocked: {exc}")
        return None

    output_text = body.get("output_text")
    if not output_text:
        pieces = []
        for item in body.get("output", []):
            if item.get("type") != "message":
                continue
            for part in item.get("content", []):
                if part.get("type") == "output_text" and part.get("text"):
                    pieces.append(part["text"])
        output_text = "".join(pieces)

    if not output_text:
        print("X INTENT: OpenAI returned no structured output; relay blocked.")
        return None

    try:
        result = json.loads(output_text)
    except (TypeError, json.JSONDecodeError) as exc:
        print(f"X INTENT: Could not parse structured output; relay blocked: {exc}")
        return None

    print(
        "X INTENT: "
        f"{result.get('intent')} | explicit={result.get('explicit_operating_intent')} "
        f"| confidence={result.get('confidence')}"
    )
    print(f"X INTENT REASON: {result.get('reason')}")
    return result


def grid4_center(grid4):
    """Return the geographic center of a valid four-character Maidenhead grid."""
    bounds = map_grid4_bounds(grid4)
    if bounds is None:
        return None
    west, south, east, north = bounds
    return (south + north) / 2.0, (west + east) / 2.0


def validate_rover_x_operation(intent_result, current_latitude, current_longitude):
    """Apply hard deterministic rules after AI intent classification."""
    if not intent_result:
        return None

    intent = intent_result.get("intent")
    if intent not in {"FUTURE_OPERATION", "CURRENT_OPERATION"}:
        print(f"X INTENT: {intent} is not eligible for a relay.")
        return None
    if intent_result.get("explicit_operating_intent") is not True:
        print("X INTENT: operating intent was not explicit; relay blocked.")
        return None

    satellites = [str(x).upper() for x in intent_result.get("satellites", [])]
    grids = [str(x).upper() for x in intent_result.get("grids", [])]
    supported_satellites = [x for x in satellites if x in PASS_SATELLITES]
    valid_grids = [x for x in grids if re.fullmatch(r"[A-R]{2}[0-9]{2}", x)]

    if len(supported_satellites) != 1 or len(valid_grids) != 1:
        print(
            "X INTENT: relay requires exactly one supported satellite and "
            "one explicit four-character grid."
        )
        return None

    satellite = supported_satellites[0]
    grid = valid_grids[0]

    # Use the live APRS coordinate when it is actually in the reported grid;
    # otherwise use the reported grid center only for visibility validation.
    if maidenhead4(current_latitude, current_longitude) == grid:
        pass_latitude, pass_longitude = current_latitude, current_longitude
        location_source = "current APRS position"
    else:
        center = grid4_center(grid)
        if center is None:
            return None
        pass_latitude, pass_longitude = center
        location_source = f"center of {grid}"

    now = datetime.now(timezone.utc)
    passes, gp_source = get_qualifying_passes(pass_latitude, pass_longitude, now)
    if passes is None:
        print("X INTENT: pass data unavailable; relay blocked safely.")
        return None

    matches = [item for item in passes if item["satellite"] == satellite]
    if intent == "CURRENT_OPERATION":
        matches = [item for item in matches if item.get("in_progress")]
    else:
        matches = [item for item in matches if item["los"] > now]

    if not matches:
        print(
            f"X INTENT: no qualifying {'in-progress' if intent == 'CURRENT_OPERATION' else 'upcoming'} "
            f"{satellite} pass from {location_source}; relay blocked."
        )
        return None

    pass_info = matches[0]
    print(
        f"X INTENT PASS CHECK: {satellite} from {grid} using {location_source}; "
        f"AOS {pass_info['aos'].strftime('%H:%MZ')}, "
        f"LOS {pass_info['los'].strftime('%H:%MZ')}, "
        f"MAX {pass_info['max_elevation']:.1f} deg ({gp_source})."
    )
    return {
        "intent": intent,
        "satellite": satellite,
        "grid": grid,
        "pass": pass_info,
    }


def build_rover_report_post(details):
    """Create SatRoverWatch's own concise report from verified intent/pass facts."""
    pass_info = details["pass"]
    satellite = details["satellite"]
    grid = details["grid"]

    if details["intent"] == "CURRENT_OPERATION":
        post = (
            f"{X_ROVER_HANDLE} {CALLSIGN} reports current operation on "
            f"{satellite} from {grid} now. 📡🛰️"
        )
        post += f"\nCalculated LOS: {pass_info['los'].strftime('%H:%MZ')}"
    else:
        post = (
            f"{X_ROVER_HANDLE} {CALLSIGN} reports plans to operate "
            f"{satellite} from {grid}. 📡🛰️"
        )
        if pass_info.get("in_progress"):
            post += f"\nPass is in progress; calculated LOS {pass_info['los'].strftime('%H:%MZ')}."
        else:
            post += (
                f"\nCalculated pass: AOS {pass_info['aos'].strftime('%H:%MZ')}, "
                f"MAX {pass_info['max_elevation']:.0f}°, "
                f"LOS {pass_info['los'].strftime('%H:%MZ')}."
            )
    return post


# ------------------------------------------------------------
# Rover map image
# ------------------------------------------------------------


# ------------------------------------------------------------
# X posting
# ------------------------------------------------------------

def x_credentials_available():
    """Return True when all four OAuth 1.0a credentials are available."""

    return all(
        (
            X_CONSUMER_KEY,
            X_CONSUMER_SECRET,
            X_ACCESS_TOKEN,
            X_ACCESS_TOKEN_SECRET,
        )
    )


def upload_x_image(media_path):
    """Upload one PNG/JPEG image to X and return its media ID, or None."""
    if not media_path:
        return None

    media_path = Path(media_path)
    if not media_path.exists():
        print(f"MAP: media file does not exist: {media_path}")
        return None

    auth = x_oauth1()
    if auth is None:
        print("MAP: X OAuth credentials unavailable for media upload.")
        return None

    try:
        with media_path.open("rb") as image_file:
            response = requests.post(
                "https://upload.twitter.com/1.1/media/upload.json",
                auth=auth,
                files={"media": (media_path.name, image_file)},
                timeout=30,
            )
    except (OSError, requests.RequestException) as exc:
        print(f"MAP: X media upload failed: {exc}")
        return None

    if response.status_code not in (200, 201, 202):
        print(
            "MAP: X media upload was not accepted. "
            f"HTTP status: {response.status_code}"
        )
        print(response.text)
        return None

    try:
        response_data = response.json()
        media_id = response_data.get("media_id_string") or response_data.get("media_id")
    except ValueError:
        print("MAP: X media upload returned invalid JSON.")
        print(response.text)
        return None

    if media_id is None:
        print("MAP: X media upload response contained no media ID.")
        print(response.text)
        return None

    media_id = str(media_id)
    print(f"MAP: X media upload succeeded. Media ID: {media_id}")
    return media_id


def post_to_x(post_text, media_path=None):
    """Publish one post to X and return its Post ID on success."""

    print()
    print("X POST:")
    print("----------------------------------------")
    print(post_text)
    print("----------------------------------------")

    if not X_POSTING_ENABLED:
        print("X posting is DISABLED. Nothing was published.")
        return None

    if not x_credentials_available():
        print(
            "ERROR: X posting is enabled, but one or more "
            "OAuth credentials are missing."
        )
        return None

    auth = x_oauth1()

    payload = {"text": post_text}
    if media_path:
        try:
            media_id = upload_x_image(media_path)
        except Exception as exc:
            # A map is optional. An unexpected media problem must never prevent
            # the underlying SatRoverWatch alert from being published.
            print(f"MAP: unexpected media-upload error: {exc}")
            media_id = None

        if media_id:
            payload["media"] = {"media_ids": [media_id]}
        else:
            print("MAP: media unavailable; publishing the normal text post.")

    try:
        response = requests.post(
            "https://api.x.com/2/tweets",
            auth=auth,
            json=payload,
            timeout=15,
        )

    except requests.RequestException as exc:
        print(f"ERROR: X request failed: {exc}")
        return None

    if response.status_code != 201:
        print(
            f"ERROR: X did not create the post. "
            f"HTTP status: {response.status_code}"
        )
        print(response.text)
        return None

    try:
        response_data = response.json()
        post_id = response_data["data"]["id"]

    except (ValueError, KeyError, TypeError):
        print("ERROR: X returned an unexpected response.")
        print(response.text)
        return None

    print(f"SUCCESS: X post created. Post ID: {post_id}")

    return post_id




def post_to_x_with_map(
    post_text,
    latitude,
    longitude,
    grid6,
    grid_transition=None,
    previous_latitude=None,
    previous_longitude=None,
    prefer_track_map=False,
):
    """Generate the appropriate deterministic rover map, then publish."""
    map_path = None

    # Grid changes use the proven wide-area SQLite history map: recent route,
    # exact Maidenhead boundaries/labels, and the latest rover position.
    if grid_transition is not None or prefer_track_map:
        try:
            map_path = generate_recent_track_map(
                CALLSIGN,
                TRACK_MAP_WINDOW_MINUTES,
            )
        except Exception as exc:
            print(
                "WARNING: Wide-area APRS track map failed; "
                f"falling back to the normal rover map: {exc}"
            )

    # Idle events and any track-map failure retain the existing location-map
    # behavior. Return events explicitly request the newer track-style map.
    if map_path is None:
        map_path = generate_rover_map(
            latitude,
            longitude,
            CALLSIGN,
            grid6,
            grid_transition=grid_transition,
            previous_latitude=previous_latitude,
            previous_longitude=previous_longitude,
        )

    return post_to_x(post_text, media_path=map_path)


# ------------------------------------------------------------
# Historical APRS position database
# ------------------------------------------------------------

def database_optional_float(value):
    """Convert an optional APRS.fi value to float without affecting watcher flow."""
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def database_first_value(mapping, *names):
    """Return the first non-empty value from a set of possible APRS.fi keys."""
    for name in names:
        value = mapping.get(name)
        if value not in (None, ""):
            return value
    return None


def database_record_aprs_position(station, callsign, aprs_callsign):
    """Fail-safe historical recorder for one APRS.fi location report.

    The live watcher remains authoritative for event handling through state.json.
    This SQLite database is historical storage only. Any database failure is
    logged and deliberately does not stop APRS/event/X processing.
    """
    try:
        db_latitude = float(station["lat"])
        db_longitude = float(station["lng"])
        db_timestamp_raw = database_first_value(station, "lasttime", "time")
        if db_timestamp_raw is None:
            raise ValueError("APRS.fi entry contained no position timestamp")
        db_timestamp = int(float(db_timestamp_raw))

        db_grid4 = maidenhead4(db_latitude, db_longitude)
        db_grid6 = maidenhead6(db_latitude, db_longitude)
        now_text = datetime.now(timezone.utc).isoformat(timespec="seconds")
        raw_json = json.dumps(station, sort_keys=True, separators=(",", ":"))

        with sqlite3.connect(DATABASE_FILE, timeout=10) as conn:
            conn.execute("PRAGMA foreign_keys = ON")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS rovers (
                    id              INTEGER PRIMARY KEY,
                    callsign        TEXT NOT NULL COLLATE NOCASE,
                    aprs_callsign   TEXT NOT NULL COLLATE NOCASE,
                    enabled         INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
                    created_at      TEXT NOT NULL,
                    updated_at      TEXT NOT NULL,
                    UNIQUE(callsign, aprs_callsign)
                );

                CREATE TABLE IF NOT EXISTS positions (
                    id                  INTEGER PRIMARY KEY,
                    rover_id            INTEGER NOT NULL,
                    aprs_timestamp      INTEGER NOT NULL,
                    latitude            REAL NOT NULL,
                    longitude           REAL NOT NULL,
                    speed_kmh           REAL,
                    course_deg          REAL,
                    altitude_m          REAL,
                    grid4               TEXT NOT NULL,
                    grid6               TEXT NOT NULL,
                    symbol_table        TEXT,
                    symbol_code         TEXT,
                    comment_text        TEXT,
                    status_text         TEXT,
                    aprs_type           TEXT,
                    aprs_lasttime       TEXT,
                    aprs_lastpath       TEXT,
                    aprs_srccall        TEXT,
                    raw_entry_json      TEXT NOT NULL,
                    recorded_at         TEXT NOT NULL,
                    FOREIGN KEY (rover_id) REFERENCES rovers(id) ON DELETE CASCADE,
                    UNIQUE(rover_id, aprs_timestamp)
                );

                CREATE INDEX IF NOT EXISTS idx_positions_rover_time
                    ON positions(rover_id, aprs_timestamp);
                CREATE INDEX IF NOT EXISTS idx_positions_grid4
                    ON positions(grid4);
                CREATE INDEX IF NOT EXISTS idx_positions_grid6
                    ON positions(grid6);
                """
            )

            rover = conn.execute(
                """
                SELECT id FROM rovers
                WHERE callsign = ? AND aprs_callsign = ?
                """,
                (callsign, aprs_callsign),
            ).fetchone()

            if rover is None:
                cursor = conn.execute(
                    """
                    INSERT INTO rovers
                        (callsign, aprs_callsign, enabled, created_at, updated_at)
                    VALUES (?, ?, 1, ?, ?)
                    """,
                    (callsign.upper(), aprs_callsign.upper(), now_text, now_text),
                )
                rover_id = cursor.lastrowid
            else:
                rover_id = rover[0]
                conn.execute(
                    "UPDATE rovers SET updated_at = ? WHERE id = ?",
                    (now_text, rover_id),
                )

            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO positions (
                    rover_id, aprs_timestamp, latitude, longitude,
                    speed_kmh, course_deg, altitude_m, grid4, grid6,
                    symbol_table, symbol_code, comment_text, status_text,
                    aprs_type, aprs_lasttime, aprs_lastpath, aprs_srccall,
                    raw_entry_json, recorded_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    rover_id,
                    db_timestamp,
                    db_latitude,
                    db_longitude,
                    database_optional_float(station.get("speed")),
                    database_optional_float(station.get("course")),
                    database_optional_float(database_first_value(station, "altitude", "alt")),
                    db_grid4,
                    db_grid6,
                    database_first_value(station, "symbol_table", "symboltable"),
                    database_first_value(station, "symbol_code", "symbol"),
                    database_first_value(station, "comment", "comment_text"),
                    database_first_value(station, "status", "status_text"),
                    station.get("type"),
                    station.get("lasttime"),
                    database_first_value(station, "lastpath", "path"),
                    database_first_value(station, "srccall", "name"),
                    raw_json,
                    now_text,
                ),
            )

        if cursor.rowcount == 1:
            print(
                "DATABASE: Stored new APRS position "
                f"for {aprs_callsign} at {utc_text(db_timestamp)}."
            )
        else:
            print(
                "DATABASE: APRS position already stored "
                f"for {aprs_callsign} at {utc_text(db_timestamp)}."
            )
        return True

    except (OSError, sqlite3.Error, TypeError, ValueError, KeyError) as exc:
        print(f"WARNING: DATABASE: Unable to store APRS position: {exc}")
        return False


# ------------------------------------------------------------
# Rover tracking configuration
# ------------------------------------------------------------

print()
print("ROVER TRACKING:")
print(f"{CALLSIGN} = ENABLED (APRS: {APRS_CALLSIGN})")


# ------------------------------------------------------------
# Query APRS.fi
# ------------------------------------------------------------

url = "https://api.aprs.fi/api/get"

params = {
    "name": APRS_CALLSIGN,
    "what": "loc",
    "apikey": APRSFI_API_KEY,
    "format": "json",
}

headers = {
    "User-Agent": "SatRoverWatch/0.2 (https://x.com/SatRoverWatch)"
}

try:
    response = requests.get(
        url,
        params=params,
        headers=headers,
        timeout=10,
    )

    response.raise_for_status()
    data = response.json()

except requests.RequestException as exc:
    raise SystemExit(f"APRS.fi request failed: {exc}")

except ValueError as exc:
    raise SystemExit(f"Invalid response from APRS.fi: {exc}")


# ------------------------------------------------------------
# Validate APRS.fi response
# ------------------------------------------------------------

if data.get("result") != "ok":
    raise SystemExit(
        f"APRS.fi error: "
        f"{data.get('description', 'Unknown error')}"
    )

if int(data.get("found", 0)) == 0:
    raise SystemExit(
        f"No APRS.fi position found for {CALLSIGN}"
    )

station = data["entries"][0]


# ------------------------------------------------------------
# Current APRS information
# ------------------------------------------------------------

latitude = float(station["lat"])
longitude = float(station["lng"])

grid = maidenhead4(latitude, longitude)
grid6 = maidenhead6(latitude, longitude)

timestamp = station.get("lasttime") or station.get("time")

if not timestamp:
    raise SystemExit(
        "ERROR: APRS.fi did not return a position timestamp."
    )

position_timestamp = int(timestamp)

# Historical storage is intentionally independent of state/event processing.
# A database failure is logged inside the recorder and never stops the watcher.
database_record_aprs_position(station, CALLSIGN, APRS_CALLSIGN)

position_time = datetime.fromtimestamp(
    position_timestamp,
    tz=timezone.utc,
)

now = datetime.now(timezone.utc)

age_seconds = max(
    0,
    (now - position_time).total_seconds(),
)

age_hours = age_seconds / 3600

position_time_text = utc_text(position_timestamp)
age_text = format_duration(age_seconds)

speed = None

if "speed" in station:
    try:
        speed = float(station["speed"])
    except (TypeError, ValueError):
        pass

course = station.get("course")

is_moving = (
    speed is not None
    and speed >= MOVING_SPEED_KMH
)

currently_stale = (
    age_hours >= REPORTING_GAP_HOURS
)


# ------------------------------------------------------------
# Load previous state
# ------------------------------------------------------------

previous_state = load_state()

previous_timestamp = previous_state.get(
    "last_position_timestamp"
)

previous_grid = previous_state.get("grid")

previous_latitude = previous_state.get("latitude")
previous_longitude = previous_state.get("longitude")
previous_speed = previous_state.get("speed_kmh")

previous_x_event_key = previous_state.get(
    "last_x_event_key"
)

previous_x_post_id = previous_state.get(
    "last_x_post_id"
)

previous_idle_message = previous_state.get(
    "last_idle_message"
)

previous_message_choices = previous_state.get("last_message_choices", {})
if not isinstance(previous_message_choices, dict):
    previous_message_choices = {}

previous_silent_stop_event_key = previous_state.get(
    "last_silent_stop_event_key"
)

previous_rover_x_user_id = previous_state.get("rover_x_user_id")
previous_rover_x_post_id = previous_state.get("last_rover_x_post_id")

previous_home_geofence_inside = bool(
    previous_state.get("home_geofence_inside", False)
)
previous_home_arrival_event_key = previous_state.get(
    "last_home_arrival_event_key"
)

previous_idle_status = previous_state.get(
    "idle_status",
    "MOVING",
)

previous_idle_anchor_lat = previous_state.get(
    "idle_anchor_latitude"
)

previous_idle_anchor_lon = previous_state.get(
    "idle_anchor_longitude"
)

previous_idle_candidate_start = previous_state.get(
    "idle_candidate_start_timestamp"
)

previous_idle_since = previous_state.get(
    "idle_since_timestamp"
)

previous_idle_report_count = int(
    previous_state.get("idle_report_count", 0) or 0
)

first_run = previous_timestamp is None

new_report = (
    previous_timestamp is not None
    and position_timestamp > previous_timestamp
)


# ------------------------------------------------------------
# Calculate gap between APRS reports
# ------------------------------------------------------------

reporting_gap_seconds = None
long_reporting_gap = False

if new_report:

    reporting_gap_seconds = (
        position_timestamp - previous_timestamp
    )

    long_reporting_gap = (
        reporting_gap_seconds / 3600
        >= REPORTING_GAP_HOURS
    )


# ------------------------------------------------------------
# Detect grid change
# ------------------------------------------------------------

grid_changed = (
    new_report
    and previous_grid is not None
    and grid != previous_grid
)


# ------------------------------------------------------------
# Idle / stationary detection
#
# A candidate idle position begins when a new APRS report shows
# the rover below MOVING_SPEED_KMH. The first such position becomes
# a fixed anchor. If later APRS positions stay within 250 meters of
# that anchor for 10 minutes of APRS reporting time, the rover is
# classified IDLE.
#
# The anchor does NOT move with every report. This prevents a slowly
# creeping rover from carrying the idle circle along with it.
#
# A newly confirmed IDLE state can create one X post.
# ------------------------------------------------------------

idle_status = previous_idle_status
idle_anchor_lat = previous_idle_anchor_lat
idle_anchor_lon = previous_idle_anchor_lon
idle_candidate_start = previous_idle_candidate_start
idle_since = previous_idle_since
idle_report_count = previous_idle_report_count

idle_distance_from_anchor = None
idle_transition = None

if idle_status not in ("MOVING", "CANDIDATE", "IDLE"):
    idle_status = "MOVING"
    idle_anchor_lat = None
    idle_anchor_lon = None
    idle_candidate_start = None
    idle_since = None


# Migration helper:
# The old script already saved the previous coordinate and speed.
# If this is our first run with idle fields and both the previous
# and current reports indicate a stop in nearly the same place,
# use the previous report as the beginning of the candidate.
if (
    new_report
    and previous_idle_status == "MOVING"
    and "idle_status" not in previous_state
    and previous_timestamp is not None
    and previous_latitude is not None
    and previous_longitude is not None
    and previous_speed is not None
    and previous_speed < MOVING_SPEED_KMH
    and not is_moving
):
    migration_distance = distance_meters(
        float(previous_latitude),
        float(previous_longitude),
        latitude,
        longitude,
    )

    if migration_distance <= IDLE_RADIUS_METERS:
        idle_status = "CANDIDATE"
        idle_anchor_lat = float(previous_latitude)
        idle_anchor_lon = float(previous_longitude)
        idle_candidate_start = previous_timestamp
        idle_since = None
        idle_report_count = 2
        idle_transition = "CANDIDATE"


if new_report:

    if idle_status == "MOVING":

        if not is_moving:
            idle_status = "CANDIDATE"
            idle_anchor_lat = latitude
            idle_anchor_lon = longitude
            idle_candidate_start = position_timestamp
            idle_since = None
            idle_report_count = 1

            if idle_transition is None:
                idle_transition = "CANDIDATE"

    elif idle_status == "CANDIDATE":

        if (
            idle_anchor_lat is None
            or idle_anchor_lon is None
            or idle_candidate_start is None
        ):
            idle_status = "MOVING"
            idle_anchor_lat = None
            idle_anchor_lon = None
            idle_candidate_start = None
            idle_since = None

        else:
            idle_distance_from_anchor = distance_meters(
                float(idle_anchor_lat),
                float(idle_anchor_lon),
                latitude,
                longitude,
            )

            if idle_distance_from_anchor <= IDLE_RADIUS_METERS:

                idle_report_count += 1

                candidate_seconds = (
                    position_timestamp
                    - int(idle_candidate_start)
                )

                # Two successive stationary reports inside the fixed radius are
                # enough to treat this as a stop. We no longer require OSM to
                # decide whether the stop is "ordinary" first: a rover can eat,
                # refuel, take a break, and still work a satellite from the same spot.
                two_report_stop = (
                    idle_report_count >= POSSIBLE_ACTIVATION_REPORTS
                )

                if (
                    two_report_stop
                    or candidate_seconds >= IDLE_CONFIRM_MINUTES * 60
                ):
                    idle_status = "IDLE"
                    idle_since = int(idle_candidate_start)
                    idle_transition = "IDLE"

            else:

                if is_moving:
                    idle_status = "MOVING"
                    idle_anchor_lat = None
                    idle_anchor_lon = None
                    idle_candidate_start = None
                    idle_since = None
                    idle_transition = "MOVING"

                else:
                    # The rover moved outside the old idle circle
                    # but is again slow/stopped. Start a new candidate
                    # circle at the new position.
                    idle_status = "CANDIDATE"
                    idle_anchor_lat = latitude
                    idle_anchor_lon = longitude
                    idle_candidate_start = position_timestamp
                    idle_since = None
                    idle_report_count = 1
                    idle_distance_from_anchor = 0.0
                    idle_transition = "CANDIDATE_RESET"

    elif idle_status == "IDLE":

        if (
            idle_anchor_lat is None
            or idle_anchor_lon is None
        ):
            idle_status = "MOVING"
            idle_anchor_lat = None
            idle_anchor_lon = None
            idle_candidate_start = None
            idle_since = None

        else:
            idle_distance_from_anchor = distance_meters(
                float(idle_anchor_lat),
                float(idle_anchor_lon),
                latitude,
                longitude,
            )

            if idle_distance_from_anchor > IDLE_RADIUS_METERS:

                if is_moving:
                    idle_status = "MOVING"
                    idle_anchor_lat = None
                    idle_anchor_lon = None
                    idle_candidate_start = None
                    idle_since = None
                    idle_transition = "MOVING"

                else:
                    idle_status = "CANDIDATE"
                    idle_anchor_lat = latitude
                    idle_anchor_lon = longitude
                    idle_candidate_start = position_timestamp
                    idle_since = None
                    idle_report_count = 1
                    idle_distance_from_anchor = 0.0
                    idle_transition = "CANDIDATE_RESET"


# Calculate current distance from the fixed anchor for display,
# including cron runs where APRS.fi has no newer report.
if (
    idle_status in ("CANDIDATE", "IDLE")
    and idle_anchor_lat is not None
    and idle_anchor_lon is not None
):
    idle_distance_from_anchor = distance_meters(
        float(idle_anchor_lat),
        float(idle_anchor_lon),
        latitude,
        longitude,
    )


# ------------------------------------------------------------
# Display current rover status
# ------------------------------------------------------------

print()
print("SatRoverWatch - APRS.fi Rover Watcher")
print("--------------------------------------")

print(f"Station:     {station['name']}")
print(f"Latitude:    {latitude:.5f}")
print(f"Longitude:   {longitude:.5f}")
print(f"Grid:        {grid}")
print(f"Grid (6):    {grid6}")
print(f"Last heard:  {position_time_text}")
print(f"Report age:  {age_text}")

if speed is not None:
    print(f"Speed:       {speed:.1f} km/h")

if course is not None:
    print(f"Course:      {course} degrees")

print()

if currently_stale:
    print(
        f"APRS STATUS: No new position report for "
        f"{REPORTING_GAP_HOURS}+ hours."
    )
else:
    print("APRS STATUS: Reporting recently.")

print(
    "X POSTING:  "
    + ("ENABLED" if X_POSTING_ENABLED else "DISABLED")
)

if idle_status == "MOVING":
    print("ROVER STATE: MOVING")

elif idle_status == "CANDIDATE":
    candidate_seconds = max(
        0,
        position_timestamp - int(idle_candidate_start),
    )

    print(
        "ROVER STATE: IDLE CANDIDATE "
        f"({format_duration(candidate_seconds)} "
        f"within {IDLE_RADIUS_METERS:.0f} m)"
    )

    if idle_distance_from_anchor is not None:
        print(
            "Idle radius: "
            f"{idle_distance_from_anchor:.0f} m "
            "from fixed anchor"
        )

elif idle_status == "IDLE":
    idle_seconds = max(
        0,
        position_timestamp - int(idle_since),
    )

    print(
        "ROVER STATE: IDLE "
        f"({format_duration(idle_seconds)} at location)"
    )

    if idle_distance_from_anchor is not None:
        print(
            "Idle radius: "
            f"{idle_distance_from_anchor:.0f} m "
            "from fixed anchor"
        )


# ------------------------------------------------------------
# Home geofence status
# ------------------------------------------------------------

if (
    HOME_GEOFENCE_ENABLED
    and HOME_GEOFENCE_LATITUDE is not None
    and HOME_GEOFENCE_LONGITUDE is not None
):
    home_distance_m = distance_meters(
        latitude,
        longitude,
        float(HOME_GEOFENCE_LATITUDE),
        float(HOME_GEOFENCE_LONGITUDE),
    )
    home_distance_km = home_distance_m / 1000.0
    home_geofence_inside = home_distance_km <= HOME_GEOFENCE_RADIUS_KM

    print()
    print(
        f"HOME GEOFENCE: {home_distance_km:.1f} km from home-zone center "
        f"({'INSIDE' if home_geofence_inside else 'OUTSIDE'} "
        f"{HOME_GEOFENCE_RADIUS_KM:.0f} km radius)"
    )
else:
    home_distance_km = None
    home_geofence_inside = False


# ------------------------------------------------------------
# Event detection
# ------------------------------------------------------------

print()
print("EVENTS:")

new_x_event_key = previous_x_event_key
new_x_post_id = previous_x_post_id
new_idle_message = previous_idle_message
new_message_choices = dict(previous_message_choices)
new_silent_stop_event_key = previous_silent_stop_event_key
new_rover_x_user_id = previous_rover_x_user_id
new_rover_x_post_id = previous_rover_x_post_id
new_home_arrival_event_key = previous_home_arrival_event_key


if first_run:

    print("Initial rover state recorded.")


elif new_report:

    print("New APRS report received.")

    if reporting_gap_seconds is not None:
        print(
            "Time since previous APRS report: "
            f"{format_duration(reporting_gap_seconds)}"
        )

    if idle_transition == "CANDIDATE":
        print()
        print(
            "*** IDLE CANDIDATE STARTED: "
            f"watching a {IDLE_RADIUS_METERS:.0f} m radius "
            f"for {IDLE_CONFIRM_MINUTES} minutes ***"
        )

    elif idle_transition == "CANDIDATE_RESET":
        print()
        print(
            "*** IDLE CANDIDATE RESET: "
            "rover left the previous idle circle but "
            "is slow/stopped again ***"
        )

    elif idle_transition == "IDLE":
        print()
        print(
            f"*** ROVER IDLE: remained within "
            f"{IDLE_RADIUS_METERS:.0f} m for "
            f"{IDLE_CONFIRM_MINUTES}+ minutes ***"
        )

    elif idle_transition == "MOVING":
        print()
        print(
            "*** ROVER MOVING AGAIN: "
            f"left the {IDLE_RADIUS_METERS:.0f} m idle radius ***"
        )

    # --------------------------------------------------------
    # Home arrival
    # A new APRS report crossing from outside to inside the 10 km home zone
    # takes priority over return/grid/idle events so one packet creates only
    # one public SatRoverWatch post. The exact residence is never published.
    # --------------------------------------------------------

    home_arrival = (
        HOME_GEOFENCE_ENABLED
        and home_geofence_inside
        and not previous_home_geofence_inside
    )

    if home_arrival:

        print()
        print(
            f"*** HOME ARRIVAL: entered {HOME_GEOFENCE_RADIUS_KM:.0f} km "
            f"home geofence ({home_distance_km:.1f} km from center) ***"
        )

        home_event_key = f"home:{CALLSIGN}:{position_timestamp}"

        print()
        print("HOME ARRIVAL EVENT KEY:")
        print(home_event_key)

        if home_event_key == previous_home_arrival_event_key:
            print(
                "This home-arrival event was already handled. "
                "No duplicate X post."
            )
        else:
            # Deliberately text-only: do not attach a map to a home-arrival post.
            home_post_id = post_to_x(HOME_ARRIVAL_MESSAGE)

            if X_POSTING_ENABLED and home_post_id:
                new_x_event_key = home_event_key
                new_x_post_id = home_post_id
                new_home_arrival_event_key = home_event_key

    # --------------------------------------------------------
    # Return after long reporting gap
    # This takes priority over a simultaneous grid change.
    # --------------------------------------------------------

    elif long_reporting_gap and not currently_stale:

        print()
        print(
            f"*** RETURN AFTER "
            f"{REPORTING_GAP_HOURS}+ HOUR APRS GAP ***"
        )

        friendly_message = choose_message(
            "ROVER_RETURN",
            previous_message_choices.get("ROVER_RETURN"),
        )
        new_message_choices["ROVER_RETURN"] = friendly_message

        # Weather is intentionally queried only for an 8+ hour return event.
        # Failure or non-U.S. coverage never prevents the rover alert.
        weather_line = get_nws_daytime_weather(
            latitude,
            longitude,
        )

        if is_moving:

            return_post = (
                f"{CALLSIGN} has turned on APRS "
                f"and is on the move! 📡🚙\n"
                f"Current grid: {grid}"
            )

            if speed is not None:
                mph = speed * 0.621371
                return_post += (
                    f" | Current speed: {mph:.0f} mph"
                )

        else:

            return_post = (
                f"{CALLSIGN} has turned on APRS! 📡\n"
                f"Current grid: {grid}"
            )

        if grid_changed:
            return_post += (
                f"\nStarting the day in {grid}, "
                f"after last reporting from {previous_grid}."
            )

        if weather_line:
            return_post += f"\n\n{weather_line}"

        return_post += (
            f"\n\n{X_ROVER_HANDLE} {friendly_message} 🛰️\n"
            f"Position via APRS.fi: https://aprs.fi/{CALLSIGN}"
        )

        return_event_key = (
            f"return:{CALLSIGN}:{position_timestamp}"
        )

        print()
        print("RETURN EVENT KEY:")
        print(return_event_key)

        if return_event_key == previous_x_event_key:

            print(
                "This return event was already handled. "
                "No duplicate X post."
            )

        else:

            return_post_id = post_to_x_with_map(
                return_post, latitude, longitude, grid6,
                prefer_track_map=True,
            )

            if X_POSTING_ENABLED and return_post_id:
                new_x_event_key = return_event_key
                new_x_post_id = return_post_id


    # --------------------------------------------------------
    # Normal grid change
    # Do not send separately if this was also a return event.
    # --------------------------------------------------------

    elif grid_changed:

        print()
        print(
            f"*** GRID CHANGE: "
            f"{previous_grid} -> {grid} ***"
        )

        grid_message = render_message(
            "GRID_CHANGE",
            previous_message_choices.get("GRID_CHANGE"),
            callsign=CALLSIGN,
            old_grid=previous_grid,
            new_grid=grid,
        )
        new_message_choices["GRID_CHANGE"] = grid_message
        grid_post = grid_message

        if is_moving:
            moving_message = choose_message(
                "GRID_MOVING",
                previous_message_choices.get("GRID_MOVING"),
            )
            new_message_choices["GRID_MOVING"] = moving_message
            grid_post += f"\n{moving_message}"

        grid_signoff = choose_message(
            "GRID_SIGNOFF",
            previous_message_choices.get("GRID_SIGNOFF"),
        )
        new_message_choices["GRID_SIGNOFF"] = grid_signoff

        grid_post += (
            f"\n\n{X_ROVER_HANDLE} {grid_signoff}\n"
            f"Position via APRS.fi: https://aprs.fi/{CALLSIGN}"
        )

        grid_event_key = (
            f"grid:{CALLSIGN}:"
            f"{previous_grid}:{grid}:"
            f"{position_timestamp}"
        )

        print()
        print("GRID EVENT KEY:")
        print(grid_event_key)

        if grid_event_key == previous_x_event_key:

            print(
                "This grid event was already handled. "
                "No duplicate X post."
            )

        else:

            grid_post_id = post_to_x_with_map(
                grid_post,
                latitude,
                longitude,
                grid6,
                grid_transition=(previous_grid, grid),
                previous_latitude=previous_latitude,
                previous_longitude=previous_longitude,
            )

            if X_POSTING_ENABLED and grid_post_id:
                new_x_event_key = grid_event_key
                new_x_post_id = grid_post_id


    # --------------------------------------------------------
    # Confirmed idle
    # Return and grid-change events keep priority so one APRS
    # report cannot create multiple X posts.
    # --------------------------------------------------------

    elif idle_transition == "IDLE":

        county_text = reverse_geocode_county(
            latitude,
            longitude,
        )

        nearby_features = nearby_osm_features(
            latitude,
            longitude,
        )

        if nearby_features is None:
            print(
                "IDLE CONTEXT: OSM lookup unavailable. "
                "Continuing with a generic stop message and satellite passes."
            )
            nearby_features = []

        if True:
            if nearby_features:
                print()
                print(f"OpenStreetMap features within {OSM_NEARBY_RADIUS_METERS} m:")
                for feature in nearby_features[:5]:
                    print(f"  {feature['distance_m']:.0f} m - {feature['name']} ({feature['kind']})")

            idle_category, public_nearby_text = classify_idle_context(nearby_features)
            idle_message = choose_idle_message(idle_category, previous_idle_message)

            ordinary_stop = ordinary_stop_feature(nearby_features)
            if ordinary_stop:
                print()
                print(
                    "SATELLITE PASS CHECK: Suppressed because an ordinary-stop "
                    f"POI is within {ORDINARY_STOP_RADIUS_METERS} m "
                    f"({ordinary_stop['kind']}, {ordinary_stop['distance_m']:.0f} m)."
                )
                activation_pass_lines = []
            else:
                activation_passes, activation_gp_source = get_qualifying_passes(
                    latitude,
                    longitude,
                    now,
                )
                print_pass_context(activation_passes, activation_gp_source)
                activation_pass_lines = public_pass_lines(
                    activation_passes,
                    now,
                )

            print()
            print(f"Idle message category: {idle_category}")
            print(f"Selected idle message: {idle_message}")

            idle_post = f"{CALLSIGN} has stopped in {grid6}. 📡🚙\n"
            if county_text:
                idle_post += f"Location: {county_text}\n"
            # We deliberately keep the reason for the stop ambiguous. Nearby
            # food/fuel/lodging is context, not proof that satellites are not
            # also part of the stop.
            if public_nearby_text:
                idle_post += f"Could be {public_nearby_text} — or satellites. 📡🛰️\n"
            else:
                idle_post += "Food, fuel, a rover break — or satellites? 📡🛰️\n"
            idle_post += "Our guess is as good as yours!\n"

            if activation_pass_lines:
                idle_post += "\nNext 5 qualifying passes:\n"
                for pass_line in activation_pass_lines:
                    idle_post += f"• {pass_line}\n"

            idle_post += (
                f"\n{X_ROVER_HANDLE} We will keep an eye on you! 😄\n"
                f"Position via APRS.fi: https://aprs.fi/{CALLSIGN}"
            )

            idle_event_key = (
                f"idle:{CALLSIGN}:"
                f"{int(idle_since)}:"
                f"{grid6}"
            )

            print()
            print("IDLE EVENT KEY:")
            print(idle_event_key)

            if idle_event_key == previous_x_event_key:

                print(
                    "This idle event was already handled. "
                    "No duplicate X post."
                )

            else:

                idle_post_id = post_to_x_with_map(idle_post, latitude, longitude, grid6)

                if X_POSTING_ENABLED and idle_post_id:
                    new_x_event_key = idle_event_key
                    new_x_post_id = idle_post_id
                    new_idle_message = idle_message


else:

    print("No new APRS report.")

    # A rover may arrive at lodging while still rolling slowly, send one final
    # APRS packet, and then turn APRS off. Cron still runs, so after the configured delay
    # of silence we can cautiously check whether mapped lodging is very close
    # to that final coordinate. Generic signal loss is not enough: the final
    # speed must be below SILENT_STOP_MAX_SPEED_KMH and lodging must be nearby.
    silent_stop_candidate = (
        speed is not None
        and speed < SILENT_STOP_MAX_SPEED_KMH
        and age_seconds >= SILENT_STOP_CONFIRM_MINUTES * 60
        and age_seconds < REPORTING_GAP_HOURS * 3600
    )

    if silent_stop_candidate:
        silent_stop_event_key = (
            f"silent-stop:{CALLSIGN}:{position_timestamp}:{grid6}"
        )

        if silent_stop_event_key == previous_silent_stop_event_key:
            print(
                "Silent-stop event already handled. "
                "No duplicate lodging post."
            )
        else:
            print()
            print(
                "*** STOPPED APRS PACKET FOLLOWED BY "
                f"{SILENT_STOP_CONFIRM_MINUTES}+ MINUTES OF SILENCE ***"
            )

            nearby_features = nearby_osm_features(latitude, longitude)

            if nearby_features is None:
                print(
                    "SILENT-STOP CHECK: OSM lookup unavailable. "
                    "No lodging decision this cycle."
                )
                lodging_feature = None

            else:
                lodging_feature = nearby_lodging_feature(nearby_features)

                if nearby_features:
                    print()
                    print(
                        "OpenStreetMap features within "
                        f"{OSM_NEARBY_RADIUS_METERS} m:"
                    )
                    for feature in nearby_features[:5]:
                        print(
                            f"  {feature['distance_m']:.0f} m - "
                            f"{feature['name']} ({feature['kind']})"
                        )

            if nearby_features is None:
                pass

            elif lodging_feature is None:
                print(
                    f"No lodging within {LODGING_RADIUS_METERS} m. "
                    "No silent-stop post."
                )
            else:
                county_text = reverse_geocode_county(latitude, longitude)
                idle_message = choose_idle_message(
                    "QUIET_TIME",
                    previous_idle_message,
                )

                silent_post = (
                    f"{CALLSIGN}'s APRS has gone quiet after "
                    f"stopping in {grid6}. 📡🚙\n"
                )
                if county_text:
                    silent_post += f"Location: {county_text}\n"

                silent_post += (
                    "Looks like the rover may be near a hotel or motel.\n"
                    f"\n{idle_message}\n\n"
                    f"{X_ROVER_HANDLE} We will keep an eye on you! 😄\n"
                    f"Position via APRS.fi: https://aprs.fi/{CALLSIGN}"
                )

                print()
                print("SILENT-STOP EVENT KEY:")
                print(silent_stop_event_key)

                silent_post_id = post_to_x_with_map(silent_post, latitude, longitude, grid6)

                if X_POSTING_ENABLED and silent_post_id:
                    new_x_event_key = silent_stop_event_key
                    new_x_post_id = silent_post_id
                    new_idle_message = idle_message
                    new_silent_stop_event_key = silent_stop_event_key


# ------------------------------------------------------------
# Check the configured rover's X posts for explicit activation reports
#
# This is intentionally independent of APRS event detection. The rover operator's own
# statement is stronger evidence than our "possible activation" inference.
# Protected-post text is never copied into SatRoverWatch's public post.
# ------------------------------------------------------------

print()
print("X ROVER POSTS:")

if not X_ROVER_POSTS_ENABLED or not X_ROVER_USERNAME:
    print("X rover-post reading disabled for this rover.")
elif not x_credentials_available():
    print("X rover-post reading skipped: OAuth credentials unavailable.")
else:
    rover_user = None

    if new_rover_x_user_id:
        rover_user = {
            "id": str(new_rover_x_user_id),
            "username": X_ROVER_USERNAME,
        }
    else:
        rover_user = get_x_user_by_username(X_ROVER_USERNAME)
        if rover_user:
            new_rover_x_user_id = rover_user.get("id")

    if not rover_user or not new_rover_x_user_id:
        print("Could not resolve rover X account. No X relay attempted.")
    else:
        rover_posts = get_recent_rover_x_posts(
            new_rover_x_user_id,
            previous_rover_x_post_id,
        )

        if rover_posts is None:
            print("Rover X timeline unavailable. No X relay attempted.")

        elif not rover_posts:
            print("No new rover X posts.")

        else:
            # Process oldest-to-newest so state advances in chronological order.
            rover_posts = sorted(
                rover_posts,
                key=lambda item: int(item.get("id", 0)),
            )

            for rover_post in rover_posts:
                rover_post_id = rover_post.get("id")
                rover_post_text = rover_post.get("text", "")
                rover_post_created = rover_post.get("created_at")

                if not rover_post_id:
                    continue

                # Always advance the seen-post marker, even when a post is
                # irrelevant. That prevents re-reading ambiguous posts forever.
                new_rover_x_post_id = rover_post_id

                # Ignore old material. This also makes first installation safe:
                # it will not suddenly relay a backlog of protected posts.
                recent_enough = True
                if rover_post_created:
                    try:
                        created_dt = datetime.fromisoformat(
                            rover_post_created.replace("Z", "+00:00")
                        )
                        post_age_hours = (
                            datetime.now(timezone.utc) - created_dt
                        ).total_seconds() / 3600
                        recent_enough = (
                            0 <= post_age_hours <= X_ROVER_POST_LOOKBACK_HOURS
                        )
                    except ValueError:
                        recent_enough = False

                if not recent_enough:
                    print(
                        f"Rover post {rover_post_id}: older than "
                        f"{X_ROVER_POST_LOOKBACK_HOURS} hours; marked seen."
                    )
                    continue

                intent_result = classify_rover_x_intent(rover_post_text)
                activation = validate_rover_x_operation(
                    intent_result,
                    latitude,
                    longitude,
                )

                if activation is None:
                    print(
                        f"Rover post {rover_post_id}: no verified current/future "
                        "satellite operation; marked seen."
                    )
                    continue

                relay_post = build_rover_report_post(activation)

                print(
                    f"Rover post {rover_post_id}: verified {activation['intent']} - "
                    f"{activation['satellite']} from {activation['grid']}"
                )

                relay_event_key = f"rover-x:{CALLSIGN}:{rover_post_id}"

                if relay_event_key == new_x_event_key:
                    print("This rover X report was already relayed.")
                    continue

                relay_post_id = post_to_x(relay_post)

                if X_POSTING_ENABLED and relay_post_id:
                    new_x_event_key = relay_event_key
                    new_x_post_id = relay_post_id


# ------------------------------------------------------------
# Save current state
# ------------------------------------------------------------

state = {
    "callsign": CALLSIGN,
    "last_position_timestamp": position_timestamp,
    "last_position_utc": position_time_text,
    "latitude": latitude,
    "longitude": longitude,
    "grid": grid,
    "grid6": grid6,
    "speed_kmh": speed,
    "course": course,
    "currently_stale": currently_stale,
    "checked_utc": now.strftime(
        "%Y-%m-%d %H:%M:%S UTC"
    ),
    "idle_status": idle_status,
    "idle_anchor_latitude": idle_anchor_lat,
    "idle_anchor_longitude": idle_anchor_lon,
    "idle_candidate_start_timestamp": idle_candidate_start,
    "idle_since_timestamp": idle_since,
    "idle_report_count": idle_report_count,
    "last_x_event_key": new_x_event_key,
    "last_x_post_id": new_x_post_id,
    "last_idle_message": new_idle_message,
    "last_message_choices": new_message_choices,
    "last_silent_stop_event_key": new_silent_stop_event_key,
    "home_geofence_inside": home_geofence_inside,
    "home_geofence_distance_km": (
        round(home_distance_km, 3) if home_distance_km is not None else None
    ),
    "last_home_arrival_event_key": new_home_arrival_event_key,
    "rover_x_user_id": new_rover_x_user_id,
    "last_rover_x_post_id": new_rover_x_post_id,
}

save_state(state)

print()
print(f"State saved to: {STATE_FILE}")
print()
