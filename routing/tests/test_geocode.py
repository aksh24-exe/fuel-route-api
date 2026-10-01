"""Offline geocoding: name normalisation and query parsing."""

from django.test import SimpleTestCase

from routing.services.geocode import (
    GeocodeError,
    geocode,
    get_place_index,
    normalize_place,
    normalize_state,
    place_keys,
    primary_key,
    search_places,
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


class PlaceSearchTests(SimpleTestCase):
    """The picker searches every distinct place, not a short list of big cities."""

    def test_the_index_keeps_one_spelled_out_name_per_place(self):
        index = get_place_index()
        labels = [place.label for place in index.entries]
        self.assertGreater(index.row_count, 190_000)
        self.assertGreater(len(labels), 100_000)
        self.assertLess(len(labels), index.row_count)
        self.assertEqual(labels.count('Chicago, IL'), 1)
        self.assertEqual(labels.count('New York City, NY'), 1)
        self.assertIn('Fort Worth, TX', labels)
        self.assertNotIn('Ft Worth, TX', labels)
        self.assertIn('Big Cabin, OK', labels)

    def test_a_typed_query_reaches_a_small_town_and_a_spelling_variant(self):
        self.assertIn('Big Cabin, OK', search_places('big cabin')['places'])
        fort_worth = search_places('ft worth')['places']
        self.assertIn('Fort Worth, TX', fort_worth)
        self.assertNotIn('Ft Worth, TX', fort_worth)

    def test_an_empty_query_starts_with_the_largest_city(self):
        places = search_places('')['places']
        self.assertEqual(places[0], 'New York City, NY')

    def test_a_picker_label_is_a_valid_route_query(self):
        for label in ('Chicago, IL', 'Dallas, TX', 'New York City, NY', 'Fort Worth, TX', 'Big Cabin, OK'):
            place = geocode(label)
            self.assertEqual(place.source, 'gazetteer')
