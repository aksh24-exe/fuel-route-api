"""The route endpoint.

One view answers the whole brief: given two US locations it returns the route,
the cheapest sequence of truckstops to fuel at, and what the fuel costs. The
same URL renders a Leaflet map when asked for HTML.

The work itself lives in :mod:`routing.services`; this module is the seam
between HTTP and that pipeline, so its job is validating input, ordering the
five steps, and turning each failure into a status code and a sentence someone
can act on.
"""

from __future__ import annotations

import time

from django.conf import settings
from django.shortcuts import render
from django.urls import reverse
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from .serializers import RouteQuerySerializer
from .services.corridor import RoutePath, get_station_index, thin_candidates
from .services.geo import haversine_miles
from .services.geocode import (
    GazetteerMissing,
    GeocodeError,
    Place,
    geocode,
    get_place_index,
    search_places,
)
from .services.optimizer import (
    InfeasibleRoute,
    plan_fuel_stops,
    tank_capacity_gallons,
)
from .services.osrm import NoRouteFound, RoutingError, get_route

# The geometry returned in JSON is resampled: a caller drawing a 967-mile route
# does not need a vertex every few metres, and the full polyline travels in its
# own compact encoded field for anyone who wants it.
GEOMETRY_SAMPLE_MILES = 0.5


def _error(message: str, code: str, http_status: int, **extra) -> Response:
    """A consistent error body, so clients can branch on ``code``."""
    body = {'error': {'code': code, 'message': message}}
    body['error'].update(extra)
    return Response(body, status=http_status)


def home(request):
    """City picker. The page calls ``GET /api/route/`` and draws the answer."""
    try:
        index = get_place_index()
        place_count = len(index.entries)
        row_count = index.row_count
    except GazetteerMissing:
        place_count = 0
        row_count = 0
    return render(
        request,
        'index.html',
        {
            'place_count': place_count,
            'row_count': row_count,
            'route_url': reverse('routing:route'),
            'places_url': reverse('routing:places'),
        },
    )


class PlacesView(APIView):
    """``GET /api/places/?q=<text>``

    Searches every place in the gazetteer. The response lists the closest
    matches; ``count`` is how many distinct places exist in total.
    """

    def get(self, request):
        try:
            payload = search_places(request.query_params.get('q', ''))
        except GazetteerMissing as error:
            return _error(
                str(error),
                'gazetteer_missing',
                status.HTTP_500_INTERNAL_SERVER_ERROR,
            )
        return Response(payload)


