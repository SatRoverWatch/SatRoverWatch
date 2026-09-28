"""SatRoverWatch satellite pass prediction."""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

import requests
from skyfield.api import EarthSatellite, load, wgs84

PROJECT_DIR = Path(__file__).resolve().parent

PASS_SATELLITES = {
    "AO-73": 39444,
    "AO-7": 7530,
    "FO-29": 24278,
    "ISS": 25544,
    "JO-97": 43803,
    "RS-44": 44909,
    "SO-50": 27607,
}
PASS_MIN_MAX_ELEVATION_DEG = 10.0
PASS_LOOKBACK_MINUTES = 90
PASS_SEARCH_FORWARD_HOURS = 36
PASS_RESULT_LIMIT = 5
PASS_PUBLIC_LIMIT = 5

# Reuse a successful GP cache for up to 24 hours.
CELESTRAK_CACHE_HOURS = 24
CELESTRAK_AMATEUR_GP_URL = (
    "https://celestrak.org/NORAD/elements/gp.php?GROUP=amateur&FORMAT=JSON"
)

def load_celestrak_amateur_gp():
    """Return CelesTrak amateur GP JSON with a small on-disk cache."""
    cache_dir = PROJECT_DIR / "cache"
    cache_file = cache_dir / "celestrak_amateur_gp.json"
    cache_dir.mkdir(parents=True, exist_ok=True)

    cache_fresh = False
    if cache_file.exists():
        try:
            age_seconds = (
                datetime.now(timezone.utc).timestamp()
                - cache_file.stat().st_mtime
            )
            cache_fresh = age_seconds < CELESTRAK_CACHE_HOURS * 3600
        except OSError:
            cache_fresh = False

    if cache_fresh:
        try:
            with cache_file.open("r", encoding="utf-8") as file:
                return json.load(file), "CelesTrak cache"
        except (OSError, ValueError):
            pass

    headers = {
        "User-Agent": "SatRoverWatch/0.5 (https://x.com/SatRoverWatch)"
    }

    try:
        response = requests.get(
            CELESTRAK_AMATEUR_GP_URL,
            headers=headers,
            timeout=20,
        )
        response.raise_for_status()
        records = response.json()
        if not isinstance(records, list):
            raise ValueError("CelesTrak response was not a JSON list")

        try:
            with cache_file.open("w", encoding="utf-8") as file:
                json.dump(records, file)
        except OSError as exc:
            print(f"PASS CHECK: Could not update CelesTrak cache: {exc}")

        return records, "CelesTrak downloaded"

    except (requests.RequestException, ValueError) as exc:
        print(f"PASS CHECK: CelesTrak download failed: {exc}")

        # A stale cache is better than losing pass context entirely. It is used
        # only as supporting evidence for a POSSIBLE activation, never proof.
        if cache_file.exists():
            try:
                with cache_file.open("r", encoding="utf-8") as file:
                    return json.load(file), "stale CelesTrak cache"
            except (OSError, ValueError):
                pass

        return None, None


