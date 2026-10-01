"""Import the OPIS fuel price file into the FuelStation table.

Geocoding happens here, against the gazetteer committed to the repository, so
this command makes no network calls at all. That is the point of the two-phase
design: everything expensive is paid once, before the server starts, and a
request then costs one call to the routing API and nothing else.

    uv run manage.py load_fuel_prices

The command is idempotent - running it twice leaves the same table - so it is
safe to re-run after refreshing the price file.
"""

from __future__ import annotations

import csv
from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from routing.models import FuelStation
from routing.services.geocode import (
    CA_PROVINCES,
    GazetteerMissing,
    US_STATES,
    get_gazetteer,
)

PRICE_PLACES = Decimal('0.0001')


@dataclass
class Truckstop:
    """Accumulator for the rows sharing one OPIS Truckstop ID."""

    opis_id: int
    name: str = ''
    address: str = ''
    city: str = ''
    state: str = ''
    rack_id: int | None = None
    prices: list[Decimal] = field(default_factory=list)

    def absorb(self, row: dict[str, str]) -> None:
        # The feed spells one truckstop several ways - "PILOT TRAVEL CENTER
        # #1243" and "PILOT #1243" share an ID. The longer form carries more
        # information, so it wins.
        name = (row.get('Truckstop Name') or '').strip()
        if len(name) > len(self.name):
            self.name = name

        address = (row.get('Address') or '').strip()
        if len(address) > len(self.address):
            self.address = address

        if not self.city:
            self.city = (row.get('City') or '').strip()
        if not self.state:
            self.state = (row.get('State') or '').strip().upper()

        if self.rack_id is None:
            try:
                self.rack_id = int((row.get('Rack ID') or '').strip())
            except ValueError:
                self.rack_id = None

        try:
            self.prices.append(Decimal((row.get('Retail Price') or '').strip()))
        except Exception:
            pass

    @property
    def mean_price(self) -> Decimal:
        """Mean of the observations, to four decimal places.

        The mean, not the minimum: several rows for one truckstop are repeated
        observations of a moving price, and the file carries no date to pick a
        current one. Taking the cheapest would quietly bias every route.
        """
        total = sum(self.prices, Decimal('0'))
        return (total / len(self.prices)).quantize(
            PRICE_PLACES, rounding=ROUND_HALF_UP
        )


