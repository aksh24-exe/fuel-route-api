"""The route endpoint, end to end, with the routing provider stubbed.

The stub matters for two reasons. The suite has to pass without network access,
and stubbing the one external call is also the cleanest way to assert the thing
the brief actually asks about: that a request makes exactly one call to the
routing API, and a repeat makes none.
"""

from __future__ import annotations

import math
from unittest.mock import patch

from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse

from routing.models import FuelStation
from routing.services.corridor import reset_station_index
from routing.services.geo import MILES_PER_DEGREE_LAT
from routing.services.osrm import NoRouteFound, Route, RoutingError

# A route running due east along the 40th parallel, where one degree of
# longitude is a predictable 52.9 miles.
LATITUDE = 40.0
MILES_PER_DEGREE_LON = MILES_PER_DEGREE_LAT * math.cos(math.radians(LATITUDE))
START_LONGITUDE = -100.0


def longitude_at(mile: float) -> float:
    return START_LONGITUDE + mile / MILES_PER_DEGREE_LON


def stub_route(distance_miles: float = 600.0, from_cache: bool = False) -> Route:
    steps = 200
    points = tuple(
        (LATITUDE, longitude_at(distance_miles * step / steps))
        for step in range(steps + 1)
    )
    return Route(
        points=points,
        distance_miles=distance_miles,
        duration_seconds=distance_miles / 55 * 3600,
        from_cache=from_cache,
        encoded='',
    )


