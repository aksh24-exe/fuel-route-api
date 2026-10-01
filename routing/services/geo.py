"""Geographic primitives shared by the geocoder and the corridor search.

Distances are in statute miles throughout, because the brief states range in
miles and fuel economy in miles per gallon. Converting once at the edges is
cheaper than carrying two unit systems through the code.
"""

from __future__ import annotations

import math

# Mean Earth radius. Using the mean rather than the equatorial radius keeps the
# error under a tenth of a percent across the continental United States, which
# is far tighter than the error we already accept by resolving truckstops to
# their city centre.
EARTH_RADIUS_MILES = 3958.7613

# One degree of latitude is very nearly constant; one degree of longitude
# shrinks with the cosine of latitude, so it has to be computed per point.
MILES_PER_DEGREE_LAT = 69.0547


def miles_per_degree_lon(latitude: float) -> float:
    """Length of one degree of longitude, in miles, at ``latitude``."""
    return MILES_PER_DEGREE_LAT * math.cos(math.radians(latitude))


def haversine_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance between two points, in miles.

    Used where accuracy matters: reporting a detour, measuring the distance
    between consecutive route vertices. The corridor scan uses the cheaper
    planar approximation below instead.
    """
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = phi2 - phi1
    d_lambda = math.radians(lon2 - lon1)

    a = (
        math.sin(d_phi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    )
    return 2 * EARTH_RADIUS_MILES * math.asin(math.sqrt(a))


def to_local_xy(
    lat: float, lon: float, ref_lat: float
) -> tuple[float, float]:
    """Project a point to local miles, east-north, about ``ref_lat``.

    An equirectangular projection. Over the tens of miles a corridor test spans
    it is accurate to well under a mile, and it turns every distance check into
    plain arithmetic instead of a pair of trigonometric calls. That is the
    difference that makes scanning thousands of candidates per request cheap.
    """
    return lon * miles_per_degree_lon(ref_lat), lat * MILES_PER_DEGREE_LAT


def point_to_segment_miles(
    px: float,
    py: float,
    ax: float,
    ay: float,
    bx: float,
    by: float,
) -> tuple[float, float]:
    """Distance from point P to segment AB, and how far along AB the foot lies.

    Both inputs and outputs are in the local mile plane from :func:`to_local_xy`.
    Returns ``(distance, along)`` where ``along`` is clamped to the segment, so
    a point beyond either end measures to that endpoint. The caller needs both:
    the distance decides whether a truckstop is in the corridor, and ``along``
    places it at a mile offset on the route.
    """
    abx, aby = bx - ax, by - ay
    length_squared = abx * abx + aby * aby

    if length_squared == 0.0:
        # Degenerate segment: A and B coincide.
        dx, dy = px - ax, py - ay
        return math.sqrt(dx * dx + dy * dy), 0.0

    t = ((px - ax) * abx + (py - ay) * aby) / length_squared
    t = max(0.0, min(1.0, t))

    foot_x, foot_y = ax + t * abx, ay + t * aby
    dx, dy = px - foot_x, py - foot_y
    return math.sqrt(dx * dx + dy * dy), t * math.sqrt(length_squared)


def bounding_box(
    lat: float, lon: float, radius_miles: float
) -> tuple[float, float, float, float]:
    """Degree box that certainly contains everything within ``radius_miles``.

    Returns ``(min_lat, min_lon, max_lat, max_lon)``. Deliberately generous near
    the poles; the continental United States never gets close enough for that to
    matter.
    """
    d_lat = radius_miles / MILES_PER_DEGREE_LAT
    per_lon = miles_per_degree_lon(lat)
    d_lon = radius_miles / per_lon if per_lon > 1e-9 else 180.0
    return lat - d_lat, lon - d_lon, lat + d_lat, lon + d_lon
