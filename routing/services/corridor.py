"""Finding the truckstops that sit near a route.

The naive version of this is a nested loop: every truckstop against every
vertex of the route. For Chicago to Dallas that is 6,607 stations against 9,256
vertices, a little over 61 million distance computations on every request, and
it grows with the length of the route.

Two changes remove it without approximating the answer:

* the route is resampled to one point every couple of miles, because a 500-mile
  range problem does not need metre-level resolution to decide which truckstops
  are nearby;
* stations live in a latitude/longitude grid built once per process, so a
  request only opens the cells the route actually passes through.

A truckstop outside the corridor could never have been a candidate, so nothing
is lost by never looking at it.
"""

from __future__ import annotations

import math
import threading
from dataclasses import dataclass

from django.conf import settings

from .geo import (
    MILES_PER_DEGREE_LAT,
    haversine_miles,
    point_to_segment_miles,
    to_local_xy,
)
from .osrm import Route


# ---------------------------------------------------------------------------
# The route, as a measured path
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RoutePoint:
    """A point on the route, with how far along the route it lies."""

    latitude: float
    longitude: float
    mile: float


class RoutePath:
    """A route resampled to a manageable number of measured points.

    ``total_miles`` is the distance the routing provider reported, and the
    per-point mile markers are scaled to agree with it. Summing great-circle
    hops along the polyline slightly undercounts road distance, and letting the
    two disagree would put a fuel stop at "mile 214" on a route the response
    calls 967 miles long.
    """

    __slots__ = ('points', 'total_miles')

    def __init__(self, points: tuple[RoutePoint, ...], total_miles: float):
        self.points = points
        self.total_miles = total_miles

    def __len__(self) -> int:
        return len(self.points)

    @classmethod
    def from_route(cls, route: Route, sample_miles: float | None = None) -> RoutePath:
        if sample_miles is None:
            sample_miles = settings.FUEL_ROUTE['ROUTE_SAMPLE_MILES']

        vertices = route.points
        if len(vertices) < 2:
            raise ValueError('A route needs at least two points.')

        # Walk the polyline once, emitting a sample whenever enough distance
        # has accumulated since the last one. The first and last vertices are
        # always kept, so the path starts at mile 0 and ends at the finish.
        samples: list[tuple[float, float, float]] = [(*vertices[0], 0.0)]
        travelled = 0.0
        since_last_sample = 0.0

        for index in range(1, len(vertices)):
            previous_lat, previous_lon = vertices[index - 1]
            latitude, longitude = vertices[index]
            step = haversine_miles(previous_lat, previous_lon, latitude, longitude)
            travelled += step
            since_last_sample += step

            if since_last_sample >= sample_miles:
                samples.append((latitude, longitude, travelled))
                since_last_sample = 0.0

        last_lat, last_lon = vertices[-1]
        if samples[-1][:2] != (last_lat, last_lon):
            samples.append((last_lat, last_lon, travelled))

        # Scale the geometric mileage onto the provider's reported distance.
        total_miles = route.distance_miles
        scale = (total_miles / travelled) if travelled > 0 else 1.0

        points = tuple(
            RoutePoint(latitude, longitude, mile * scale)
            for latitude, longitude, mile in samples
        )
        return cls(points, total_miles)


# ---------------------------------------------------------------------------
# Stations
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class StationRecord:
    """A truckstop, flattened out of the ORM for the hot path."""

    opis_id: int
    name: str
    address: str
    city: str
    state: str
    price: float
    latitude: float
    longitude: float


@dataclass(frozen=True, slots=True)
class Candidate:
    """A truckstop near the route, placed at a mile marker along it."""

    station: StationRecord
    offset_miles: float  # distance from the start, measured along the route
    detour_miles: float  # how far off the route it sits


