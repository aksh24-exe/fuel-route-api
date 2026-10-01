"""The corridor search: resampling, the grid index, and candidate thinning.

The route used here runs due east along the 40th parallel, so every expected
distance can be worked out on paper: one degree of longitude at that latitude is
about 52.9 miles.
"""

from django.test import TestCase

from routing.models import FuelStation
from routing.services.corridor import (
    Candidate,
    RoutePath,
    StationIndex,
    StationRecord,
    reset_station_index,
    thin_candidates,
)
from routing.services.geo import MILES_PER_DEGREE_LAT
from routing.services.osrm import Route

# Due east along the 40th parallel, five degrees of longitude.
ROUTE_LATITUDE = 40.0
ROUTE_MILES = 264.5


def straight_route(distance_miles: float = ROUTE_MILES) -> Route:
    points = tuple(
        (ROUTE_LATITUDE, -100.0 + step * 0.05) for step in range(101)
    )
    return Route(
        points=points,
        distance_miles=distance_miles,
        duration_seconds=distance_miles / 60 * 3600,
        from_cache=False,
        encoded='',
    )


class RoutePathTests(TestCase):
    def test_resampling_reduces_the_point_count(self):
        route = straight_route()
        path = RoutePath.from_route(route, sample_miles=25.0)
        self.assertLess(len(path), len(route.points))
        self.assertGreater(len(path), 2)

    def test_mile_markers_start_at_zero_and_end_at_the_total(self):
        path = RoutePath.from_route(straight_route(), sample_miles=25.0)
        self.assertAlmostEqual(path.points[0].mile, 0.0, places=3)
        self.assertAlmostEqual(path.points[-1].mile, path.total_miles, places=3)

    def test_the_total_is_the_distance_the_provider_reported(self):
        # Great-circle hops along the polyline undercount road distance, so the
        # markers are scaled onto the provider's figure rather than the geometry.
        route = straight_route(distance_miles=300.0)
        path = RoutePath.from_route(route, sample_miles=25.0)
        self.assertAlmostEqual(path.total_miles, 300.0)
        self.assertAlmostEqual(path.points[-1].mile, 300.0, places=3)

    def test_mile_markers_only_increase(self):
        path = RoutePath.from_route(straight_route(), sample_miles=10.0)
        miles = [point.mile for point in path.points]
        self.assertEqual(miles, sorted(miles))

    def test_endpoints_are_preserved_exactly(self):
        route = straight_route()
        path = RoutePath.from_route(route, sample_miles=40.0)
        self.assertAlmostEqual(path.points[0].latitude, route.points[0][0])
        self.assertAlmostEqual(path.points[-1].longitude, route.points[-1][1])


def record(name, price, latitude, longitude) -> StationRecord:
    return StationRecord(
        opis_id=abs(hash(name)) % 100000,
        name=name,
        address='',
        city='Town',
        state='KS',
        price=price,
        latitude=latitude,
        longitude=longitude,
    )


