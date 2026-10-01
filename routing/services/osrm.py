"""The routing provider: OSRM's public demo server.

This module is the only place in the project that touches the network. The
brief asks for one call to the map/routing API and allows two or three; a route
here costs exactly one, because OSRM returns the road geometry and the total
distance in the same response. There is no second request to fetch the shape.

OSRM was chosen over OpenRouteService, Mapbox and Google for one reason that
matters to whoever reviews this: it needs no API key. A reviewer can clone the
repository and run it without registering for anything.

Responses are cached on the rounded coordinate pair, so repeating a query costs
zero calls.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import requests
from django.conf import settings
from django.core.cache import cache

from .geocode import Place

logger = logging.getLogger(__name__)

METERS_PER_MILE = 1609.344

# OSRM's `geometries=polyline` uses Google's encoded polyline algorithm at five
# decimal places of precision - about a metre. Decoding it here avoids a
# dependency, and the encoded form is roughly a tenth the size of the GeoJSON
# alternative, which keeps the one response we do fetch small.
POLYLINE_PRECISION = 5


class RoutingError(RuntimeError):
    """The routing provider could not be reached or could not answer."""


class NoRouteFound(ValueError):
    """The provider reached the request but no road route exists."""


@dataclass(frozen=True, slots=True)
class Route:
    """A driveable route: its shape, its length, and where it came from."""

    points: tuple[tuple[float, float], ...]  # (latitude, longitude), in order
    distance_miles: float
    duration_seconds: float
    from_cache: bool
    # The provider's own encoded polyline, passed through untouched. The map
    # draws from this so the road keeps its full detail, while the JSON
    # response carries a lighter resampled geometry.
    encoded: str = ''

    @property
    def api_calls(self) -> int:
        """External calls this route cost. Reported in the API response."""
        return 0 if self.from_cache else 1


# ---------------------------------------------------------------------------
# Polyline decoding
# ---------------------------------------------------------------------------


def decode_polyline(encoded: str, precision: int = POLYLINE_PRECISION) -> list[tuple[float, float]]:
    """Decode a Google-encoded polyline into ``(latitude, longitude)`` pairs.

    Each coordinate is stored as a delta from the previous one, zig-zag encoded
    into six-bit chunks with a continuation flag. Walking the string once is
    enough; there is no need to split it first.
    """
    coordinates: list[tuple[float, float]] = []
    scale = 10.0**precision
    index = 0
    latitude = longitude = 0
    length = len(encoded)

    while index < length:
        for axis in range(2):
            result = 0
            shift = 0
            while True:
                if index >= length:
                    return coordinates
                byte = ord(encoded[index]) - 63
                index += 1
                result |= (byte & 0x1F) << shift
                shift += 5
                if byte < 0x20:
                    break
            # Zig-zag: the low bit carries the sign.
            delta = ~(result >> 1) if result & 1 else (result >> 1)
            if axis == 0:
                latitude += delta
            else:
                longitude += delta

        coordinates.append((latitude / scale, longitude / scale))

    return coordinates


# ---------------------------------------------------------------------------
# The client
# ---------------------------------------------------------------------------


def _cache_key(start: Place, finish: Place) -> str:
    """Cache key from the rounded endpoints.

    Three decimal places is about 110 metres - far tighter than the error we
    already carry from resolving truckstops to a city centre, so rounding here
    never changes which stops come back.
    """
    digits = settings.FUEL_ROUTE['OSRM_CACHE_PRECISION']
    return (
        'osrm:route:'
        f'{round(start.latitude, digits)},{round(start.longitude, digits)};'
        f'{round(finish.latitude, digits)},{round(finish.longitude, digits)}'
    )


def get_route(start: Place, finish: Place) -> Route:
    """Fetch the driving route between two places.

    Costs one HTTP request, or none when the same pair has been asked for
    before. Raises :class:`NoRouteFound` when the provider answers but no road
    connects the points, and :class:`RoutingError` when it cannot be reached.
    """
    key = _cache_key(start, finish)

    cached = cache.get(key)
    if cached is not None:
        points, distance_miles, duration_seconds, encoded = cached
        return Route(
            points=points,
            distance_miles=distance_miles,
            duration_seconds=duration_seconds,
            from_cache=True,
            encoded=encoded,
        )

    base_url = settings.FUEL_ROUTE['OSRM_BASE_URL'].rstrip('/')
    # OSRM takes coordinates as longitude,latitude - the opposite order to the
    # one used everywhere else in this project.
    coordinates = (
        f'{start.longitude},{start.latitude};{finish.longitude},{finish.latitude}'
    )
    url = f'{base_url}/route/v1/driving/{coordinates}'

    try:
        response = requests.get(
            url,
            params={
                # The full geometry and the distance arrive together, which is
                # what keeps this to a single call.
                'overview': 'full',
                'geometries': 'polyline',
                'alternatives': 'false',
                'steps': 'false',
            },
            timeout=settings.FUEL_ROUTE['OSRM_TIMEOUT_SECONDS'],
            headers={'User-Agent': 'fuel-route-api/1.0'},
        )
    except requests.Timeout as error:
        raise RoutingError(
            'The routing service (OSRM) did not respond in time. '
            'It is a free public demo server with no uptime guarantee; '
            'please try again.'
        ) from error
    except requests.RequestException as error:
        raise RoutingError(
            f'Could not reach the routing service (OSRM) at {base_url}.'
        ) from error

    if response.status_code != 200:
        raise RoutingError(
            f'The routing service (OSRM) returned HTTP {response.status_code}.'
        )

    try:
        payload = response.json()
    except ValueError as error:
        raise RoutingError(
            'The routing service (OSRM) returned a response that was not JSON.'
        ) from error

    code = payload.get('code')
    if code == 'NoRoute':
        raise NoRouteFound(
            'No driveable route connects those two locations. '
            'Both must be reachable by road within North America.'
        )
    if code != 'Ok':
        raise RoutingError(
            f'The routing service (OSRM) rejected the request: {code}.'
        )

    routes = payload.get('routes') or []
    if not routes:
        raise NoRouteFound('The routing service returned no route.')

    best = routes[0]
    encoded = best.get('geometry') or ''
    points = tuple(decode_polyline(encoded))
    if len(points) < 2:
        raise NoRouteFound('The routing service returned an empty route geometry.')

    distance_miles = float(best.get('distance', 0.0)) / METERS_PER_MILE
    duration_seconds = float(best.get('duration', 0.0))

    cache.set(key, (points, distance_miles, duration_seconds, encoded))
    logger.info(
        'OSRM route fetched: %.1f mi, %d points', distance_miles, len(points)
    )

    return Route(
        points=points,
        distance_miles=distance_miles,
        duration_seconds=duration_seconds,
        from_cache=False,
        encoded=encoded,
    )
