"""Offline geocoding against a gazetteer bundled with the repository.

The supplied fuel price file gives a city and a state for each truckstop but no
coordinates, and the corridor search cannot run without them. Geocoding 6,738
addresses through a web service would take hours at the rate limits free
geocoders impose, and would make the API depend on a second upstream.

So this module reads a compact gazetteer built once from the GeoNames US export
(public domain) and committed to the repository. Both resolving a truckstop at
import time and resolving the caller's start and finish at request time are
dictionary lookups. No network access happens here, ever.

The cost of that choice is honest and documented: a truckstop resolves to the
centre of its city, not to its interstate exit. Against a 500-mile range the
resulting error of a few miles does not change which stops are optimal.
"""

from __future__ import annotations

import bisect
import csv
import gzip
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple

from django.conf import settings

# ---------------------------------------------------------------------------
# States
# ---------------------------------------------------------------------------

US_STATES = {
    'AL': 'ALABAMA', 'AK': 'ALASKA', 'AZ': 'ARIZONA', 'AR': 'ARKANSAS',
    'CA': 'CALIFORNIA', 'CO': 'COLORADO', 'CT': 'CONNECTICUT',
    'DE': 'DELAWARE', 'DC': 'DISTRICT OF COLUMBIA', 'FL': 'FLORIDA',
    'GA': 'GEORGIA', 'HI': 'HAWAII', 'ID': 'IDAHO', 'IL': 'ILLINOIS',
    'IN': 'INDIANA', 'IA': 'IOWA', 'KS': 'KANSAS', 'KY': 'KENTUCKY',
    'LA': 'LOUISIANA', 'ME': 'MAINE', 'MD': 'MARYLAND',
    'MA': 'MASSACHUSETTS', 'MI': 'MICHIGAN', 'MN': 'MINNESOTA',
    'MS': 'MISSISSIPPI', 'MO': 'MISSOURI', 'MT': 'MONTANA',
    'NE': 'NEBRASKA', 'NV': 'NEVADA', 'NH': 'NEW HAMPSHIRE',
    'NJ': 'NEW JERSEY', 'NM': 'NEW MEXICO', 'NY': 'NEW YORK',
    'NC': 'NORTH CAROLINA', 'ND': 'NORTH DAKOTA', 'OH': 'OHIO',
    'OK': 'OKLAHOMA', 'OR': 'OREGON', 'PA': 'PENNSYLVANIA',
    'RI': 'RHODE ISLAND', 'SC': 'SOUTH CAROLINA', 'SD': 'SOUTH DAKOTA',
    'TN': 'TENNESSEE', 'TX': 'TEXAS', 'UT': 'UTAH', 'VT': 'VERMONT',
    'VA': 'VIRGINIA', 'WA': 'WASHINGTON', 'WV': 'WEST VIRGINIA',
    'WI': 'WISCONSIN', 'WY': 'WYOMING',
    # Territories, included so that a row referring to one is recognised
    # rather than silently treated as a typo.
    'PR': 'PUERTO RICO', 'VI': 'VIRGIN ISLANDS', 'GU': 'GUAM',
    'AS': 'AMERICAN SAMOA', 'MP': 'NORTHERN MARIANA ISLANDS',
}

STATE_NAME_TO_CODE = {name: code for code, name in US_STATES.items()}

# Provinces appear in the supplied price file. The brief scopes the problem to
# routes within the USA, so these rows are out of scope; naming them lets the
# loader report "skipped, not in the USA" instead of "failed to geocode".
CA_PROVINCES = {
    'AB': 'ALBERTA', 'BC': 'BRITISH COLUMBIA', 'MB': 'MANITOBA',
    'NB': 'NEW BRUNSWICK', 'NL': 'NEWFOUNDLAND AND LABRADOR',
    'NS': 'NOVA SCOTIA', 'NT': 'NORTHWEST TERRITORIES', 'NU': 'NUNAVUT',
    'ON': 'ONTARIO', 'PE': 'PRINCE EDWARD ISLAND', 'QC': 'QUEBEC',
    'SK': 'SASKATCHEWAN', 'YT': 'YUKON',
}