class CorridorSearchTests(TestCase):
    def setUp(self):
        three_miles_north = ROUTE_LATITUDE + 3.0 / MILES_PER_DEGREE_LAT
        far_north = ROUTE_LATITUDE + 40.0 / MILES_PER_DEGREE_LAT

        self.index = StationIndex(
            [
                record('on-route', 3.00, ROUTE_LATITUDE, -98.0),
                record('near-route', 3.10, three_miles_north, -97.0),
                record('far-off', 2.00, far_north, -97.5),
            ],
            cell_degrees=0.5,
        )
        self.path = RoutePath.from_route(straight_route(), sample_miles=2.0)

    def test_it_finds_truckstops_inside_the_corridor(self):
        found = {
            candidate.station.name
            for candidate in self.index.find_candidates(self.path, max_detour_miles=5.0)
        }
        self.assertIn('on-route', found)
        self.assertIn('near-route', found)

    def test_it_excludes_truckstops_outside_the_corridor(self):
        found = {
            candidate.station.name
            for candidate in self.index.find_candidates(self.path, max_detour_miles=5.0)
        }
        # Cheapest of the three, and correctly ignored - being cheap does not
        # help if the truck would have to drive 40 miles off the route.
        self.assertNotIn('far-off', found)

    def test_a_wider_corridor_admits_more(self):
        found = {
            candidate.station.name
            for candidate in self.index.find_candidates(self.path, max_detour_miles=45.0)
        }
        self.assertIn('far-off', found)

    def test_an_on_route_truckstop_reports_no_detour(self):
        candidates = self.index.find_candidates(self.path, max_detour_miles=5.0)
        on_route = next(c for c in candidates if c.station.name == 'on-route')
        self.assertLess(on_route.detour_miles, 0.5)

    def test_detour_distance_is_measured(self):
        candidates = self.index.find_candidates(self.path, max_detour_miles=5.0)
        near = next(c for c in candidates if c.station.name == 'near-route')
        self.assertAlmostEqual(near.detour_miles, 3.0, delta=0.4)

    def test_offsets_are_measured_along_the_route(self):
        candidates = self.index.find_candidates(self.path, max_detour_miles=5.0)
        on_route = next(c for c in candidates if c.station.name == 'on-route')
        # Two degrees east of the start, at roughly 52.9 miles per degree.
        self.assertAlmostEqual(on_route.offset_miles, 105.8, delta=6.0)

    def test_results_are_ordered_along_the_route(self):
        candidates = self.index.find_candidates(self.path, max_detour_miles=45.0)
        offsets = [candidate.offset_miles for candidate in candidates]
        self.assertEqual(offsets, sorted(offsets))

    def test_no_offset_exceeds_the_route_length(self):
        for candidate in self.index.find_candidates(self.path, max_detour_miles=45.0):
            self.assertLessEqual(candidate.offset_miles, self.path.total_miles + 1e-6)


class StationIndexFromDatabaseTests(TestCase):
    def setUp(self):
        reset_station_index()
        FuelStation.objects.create(
            opis_id=1,
            name='TEST TRUCKSTOP',
            city='Salina',
            state='KS',
            price_per_gallon='3.2500',
            latitude=ROUTE_LATITUDE,
            longitude=-97.6,
        )

    def tearDown(self):
        reset_station_index()

    def test_it_loads_rows_from_the_table(self):
        index = StationIndex.from_database()
        self.assertEqual(index.count, 1)

    def test_prices_survive_as_floats(self):
        index = StationIndex.from_database()
        station = next(iter(next(iter(index.grid.values()))))
        self.assertAlmostEqual(station.price, 3.25)


class ThinningTests(TestCase):
    def test_it_keeps_the_cheapest_per_bin(self):
        candidates = [
            Candidate(record('dear', 4.00, 40.0, -98.0), 5.0, 0.0),
            Candidate(record('cheap', 3.00, 40.0, -98.1), 8.0, 0.0),
            Candidate(record('mid', 3.50, 40.0, -98.2), 12.0, 0.0),
        ]
        kept = thin_candidates(candidates, bin_miles=25.0, per_bin=1)
        self.assertEqual([c.station.name for c in kept], ['cheap'])

    def test_it_keeps_one_bin_per_stretch(self):
        candidates = [
            Candidate(record('first', 3.00, 40.0, -98.0), 5.0, 0.0),
            Candidate(record('second', 3.90, 40.0, -97.0), 60.0, 0.0),
        ]
        kept = thin_candidates(candidates, bin_miles=25.0, per_bin=1)
        self.assertEqual(len(kept), 2)

    def test_ties_on_price_prefer_the_shorter_detour(self):
        candidates = [
            Candidate(record('long-detour', 3.00, 40.0, -98.0), 5.0, 4.5),
            Candidate(record('short-detour', 3.00, 40.0, -98.1), 8.0, 0.2),
        ]
        kept = thin_candidates(candidates, bin_miles=25.0, per_bin=1)
        self.assertEqual(kept[0].station.name, 'short-detour')

    def test_output_stays_ordered_along_the_route(self):
        candidates = [
            Candidate(record(f's{i}', 3.0 + i * 0.1, 40.0, -98.0 - i), float(i * 30), 0.0)
            for i in range(8)
        ]
        kept = thin_candidates(candidates, bin_miles=25.0, per_bin=2)
        offsets = [candidate.offset_miles for candidate in kept]
        self.assertEqual(offsets, sorted(offsets))