class RouteView(APIView):
    """``GET /api/route/?start=<place>&finish=<place>``

    Add ``&format=html`` to see the route drawn on a map instead of as JSON.
    """

    def get(self, request):
        started = time.perf_counter()

        query = RouteQuerySerializer(data=request.query_params)
        if not query.is_valid():
            return _error(
                'Check the query parameters.',
                'invalid_parameters',
                status.HTTP_400_BAD_REQUEST,
                fields=query.errors,
            )
        params = query.validated_data

        # 1. Resolve both endpoints. Offline; costs no API calls.
        try:
            start = geocode(params['start'])
            finish = geocode(params['finish'])
        except GeocodeError as error:
            return _error(
                str(error), 'location_not_found', status.HTTP_400_BAD_REQUEST
            )
        except GazetteerMissing as error:
            return _error(
                str(error),
                'gazetteer_missing',
                status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        # 2. The one external call.
        try:
            route = get_route(start, finish)
        except NoRouteFound as error:
            return _error(str(error), 'no_route', status.HTTP_422_UNPROCESSABLE_ENTITY)
        except RoutingError as error:
            return _error(
                str(error),
                'routing_unavailable',
                status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        # 3-5. Everything from here on is in memory.
        path = RoutePath.from_route(route)
        index = get_station_index()
        candidates = index.find_candidates(path, params.get('max_detour_miles'))
        shortlist = thin_candidates(candidates)

        try:
            plan = plan_fuel_stops(
                shortlist,
                path.total_miles,
                start_gallons=params.get('start_gallons'),
                corridor_prices=[c.station.price for c in candidates],
            )
        except InfeasibleRoute as error:
            return _error(
                str(error),
                'route_not_fuelable',
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                gap_start_miles=round(error.gap_start, 1),
                gap_end_miles=round(error.gap_end, 1),
            )

        elapsed_ms = (time.perf_counter() - started) * 1000.0
        payload = self._build_payload(
            start, finish, route, path, candidates, plan, elapsed_ms
        )

        if request.query_params.get('format') == 'html':
            return render(
                request,
                'map.html',
                {'payload': payload, 'encoded_polyline': route.encoded},
            )

        return Response(payload)

    # -- response assembly -------------------------------------------------

    def _build_payload(
        self, start: Place, finish: Place, route, path, candidates, plan, elapsed_ms
    ) -> dict:
        config = settings.FUEL_ROUTE
        return {
            'start': {
                'query': start.label,
                'latitude': round(start.latitude, 6),
                'longitude': round(start.longitude, 6),
                'resolved_by': start.source,
            },
            'finish': {
                'query': finish.label,
                'latitude': round(finish.latitude, 6),
                'longitude': round(finish.longitude, 6),
                'resolved_by': finish.source,
            },
            'route': {
                'total_distance_miles': round(path.total_miles, 1),
                'estimated_drive_time_hours': round(route.duration_seconds / 3600, 1),
                'geometry': self._geometry(route),
                'geometry_encoded': route.encoded,
            },
            'vehicle': {
                'max_range_miles': config['MAX_RANGE_MILES'],
                'miles_per_gallon': config['MILES_PER_GALLON'],
                'tank_capacity_gallons': round(tank_capacity_gallons(), 1),
                'start_gallons': round(plan.start_gallons, 1),
            },
            'fuel_stops': [
                {
                    'sequence': stop.sequence,
                    'truckstop_name': stop.station.name,
                    'address': stop.station.address,
                    'city': stop.station.city,
                    'state': stop.station.state,
                    'latitude': round(stop.station.latitude, 6),
                    'longitude': round(stop.station.longitude, 6),
                    'route_offset_miles': round(stop.offset_miles, 1),
                    'detour_miles': round(stop.detour_miles, 2),
                    'price_per_gallon': round(stop.price, 3),
                    'gallons': round(stop.gallons, 2),
                    'cost': round(stop.cost, 2),
                }
                for stop in plan.stops
            ],
            'totals': {
                'gallons_purchased': round(plan.gallons_purchased, 2),
                'total_fuel_cost': round(plan.total_cost, 2),
                'fuel_cost_per_mile': round(plan.fuel_cost_per_mile, 3),
                # What the same fuel would cost at the average price of the
                # truckstops on this corridor - roughly what a driver refuelling
                # without price information would pay.
                'baseline_price_per_gallon': round(plan.baseline_price_per_gallon, 3),
                'baseline_cost': round(plan.baseline_cost, 2),
                'savings': round(plan.savings, 2),
                'savings_percent': round(plan.savings_percent, 1),
            },
            'meta': {
                # The brief cares how often the routing API is called, so the
                # response states it rather than asking anyone to take it on trust.
                'routing_api_calls': route.api_calls,
                'geocoding_api_calls': 0,
                'cache_hit': route.from_cache,
                'truckstops_in_corridor': len(candidates),
                'max_detour_miles': config['MAX_DETOUR_MILES'],
                'elapsed_ms': round(elapsed_ms, 1),
            },
        }

    def _geometry(self, route) -> dict:
        """GeoJSON LineString for the route, resampled to keep it small."""
        vertices = route.points
        coordinates = [[round(vertices[0][1], 5), round(vertices[0][0], 5)]]
        since_last = 0.0

        for index in range(1, len(vertices)):
            previous_lat, previous_lon = vertices[index - 1]
            latitude, longitude = vertices[index]
            since_last += haversine_miles(
                previous_lat, previous_lon, latitude, longitude
            )
            if since_last >= GEOMETRY_SAMPLE_MILES:
                coordinates.append([round(longitude, 5), round(latitude, 5)])
                since_last = 0.0

        last_lat, last_lon = vertices[-1]
        if coordinates[-1] != [round(last_lon, 5), round(last_lat, 5)]:
            coordinates.append([round(last_lon, 5), round(last_lat, 5)])

        return {'type': 'LineString', 'coordinates': coordinates}
