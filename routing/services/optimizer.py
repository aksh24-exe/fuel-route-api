"""Choosing where to fuel, and how much to buy at each stop.

This is the gas station problem. A vehicle travels a fixed route, may refuel at
known points at known prices, and carries a fixed-capacity tank. Minimise the
money spent.

Greedy is provably optimal here, and the rule is short:

* standing at a truckstop, look ahead as far as the tank can carry you. If any
  truckstop in that window is cheaper, buy exactly enough fuel to reach it -
  every extra gallon bought here is a gallon not bought at the better price;
* if nothing ahead is cheaper, this is the best price you will see for a
  tankful, so fill up and drive to the cheapest truckstop within range;
* if the destination itself is within range, buy exactly enough to arrive.

At the start the vehicle is not at a truckstop and its fuel is already paid
for, so the first move is simply to drive to the cheapest truckstop it can
reach.
"""

from __future__ import annotations

from dataclasses import dataclass

from django.conf import settings

from .corridor import Candidate, StationRecord

# Floating point comparisons of distance need a little slack; a thousandth of a
# mile is under two metres and far below the precision of anything upstream.
EPSILON = 1e-3


class InfeasibleRoute(ValueError):
    """No fuelling plan exists, because some stretch is longer than the range.

    Carries the gap so the API can say which part of the route is the problem
    rather than just refusing.
    """

    def __init__(self, message: str, gap_start: float, gap_end: float):
        super().__init__(message)
        self.gap_start = gap_start
        self.gap_end = gap_end


@dataclass(frozen=True, slots=True)
class FuelStop:
    """One fuelling stop in the plan."""

    sequence: int
    station: StationRecord
    offset_miles: float
    detour_miles: float
    gallons: float
    cost: float

    @property
    def price(self) -> float:
        return self.station.price


@dataclass(frozen=True, slots=True)
class FuelPlan:
    """The complete answer: where to stop, what it costs, what it saves."""

    stops: tuple[FuelStop, ...]
    gallons_purchased: float
    total_cost: float
    total_distance_miles: float
    tank_capacity_gallons: float
    start_gallons: float
    # Savings are measured against buying the same fuel at the average price of
    # the truckstops on this corridor - what a driver refuelling without price
    # information would broadly pay.
    baseline_price_per_gallon: float
    baseline_cost: float
    savings: float
    savings_percent: float
    fuel_cost_per_mile: float


def tank_capacity_gallons() -> float:
    """Usable tank, derived from the range and economy in the brief."""
    config = settings.FUEL_ROUTE
    return config['MAX_RANGE_MILES'] / config['MILES_PER_GALLON']