def maidenhead6(latitude, longitude):
    """Return the conventional six-character Maidenhead locator."""
    lon = float(longitude) + 180.0
    lat = float(latitude) + 90.0
    field_lon, field_lat = int(lon // 20), int(lat // 10)
    lon, lat = lon - field_lon * 20, lat - field_lat * 10
    square_lon, square_lat = int(lon // 2), int(lat // 1)
    lon, lat = lon - square_lon * 2, lat - square_lat
    subsquare_lon = min(max(int(lon / (2 / 24)), 0), 23)
    subsquare_lat = min(max(int(lat / (1 / 24)), 0), 23)
    return (
        f"{chr(ord('A') + field_lon)}{chr(ord('A') + field_lat)}"
        f"{square_lon}{square_lat}"
        f"{chr(ord('a') + subsquare_lon)}{chr(ord('a') + subsquare_lat)}"
    )


def satmatch_pass_url(satellite_name, grid6, aos):
    """Return the SatMatch URL for one specific satellite pass."""
    satellite = quote(str(satellite_name), safe="-")
    locator = quote(str(grid6), safe="")
    timestamp = aos.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return (
        f"https://www.satmatch.com/satellite/{satellite}/"
        f"obs1/{locator}/pass/{timestamp}"
    )


def get_qualifying_passes(latitude, longitude, calculation_time=None):
    """Return in-progress and upcoming qualifying passes sorted by AOS."""
    if calculation_time is None:
        calculation_time = datetime.now(timezone.utc)
    elif calculation_time.tzinfo is None:
        calculation_time = calculation_time.replace(tzinfo=timezone.utc)
    else:
        calculation_time = calculation_time.astimezone(timezone.utc)

    records, gp_source = load_celestrak_amateur_gp()
    if records is None:
        return None, None

    by_catnr = {}
    for record in records:
        try:
            by_catnr[int(record.get("NORAD_CAT_ID"))] = record
        except (TypeError, ValueError):
            continue

    ts = load.timescale()
    observer = wgs84.latlon(latitude, longitude)
    observer_grid6 = maidenhead6(latitude, longitude)
    search_start = calculation_time - timedelta(minutes=PASS_LOOKBACK_MINUTES)
    search_end = calculation_time + timedelta(hours=PASS_SEARCH_FORWARD_HOURS)
    t0 = ts.from_datetime(search_start)
    t1 = ts.from_datetime(search_end)
    now_t = ts.from_datetime(calculation_time)

    passes = []

    for satellite_name, catnr in PASS_SATELLITES.items():
        record = by_catnr.get(catnr)
        if record is None:
            print(
                f"PASS CHECK: {satellite_name} ({catnr}) not found "
                "in CelesTrak amateur GP data."
            )
            continue

        try:
            satellite = EarthSatellite.from_omm(ts, record)
            times, events = satellite.find_events(
                observer,
                t0,
                t1,
                altitude_degrees=0.0,
            )
        except Exception as exc:
            print(f"PASS CHECK: Could not calculate {satellite_name}: {exc}")
            continue

        current_rise = None
        current_culmination = None

        for event_time, event_code in zip(times, events):
            event_dt = event_time.utc_datetime()
            if event_dt.tzinfo is None:
                event_dt = event_dt.replace(tzinfo=timezone.utc)
            else:
                event_dt = event_dt.astimezone(timezone.utc)

            if event_code == 0:
                current_rise = (event_time, event_dt)
                current_culmination = None

            elif event_code == 1 and current_rise is not None:
                current_culmination = (event_time, event_dt)

            elif (
                event_code == 2
                and current_rise is not None
                and current_culmination is not None
            ):
                set_time = event_time
                set_dt = event_dt
                rise_time, rise_dt = current_rise
                culmination_time, culmination_dt = current_culmination

                try:
                    max_elevation = float(
                        (satellite - observer)
                        .at(culmination_time)
                        .altaz()[0]
                        .degrees
                    )
                except Exception:
                    current_rise = None
                    current_culmination = None
                    continue

                # Ignore passes that have already ended and passes whose peak
                # never reaches our 10-degree usefulness threshold.
                if (
                    set_dt > calculation_time
                    and max_elevation >= PASS_MIN_MAX_ELEVATION_DEG
                ):
                    in_progress = rise_dt <= calculation_time < set_dt
                    pass_info = {
                        "satellite": satellite_name,
                        "aos": rise_dt,
                        "max_time": culmination_dt,
                        "los": set_dt,
                        "max_elevation": max_elevation,
                        "in_progress": in_progress,
                        "grid6": observer_grid6,
                        "satmatch_url": satmatch_pass_url(
                            satellite_name, observer_grid6, rise_dt
                        ),
                    }

                    if in_progress:
                        try:
                            current_elevation = float(
                                (satellite - observer)
                                .at(now_t)
                                .altaz()[0]
                                .degrees
                            )
                        except Exception:
                            current_elevation = None

                        pass_info["current_elevation"] = current_elevation
                        pass_info["minutes_to_los"] = max(
                            0.0,
                            (set_dt - calculation_time).total_seconds() / 60.0,
                        )
                    else:
                        pass_info["minutes_to_aos"] = max(
                            0.0,
                            (rise_dt - calculation_time).total_seconds() / 60.0,
                        )

                    passes.append(pass_info)

                current_rise = None
                current_culmination = None

    passes.sort(key=lambda item: item["aos"])
    return passes[:PASS_RESULT_LIMIT], gp_source


def print_pass_context(passes, gp_source):
    """Write useful pass diagnostics to the watcher log."""
    print()
    print("SATELLITE PASS CHECK:")
    if passes is None:
        print("Pass data unavailable; possible-activation logic continues without it.")
        return

    print(f"GP data source: {gp_source}")
    print(f"Minimum maximum elevation: {PASS_MIN_MAX_ELEVATION_DEG:.0f} degrees")

    if not passes:
        print("No qualifying passes found in the search window.")
        return

    for index, item in enumerate(passes, start=1):
        aos = item["aos"].strftime("%H:%M:%SZ")
        max_time = item["max_time"].strftime("%H:%M:%SZ")
        los = item["los"].strftime("%H:%M:%SZ")
        print(
            f"{index}. {item['satellite']}  MAX {item['max_elevation']:.1f} deg  "
            f"AOS {aos}  MAX {max_time}  LOS {los}"
        )
        print(f"   SatMatch: {item['satmatch_url']}")
        if item["in_progress"]:
            current_elevation = item.get("current_elevation")
            elevation_text = (
                f"{current_elevation:.1f} deg"
                if current_elevation is not None
                else "unavailable"
            )
            print(
                f"   IN PROGRESS - current elevation {elevation_text}; "
                f"LOS in {item['minutes_to_los']:.1f} minutes"
            )
        else:
            print(f"   AOS in {item['minutes_to_aos']:.1f} minutes")


def public_pass_lines(passes, calculation_time=None):
    """Return compact lines for the next five qualifying passes."""
    if not passes:
        return []

    if calculation_time is None:
        calculation_time = datetime.now(timezone.utc)
    elif calculation_time.tzinfo is None:
        calculation_time = calculation_time.replace(tzinfo=timezone.utc)
    else:
        calculation_time = calculation_time.astimezone(timezone.utc)

    lines = []
    for item in passes:
        if len(lines) >= PASS_PUBLIC_LIMIT:
            break

        if item["in_progress"]:
            los = item["los"].strftime("%H:%MZ")
            lines.append(
                f"{item['satellite']} NOW, LOS {los}, MAX {item['max_elevation']:.0f}°"
            )
            continue

        aos = item["aos"].strftime("%H:%MZ")
        lines.append(
            f"{item['satellite']} {aos}, MAX {item['max_elevation']:.0f}°"
        )

    return lines