# ---------------------------------------------------------------------------
# Name normalisation
# ---------------------------------------------------------------------------
#
# The price file and GeoNames spell the same town differently often enough to
# matter: "ST. LOUIS" against "Saint Louis", "MT. VERNON" against "Mount
# Vernon", "WINSTON-SALEM" against "Winston Salem". Rather than guess at lookup
# time, both sides run through the same function and every spelling variant is
# written into the gazetteer, so a hit is an exact dictionary match.

_PUNCTUATION = re.compile(r"[.’']")
_NON_ALNUM = re.compile(r"[^A-Z0-9]+")

# Tokens that one source spells out and the other abbreviates. Applied at any
# position, not just the start, because "Sault Sainte Marie" is written
# "Sault Ste. Marie" by GeoNames.
_TOKEN_SWAPS = (
    ('SAINTE', 'STE'),
    ('SAINT', 'ST'),
    ('MOUNT', 'MT'),
    ('FORT', 'FT'),
    ('NORTH', 'N'),
    ('SOUTH', 'S'),
    ('EAST', 'E'),
    ('WEST', 'W'),
)

# Name particles the price file separates with a space and GeoNames glues to the
# following word: "Mc Calla" against "McCalla", "De Forest" against "DeForest".
# Any single letter behaves the same way, which is what rescues "Bois D Arc"
# from GeoNames' "Bois D'Arc".
_GLUED_PARTICLES = frozenset({'MC', 'MAC', 'DE', 'DU', 'DES', 'LA', 'LE', 'VAN', 'VON'})


def normalize_place(name: str) -> str:
    """Canonical form of a place name: upper case, alphanumerics and spaces."""
    upper = _PUNCTUATION.sub('', name.upper())
    return _NON_ALNUM.sub(' ', upper).strip()


def place_keys(name: str) -> set[str]:
    """Every spelling of ``name`` that should resolve to the same place.

    Both the gazetteer build and every lookup run through this function, so a
    hit is always an exact dictionary match rather than a fuzzy comparison. Only
    the spaced-out form needs to generate the glued one: the price file is the
    side that writes "Mc Calla", and GeoNames is the side that already holds
    "McCalla", so the two meet on the glued key without also having to split
    every name that merely begins with those letters.
    """
    base = normalize_place(name)
    if not base:
        return set()

    keys = {base}
    tokens = base.split(' ')

    # Abbreviation swaps, one token at a time. Every real case in the supplied
    # data involves a single abbreviated token, so the full cross product would
    # only add keys nothing ever asks for.
    for index, token in enumerate(tokens):
        for spelled_out, abbreviated in _TOKEN_SWAPS:
            for source, target in ((spelled_out, abbreviated), (abbreviated, spelled_out)):
                if token == source:
                    variant = list(tokens)
                    variant[index] = target
                    keys.add(' '.join(variant))

    # Glue a name particle to the word after it.
    for index, token in enumerate(tokens[:-1]):
        if token in _GLUED_PARTICLES or len(token) == 1:
            variant = list(tokens)
            variant[index : index + 2] = [token + tokens[index + 1]]
            keys.add(' '.join(variant))

    # GeoNames records the largest cities under their formal names: "New York
    # City", "The Bronx". Nobody types those, so index the short forms too.
    for key in tuple(keys):
        words = key.split(' ')
        if len(words) > 1 and words[-1] == 'CITY':
            keys.add(' '.join(words[:-1]))
        if len(words) > 1 and words[0] == 'THE':
            keys.add(' '.join(words[1:]))

    return keys


def primary_key(name: str) -> str:
    """The one key that *is* this place, as opposed to a spelling variant.

    The build uses it to make sure a variant never displaces a real town: the
    short form of "Jefferson City" must not take the slot belonging to the
    actual village of Jefferson in the same state.
    """
    return normalize_place(name)


def normalize_state(value: str) -> str:
    """Two-letter code for a state given either its code or its full name."""
    token = normalize_place(value)
    if len(token) == 2 and token in US_STATES:
        return token
    if len(token) == 2 and token in CA_PROVINCES:
        return token
    return STATE_NAME_TO_CODE.get(token, token)


