"""Offline geocoding: name normalisation and query parsing."""

from django.test import SimpleTestCase

from routing.services.geocode import (
    GeocodeError,
    geocode,
    normalize_place,
    normalize_state,
    place_keys,
    primary_key,
)


class NormalisationTests(SimpleTestCase):
    def test_punctuation_and_case_are_stripped(self):
        self.assertEqual(normalize_place('St. Louis'), 'ST LOUIS')
        self.assertEqual(normalize_place("O'Fallon"), 'OFALLON')
        self.assertEqual(normalize_place('Winston-Salem'), 'WINSTON SALEM')

    def test_abbreviated_and_spelled_out_names_share_a_key(self):
        # This is the property the gazetteer relies on: both spellings of a
        # place must produce a key the other one also produces.
        self.assertTrue(place_keys('ST. LOUIS') & place_keys('Saint Louis'))
        self.assertTrue(place_keys('MT. VERNON') & place_keys('Mount Vernon'))
        self.assertTrue(place_keys('Ft. Worth') & place_keys('Fort Worth'))

    def test_spaced_particles_reach_the_glued_spelling(self):
        self.assertIn('MCCALLA', place_keys('Mc Calla'))
        self.assertIn('DEFOREST', place_keys('De Forest'))
        self.assertIn('LAPLACE', place_keys('La Place'))
        self.assertIn('BOIS DARC', place_keys('Bois D Arc'))

    def test_formal_city_names_gain_their_short_form(self):
        self.assertIn('NEW YORK', place_keys('New York City'))
        self.assertIn('BRONX', place_keys('The Bronx'))

    def test_directional_abbreviations_expand(self):
        self.assertIn('SOUTH COFFEYVILLE', place_keys('S Coffeyville'))

    def test_primary_key_is_the_literal_spelling_only(self):
        self.assertEqual(primary_key('Jefferson City'), 'JEFFERSON CITY')
        self.assertNotIn('JEFFERSON', {primary_key('Jefferson City')})

    def test_states_resolve_from_code_or_name(self):
        self.assertEqual(normalize_state('IL'), 'IL')
        self.assertEqual(normalize_state('Illinois'), 'IL')
        self.assertEqual(normalize_state('texas'), 'TX')


class GeocodeQueryTests(SimpleTestCase):
    """These read the gazetteer committed to the repository."""

    def test_city_and_state_code(self):
        place = geocode('Chicago, IL')
        self.assertAlmostEqual(place.latitude, 41.85, delta=0.2)
        self.assertAlmostEqual(place.longitude, -87.65, delta=0.2)
        self.assertEqual(place.source, 'gazetteer')

    def test_city_and_full_state_name(self):
        place = geocode('Dallas, Texas')
        self.assertAlmostEqual(place.latitude, 32.78, delta=0.2)

    def test_bare_city_picks_the_largest_match(self):
        place = geocode('Chicago')
        self.assertAlmostEqual(place.latitude, 41.85, delta=0.2)

    def test_formal_city_name_resolves(self):
        place = geocode('New York, NY')
        self.assertAlmostEqual(place.latitude, 40.71, delta=0.2)

    def test_raw_coordinates_bypass_the_gazetteer(self):
        place = geocode('32.7767,-96.7970')
        self.assertEqual(place.source, 'coordinates')
        self.assertAlmostEqual(place.latitude, 32.7767)

    def test_no_network_is_used(self):
        # A guard rather than a formality: the whole design rests on geocoding
        # never reaching out, so a future change that adds a call should fail.
        import requests

        def explode(*args, **kwargs):  # pragma: no cover - must not run
            raise AssertionError('geocoding must not make network calls')

        original = requests.get
        requests.get = explode
        try:
            geocode('Effingham, IL')
        finally:
            requests.get = original


class GeocodeErrorTests(SimpleTestCase):
    def test_unknown_state_is_rejected_by_name(self):
        with self.assertRaises(GeocodeError) as caught:
            geocode('Nowhere, ZZ')
        self.assertIn('not a US state', str(caught.exception))

    def test_canadian_province_is_reported_as_out_of_scope(self):
        with self.assertRaises(GeocodeError) as caught:
            geocode('Toronto, ON')
        self.assertIn('Canada', str(caught.exception))

    def test_empty_query_names_the_accepted_forms(self):
        with self.assertRaises(GeocodeError) as caught:
            geocode('')
        self.assertIn('Accepted forms', str(caught.exception))

    def test_out_of_range_latitude_is_rejected(self):
        with self.assertRaises(GeocodeError):
            geocode('120.0,-96.0')