class RouteEndpointTests(TestCase):
    """A 600-mile route, which is longer than the 500-mile range, so it needs a stop."""

    def setUp(self):
        cache.clear()
        reset_station_index()
        # Dear early, cheap in the middle, dearest at the end.
        for opis_id, (name, price, mile) in enumerate(
            [
                ('EARLY DEAR TRUCKSTOP', '4.0000', 80.0),
                ('MIDWAY CHEAP TRUCKSTOP', '3.0000', 300.0),
                ('LATE DEAREST TRUCKSTOP', '5.0000', 480.0),
            ],
            start=1,
        ):
            FuelStation.objects.create(
                opis_id=opis_id,
                name=name,
                address='I-70 exit',
                city='Town',
                state='KS',
                price_per_gallon=price,
                latitude=LATITUDE,
                longitude=longitude_at(mile),
            )
        self.url = reverse('routing:route')

    def tearDown(self):
        reset_station_index()
        cache.clear()

    def _get(self, **params):
        query = {'start': 'Chicago, IL', 'finish': 'Dallas, TX'}
        query.update(params)
        return self.client.get(self.url, query)

    # -- happy path -------------------------------------------------------

    def test_it_answers_with_the_route_and_a_plan(self):
        with patch('routing.views.get_route', return_value=stub_route()):
            response = self._get()
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertAlmostEqual(body['route']['total_distance_miles'], 600.0, places=1)
        self.assertGreater(len(body['fuel_stops']), 0)

    def test_it_reports_exactly_one_routing_call(self):
        with patch('routing.views.get_route', return_value=stub_route()):
            body = self._get().json()
        self.assertEqual(body['meta']['routing_api_calls'], 1)
        self.assertFalse(body['meta']['cache_hit'])

    def test_a_cached_route_reports_no_routing_call(self):
        with patch('routing.views.get_route', return_value=stub_route(from_cache=True)):
            body = self._get().json()
        self.assertEqual(body['meta']['routing_api_calls'], 0)
        self.assertTrue(body['meta']['cache_hit'])

    def test_geocoding_never_costs_a_call(self):
        with patch('routing.views.get_route', return_value=stub_route()):
            body = self._get().json()
        self.assertEqual(body['meta']['geocoding_api_calls'], 0)

    def test_the_routing_provider_is_called_once_per_request(self):
        with patch('routing.views.get_route', return_value=stub_route()) as stub:
            self._get()
        self.assertEqual(stub.call_count, 1)

    def test_it_stops_at_the_cheapest_reachable_truckstop(self):
        with patch('routing.views.get_route', return_value=stub_route()):
            body = self._get().json()
        names = [stop['truckstop_name'] for stop in body['fuel_stops']]
        self.assertIn('MIDWAY CHEAP TRUCKSTOP', names)
        self.assertNotIn('LATE DEAREST TRUCKSTOP', names)

    def test_the_vehicle_block_reflects_the_brief(self):
        with patch('routing.views.get_route', return_value=stub_route()):
            vehicle = self._get().json()['vehicle']
        self.assertEqual(vehicle['max_range_miles'], 500.0)
        self.assertEqual(vehicle['miles_per_gallon'], 10.0)
        self.assertEqual(vehicle['tank_capacity_gallons'], 50.0)

    def test_totals_are_internally_consistent(self):
        with patch('routing.views.get_route', return_value=stub_route()):
            body = self._get().json()
        stops = body['fuel_stops']
        totals = body['totals']
        self.assertAlmostEqual(
            totals['total_fuel_cost'], sum(s['cost'] for s in stops), places=1
        )
        self.assertAlmostEqual(
            totals['gallons_purchased'], sum(s['gallons'] for s in stops), places=1
        )
        self.assertAlmostEqual(
            totals['savings'], totals['baseline_cost'] - totals['total_fuel_cost'],
            places=1,
        )

    def test_cost_per_mile_matches_the_total(self):
        with patch('routing.views.get_route', return_value=stub_route()):
            body = self._get().json()
        expected = body['totals']['total_fuel_cost'] / body['route']['total_distance_miles']
        self.assertAlmostEqual(body['totals']['fuel_cost_per_mile'], expected, places=2)

    def test_geometry_comes_back_as_geojson(self):
        with patch('routing.views.get_route', return_value=stub_route()):
            geometry = self._get().json()['route']['geometry']
        self.assertEqual(geometry['type'], 'LineString')
        self.assertGreater(len(geometry['coordinates']), 1)
        # GeoJSON is longitude first, which is the opposite of everything else here.
        longitude, latitude = geometry['coordinates'][0]
        self.assertAlmostEqual(latitude, LATITUDE, places=2)
        self.assertAlmostEqual(longitude, START_LONGITUDE, places=2)

    def test_a_short_route_needs_no_stop(self):
        with patch('routing.views.get_route', return_value=stub_route(distance_miles=300.0)):
            body = self._get().json()
        self.assertEqual(body['fuel_stops'], [])
        self.assertEqual(body['totals']['total_fuel_cost'], 0)

    def test_an_empty_tank_with_nothing_at_the_origin_is_refused(self):
        # The nearest truckstop here is 80 miles out, and a truck with no fuel
        # cannot cover them, so refusing is the correct answer rather than
        # inventing a stop.
        with patch('routing.views.get_route', return_value=stub_route()):
            response = self._get(start_gallons=0)
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()['error']['code'], 'route_not_fuelable')

    # -- the map ----------------------------------------------------------

    def test_format_html_renders_a_map(self):
        with patch('routing.views.get_route', return_value=stub_route()):
            response = self._get(format='html')
        self.assertEqual(response.status_code, 200)
        self.assertIn('text/html', response['Content-Type'])
        self.assertTemplateUsed(response, 'map.html')

    def test_the_map_names_the_chosen_truckstop(self):
        with patch('routing.views.get_route', return_value=stub_route()):
            response = self._get(format='html')
        self.assertContains(response, 'MIDWAY CHEAP TRUCKSTOP')

    # -- failures ---------------------------------------------------------

    def test_a_missing_parameter_is_a_400(self):
        response = self.client.get(self.url, {'start': 'Chicago, IL'})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()['error']['code'], 'invalid_parameters')

    def test_an_unknown_location_is_a_400(self):
        response = self._get(start='Nowhere, ZZ')
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()['error']['code'], 'location_not_found')

    def test_a_canadian_location_is_refused_with_a_reason(self):
        response = self._get(start='Toronto, ON')
        self.assertEqual(response.status_code, 400)
        self.assertIn('Canada', response.json()['error']['message'])

    def test_more_fuel_than_the_tank_holds_is_a_400(self):
        response = self._get(start_gallons=999)
        self.assertEqual(response.status_code, 400)

    def test_an_unreachable_provider_is_a_503(self):
        with patch('routing.views.get_route', side_effect=RoutingError('OSRM is down')):
            response = self._get()
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()['error']['code'], 'routing_unavailable')

    def test_no_road_route_is_a_422(self):
        with patch('routing.views.get_route', side_effect=NoRouteFound('no road')):
            response = self._get()
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()['error']['code'], 'no_route')