def plan_fuel_stops(
    candidates: list[Candidate],
    total_distance_miles: float,
    start_gallons: float | None = None,
    corridor_prices: list[float] | None = None,
) -> FuelPlan:
    """Work out the cheapest way to cover ``total_distance_miles``.

    ``candidates`` must be sorted by ``offset_miles``. ``start_gallons``
    defaults to a full tank; the brief does not specify the starting fuel, and
    it changes every figure in the result, so it is a parameter rather than a
    buried assumption.
    """
    config = settings.FUEL_ROUTE
    miles_per_gallon = config['MILES_PER_GALLON']
    capacity = tank_capacity_gallons()

    if start_gallons is None:
        configured = config.get('START_GALLONS')
        start_gallons = capacity if configured is None else float(configured)
    start_gallons = max(0.0, min(float(start_gallons), capacity))

    stations = [c for c in candidates if 0.0 <= c.offset_miles <= total_distance_miles]
    _check_feasible(stations, total_distance_miles, start_gallons, miles_per_gallon, capacity)

    stops: list[FuelStop] = []
    position = 0.0
    fuel = start_gallons
    # The candidate the vehicle is standing at, or None before the first stop.
    # Held as the object itself rather than looked up by mile marker, because
    # several truckstops in one town share a mile marker to the tenth.
    here: Candidate | None = None

    while True:
        remaining = total_distance_miles - position
        if fuel * miles_per_gallon >= remaining - EPSILON:
            break  # The destination is already in range; buy nothing more.

        if here is None:
            # Not at a truckstop yet, and the fuel already in the tank is a sunk
            # cost, so the only question is which reachable truckstop is
            # cheapest. Ties go to the nearer one.
            reach = position + fuel * miles_per_gallon
            # A truckstop at the origin itself counts here, which is the only
            # thing that makes an empty tank workable: with no fuel the vehicle
            # cannot move, so its first purchase has to happen where it stands.
            reachable = [
                station
                for station in stations
                if position - EPSILON <= station.offset_miles <= reach + EPSILON
            ]
            if not reachable:
                raise InfeasibleRoute(
                    f'No truckstop lies within {reach - position:.0f} miles after '
                    f'mile {position:.0f}, which is as far as the starting fuel '
                    'reaches.',
                    gap_start=position,
                    gap_end=total_distance_miles,
                )
            here = min(
                reachable, key=lambda c: (c.station.price, c.offset_miles)
            )
            fuel -= (here.offset_miles - position) / miles_per_gallon
            position = here.offset_miles
            continue

        # Standing at a truckstop, so the window is a full tank rather than
        # whatever is left in it.
        current_price = here.station.price
        full_reach = position + capacity * miles_per_gallon
        cheaper = [
            station
            for station in stations
            if position + EPSILON < station.offset_miles <= full_reach + EPSILON
            and station.station.price < current_price - 1e-9
        ]

        if cheaper:
            # Buy exactly enough to reach the first cheaper truckstop: every
            # extra gallon bought here is one not bought at the better price.
            target = min(cheaper, key=lambda c: c.offset_miles)
            needed = (target.offset_miles - position) / miles_per_gallon
        else:
            # Nothing cheaper within a tankful, so this is the best price we
            # will see - fill up, but never buy more than finishes the journey.
            target = None
            needed = min(
                capacity, (total_distance_miles - position) / miles_per_gallon
            )

        purchase = max(0.0, min(needed - fuel, capacity - fuel))
        if purchase > EPSILON:
            stops.append(
                FuelStop(
                    sequence=len(stops) + 1,
                    station=here.station,
                    offset_miles=here.offset_miles,
                    detour_miles=here.detour_miles,
                    gallons=purchase,
                    cost=purchase * current_price,
                )
            )
            fuel += purchase

        if target is None:
            reach = position + fuel * miles_per_gallon
            if reach >= total_distance_miles - EPSILON:
                break
            onward = [
                station
                for station in stations
                if position + EPSILON < station.offset_miles <= reach + EPSILON
            ]
            if not onward:
                raise InfeasibleRoute(
                    f'The vehicle cannot get past mile {position:.0f}; the next '
                    'truckstop is beyond a full tank.',
                    gap_start=position,
                    gap_end=total_distance_miles,
                )
            target = min(onward, key=lambda c: (c.station.price, c.offset_miles))

        fuel -= (target.offset_miles - position) / miles_per_gallon
        position = target.offset_miles
        here = target

    return _summarise(
        stops=stops,
        total_distance_miles=total_distance_miles,
        capacity=capacity,
        start_gallons=start_gallons,
        corridor_prices=corridor_prices
        or [candidate.station.price for candidate in candidates],
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _check_feasible(
    stations: list[Candidate],
    total_distance_miles: float,
    start_gallons: float,
    miles_per_gallon: float,
    capacity: float,
) -> None:
    """Fail early, and say where, when some stretch is longer than the range.

    Discovering this halfway through the greedy pass would produce a confusing
    message about an arbitrary mile marker; checking the gaps up front lets the
    API name the stretch that is actually impossible.
    """
    start_range = start_gallons * miles_per_gallon
    full_range = capacity * miles_per_gallon

    if total_distance_miles <= start_range + EPSILON:
        return  # Reachable without stopping at all.

    if not stations:
        raise InfeasibleRoute(
            'No truckstops were found near this route, and it is longer than '
            'the vehicle can travel on its starting fuel.',
            gap_start=0.0,
            gap_end=total_distance_miles,
        )

    if stations[0].offset_miles > start_range + EPSILON:
        raise InfeasibleRoute(
            f'The first truckstop on the route is at mile '
            f'{stations[0].offset_miles:.0f}, beyond the '
            f'{start_range:.0f} miles the starting fuel allows.',
            gap_start=0.0,
            gap_end=stations[0].offset_miles,
        )

    for previous, current in zip(stations, stations[1:]):
        gap = current.offset_miles - previous.offset_miles
        if gap > full_range + EPSILON:
            raise InfeasibleRoute(
                f'There is a {gap:.0f}-mile stretch with no truckstop between '
                f'mile {previous.offset_miles:.0f} and mile '
                f'{current.offset_miles:.0f}, longer than the vehicle\'s '
                f'{full_range:.0f}-mile range.',
                gap_start=previous.offset_miles,
                gap_end=current.offset_miles,
            )

    final_gap = total_distance_miles - stations[-1].offset_miles
    if final_gap > full_range + EPSILON:
        raise InfeasibleRoute(
            f'The last truckstop is at mile {stations[-1].offset_miles:.0f}, '
            f'leaving {final_gap:.0f} miles to the destination on a '
            f'{full_range:.0f}-mile range.',
            gap_start=stations[-1].offset_miles,
            gap_end=total_distance_miles,
        )


def _summarise(
    stops: list[FuelStop],
    total_distance_miles: float,
    capacity: float,
    start_gallons: float,
    corridor_prices: list[float],
) -> FuelPlan:
    gallons = sum(stop.gallons for stop in stops)
    total_cost = sum(stop.cost for stop in stops)

    # The baseline answers "what would this have cost without price
    # intelligence": the same gallons, at the average price on this corridor.
    # It is deliberately not the worst price available, which would flatter the
    # result.
    baseline_price = (
        sum(corridor_prices) / len(corridor_prices) if corridor_prices else 0.0
    )
    baseline_cost = gallons * baseline_price
    savings = baseline_cost - total_cost

    return FuelPlan(
        stops=tuple(stops),
        gallons_purchased=gallons,
        total_cost=total_cost,
        total_distance_miles=total_distance_miles,
        tank_capacity_gallons=capacity,
        start_gallons=start_gallons,
        baseline_price_per_gallon=baseline_price,
        baseline_cost=baseline_cost,
        savings=savings,
        savings_percent=(savings / baseline_cost * 100.0) if baseline_cost else 0.0,
        fuel_cost_per_mile=(
            total_cost / total_distance_miles if total_distance_miles else 0.0
        ),
    )