# ---------------------------------------------------------------------------
# Results and errors
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Place:
    """A resolved location."""

    latitude: float
    longitude: float
    label: str
    source: str  # 'gazetteer' or 'coordinates'


class GeocodeError(ValueError):
    """Raised when a location cannot be resolved.

    Carries a message aimed at whoever typed the query, listing the forms that
    do work, rather than a stack trace.
    """


class GazetteerMissing(RuntimeError):
    """The gazetteer file has not been generated yet."""


# ---------------------------------------------------------------------------
# The gazetteer
# ---------------------------------------------------------------------------


class Gazetteer:
    """In-memory place index, loaded once from the committed gazetteer file.

    Two indexes are kept. ``by_state`` answers the precise question the loader
    asks ("Effingham in Illinois"). ``by_name`` answers a bare city name by
    returning the most populous match, which is what someone typing "Dallas"
    with no state almost certainly means.
    """

    __slots__ = ('by_state', 'by_name', 'path')

    def __init__(self, path: Path):
        self.path = path
        self.by_state: dict[tuple[str, str], tuple[float, float]] = {}
        self.by_name: dict[str, tuple[float, float, str, int]] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            raise GazetteerMissing(
                f'Gazetteer not found at {self.path}. '
                'Generate it with:  uv run manage.py build_gazetteer'
            )

        with gzip.open(self.path, 'rt', encoding='utf-8', newline='') as handle:
            for row in csv.reader(handle):
                if len(row) != 5:
                    continue
                key, state, lat_text, lon_text, population_text = row
                latitude, longitude = float(lat_text), float(lon_text)

                self.by_state[(key, state)] = (latitude, longitude)

                population = int(population_text)
                existing = self.by_name.get(key)
                if existing is None or population > existing[3]:
                    self.by_name[key] = (latitude, longitude, state, population)

    def __len__(self) -> int:
        return len(self.by_state)

    # -- lookups ----------------------------------------------------------

    def lookup(self, city: str, state: str) -> tuple[float, float] | None:
        """Coordinates for a city in a known state, or ``None``."""
        state_code = normalize_state(state)
        # The literal spelling first, so an exact name always beats a variant.
        exact = self.by_state.get((primary_key(city), state_code))
        if exact is not None:
            return exact
        for key in place_keys(city):
            found = self.by_state.get((key, state_code))
            if found is not None:
                return found
        return None

    def lookup_city(self, city: str) -> tuple[float, float, str] | None:
        """Coordinates for a bare city name, preferring the largest match."""
        best: tuple[float, float, str, int] | None = None
        for key in place_keys(city):
            candidate = self.by_name.get(key)
            if candidate is not None and (best is None or candidate[3] > best[3]):
                best = candidate
        if best is None:
            return None
        return best[0], best[1], best[2]


# The gazetteer is a few megabytes and entirely read-only, so it is loaded once
# per process and shared. The lock keeps two simultaneous first requests from
# both paying to parse it.
_gazetteer: Gazetteer | None = None
_gazetteer_lock = threading.Lock()
_place_index: PlaceIndex | None = None

# How many matches the city picker shows at once. The index itself holds every
# place; the cap only keeps the open list short enough to choose from.
PICKER_RESULT_LIMIT = 20


def get_gazetteer() -> Gazetteer:
    """The process-wide gazetteer, loading it on first use."""
    global _gazetteer
    if _gazetteer is None:
        with _gazetteer_lock:
            if _gazetteer is None:
                _gazetteer = Gazetteer(Path(settings.FUEL_ROUTE['GAZETTEER_PATH']))
    return _gazetteer


def _display_name(key: str) -> str:
    """Turn ``SAINT LOUIS`` into ``Saint Louis``, which the geocoder still accepts."""
    return ' '.join(part.capitalize() for part in key.split())


class _Place(NamedTuple):
    label: str
    name_key: str
    state: str
    population: int