class UnfuelableRouteTests(TestCase):
    """A long route with one truckstop near the start and nothing after it."""

    def setUp(self):
        cache.clear()
        reset_station_index()
        FuelStation.objects.create(
            opis_id=1,
            name='LONELY TRUCKSTOP',
            city='Town',
            state='KS',
            price_per_gallon='3.0000',
            latitude=LATITUDE,
            longitude=longitude_at(100.0),
        )
        self.url = reverse('routing:route')

    def tearDown(self):
        reset_station_index()
        cache.clear()

    def test_an_unfuelable_route_is_a_422_naming_the_gap(self):
        with patch('routing.views.get_route', return_value=stub_route(distance_miles=1400.0)):
            response = self.client.get(
                self.url, {'start': 'Chicago, IL', 'finish': 'Dallas, TX'}
            )
        self.assertEqual(response.status_code, 422)
        error = response.json()['error']
        self.assertEqual(error['code'], 'route_not_fuelable')
        self.assertIn('gap_start_miles', error)
        self.assertIn('gap_end_miles', error)


class EmptyTankTests(TestCase):
    """Departing on an empty tank, with somewhere to fuel at the origin."""

    def setUp(self):
        cache.clear()
        reset_station_index()
        for opis_id, (name, price, mile) in enumerate(
            [
                ('ORIGIN TRUCKSTOP', '3.5000', 0.0),
                ('MIDWAY CHEAP TRUCKSTOP', '3.0000', 300.0),
            ],
            start=1,
        ):
            FuelStation.objects.create(
                opis_id=opis_id,
                name=name,
                city='Town',
                state='KS',
                price_per_gallon=price,
                latitude=LATITUDE,
                longitude=longitude_at(mile),
            )
        self.url = reverse('routing:route')

    def tearDown(self):
        reset_station_index()
        cache.clear()

    def _get(self, **params):
        query = {'start': 'Chicago, IL', 'finish': 'Dallas, TX'}
        query.update(params)
        return self.client.get(self.url, query)

    def test_an_empty_tank_buys_more_fuel_than_a_full_one(self):
        with patch('routing.views.get_route', return_value=stub_route()):
            full = self._get().json()
            empty = self._get(start_gallons=0).json()
        self.assertGreater(
            empty['totals']['gallons_purchased'],
            full['totals']['gallons_purchased'],
        )

    def test_an_empty_tank_fuels_at_the_origin_first(self):
        with patch('routing.views.get_route', return_value=stub_route()):
            body = self._get(start_gallons=0).json()
        self.assertEqual(body['fuel_stops'][0]['route_offset_miles'], 0.0)

    def test_an_empty_tank_buys_enough_to_cover_the_route(self):
        with patch('routing.views.get_route', return_value=stub_route()):
            body = self._get(start_gallons=0).json()
        # 600 miles at 10 mpg is 60 gallons, whatever the split between stops.
        self.assertAlmostEqual(body['totals']['gallons_purchased'], 60.0, delta=0.5)

    def test_the_reported_start_gallons_reflects_the_request(self):
        with patch('routing.views.get_route', return_value=stub_route()):
            body = self._get(start_gallons=0).json()
        self.assertEqual(body['vehicle']['start_gallons'], 0.0)
