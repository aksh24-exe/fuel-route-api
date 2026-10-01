"""The fuel-stop optimizer, checked against calculations done by hand.

Every expected figure below is arithmetic a reader can verify, which is the
point: a greedy rule that looks plausible but buys a few gallons too many at the
wrong stop would still return a sensible-looking answer.
"""

from django.test import SimpleTestCase

from routing.services.corridor import Candidate, StationRecord
from routing.services.optimizer import (
    InfeasibleRoute,
    plan_fuel_stops,
    tank_capacity_gallons,
)


def station(name: str, price: float) -> StationRecord:
    return StationRecord(
        opis_id=0,
        name=name,
        address='',
        city='Somewhere',
        state='TX',
        price=price,
        latitude=0.0,
        longitude=0.0,
    )


def candidate(name: str, price: float, mile: float, detour: float = 0.0) -> Candidate:
    return Candidate(station=station(name, price), offset_miles=mile, detour_miles=detour)


class TankTests(SimpleTestCase):
    def test_capacity_is_derived_from_the_brief(self):
        # 500 miles of range at 10 mpg is a 50 gallon tank.
        self.assertAlmostEqual(tank_capacity_gallons(), 50.0)


class WorkedExampleTests(SimpleTestCase):
    """A 1,000-mile route with five truckstops, worked through by hand.

    Starting full (50 gal, 500 mi):

    * mile 120 at $3.80 - passed, because $3.10 at mile 380 is reachable and
      cheaper;
    * mile 380 at $3.10 - 12 gal left, and $3.05 at mile 760 is within a full
      tank, so buy exactly the 26 gal that reaches it: $80.60;
    * mile 540 at $4.20 - passed;
    * mile 760 at $3.05 - nothing cheaper ahead, and the finish is 240 miles
      away, so buy exactly 24 gal: $73.20;
    * mile 940 at $3.60 - passed, the finish is already in range.

    Total: 50 gal for $153.80.
    """

    def setUp(self):
        self.candidates = [
            candidate('A', 3.80, 120.0),
            candidate('B', 3.10, 380.0),
            candidate('C', 4.20, 540.0),
            candidate('D', 3.05, 760.0),
            candidate('E', 3.60, 940.0),
        ]

    def test_total_cost_matches_the_hand_calculation(self):
        plan = plan_fuel_stops(self.candidates, 1000.0)
        self.assertAlmostEqual(plan.total_cost, 153.80, places=2)
        self.assertAlmostEqual(plan.gallons_purchased, 50.0, places=2)

    def test_it_stops_only_at_the_two_cheapest_truckstops(self):
        plan = plan_fuel_stops(self.candidates, 1000.0)
        self.assertEqual([stop.station.name for stop in plan.stops], ['B', 'D'])

    def test_it_buys_just_enough_to_reach_the_cheaper_stop(self):
        plan = plan_fuel_stops(self.candidates, 1000.0)
        first = plan.stops[0]
        self.assertAlmostEqual(first.gallons, 26.0, places=2)
        self.assertAlmostEqual(first.cost, 80.60, places=2)

    def test_the_final_purchase_is_only_what_finishes_the_trip(self):
        plan = plan_fuel_stops(self.candidates, 1000.0)
        last = plan.stops[-1]
        self.assertAlmostEqual(last.gallons, 24.0, places=2)
        self.assertAlmostEqual(last.cost, 73.20, places=2)

    def test_savings_compare_against_the_corridor_average(self):
        plan = plan_fuel_stops(self.candidates, 1000.0)
        # Mean of 3.80, 3.10, 4.20, 3.05, 3.60 is 3.55; 50 gal is $177.50.
        self.assertAlmostEqual(plan.baseline_price_per_gallon, 3.55, places=3)
        self.assertAlmostEqual(plan.baseline_cost, 177.50, places=2)
        self.assertAlmostEqual(plan.savings, 23.70, places=2)
        self.assertAlmostEqual(plan.savings_percent, 13.35, places=1)

    def test_cost_per_mile_is_reported(self):
        plan = plan_fuel_stops(self.candidates, 1000.0)
        self.assertAlmostEqual(plan.fuel_cost_per_mile, 153.80 / 1000.0, places=4)


