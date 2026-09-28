"""
SatRoverWatch deterministic map generation.

This module is intentionally AI-free. It contains the existing proven map
rendering, OpenStreetMap tile/location/weather helpers, Maidenhead geometry,
and SQLite recent-track map generation extracted from the former monolithic
watcher.
"""

import io
import math
import re
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from PIL import Image, ImageDraw, ImageFont

MAP_OUTPUT_WIDTH = 2400
MAP_OUTPUT_HEIGHT = 1350
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
MAP_CACHE_DIR = PROJECT_DIR / "cache" / "osm_tiles"
MAP_OUTPUT_FILE = PROJECT_DIR / "cache" / "satroverwatch_map.png"
TRACK_MAP_OUTPUT_FILE = PROJECT_DIR / "cache" / "satroverwatch_track_map.png"
TRACK_MAP_WINDOW_MINUTES = 60
TRACK_MAP_TILE_ZOOM_BOOST = 1
DATABASE_FILE = PROJECT_DIR / "satroverwatch.db"


def maidenhead4(latitude, longitude):
    """Convert decimal latitude/longitude to a 4-character Maidenhead grid."""

    lon = longitude + 180.0
    lat = latitude + 90.0

    field_lon = int(lon // 20)
    field_lat = int(lat // 10)

    square_lon = int((lon % 20) // 2)
    square_lat = int(lat % 10)

    return (
        chr(ord("A") + field_lon)
        + chr(ord("A") + field_lat)
        + str(square_lon)
        + str(square_lat)
    )


def maidenhead6(latitude, longitude):
    """Convert decimal latitude/longitude to a 6-character Maidenhead grid."""

    lon = longitude + 180.0
    lat = latitude + 90.0

    field_lon = int(lon // 20)
    field_lat = int(lat // 10)

    lon_remainder = lon % 20
    lat_remainder = lat % 10

    square_lon = int(lon_remainder // 2)
    square_lat = int(lat_remainder // 1)

    subsquare_lon = int(
        ((lon_remainder % 2) / 2) * 24
    )
    subsquare_lat = int(
        (lat_remainder % 1) * 24
    )

    return (
        chr(ord("A") + field_lon)
        + chr(ord("A") + field_lat)
        + str(square_lon)
        + str(square_lat)
        + chr(ord("A") + subsquare_lon).lower()
        + chr(ord("A") + subsquare_lat).lower()
    )



def utc_text(timestamp):
    """Convert a Unix timestamp to readable UTC."""
    return datetime.fromtimestamp(
        int(timestamp),
        tz=timezone.utc,
    ).strftime("%Y-%m-%d %H:%M:%S UTC")


def map_clamp(value, low, high):
    return max(low, min(high, value))


def map_meters_per_pixel(latitude, zoom):
    return (
        156543.03392804097
        * math.cos(math.radians(latitude))
        / (2 ** zoom)
    )


def map_choose_zoom(latitude):
    target_mpp = (MAP_WIDTH_KM * 1000.0) / MAP_OUTPUT_WIDTH
    raw_zoom = math.log2(
        156543.03392804097
        * math.cos(math.radians(latitude))
        / target_mpp
    )
    return int(map_clamp(round(raw_zoom), 0, 19))


def map_latlon_to_world_pixel(latitude, longitude, zoom):
    latitude = map_clamp(latitude, -85.05112878, 85.05112878)
    scale = MAP_TILE_SIZE * (2 ** zoom)
    x = (longitude + 180.0) / 360.0 * scale
    sin_lat = math.sin(math.radians(latitude))
    y = (
        0.5
        - math.log((1 + sin_lat) / (1 - sin_lat)) / (4 * math.pi)
    ) * scale
    return x, y


def map_load_font(size, bold=False):
    """Load a genuinely scalable font for map labels and panels."""
    candidates = (
        [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf",
            "DejaVuSans-Bold.ttf",
            "LiberationSans-Bold.ttf",
        ]
        if bold
        else [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
            "DejaVuSans.ttf",
            "LiberationSans-Regular.ttf",
        ]
    )
    for candidate in candidates:
        try:
            return ImageFont.truetype(candidate, size=size)
        except OSError:
            pass
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def map_fetch_tile(session, zoom, x, y):
    max_tile = (2 ** zoom) - 1
    x = x % (2 ** zoom)
    y = int(map_clamp(y, 0, max_tile))
    path = MAP_CACHE_DIR / str(zoom) / str(x) / f"{y}.png"

    if path.exists():
        age = time.time() - path.stat().st_mtime
        if age < MAP_TILE_CACHE_SECONDS:
            try:
                return Image.open(path).convert("RGB")
            except OSError:
                pass

    path.parent.mkdir(parents=True, exist_ok=True)
    response = session.get(
        MAP_TILE_URL.format(z=zoom, x=x, y=y),
        timeout=15,
    )
    response.raise_for_status()
    tile = Image.open(io.BytesIO(response.content)).convert("RGB")
    tile.save(path, format="PNG")
    return tile


def map_reverse_location(session, latitude, longitude):
    params = {
        "lat": latitude,
        "lon": longitude,
        "format": "geocodejson",
        "addressdetails": 1,
        "accept-language": "en",
        "zoom": 18,
    }
    try:
        response = session.get(
            MAP_NOMINATIM_URL,
            params=params,
            timeout=15,
        )
        response.raise_for_status()
        features = response.json().get("features") or []
        if not features:
            return {}
        geo = features[0].get("properties", {}).get("geocoding", {})
        return {
            "city": geo.get("city") or geo.get("locality") or geo.get("district"),
            "county": geo.get("county"),
            "state": geo.get("state"),
        }
    except Exception as exc:
        print(f"MAP: location lookup unavailable: {exc}")
        return {}


def map_c_to_f(value):
    try:
        return float(value) * 9.0 / 5.0 + 32.0
    except (TypeError, ValueError):
        return None


def map_kmh_to_mph(value):
    try:
        return float(value) * 0.621371
    except (TypeError, ValueError):
        return None


def map_current_weather(latitude, longitude):
    """Latest nearby NWS station observation. Failure never blocks a post."""
    try:
        points_url = MAP_NWS_POINTS_URL.format(
            lat=f"{latitude:.4f}",
            lon=f"{longitude:.4f}",
        )
        response = requests.get(
            points_url,
            headers=MAP_NWS_HEADERS,
            timeout=15,
        )
        if response.status_code in (400, 404):
            return None
        response.raise_for_status()
        props = response.json().get("properties") or {}
        stations_url = props.get("observationStations")
        if not stations_url:
            return None

        response = requests.get(
            stations_url,
            headers=MAP_NWS_HEADERS,
            timeout=15,
        )
        response.raise_for_status()
        stations = response.json().get("features") or []
        if not stations:
            return None

        station_url = stations[0].get("id")
        if not station_url:
            return None

        response = requests.get(
            station_url.rstrip("/") + "/observations/latest",
            headers=MAP_NWS_HEADERS,
            timeout=15,
        )
        response.raise_for_status()
        obs = response.json().get("properties") or {}

        result = {"description": obs.get("textDescription")}

        temp_f = map_c_to_f((obs.get("temperature") or {}).get("value"))
        if temp_f is not None:
            result["temperature_f"] = round(temp_f)

        wind_mph = map_kmh_to_mph((obs.get("windSpeed") or {}).get("value"))
        if wind_mph is not None:
            result["wind_mph"] = round(wind_mph)

        wind_dir = (obs.get("windDirection") or {}).get("value")
        if wind_dir is not None:
            degrees = float(wind_dir) % 360
            directions = [
                "N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
                "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW",
            ]
            result["wind_direction"] = directions[
                int((degrees + 11.25) // 22.5) % 16
            ]

        return result

    except Exception as exc:
        print(f"MAP: current weather unavailable: {exc}")
        return None



def map_grid4_bounds(grid4):
    """Return west, south, east, north bounds for a 4-character Maidenhead grid."""
    grid4 = (grid4 or "").strip().upper()
    if not re.fullmatch(r"[A-R]{2}[0-9]{2}", grid4):
        return None

    field_lon = ord(grid4[0]) - ord("A")
    field_lat = ord(grid4[1]) - ord("A")
    square_lon = int(grid4[2])
    square_lat = int(grid4[3])

    west = -180.0 + field_lon * 20.0 + square_lon * 2.0
    south = -90.0 + field_lat * 10.0 + square_lat
    return west, south, west + 2.0, south + 1.0


def map_world_to_output_pixel(latitude, longitude, zoom, left, top,
                              requested_width_px, requested_height_px):
    """Convert a lat/lon to final 1200x675 image coordinates."""
    world_x, world_y = map_latlon_to_world_pixel(latitude, longitude, zoom)
    x = (world_x - left) * MAP_OUTPUT_WIDTH / requested_width_px
    y = (world_y - top) * MAP_OUTPUT_HEIGHT / requested_height_px
    return x, y


def map_grid_crossings_between(lat1, lon1, lat2, lon2):
    """
    Return 4-character Maidenhead boundaries crossed by the straight segment
    between two APRS reports, sorted in travel order.
    """
    crossings = []
    eps = 1e-12

    if abs(lat2 - lat1) > eps:
        low = min(lat1, lat2)
        high = max(lat1, lat2)
        first = math.floor(low) + 1
        last = math.floor(high)
        for boundary_lat in range(first, last + 1):
            if not (low < boundary_lat <= high):
                continue
            t = (boundary_lat - lat1) / (lat2 - lat1)
            if 0.0 <= t <= 1.0:
                crossings.append({
                    "t": t,
                    "orientation": "horizontal",
                    "boundary": float(boundary_lat),
                    "latitude": float(boundary_lat),
                    "longitude": lon1 + t * (lon2 - lon1),
                })

    if abs(lon2 - lon1) > eps:
        low = min(lon1, lon2)
        high = max(lon1, lon2)
        start_index = math.floor((low + 180.0) / 2.0) + 1
        end_index = math.floor((high + 180.0) / 2.0)
        for index in range(start_index, end_index + 1):
            boundary_lon = -180.0 + index * 2.0
            if not (low < boundary_lon <= high):
                continue
            t = (boundary_lon - lon1) / (lon2 - lon1)
            if 0.0 <= t <= 1.0:
                crossings.append({
                    "t": t,
                    "orientation": "vertical",
                    "boundary": boundary_lon,
                    "latitude": lat1 + t * (lat2 - lat1),
                    "longitude": boundary_lon,
                })

    crossings.sort(key=lambda item: item["t"])
    return crossings


def map_draw_grid_label(draw, x, y, text):
    """Draw a large Maidenhead label directly on the map."""
    font = map_load_font(96, bold=True)
    bbox = draw.textbbox((0, 0), text, font=font, stroke_width=7)
    width = bbox[2] - bbox[0]
    height = bbox[3] - bbox[1]

    x = map_clamp(x, 24, MAP_OUTPUT_WIDTH - width - 24)
    y = map_clamp(y, 24, MAP_OUTPUT_HEIGHT - height - 24)

    draw.text(
        (x, y),
        text,
        font=font,
        fill=(180, 20, 20, 255),
        stroke_width=7,
        stroke_fill=(255, 255, 255, 245),
    )


def map_draw_crossing_panel(draw, callsign, old_grid, new_grid):
    """Draw a large, simple panel for a reconstructed grid crossing."""
    lines = [
        f"{callsign} GRID CROSSING",
        f"{old_grid} to {new_grid}",
        "Estimated crossing between APRS reports",
    ]
    title_font = map_load_font(58, bold=True)
    grid_font = map_load_font(54, bold=True)
    body_font = map_load_font(38, bold=False)
    fonts = [title_font, grid_font, body_font]

    x = 34
    y = 31
    pad_x = 24
    pad_y = 20
    gap = 16

    widths = []
    heights = []
    for text, font in zip(lines, fonts):
        bbox = draw.textbbox((0, 0), text, font=font)
        widths.append(bbox[2] - bbox[0])
        heights.append(bbox[3] - bbox[1])

    panel_w = max(widths) + pad_x * 2
    panel_h = sum(heights) + pad_y * 2 + gap * (len(lines) - 1)

    draw.rounded_rectangle(
        (12, 11, 12 + panel_w, 11 + panel_h),
        radius=16,
        fill=(255, 255, 255, 242),
        outline=(40, 40, 40),
        width=3,
    )

    cy = y
    for text, font, height in zip(lines, fonts, heights):
        draw.text((x, cy), text, font=font, fill=(15, 15, 15))
        cy += height + gap


def map_draw_estimated_crossing(
    draw,
    crossing,
    zoom,
    left,
    top,
    requested_width_px,
    requested_height_px,
):
    """Draw one actual segment/boundary intersection and its adjacent grids."""
    cx, cy = map_world_to_output_pixel(
        crossing["latitude"],
        crossing["longitude"],
        zoom,
        left,
        top,
        requested_width_px,
        requested_height_px,
    )

    line_fill = (205, 35, 35, 225)
    line_width = 7

    if crossing["orientation"] == "horizontal":
        draw.line(
            (0, cy, MAP_OUTPUT_WIDTH, cy),
            fill=line_fill,
            width=line_width,
        )
        north_grid = maidenhead4(
            crossing["latitude"] + 0.001,
            crossing["longitude"],
        )
        south_grid = maidenhead4(
            crossing["latitude"] - 0.001,
            crossing["longitude"],
        )
        map_draw_grid_label(draw, MAP_OUTPUT_WIDTH * 0.70, cy - 145, north_grid)
        map_draw_grid_label(draw, MAP_OUTPUT_WIDTH * 0.70, cy + 38, south_grid)

    else:
        draw.line(
            (cx, 0, cx, MAP_OUTPUT_HEIGHT),
            fill=line_fill,
            width=line_width,
        )
        west_grid = maidenhead4(
            crossing["latitude"],
            crossing["longitude"] - 0.001,
        )
        east_grid = maidenhead4(
            crossing["latitude"],
            crossing["longitude"] + 0.001,
        )
        map_draw_grid_label(draw, cx - 285, MAP_OUTPUT_HEIGHT * 0.67, west_grid)
        map_draw_grid_label(draw, cx + 55, MAP_OUTPUT_HEIGHT * 0.67, east_grid)

    # This is deliberately not the normal rover marker: no APRS packet was
    # received exactly here. It marks the interpolated crossing estimate.
    radius = 10
    draw.ellipse(
        (cx - radius, cy - radius, cx + radius, cy + radius),
        fill=(255, 255, 255, 245),
        outline=(160, 15, 15, 255),
        width=4,
    )


def map_draw_rover_marker(draw, x, y, callsign, travel_vector=None):
    """Draw the current rover target and place its callsign on the forward side."""
    outer = 16
    draw.ellipse(
        (x - outer, y - outer, x + outer, y + outer),
        fill=(255, 255, 255),
        outline=(20, 20, 20),
        width=4,
    )
    draw.ellipse(
        (x - 9, y - 9, x + 9, y + 9),
        fill=(210, 35, 35),
        outline=(20, 20, 20),
        width=2,
    )
    draw.ellipse((x - 3, y - 3, x + 3, y + 3), fill=(255, 255, 255))

    font = map_load_font(25, bold=True)
    bbox = draw.textbbox((0, 0), callsign, font=font)
    text_w = bbox[2] - bbox[0]
    text_h = bbox[3] - bbox[1]

    pad_x = 8
    pad_y = 7
    box_w = text_w + 2 * pad_x
    box_h = text_h + 2 * pad_y
    gap = 10

    # Put the label on the forward side of the current target, using the
    # direction of the most recent real movement segment. Eight sectors keep
    # the label close to the target while naturally avoiding the route behind.
    dx, dy = travel_vector or (0.0, 0.0)
    magnitude = math.hypot(dx, dy)

    if magnitude >= 1.0:
        angle = math.atan2(dy, dx)
        ux = math.cos(angle)
        uy = math.sin(angle)

        # Snap the movement direction to one of eight compass-like screen
        # directions: E, SE, S, SW, W, NW, N, NE.
        sector = int(round(angle / (math.pi / 4))) % 8
        directions = [
            (1, 0),
            (1, 1),
            (0, 1),
            (-1, 1),
            (-1, 0),
            (-1, -1),
            (0, -1),
            (1, -1),
        ]
        sx, sy = directions[sector]
    else:
        # No meaningful recent movement: retain a simple right-side fallback.
        sx, sy = 1, 0

    if sx < 0:
        box_x = x - outer - gap - box_w
    elif sx > 0:
        box_x = x + outer + gap
    else:
        box_x = x - box_w / 2

    if sy < 0:
        box_y = y - outer - gap - box_h
    elif sy > 0:
        box_y = y + outer + gap
    else:
        box_y = y - box_h / 2

    # Keep the label fully on the image if the rover is near an edge.
    box_x = map_clamp(box_x, 8, MAP_OUTPUT_WIDTH - box_w - 8)
    box_y = map_clamp(box_y, 8, MAP_OUTPUT_HEIGHT - box_h - 8)

    draw.rounded_rectangle(
        (box_x, box_y, box_x + box_w, box_y + box_h),
        radius=8,
        fill=(255, 255, 255, 235),
        outline=(40, 40, 40),
        width=2,
    )

    # Account for the font's top bearing so the visible callsign is centered.
    text_x = box_x + pad_x - bbox[0]
    text_y = box_y + (box_h - text_h) / 2 - bbox[1]
    draw.text((text_x, text_y), callsign, font=font, fill=(15, 15, 15))

def map_draw_info_panel(
    draw,
    callsign,
    grid6,
    latitude,
    longitude,
    location,
    weather,
):
    city = location.get("city")
    county = location.get("county")
    state = location.get("state")

    lines = [callsign]

    if city and state:
        lines.append(f"{city}, {state}")
    elif city:
        lines.append(city)
    elif state:
        lines.append(state)

    if county:
        county_text = county if "county" in county.lower() else f"{county} County"
        lines.append(county_text)

    lines.append(f"{latitude:.5f}, {longitude:.5f}")
    lines.append(f"Grid: {grid6}")

    if weather:
        parts = []
        temp = weather.get("temperature_f")
        description = weather.get("description")
        wind_mph = weather.get("wind_mph")
        wind_direction = weather.get("wind_direction")

        if temp is not None:
            parts.append(f"{temp}°F")
        if description:
            parts.append(str(description))
        if wind_mph is not None:
            if wind_mph <= 1:
                parts.append("Wind calm")
            elif wind_direction:
                parts.append(f"Wind {wind_direction} {wind_mph} mph")
            else:
                parts.append(f"Wind {wind_mph} mph")

        if parts:
            lines.append(" | ".join(parts))

    # Intentionally large for readability after X timeline resizing.
    title_font = map_load_font(54, bold=True)
    body_font = map_load_font(42, bold=False)

    x = 34
    y = 31
    pad_x = 24
    pad_y = 22
    gap = 12

    max_width = 0
    heights = []
    for index, line in enumerate(lines):
        font = title_font if index == 0 else body_font
        bbox = draw.textbbox((0, 0), line, font=font)
        max_width = max(max_width, bbox[2] - bbox[0])
        heights.append((bbox[3] - bbox[1], font))

    total_height = (
        pad_y * 2
        + sum(height for height, _ in heights)
        + gap * (len(lines) - 1)
    )

    draw.rounded_rectangle(
        (
            x - pad_x,
            y - pad_y,
            x + max_width + pad_x,
            y + total_height - pad_y,
        ),
        radius=12,
        fill=(255, 255, 255, 238),
        outline=(40, 40, 40),
        width=2,
    )

    cy = y
    for index, line in enumerate(lines):
        height, font = heights[index]
        draw.text((x, cy), line, font=font, fill=(15, 15, 15))
        cy += height + gap


def generate_rover_map(
    latitude,
    longitude,
    callsign,
    grid6,
    grid_transition=None,
    previous_latitude=None,
    previous_longitude=None,
):
    """
    Generate a 1200x675, approximately 1 km wide OSM map.

    Normal location events remain centered on the current APRS packet.
    Grid-change events are centered on the estimated boundary crossing between
    the previous and current APRS reports. Map failure never blocks X text.
    """
    try:
        crossing = None
        map_latitude = latitude
        map_longitude = longitude

        if (
            grid_transition
            and previous_latitude is not None
            and previous_longitude is not None
        ):
            crossings = map_grid_crossings_between(
                float(previous_latitude),
                float(previous_longitude),
                float(latitude),
                float(longitude),
            )
            if crossings:
                # If a sparse APRS interval crossed more than one grid boundary,
                # the last intersection is the actual entry into the current
                # reported grid. Do not falsely claim an exact grid corner.
                crossing = crossings[-1]
                map_latitude = crossing["latitude"]
                map_longitude = crossing["longitude"]
                print(
                    "MAP GRID CROSSING: "
                    f"{grid_transition[0]} -> {grid_transition[1]} "
                    f"estimated at {map_latitude:.5f}, {map_longitude:.5f}"
                )
                if len(crossings) > 1:
                    print(
                        "MAP GRID CROSSING: APRS segment crossed "
                        f"{len(crossings)} boundaries; showing the final "
                        "boundary entering the current grid."
                    )
            else:
                print(
                    "MAP GRID CROSSING: Could not calculate a segment "
                    "intersection; falling back to current APRS position."
                )

        zoom = map_choose_zoom(map_latitude)
        center_x, center_y = map_latlon_to_world_pixel(
            map_latitude,
            map_longitude,
            zoom,
        )

        source_mpp = map_meters_per_pixel(map_latitude, zoom)
        requested_width_px = (MAP_WIDTH_KM * 1000.0) / source_mpp
        requested_height_px = (
            requested_width_px * MAP_OUTPUT_HEIGHT / MAP_OUTPUT_WIDTH
        )

        left = center_x - requested_width_px / 2
        top = center_y - requested_height_px / 2
        right = center_x + requested_width_px / 2
        bottom = center_y + requested_height_px / 2

        tile_x0 = math.floor(left / MAP_TILE_SIZE)
        tile_y0 = math.floor(top / MAP_TILE_SIZE)
        tile_x1 = math.floor((right - 1) / MAP_TILE_SIZE)
        tile_y1 = math.floor((bottom - 1) / MAP_TILE_SIZE)

        mosaic = Image.new(
            "RGB",
            (
                (tile_x1 - tile_x0 + 1) * MAP_TILE_SIZE,
                (tile_y1 - tile_y0 + 1) * MAP_TILE_SIZE,
            ),
        )

        session = requests.Session()
        session.headers.update({"User-Agent": MAP_USER_AGENT})

        # Normal maps describe the current APRS report. Crossing maps use a
        # crossing-specific panel and therefore do not need current weather or
        # reverse-geocoded current-position text on the image.
        if crossing is None:
            location = map_reverse_location(session, latitude, longitude)
            weather = map_current_weather(latitude, longitude)
        else:
            location = {}
            weather = None

        for tx in range(tile_x0, tile_x1 + 1):
            for ty in range(tile_y0, tile_y1 + 1):
                tile = map_fetch_tile(session, zoom, tx, ty)
                mosaic.paste(
                    tile,
                    (
                        (tx - tile_x0) * MAP_TILE_SIZE,
                        (ty - tile_y0) * MAP_TILE_SIZE,
                    ),
                )

        crop_left = left - tile_x0 * MAP_TILE_SIZE
        crop_top = top - tile_y0 * MAP_TILE_SIZE

        rendered = mosaic.crop(
            (
                round(crop_left),
                round(crop_top),
                round(crop_left + requested_width_px),
                round(crop_top + requested_height_px),
            )
        )
        rendered = rendered.resize(
            (MAP_OUTPUT_WIDTH, MAP_OUTPUT_HEIGHT),
            Image.Resampling.LANCZOS,
        ).convert("RGBA")

        overlay = Image.new("RGBA", rendered.size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(overlay)

        if crossing is not None:
            old_grid, new_grid = grid_transition
            map_draw_estimated_crossing(
                draw,
                crossing,
                zoom,
                left,
                top,
                requested_width_px,
                requested_height_px,
            )
            map_draw_crossing_panel(draw, callsign, old_grid, new_grid)
        else:
            map_draw_rover_marker(
                draw,
                MAP_OUTPUT_WIDTH // 2,
                MAP_OUTPUT_HEIGHT // 2,
                callsign,
            )
            map_draw_info_panel(
                draw,
                callsign,
                grid6,
                latitude,
                longitude,
                location,
                weather,
            )

        attribution = "© OpenStreetMap contributors"
        attribution_font = map_load_font(18)
        bbox = draw.textbbox((0, 0), attribution, font=attribution_font)
        aw = bbox[2] - bbox[0]
        ah = bbox[3] - bbox[1]
        ax = MAP_OUTPUT_WIDTH - aw - 30
        ay = MAP_OUTPUT_HEIGHT - ah - 25
        draw.rounded_rectangle(
            (
                ax - 10,
                ay - 7,
                MAP_OUTPUT_WIDTH - 14,
                MAP_OUTPUT_HEIGHT - 9,
            ),
            radius=5,
            fill=(255, 255, 255, 225),
        )
        draw.text(
            (ax, ay),
            attribution,
            font=attribution_font,
            fill=(25, 25, 25),
        )

        rendered = Image.alpha_composite(rendered, overlay).convert("RGB")
        MAP_OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
        rendered.save(MAP_OUTPUT_FILE, "PNG")

        print(
            f"MAP: Saved {MAP_OUTPUT_FILE} "
            f"({MAP_OUTPUT_WIDTH}x{MAP_OUTPUT_HEIGHT})"
        )
        return MAP_OUTPUT_FILE

    except Exception as exc:
        print(f"MAP: generation failed: {exc}")
        return None


def map_world_pixel_to_latlon(x, y, zoom):
    """Inverse Web Mercator used by the automatically framed track map."""
    scale = MAP_TILE_SIZE * (2 ** zoom)
    longitude = (x / scale) * 360.0 - 180.0
    n = math.pi - (2.0 * math.pi * y / scale)
    latitude = math.degrees(math.atan(math.sinh(n)))
    return latitude, longitude


def load_recent_track_from_database(callsign, window_minutes=TRACK_MAP_WINDOW_MINUTES):
    """Load a chronological APRS track ending at the newest stored report."""
    if not DATABASE_FILE.exists():
        raise RuntimeError(f"Database not found: {DATABASE_FILE}")

    db = sqlite3.connect(DATABASE_FILE)
    db.row_factory = sqlite3.Row
    try:
        rover = db.execute(
            """
            SELECT id, callsign, aprs_callsign
            FROM rovers
            WHERE callsign = ? COLLATE NOCASE
               OR aprs_callsign = ? COLLATE NOCASE
            ORDER BY id
            LIMIT 1
            """,
            (callsign, callsign),
        ).fetchone()

        if rover is None:
            raise RuntimeError(f"No rover record found for {callsign}")

        newest = db.execute(
            "SELECT MAX(aprs_timestamp) FROM positions WHERE rover_id = ?",
            (rover["id"],),
        ).fetchone()[0]

        if newest is None:
            return rover, []

        cutoff = int(newest) - int(window_minutes * 60)
        rows = db.execute(
            """
            SELECT id, aprs_timestamp, latitude, longitude,
                   speed_kmh, course_deg, altitude_m, grid4, grid6, recorded_at
            FROM positions
            WHERE rover_id = ?
              AND aprs_timestamp >= ?
              AND aprs_timestamp <= ?
            ORDER BY aprs_timestamp ASC, id ASC
            """,
            (rover["id"], cutoff, int(newest)),
        ).fetchall()
        return rover, rows
    finally:
        db.close()


def calculate_track_frame(rows, padding_fraction=0.14):
    """Frame a deterministic four-grid neighborhood with direction-aware selection."""
    if not rows:
        raise RuntimeError("Cannot frame an empty APRS track.")

    layout = track_relevant_grid_layout(rows)
    if layout and layout.get("mode") == "cross":
        frame_grids = [
            layout["labels"]["nw"], layout["labels"]["ne"],
            layout["labels"]["sw"], layout["labels"]["se"],
        ]
        frame_bounds = [map_grid4_bounds(g) for g in frame_grids]
        frame_bounds = [b for b in frame_bounds if b is not None]
        west = min(b[0] for b in frame_bounds)
        south = min(b[1] for b in frame_bounds)
        east = max(b[2] for b in frame_bounds)
        north = max(b[3] for b in frame_bounds)

        zoom = 8
        nw_x, nw_y = map_latlon_to_world_pixel(north, west, zoom)
        se_x, se_y = map_latlon_to_world_pixel(south, east, zoom)
        width = abs(se_x - nw_x) * 1.16
        height = abs(se_y - nw_y) * 1.16
        target_ratio = MAP_OUTPUT_WIDTH / MAP_OUTPUT_HEIGHT
        if width / height < target_ratio:
            width = height * target_ratio
        else:
            height = width / target_ratio
        cx, cy = map_latlon_to_world_pixel((south+north)/2, (west+east)/2, zoom)
        return zoom, cx-width/2, cy-height/2, width, height

    # Defensive fallback: ordinary route framing.
    lats = [float(r["latitude"]) for r in rows]
    lons = [float(r["longitude"]) for r in rows]
    center_lat = (min(lats) + max(lats)) / 2
    center_lon = (min(lons) + max(lons)) / 2
    for zoom in range(19, -1, -1):
        pts = [map_latlon_to_world_pixel(float(r["latitude"]), float(r["longitude"]), zoom) for r in rows]
        xs = [p[0] for p in pts]; ys = [p[1] for p in pts]
        px = max((max(xs)-min(xs))*(1+2*padding_fraction), 320)
        py = max((max(ys)-min(ys))*(1+2*padding_fraction), 180)
        scale = min(MAP_OUTPUT_WIDTH/px, MAP_OUTPUT_HEIGHT/py)
        if scale < 1: continue
        cx=(min(xs)+max(xs))/2; cy=(min(ys)+max(ys))/2
        width=MAP_OUTPUT_WIDTH/scale; height=MAP_OUTPUT_HEIGHT/scale
        return zoom, cx-width/2, cy-height/2, width, height
    raise RuntimeError("Could not calculate track frame.")

def track_output_pixel(latitude, longitude, zoom, left, top, width_px, height_px):
    world_x, world_y = map_latlon_to_world_pixel(latitude, longitude, zoom)
    x = (world_x - left) * MAP_OUTPUT_WIDTH / width_px
    y = (world_y - top) * MAP_OUTPUT_HEIGHT / height_px
    return x, y


def track_relevant_grid_layout(rows):
    """Always choose four labeled Maidenhead cells using route direction intelligently."""
    points = [(float(r["latitude"]), float(r["longitude"])) for r in rows
              if r["latitude"] is not None and r["longitude"] is not None]
    if not points:
        return None

    route_lat = sum(p[0] for p in points) / len(points)
    route_lon = sum(p[1] for p in points) / len(points)
    latest_lat, latest_lon = points[-1]

    # Find the most recent meaningful geographic movement.
    prev_lat, prev_lon = points[0]
    for lat, lon in reversed(points[:-1]):
        if abs(lat-latest_lat) > 1e-5 or abs(lon-latest_lon) > 1e-5:
            prev_lat, prev_lon = lat, lon
            break
    dlat = latest_lat - prev_lat
    dlon = latest_lon - prev_lon

    # Start with the nearest vertical and horizontal 4-char boundaries.
    anchor = maidenhead4(latest_lat, latest_lon)
    west, south, east, north = map_grid4_bounds(anchor)
    vertical = min((west, east), key=lambda v: abs(v-latest_lon))
    horizontal = min((south, north), key=lambda v: abs(v-latest_lat))

    # If the route actually crossed a boundary during the displayed track, prefer
    # that event boundary. The orthogonal boundary is selected by travel direction;
    # when nearly parallel, current position within the cell breaks the tie.
    crossings=[]
    for (lat1,lon1),(lat2,lon2) in zip(points, points[1:]):
        crossings.extend(map_grid_crossings_between(lat1,lon1,lat2,lon2))
    if crossings:
        last = crossings[-1]
        if last["orientation"] == "vertical":
            vertical = last["boundary"]
            if abs(dlat) > 1e-5:
                # Northbound: use the boundary north of the rover; southbound: south.
                horizontal = math.ceil(latest_lat) if dlat > 0 else math.floor(latest_lat)
            else:
                horizontal = north if (latest_lat-south) >= (north-latest_lat) else south
        else:
            horizontal = last["boundary"]
            if abs(dlon) > 1e-5:
                # Eastbound: use boundary east of rover; westbound: west.
                vertical = east if dlon > 0 else west
            else:
                vertical = east if (latest_lon-west) >= (east-latest_lon) else west

    eps=1e-6
    labels={
        "nw": maidenhead4(horizontal+eps, vertical-eps),
        "ne": maidenhead4(horizontal+eps, vertical+eps),
        "sw": maidenhead4(horizontal-eps, vertical-eps),
        "se": maidenhead4(horizontal-eps, vertical+eps),
    }
    return {"mode":"cross", "boundary_lat":horizontal, "boundary_lon":vertical,
            "labels":labels, "route_center":(route_lat,route_lon),
            "travel_delta":(dlat,dlon)}

def _track_draw_dashed_line(draw, start, end, fill, width=4, dash=14, gap=10):
    x1, y1 = start
    x2, y2 = end
    length = math.hypot(x2 - x1, y2 - y1)
    if length <= 0:
        return
    dx = (x2 - x1) / length
    dy = (y2 - y1) / length
    distance = 0.0
    while distance < length:
        stop = min(distance + dash, length)
        draw.line(
            (
                x1 + dx * distance, y1 + dy * distance,
                x1 + dx * stop, y1 + dy * stop,
            ),
            fill=fill,
            width=width,
        )
        distance += dash + gap


def _track_label(draw, text, center_x, center_y, font):
    bbox = draw.textbbox((0, 0), text, font=font, stroke_width=3)
    width = bbox[2] - bbox[0]
    height = bbox[3] - bbox[1]
    margin = 12
    x = map_clamp(center_x - width / 2, margin, MAP_OUTPUT_WIDTH - width - margin)
    y = map_clamp(center_y - height / 2, margin, MAP_OUTPUT_HEIGHT - height - margin)
    draw.text(
        (x, y),
        text,
        font=font,
        fill=(82, 46, 128, 190),
        stroke_width=4,
        stroke_fill=(255, 255, 255, 210),
    )


def draw_track_grid_boundaries(draw, zoom, left, top, width_px, height_px, rows=None):
    """Draw the four selected Maidenhead cells and centered labels."""
    if not rows:
        return

    layout = track_relevant_grid_layout(rows)
    if not layout:
        return

    scale = MAP_OUTPUT_WIDTH / 1200.0
    font = map_load_font(round(58 * scale), bold=True)
    line = (112, 70, 155, 185)
    halo = (255, 255, 255, 185)

    def px(lat, lon):
        return track_output_pixel(
            lat, lon, zoom, left, top, width_px, height_px
        )

    def dashed_horizontal(y):
        x = 0
        while x < MAP_OUTPUT_WIDTH:
            x2 = min(MAP_OUTPUT_WIDTH, x + round(18 * scale))
            draw.line((x, y, x2, y), fill=halo, width=round(5 * scale))
            draw.line((x, y, x2, y), fill=line, width=round(3 * scale))
            x += round(28 * scale)

    def dashed_vertical(x):
        y = 0
        while y < MAP_OUTPUT_HEIGHT:
            y2 = min(MAP_OUTPUT_HEIGHT, y + round(18 * scale))
            draw.line((x, y, x, y2), fill=halo, width=round(5 * scale))
            draw.line((x, y, x, y2), fill=line, width=round(3 * scale))
            y += round(28 * scale)

    def draw_grid_outline(grid):
        bounds = map_grid4_bounds(grid)
        if bounds is None:
            return
        west, south, east, north = bounds
        left_x, top_y = px(north, west)
        right_x, bottom_y = px(south, east)
        x0, x1 = sorted((left_x, right_x))
        y0, y1 = sorted((top_y, bottom_y))
        dashed_horizontal(y0)
        dashed_horizontal(y1)
        dashed_vertical(x0)
        dashed_vertical(x1)

    def draw_centered_grid_label(grid):
        bounds = map_grid4_bounds(grid)
        if bounds is None:
            return
        west, south, east, north = bounds
        cx, cy = px((south + north) / 2, (west + east) / 2)
        _track_label(draw, grid, cx, cy, font)

    mode = layout["mode"]

    if mode == "single":
        grid = layout["grids"][0]
        draw_grid_outline(grid)
        draw_centered_grid_label(grid)
        return

    if mode in {"horizontal", "vertical"}:
        grids = [layout["first_grid"], layout["second_grid"]]
        # Drawing each full outline intentionally redraws the shared boundary;
        # the identical dashed geometry remains visually one clean divider.
        for grid in grids:
            draw_grid_outline(grid)
        for grid in grids:
            draw_centered_grid_label(grid)
        return

    if mode == "cross":
        grids = [
            layout["labels"]["nw"],
            layout["labels"]["ne"],
            layout["labels"]["sw"],
            layout["labels"]["se"],
        ]
        for grid in grids:
            draw_grid_outline(grid)
        for grid in grids:
            draw_centered_grid_label(grid)

def draw_recent_aprs_track(draw, rows, callsign, zoom, left, top, width_px, height_px):
    """Draw stored APRS reports, route, start point, and current rover marker."""
    points = [
        track_output_pixel(
            float(row["latitude"]), float(row["longitude"]),
            zoom, left, top, width_px, height_px,
        )
        for row in rows
    ]

    if len(points) >= 2:
        scale = MAP_OUTPUT_WIDTH / 1200.0
        draw.line(points, fill=(255, 255, 255, 235), width=round(11 * scale), joint="curve")
        draw.line(points, fill=(25, 145, 205, 255), width=round(6 * scale), joint="curve")

    for x, y in points:
        draw.ellipse(
            (x - 6, y - 6, x + 6, y + 6),
            fill=(25, 145, 205, 255),
            outline=(255, 255, 255, 255),
            width=3,
        )

    if not points:
        return

    start_x, start_y = points[0]
    draw.ellipse(
        (start_x - 10, start_y - 10, start_x + 10, start_y + 10),
        fill=(255, 255, 255, 255),
        outline=(20, 20, 20, 255),
        width=3,
    )
    draw.ellipse(
        (start_x - 5, start_y - 5, start_x + 5, start_y + 5),
        fill=(25, 145, 205, 255),
    )

    latest_x, latest_y = points[-1]

    # Determine the rover's most recent meaningful direction of travel from
    # stored APRS positions. Walk backward past duplicate/nearly identical
    # reports until a visible movement segment is found.
    travel_vector = None
    for previous_x, previous_y in reversed(points[:-1]):
        dx = latest_x - previous_x
        dy = latest_y - previous_y
        if math.hypot(dx, dy) >= 4.0:
            travel_vector = (dx, dy)
            break

    map_draw_rover_marker(
        draw, latest_x, latest_y, callsign, travel_vector=travel_vector
    )


def generate_recent_track_map(callsign, window_minutes=TRACK_MAP_WINDOW_MINUTES):
    """
    Build the proven map_test.py-style wide-area APRS history map.

    This is deterministic: SQLite supplies the positions, Maidenhead geometry
    supplies the grid lines/labels, and OpenStreetMap supplies the base map.
    """
    rover, rows = load_recent_track_from_database(callsign, window_minutes)
    if not rows:
        raise RuntimeError(f"No stored positions found for {callsign}.")

    zoom, left, top, width_px, height_px = calculate_track_frame(rows)

    # Fetch the same geographic frame one OSM zoom level higher. This gives
    # the 1200x675 output roughly 2x the source raster resolution in each
    # dimension, improving base-map text/road sharpness and allowing OSM's
    # next zoom-level labeling while leaving the geographic composition intact.
    render_zoom = min(19, zoom + TRACK_MAP_TILE_ZOOM_BOOST)
    if render_zoom != zoom:
        factor = 2 ** (render_zoom - zoom)
        left *= factor
        top *= factor
        width_px *= factor
        height_px *= factor
        zoom = render_zoom

    right = left + width_px
    bottom = top + height_px

    tile_x0 = math.floor(left / MAP_TILE_SIZE)
    tile_y0 = math.floor(top / MAP_TILE_SIZE)
    tile_x1 = math.floor((right - 1) / MAP_TILE_SIZE)
    tile_y1 = math.floor((bottom - 1) / MAP_TILE_SIZE)

    mosaic = Image.new(
        "RGB",
        (
            (tile_x1 - tile_x0 + 1) * MAP_TILE_SIZE,
            (tile_y1 - tile_y0 + 1) * MAP_TILE_SIZE,
        ),
    )

    session = requests.Session()
    session.headers.update({"User-Agent": MAP_USER_AGENT})

    for tile_x in range(tile_x0, tile_x1 + 1):
        for tile_y in range(tile_y0, tile_y1 + 1):
            tile = map_fetch_tile(session, zoom, tile_x, tile_y)
            mosaic.paste(
                tile,
                (
                    (tile_x - tile_x0) * MAP_TILE_SIZE,
                    (tile_y - tile_y0) * MAP_TILE_SIZE,
                ),
            )

    crop_left = left - tile_x0 * MAP_TILE_SIZE
    crop_top = top - tile_y0 * MAP_TILE_SIZE
    rendered = mosaic.crop(
        (
            round(crop_left),
            round(crop_top),
            round(crop_left + width_px),
            round(crop_top + height_px),
        )
    )
    rendered = rendered.resize(
        (MAP_OUTPUT_WIDTH, MAP_OUTPUT_HEIGHT),
        Image.Resampling.LANCZOS,
    ).convert("RGBA")

    overlay = Image.new("RGBA", rendered.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    draw_track_grid_boundaries(
        draw, zoom, left, top, width_px, height_px, rows=rows
    )
    draw_recent_aprs_track(
        draw, rows, callsign, zoom, left, top, width_px, height_px
    )

    attribution = "© OpenStreetMap contributors"
    font = map_load_font(round(18 * (MAP_OUTPUT_WIDTH / 1200.0)))
    bbox = draw.textbbox((0, 0), attribution, font=font)
    aw = bbox[2] - bbox[0]
    ah = bbox[3] - bbox[1]
    ax = MAP_OUTPUT_WIDTH - aw - 30
    ay = MAP_OUTPUT_HEIGHT - ah - 25
    draw.rounded_rectangle(
        (ax - 10, ay - 7, MAP_OUTPUT_WIDTH - 14, MAP_OUTPUT_HEIGHT - 9),
        radius=5,
        fill=(255, 255, 255, 225),
    )
    draw.text((ax, ay), attribution, font=font, fill=(20, 20, 20))

    result = Image.alpha_composite(rendered, overlay).convert("RGB")
    TRACK_MAP_OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    result.save(TRACK_MAP_OUTPUT_FILE, "PNG", optimize=True)

    first_ts = int(rows[0]["aprs_timestamp"])
    latest_ts = int(rows[-1]["aprs_timestamp"])
    print(
        "TRACK MAP: Saved "
        f"{TRACK_MAP_OUTPUT_FILE} with {len(rows)} APRS reports "
        f"from {utc_text(first_ts)} through {utc_text(latest_ts)}."
    )
    return TRACK_MAP_OUTPUT_FILE