class PlaceIndex:
    """Every distinct place in the gazetteer, with prefix indexes for search.

    Spelling variants that share a coordinate collapse to the longest name,
    which is the spelled-out form ("Fort Worth", not "Ft Worth"). ``row_count``
    is the raw gazetteer size, including those variants.
    """

    __slots__ = ('entries', 'row_count', 'name_keys', 'word_keys', 'by_population')

    def __init__(
        self,
        entries: tuple[_Place, ...],
        row_count: int,
        name_keys: list[tuple[str, int]],
        word_keys: list[tuple[str, int]],
        by_population: list[int],
    ):
        self.entries = entries
        self.row_count = row_count
        self.name_keys = name_keys
        self.word_keys = word_keys
        self.by_population = by_population


def _load_place_index() -> PlaceIndex:
    path = Path(settings.FUEL_ROUTE['GAZETTEER_PATH'])
    if not path.exists():
        raise GazetteerMissing(
            f'Gazetteer not found at {path}. '
            'Generate it with:  uv run manage.py build_gazetteer'
        )

    # (state, rounded lat, rounded lon) → (name key, population)
    grouped: dict[tuple[str, float, float], tuple[str, int]] = {}
    row_count = 0
    with gzip.open(path, 'rt', encoding='utf-8', newline='') as handle:
        for row in csv.reader(handle):
            if len(row) != 5:
                continue
            row_count += 1
            key, state, lat_text, lon_text, population_text = row
            population = int(population_text)
            coord = (state, round(float(lat_text), 3), round(float(lon_text), 3))
            current = grouped.get(coord)
            if (
                current is None
                or len(key) > len(current[0])
                or (len(key) == len(current[0]) and key < current[0])
            ):
                grouped[coord] = (key, population)

    entries = tuple(
        _Place(f'{_display_name(name)}, {state}', name, state, population)
        for (state, _lat, _lon), (name, population) in grouped.items()
    )
    name_keys = sorted((place.name_key, index) for index, place in enumerate(entries))
    word_keys = sorted(
        (word, index)
        for index, place in enumerate(entries)
        for word in place.name_key.split(' ')
        if word != place.name_key
    )
    by_population = sorted(
        range(len(entries)),
        key=lambda index: entries[index].population,
        reverse=True,
    )
    return PlaceIndex(entries, row_count, name_keys, word_keys, by_population)


def get_place_index() -> PlaceIndex:
    """The process-wide place index, built on first use."""
    global _place_index
    if _place_index is None:
        with _gazetteer_lock:
            if _place_index is None:
                _place_index = _load_place_index()
    return _place_index


def _prefix_span(pairs: list[tuple[str, int]], prefix: str) -> tuple[int, int]:
    """Slice of ``pairs`` whose key starts with ``prefix``. Pairs are sorted."""
    start = bisect.bisect_left(pairs, (prefix,))
    stop = start
    size = len(pairs)
    while stop < size and pairs[stop][0].startswith(prefix):
        stop += 1
    return start, stop


def _states_for(token: str) -> set[str] | None:
    """State codes the user is narrowing to, or ``None`` when they did not."""
    if not token:
        return None
    if token in US_STATES:
        return {token}
    code = STATE_NAME_TO_CODE.get(token)
    if code:
        return {code}
    return {
        state_code
        for state_code, name in US_STATES.items()
        if state_code.startswith(token) or name.startswith(token)
    }


