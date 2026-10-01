"""The distance primitives, checked against values that can be verified by hand."""

from django.test import SimpleTestCase

from routing.services.geo import (
    MILES_PER_DEGREE_LAT,
    haversine_miles,
    point_to_segment_miles,
)


class HaversineTests(SimpleTestCase):
    def test_one_degree_of_latitude_is_about_69_miles(self):
        distance = haversine_miles(40.0, -90.0, 41.0, -90.0)
        self.assertAlmostEqual(distance, MILES_PER_DEGREE_LAT, delta=0.1)

    def test_identical_points_are_zero_apart(self):
        self.assertEqual(haversine_miles(41.88, -87.63, 41.88, -87.63), 0.0)

    def test_chicago_to_dallas_great_circle(self):
        # Straight-line distance, which is shorter than the ~967 road miles.
        distance = haversine_miles(41.8781, -87.6298, 32.7767, -96.7970)
        self.assertAlmostEqual(distance, 805.0, delta=5.0)

    def test_distance_is_symmetric(self):
        there = haversine_miles(35.0, -100.0, 39.0, -94.0)
        back = haversine_miles(39.0, -94.0, 35.0, -100.0)
        self.assertAlmostEqual(there, back, places=9)


class PointToSegmentTests(SimpleTestCase):
    """Inputs are already in the local mile plane, so these are plain geometry."""

    def test_point_on_the_segment_has_no_offset(self):
        distance, along = point_to_segment_miles(5, 0, 0, 0, 10, 0)
        self.assertAlmostEqual(distance, 0.0)
        self.assertAlmostEqual(along, 5.0)

    def test_perpendicular_offset_is_measured(self):
        distance, along = point_to_segment_miles(5, 3, 0, 0, 10, 0)
        self.assertAlmostEqual(distance, 3.0)
        self.assertAlmostEqual(along, 5.0)

    def test_point_beyond_the_end_clamps_to_the_endpoint(self):
        distance, along = point_to_segment_miles(14, 0, 0, 0, 10, 0)
        self.assertAlmostEqual(distance, 4.0)
        self.assertAlmostEqual(along, 10.0)

    def test_point_before_the_start_clamps_to_zero(self):
        distance, along = point_to_segment_miles(-3, 0, 0, 0, 10, 0)
        self.assertAlmostEqual(distance, 3.0)
        self.assertAlmostEqual(along, 0.0)

    def test_degenerate_segment_does_not_divide_by_zero(self):
        distance, along = point_to_segment_miles(3, 4, 0, 0, 0, 0)
        self.assertAlmostEqual(distance, 5.0)
        self.assertAlmostEqual(along, 0.0)