class StationIndex:
    """A uniform grid over the stations, built once and shared per process.

    The table is small and never changes at runtime, so holding it in memory
    costs a couple of megabytes and removes the database from the request path
    entirely.
    """

    __slots__ = ('cell_degrees', 'grid', 'count')

    def __init__(self, stations: list[StationRecord], cell_degrees: float):
        self.cell_degrees = cell_degrees
        self.count = len(stations)
        self.grid: dict[tuple[int, int], list[StationRecord]] = {}
        for station in stations:
            self.grid.setdefault(
                self._cell(station.latitude, station.longitude), []
            ).append(station)

    def _cell(self, latitude: float, longitude: float) -> tuple[int, int]:
        return (
            math.floor(latitude / self.cell_degrees),
            math.floor(longitude / self.cell_degrees),
        )

    @classmethod
    def from_database(cls) -> StationIndex:
        from routing.models import FuelStation

        rows = FuelStation.objects.values_list(
            'opis_id',
            'name',
            'address',
            'city',
            'state',
            'price_per_gallon',
            'latitude',
            'longitude',
        )
        stations = [
            StationRecord(
                opis_id=opis_id,
                name=name,
                address=address,
                city=city,
                state=state,
                price=float(price),
                latitude=latitude,
                longitude=longitude,
            )
            for opis_id, name, address, city, state, price, latitude, longitude in rows
        ]
        return cls(stations, settings.FUEL_ROUTE['GRID_CELL_DEGREES'])

    # -- the corridor search ----------------------------------------------

    def find_candidates(
        self, path: RoutePath, max_detour_miles: float | None = None
    ) -> list[Candidate]:
        """Truckstops within ``max_detour_miles`` of the route.

        Returns them ordered by how far along the route they sit, each carrying
        its mile marker and its detour, which is everything the optimizer needs.
        """
        if max_detour_miles is None:
            max_detour_miles = settings.FUEL_ROUTE['MAX_DETOUR_MILES']

        # How many cells out to look. A cell is about 35 miles of latitude, so
        # for a 5-mile corridor this is one ring - but deriving it keeps the
        # search correct if either setting is changed.
        cell_miles = self.cell_degrees * MILES_PER_DEGREE_LAT
        reach = max(1, math.ceil(max_detour_miles / cell_miles))

        # Which route samples fall in which cell. Built per request, and it is
        # what lets a candidate test only the handful of segments near it
        # instead of the whole path.
        samples_by_cell: dict[tuple[int, int], list[int]] = {}
        for index, point in enumerate(path.points):
            samples_by_cell.setdefault(
                self._cell(point.latitude, point.longitude), []
            ).append(index)

        # Gather the candidate stations: everything in a cell the route passes
        # through, or in a neighbouring one.
        candidate_cells: set[tuple[int, int]] = set()
        for cell_lat, cell_lon in samples_by_cell:
            for d_lat in range(-reach, reach + 1):
                for d_lon in range(-reach, reach + 1):
                    candidate_cells.add((cell_lat + d_lat, cell_lon + d_lon))

        candidates: list[Candidate] = []
        points = path.points
        last_index = len(points) - 1

        for cell in candidate_cells:
            for station in self.grid.get(cell, ()):
                nearby = self._nearby_sample_indices(
                    station, samples_by_cell, reach
                )
                if not nearby:
                    continue

                # Project into a local mile plane about the station's own
                # latitude. Over the few miles this test spans the distortion
                # is negligible, and it replaces trigonometry with arithmetic.
                reference = station.latitude
                station_x, station_y = to_local_xy(
                    station.latitude, station.longitude, reference
                )

                best_detour = float('inf')
                best_offset = 0.0

                for index in nearby:
                    for first in (index - 1, index):
                        if first < 0 or first >= last_index:
                            continue
                        start_point = points[first]
                        end_point = points[first + 1]

                        ax, ay = to_local_xy(
                            start_point.latitude, start_point.longitude, reference
                        )
                        bx, by = to_local_xy(
                            end_point.latitude, end_point.longitude, reference
                        )
                        detour, along = point_to_segment_miles(
                            station_x, station_y, ax, ay, bx, by
                        )
                        if detour < best_detour:
                            best_detour = detour
                            best_offset = start_point.mile + along

                if best_detour <= max_detour_miles:
                    candidates.append(
                        Candidate(
                            station=station,
                            offset_miles=min(best_offset, path.total_miles),
                            detour_miles=best_detour,
                        )
                    )

        candidates.sort(key=lambda candidate: candidate.offset_miles)
        return candidates

    def _nearby_sample_indices(
        self,
        station: StationRecord,
        samples_by_cell: dict[tuple[int, int], list[int]],
        reach: int,
    ) -> list[int]:
        """Route sample indices in the cells around a station."""
        cell_lat, cell_lon = self._cell(station.latitude, station.longitude)
        indices: list[int] = []
        for d_lat in range(-reach, reach + 1):
            for d_lon in range(-reach, reach + 1):
                found = samples_by_cell.get((cell_lat + d_lat, cell_lon + d_lon))
                if found:
                    indices.extend(found)
        return indices


# The index is read-only and a few megabytes, so it is built once per process.
_index: StationIndex | None = None
_index_lock = threading.Lock()


def get_station_index() -> StationIndex:
    """The process-wide station index, building it on first use."""
    global _index
    if _index is None:
        with _index_lock:
            if _index is None:
                _index = StationIndex.from_database()
    return _index


def reset_station_index() -> None:
    """Drop the cached index. Used by tests and after a data reload."""
    global _index
    with _index_lock:
        _index = None


# ---------------------------------------------------------------------------
# Thinning
# ---------------------------------------------------------------------------


def thin_candidates(
    candidates: list[Candidate],
    bin_miles: float | None = None,
    per_bin: int | None = None,
) -> list[Candidate]:
    """Keep only the cheapest few truckstops per stretch of route.

    A busy corridor can offer hundreds of truckstops, most of which are a few
    cents dearer than a neighbour a mile away and could never be worth stopping
    at. Bucketing by distance along the route and keeping the cheapest handful
    per bucket leaves the optimizer a small, genuinely distinct set to choose
    from, and keeps the map legible.

    Bins are much shorter than the vehicle's range, so this cannot remove the
    only reachable truckstop in a stretch.
    """
    if bin_miles is None:
        bin_miles = settings.FUEL_ROUTE['CANDIDATE_BIN_MILES']
    if per_bin is None:
        per_bin = settings.FUEL_ROUTE['CANDIDATES_PER_BIN']

    bins: dict[int, list[Candidate]] = {}
    for candidate in candidates:
        bins.setdefault(int(candidate.offset_miles // bin_miles), []).append(candidate)

    kept: list[Candidate] = []
    for bucket in bins.values():
        # Cheapest first; where prices tie, prefer the shorter detour.
        bucket.sort(key=lambda c: (c.station.price, c.detour_miles))
        kept.extend(bucket[:per_bin])

    kept.sort(key=lambda candidate: candidate.offset_miles)
    return kept