class Command(BaseCommand):
    help = 'Load the OPIS fuel price CSV, geocoding every truckstop offline.'

    def add_arguments(self, parser):
        parser.add_argument(
            'csv_path',
            nargs='?',
            default=None,
            help='Path to the price CSV (default: the settings value).',
        )

    def handle(self, *args, **options):
        csv_path = Path(options['csv_path'] or settings.FUEL_ROUTE['FUEL_PRICES_CSV'])
        if not csv_path.exists():
            raise CommandError(f'Price file not found: {csv_path}')

        try:
            gazetteer = get_gazetteer()
        except GazetteerMissing as error:
            raise CommandError(str(error)) from error

        self.stdout.write(f'Reading {csv_path} ...')
        truckstops, rows_read = self._group_rows(csv_path)

        self.stdout.write(f'Geocoding {len(truckstops):,} truckstops offline ...')
        stations, skipped_foreign, unresolved = self._build_stations(
            truckstops, gazetteer
        )

        if not stations:
            raise CommandError('Nothing to load - every row was skipped.')

        with transaction.atomic():
            FuelStation.objects.all().delete()
            FuelStation.objects.bulk_create(stations, batch_size=1000)

        self._report(rows_read, truckstops, stations, skipped_foreign, unresolved)

    # -- reading ----------------------------------------------------------

    def _group_rows(self, csv_path: Path) -> tuple[dict[int, Truckstop], int]:
        """Collapse the CSV to one :class:`Truckstop` per OPIS ID."""
        truckstops: dict[int, Truckstop] = {}
        rows_read = 0

        # utf-8-sig, because the supplied file carries a byte order mark that
        # would otherwise end up inside the first column name.
        with csv_path.open(encoding='utf-8-sig', newline='') as handle:
            for row in csv.DictReader(handle):
                rows_read += 1
                try:
                    opis_id = int((row.get('OPIS Truckstop ID') or '').strip())
                except ValueError:
                    continue

                truckstop = truckstops.get(opis_id)
                if truckstop is None:
                    truckstop = truckstops[opis_id] = Truckstop(opis_id=opis_id)
                truckstop.absorb(row)

        return truckstops, rows_read

    # -- geocoding --------------------------------------------------------

    def _build_stations(
        self, truckstops: dict[int, Truckstop], gazetteer
    ) -> tuple[list[FuelStation], int, dict[tuple[str, str], int]]:
        """Turn grouped rows into model instances, resolving each coordinate."""
        stations: list[FuelStation] = []
        skipped_foreign = 0
        unresolved: dict[tuple[str, str], int] = defaultdict(int)

        for truckstop in truckstops.values():
            if not truckstop.prices:
                continue

            # The brief scopes routes to the USA, and the feed carries Canadian
            # sites. Skipping them is correct, not a failure to geocode.
            if truckstop.state in CA_PROVINCES or truckstop.state not in US_STATES:
                skipped_foreign += 1
                continue

            coordinates = gazetteer.lookup(truckstop.city, truckstop.state)
            if coordinates is None:
                unresolved[(truckstop.city, truckstop.state)] += 1
                continue

            latitude, longitude = coordinates
            stations.append(
                FuelStation(
                    opis_id=truckstop.opis_id,
                    name=truckstop.name[:200],
                    address=truckstop.address[:255],
                    city=truckstop.city[:120],
                    state=truckstop.state,
                    rack_id=truckstop.rack_id,
                    price_per_gallon=truckstop.mean_price,
                    observation_count=len(truckstop.prices),
                    latitude=latitude,
                    longitude=longitude,
                )
            )

        return stations, skipped_foreign, dict(unresolved)

    # -- reporting --------------------------------------------------------

    def _report(
        self,
        rows_read: int,
        truckstops: dict[int, Truckstop],
        stations: list[FuelStation],
        skipped_foreign: int,
        unresolved: dict[tuple[str, str], int],
    ) -> None:
        collapsed = sum(1 for t in truckstops.values() if len(t.prices) > 1)
        prices = [float(s.price_per_gallon) for s in stations]
        in_scope = len(stations) + len(unresolved)

        self.stdout.write('')
        self.stdout.write(f'  rows read          {rows_read:>7,}')
        self.stdout.write(f'  distinct truckstops{len(truckstops):>7,}')
        self.stdout.write(
            f'  averaged prices    {collapsed:>7,}   sites with >1 observation'
        )
        self.stdout.write(
            f'  skipped, not USA   {skipped_foreign:>7,}   Canadian sites, out of scope'
        )
        self.stdout.write(
            f'  unresolved         {len(unresolved):>7,}   '
            f'{len(unresolved) / in_scope * 100:.2f}% of US sites'
        )
        self.stdout.write(f'  loaded             {len(stations):>7,}')
        self.stdout.write('')
        self.stdout.write(
            f'  price range        ${min(prices):.3f} - ${max(prices):.3f} per gallon'
        )
        self.stdout.write(f'  mean price         ${sum(prices) / len(prices):.3f}')
        self.stdout.write(f'  geocoding calls    {0:>7}   (bundled gazetteer)')
        self.stdout.write('')

        if unresolved:
            listed = sorted(unresolved.items(), key=lambda item: -item[1])[:10]
            self.stdout.write('  Unresolved (skipped):')
            for (city, state), count in listed:
                self.stdout.write(f'    {city}, {state}')
            if len(unresolved) > 10:
                self.stdout.write(f'    ... and {len(unresolved) - 10} more')
            self.stdout.write('')

        self.stdout.write(
            self.style.SUCCESS(f'Loaded {len(stations):,} truckstops.')
        )
