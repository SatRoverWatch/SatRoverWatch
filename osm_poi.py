"""OpenStreetMap point-of-interest (POI) lookup for SatRoverWatch.

This module provides two related capabilities:

1. Retrieve nearby mapped features through the public Overpass API.
2. Use those features to suggest a generic context for a stationary rover.

The classifications are proximity-based clues, not proof of what a rover is
actually doing. Public-facing descriptions intentionally avoid business names.

An unsuccessful lookup returns None; a successful lookup with no matching POIs
returns an empty list. Callers should preserve that distinction.
"""

import math
import time
import requests

# Maximum lookup radius and distance threshold for retained POIs (meters).
OSM_NEARBY_RADIUS_METERS = 250
# A tighter proximity threshold for common fuel, food, and lodging stops.
ORDINARY_STOP_RADIUS_METERS = 100
# Fuel/convenience stops use the same threshold as other ordinary stops.
ROVER_DOG_RADIUS_METERS = ORDINARY_STOP_RADIUS_METERS
# Default lodging search threshold. See nearby_lodging_feature() caveat.
LODGING_RADIUS_METERS = 150

def distance_meters(lat1, lon1, lat2, lon2):
    """Calculate the great-circle distance between two latitude/longitude pairs.

    Uses the haversine formula with a mean Earth radius of 6,371,008.8 meters.

    Args:
        lat1: First latitude in decimal degrees.
        lon1: First longitude in decimal degrees.
        lat2: Second latitude in decimal degrees.
        lon2: Second longitude in decimal degrees.

    Returns:
        Distance between the coordinates in meters (float).
    """

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


def nearby_osm_features(latitude, longitude):
    """Look up mapped points of interest near a rover's position.

    Searches OSM nodes, ways, and relations within 250 meters via Overpass.
    Public endpoints are tried in priority order, with a second round after
    a 10-second pause if the entire first round fails. Requests use a
    20-second client timeout; the Overpass query specifies 15 seconds.

    Nodes supply their own coordinates; ways and relations use the center
    supplied by Overpass. Results beyond the configured radius are discarded,
    deduplicated by OSM element identity, and sorted by ascending distance.
    Unnamed features remain eligible because the classifier needs categories,
    not publicly disclosed business identities.

    Args:
        latitude: Rover latitude in decimal degrees.
        longitude: Rover longitude in decimal degrees.

    Returns:
        A distance-sorted list of dictionaries, each with:
            name: OSM name, brand, or a generic 'Unnamed <kind>' label.
            kind: Feature category derived from its OSM tags.
            distance_m: Distance from rover in meters.
            rover_dog: True for fuel or convenience-store features.
        Returns [] when a query succeeds but finds no qualifying features.
        Returns None if every Overpass request fails.

    Notes:
        This function makes network requests and prints retry/failure status.
        The server list is configured locally below, not by the caller.
    """

    # Prioritize the North America dataset (US/Canada/Mexico rover focus).
    # France provides global fallback coverage; Germany and Kumi are further
    # fallbacks. Endpoint availability can change independently of our code.
    overpass_urls = (
        "https://ovp.dresel.systems/api-na/interpreter",
        "https://overpass.openstreetmap.fr/api/interpreter",
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
    """Find the first nearby hotel, motel, or hostel in the feature list.

    Args:
        features: Distance-sorted POI dictionaries, or None if lookup failed.
        radius_m: Intended decision radius in meters; see caveat below.

    Returns:
        The first matching POI dictionary, or None if no lodging qualifies.

    Caveat:
        Existing behavior compares against LODGING_RADIUS_METERS (150 m),
        ignoring the radius_m argument. This is a pre-existing issue retained
        here so documentation changes do not alter application behavior.
    """
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
    """Find a nearby feature consistent with an ordinary travel stop.

    Considers fuel, convenience stores, restaurants, cafes, and lodging
    within ORDINARY_STOP_RADIUS_METERS (100 m). The first qualifying feature
    is returned; callers normally supply distance-sorted features.

    Args:
        features: POI dictionaries, or None/[] when unavailable or empty.

    Returns:
        The first qualifying POI dictionary, or None.
    """
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
    """Suggest a generic context for a rover that appears stationary.

    Checks nearby POIs in priority order, returning the first match:
        ROVER_DOG: Fuel station or convenience store within 100 m.
        QUIET_TIME: Hotel, motel, or hostel (currently checked at 150 m;
                    see nearby_lodging_feature() caveat).
        FOOD_STOP: Restaurant, cafe, bar, or similar within 100 m.
        ROVER_BREAK: Park, campsite, attraction, museum, etc. within 250 m.
        POSSIBLE_ACTIVATION: No preceding category matched.

    These are heuristic labels, not verified rover activities. Generic
    descriptions are used instead of publishing specific business names.

    Args:
        features: Distance-sorted POI dictionaries returned by
            nearby_osm_features(), or None on lookup failure.

    Returns:
        A (classification, description) tuple. The description is a generic
        string for fuel, lodging, or food stops, otherwise None. Returns
        (None, None) if the POI lookup failed. An empty successful result
        becomes ("POSSIBLE_ACTIVATION", None).
    """
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