class NoStopNeededTests(SimpleTestCase):
    def test_a_short_route_on_a_full_tank_costs_nothing(self):
        plan = plan_fuel_stops([candidate('A', 3.80, 120.0)], 400.0)
        self.assertEqual(plan.stops, ())
        self.assertEqual(plan.total_cost, 0.0)
        self.assertEqual(plan.savings, 0.0)

    def test_exactly_the_range_still_needs_no_stop(self):
        plan = plan_fuel_stops([candidate('A', 3.80, 120.0)], 500.0)
        self.assertEqual(plan.stops, ())


class PriceChoiceTests(SimpleTestCase):
    def test_it_fills_up_when_nothing_ahead_is_cheaper(self):
        # Cheap at mile 100, dear afterwards, so the tank should leave the cheap
        # truckstop full. Reaching mile 100 burns 10 of the starting 50 gallons,
        # so filling up means buying 10 - not 50.
        plan = plan_fuel_stops(
            [candidate('cheap', 3.00, 100.0), candidate('dear', 5.00, 560.0)],
            1000.0,
        )
        self.assertEqual(plan.stops[0].station.name, 'cheap')
        self.assertAlmostEqual(plan.stops[0].gallons, 10.0, places=1)
        self.assertAlmostEqual(plan.stops[0].cost, 30.0, places=2)

    def test_it_never_buys_more_than_the_tank_holds(self):
        # Truckstops every 400 miles over 1,200, so the route is coverable and
        # the point under test is the size of each purchase.
        plan = plan_fuel_stops(
            [
                candidate('a', 3.00, 100.0),
                candidate('b', 3.10, 500.0),
                candidate('c', 3.20, 900.0),
            ],
            1200.0,
        )
        self.assertGreater(len(plan.stops), 0)
        for stop in plan.stops:
            self.assertLessEqual(stop.gallons, tank_capacity_gallons() + 1e-6)

    def test_starting_empty_is_supported(self):
        plan = plan_fuel_stops(
            [candidate('origin', 3.00, 0.0), candidate('mid', 2.90, 400.0)],
            700.0,
            start_gallons=0.0,
        )
        self.assertGreater(len(plan.stops), 0)
        self.assertEqual(plan.stops[0].offset_miles, 0.0)

    def test_a_cheaper_stop_wins_over_a_nearer_one_at_the_first_choice(self):
        # The starting fuel is already paid for, so the first stop should be
        # the cheapest one reachable, not simply the first one passed.
        plan = plan_fuel_stops(
            [
                candidate('near-dear', 4.00, 50.0),
                candidate('far-cheap', 3.00, 450.0),
                candidate('end', 3.50, 900.0),
            ],
            1000.0,
        )
        self.assertEqual(plan.stops[0].station.name, 'far-cheap')


class FeasibilityTests(SimpleTestCase):
    def test_a_gap_longer_than_the_range_is_reported_with_its_bounds(self):
        with self.assertRaises(InfeasibleRoute) as caught:
            plan_fuel_stops(
                [candidate('a', 3.00, 100.0), candidate('b', 3.00, 900.0)],
                1500.0,
            )
        error = caught.exception
        self.assertAlmostEqual(error.gap_start, 100.0)
        self.assertAlmostEqual(error.gap_end, 900.0)
        self.assertIn('no truckstop', str(error).lower())

    def test_an_unreachable_first_truckstop_is_reported(self):
        with self.assertRaises(InfeasibleRoute):
            plan_fuel_stops([candidate('a', 3.00, 600.0)], 1000.0)

    def test_a_long_final_leg_is_reported(self):
        with self.assertRaises(InfeasibleRoute) as caught:
            plan_fuel_stops([candidate('a', 3.00, 100.0)], 900.0)
        self.assertAlmostEqual(caught.exception.gap_start, 100.0)

    def test_no_truckstops_at_all_on_a_long_route(self):
        with self.assertRaises(InfeasibleRoute):
            plan_fuel_stops([], 900.0)

    def test_no_truckstops_on_a_short_route_is_fine(self):
        plan = plan_fuel_stops([], 300.0)
        self.assertEqual(plan.stops, ())