def search_places(query: str, limit: int = PICKER_RESULT_LIMIT) -> dict:
    """Places matching ``query``, drawn from the whole gazetteer.

    An empty query returns the most populous places, so the list is never
    blank. A typed query matches the start of the name or of any word in it,
    and ``"Ft Worth"`` reaches ``"Fort Worth"`` through the same spelling
    variants the geocoder uses. Results are largest first.
    """
    index = get_place_index()
    limit = max(1, min(limit, 50))
    text = (query or '').strip()
    city_text, _, state_text = text.partition(',')
    allowed_states = _states_for(normalize_place(state_text))

    if not city_text.strip():
        chosen = [
            index.entries[place_index]
            for place_index in index.by_population
            if allowed_states is None or index.entries[place_index].state in allowed_states
        ]
        shown = chosen[:limit]
        return {
            'count': len(index.entries),
            'rows': index.row_count,
            'matched': len(chosen),
            'places': [place.label for place in shown],
        }

    queries = place_keys(city_text) or {normalize_place(city_text)}
    best_rank: dict[int, int] = {}

    def consider(pairs: list[tuple[str, int]], prefix: str, rank: int) -> None:
        if not prefix:
            return
        start, stop = _prefix_span(pairs, prefix)
        for position in range(start, stop):
            place_index = pairs[position][1]
            place = index.entries[place_index]
            if allowed_states is not None and place.state not in allowed_states:
                continue
            current = best_rank.get(place_index)
            if current is None or rank < current:
                best_rank[place_index] = rank

    for prefix in queries:
        consider(index.name_keys, prefix, 0)
        consider(index.word_keys, prefix, 1)

    ranked = sorted(
        best_rank,
        key=lambda place_index: (
            best_rank[place_index],
            -index.entries[place_index].population,
            index.entries[place_index].label,
        ),
    )
    shown = ranked[:limit]
    return {
        'count': len(index.entries),
        'rows': index.row_count,
        'matched': len(ranked),
        'places': [index.entries[place_index].label for place_index in shown],
    }


def reset_gazetteer() -> None:
    """Drop the cached gazetteer. Used by tests and by the build command."""
    global _gazetteer, _place_index
    with _gazetteer_lock:
        _gazetteer = None
        _place_index = None


# ---------------------------------------------------------------------------
# Query parsing
# ---------------------------------------------------------------------------

_COORDINATE_PAIR = re.compile(
    r'^\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*$'
)

_ACCEPTED_FORMS = (
    'Accepted forms: "Dallas, TX", "Dallas, Texas", "Dallas", '
    'or a "latitude,longitude" pair such as "32.7767,-96.7970".'
)


def geocode(query: str) -> Place:
    """Resolve a user-supplied location to a :class:`Place`.

    Recognises a raw coordinate pair, "City, ST", "City, State Name", and a
    bare city name. Raises :class:`GeocodeError` with a message naming the
    accepted forms when nothing matches.
    """
    text = (query or '').strip()
    if not text:
        raise GeocodeError(f'No location given. {_ACCEPTED_FORMS}')

    # A raw coordinate pair, which lets a caller bypass the gazetteer entirely.
    coordinate_match = _COORDINATE_PAIR.match(text)
    if coordinate_match:
        latitude = float(coordinate_match.group(1))
        longitude = float(coordinate_match.group(2))
        if not -90.0 <= latitude <= 90.0:
            raise GeocodeError(
                f'Latitude {latitude} is outside -90..90. {_ACCEPTED_FORMS}'
            )
        if not -180.0 <= longitude <= 180.0:
            raise GeocodeError(
                f'Longitude {longitude} is outside -180..180. {_ACCEPTED_FORMS}'
            )
        return Place(latitude, longitude, f'{latitude},{longitude}', 'coordinates')

    gazetteer = get_gazetteer()
    parts = [piece.strip() for piece in text.split(',') if piece.strip()]

    # "City, ST" or "City, State Name"
    if len(parts) >= 2:
        city, state_text = parts[0], parts[-1]
        state_code = normalize_state(state_text)

        if state_code in CA_PROVINCES:
            raise GeocodeError(
                f'"{text}" is in Canada. This API routes between locations '
                'within the USA, as the brief specifies.'
            )

        found = gazetteer.lookup(city, state_code)
        if found is not None:
            return Place(found[0], found[1], f'{city}, {state_code}', 'gazetteer')

        if state_code not in US_STATES:
            raise GeocodeError(
                f'"{state_text}" is not a US state. {_ACCEPTED_FORMS}'
            )
        raise GeocodeError(
            f'No place called "{city}" found in {state_code}. '
            'Check the spelling, or pass coordinates directly.'
        )

    # A bare city name: take the most populous match and say which one it was.
    found_city = gazetteer.lookup_city(text)
    if found_city is not None:
        latitude, longitude, state_code = found_city
        return Place(latitude, longitude, f'{text}, {state_code}', 'gazetteer')

    raise GeocodeError(f'Could not resolve "{text}". {_ACCEPTED_FORMS}')
