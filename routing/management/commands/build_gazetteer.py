"""Build the bundled gazetteer from the GeoNames US export.

This is a development tool, not something a reviewer or a deployment needs to
run: its output, ``data/us_gazetteer.csv.gz``, is committed to the repository.
That is the whole point. Because the gazetteer ships with the code, geocoding
6,738 truckstops costs zero API calls, and so does resolving the caller's start
and finish on every request.

    uv run manage.py build_gazetteer --source /tmp/US.zip --verify

Source data: https://download.geonames.org/export/dump/US.zip (public domain,
Creative Commons Attribution 4.0).
"""

from __future__ import annotations

import csv
import gzip
import io
import zipfile
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from routing.services.geocode import (
    US_STATES,
    place_keys,
    primary_key,
    reset_gazetteer,
)

# GeoNames' main export is tab separated with a fixed column order and no
# header row. Only these five columns are needed.
COLUMN_NAME = 1
COLUMN_ASCII_NAME = 2
COLUMN_LATITUDE = 4
COLUMN_LONGITUDE = 5
COLUMN_FEATURE_CLASS = 6
COLUMN_STATE = 10
COLUMN_POPULATION = 14
MINIMUM_COLUMNS = 15

# Feature class "P" is populated places: cities, towns, villages. Everything
# else in the export - mountains, streams, schools, parks - is irrelevant to
# resolving a truckstop's mailing address, and dropping it removes 91% of the
# rows before any work is done on them.
POPULATED_PLACE = 'P'

# Four decimal places is about 11 metres. Keeping more would inflate the file
# without improving anything, given truckstops resolve to a city centre anyway.
COORDINATE_DECIMALS = 4


class Command(BaseCommand):
    help = 'Build data/us_gazetteer.csv.gz from the GeoNames US export.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--source',
            default='/tmp/US.zip',
            help='Path to GeoNames US.zip or an already extracted US.txt.',
        )
        parser.add_argument(
            '--output',
            default=None,
            help='Where to write the gazetteer (default: settings value).',
        )
        parser.add_argument(
            '--verify',
            action='store_true',
            help='After building, report the hit rate against the fuel price CSV.',
        )

    # -- main -------------------------------------------------------------

    def handle(self, *args, **options):
        source = Path(options['source'])
        if not source.exists():
            raise CommandError(
                f'Source not found: {source}\n'
                'Download it with:  curl -o /tmp/US.zip '
                'https://download.geonames.org/export/dump/US.zip'
            )

        output = Path(
            options['output'] or settings.FUEL_ROUTE['GAZETTEER_PATH']
        )
        output.parent.mkdir(parents=True, exist_ok=True)

        self.stdout.write(f'Reading {source} ...')
        places, rows_read = self._collect_places(source)

        if not places:
            raise CommandError(
                'No populated places found. Is this really the GeoNames US export?'
            )

        self.stdout.write(f'Writing {output} ...')
        keys_written = self._write(places, output)

        reset_gazetteer()

        self.stdout.write('')
        self.stdout.write(f'  rows read      {rows_read:>10,}')
        self.stdout.write(f'  places kept    {len(places):>10,}   (feature class P)')
        self.stdout.write(f'  keys written   {keys_written:>10,}   (incl. spelling variants)')
        self.stdout.write(f'  file size      {output.stat().st_size / 1_048_576:>9.1f} MB')
        self.stdout.write('')
        self.stdout.write(self.style.SUCCESS(f'Gazetteer built: {output}'))

        if options['verify']:
            self._verify()

    # -- reading ----------------------------------------------------------

    def _collect_places(
        self, source: Path
    ) -> tuple[dict[tuple[str, str], tuple[float, float, int]], int]:
        """Read the export and keep the most populous place per (key, state).

        Several distinct places often share a name within one state - a village
        and the township around it, for instance. Preferring the most populous
        picks the one a mailing address is most likely to mean.
        """
        primary: dict[tuple[str, str], tuple[float, float, int]] = {}
        variants: dict[tuple[str, str], tuple[float, float, int]] = {}
        rows_read = 0

        for line in self._iter_lines(source):
            rows_read += 1
            columns = line.rstrip('\n').split('\t')

            if len(columns) < MINIMUM_COLUMNS:
                continue
            if columns[COLUMN_FEATURE_CLASS] != POPULATED_PLACE:
                continue

            state = columns[COLUMN_STATE].strip().upper()
            if state not in US_STATES:
                continue

            try:
                latitude = float(columns[COLUMN_LATITUDE])
                longitude = float(columns[COLUMN_LONGITUDE])
            except ValueError:
                continue

            try:
                population = int(columns[COLUMN_POPULATION] or 0)
            except ValueError:
                population = 0

            entry = (latitude, longitude, population)
            names = {columns[COLUMN_NAME], columns[COLUMN_ASCII_NAME]}

            # A place's own spelling claims its key outright. Spelling variants
            # are collected separately and only fill keys no real place wants,
            # so shortening "Jefferson City" can never hide "Jefferson".
            for name in names:
                key = primary_key(name)
                if not key:
                    continue
                existing = primary.get((key, state))
                if existing is None or population > existing[2]:
                    primary[(key, state)] = entry

                for variant in place_keys(name) - {key}:
                    existing = variants.get((variant, state))
                    if existing is None or population > existing[2]:
                        variants[(variant, state)] = entry

        places = dict(primary)
        for slot, entry in variants.items():
            places.setdefault(slot, entry)

        return places, rows_read

    def _iter_lines(self, source: Path):
        """Yield text lines from either US.zip or a plain US.txt."""
        if source.suffix.lower() == '.zip':
            with zipfile.ZipFile(source) as archive:
                names = [n for n in archive.namelist() if n.lower().endswith('.txt')]
                data_names = [n for n in names if 'readme' not in n.lower()]
                if not data_names:
                    raise CommandError(f'No data .txt inside {source}')
                with archive.open(data_names[0]) as raw:
                    yield from io.TextIOWrapper(raw, encoding='utf-8')
        else:
            with source.open(encoding='utf-8') as handle:
                yield from handle

    # -- writing ----------------------------------------------------------

    def _write(
        self,
        places: dict[tuple[str, str], tuple[float, float, int]],
        output: Path,
    ) -> int:
        """Write the gazetteer as a gzipped CSV, sorted for a stable diff."""
        with gzip.open(
            output, 'wt', encoding='utf-8', newline='', compresslevel=9
        ) as handle:
            writer = csv.writer(handle)
            for (key, state), (latitude, longitude, population) in sorted(
                places.items()
            ):
                writer.writerow(
                    [
                        key,
                        state,
                        f'{latitude:.{COORDINATE_DECIMALS}f}',
                        f'{longitude:.{COORDINATE_DECIMALS}f}',
                        population,
                    ]
                )
        return len(places)

    # -- verification -----------------------------------------------------

    def _verify(self) -> None:
        """Report how much of the supplied price file this gazetteer resolves.

        Worth doing here rather than discovering it during the import: if the
        hit rate is poor, the gazetteer is the thing to fix.
        """
        from routing.services.geocode import CA_PROVINCES, get_gazetteer

        csv_path = Path(settings.FUEL_ROUTE['FUEL_PRICES_CSV'])
        if not csv_path.exists():
            self.stdout.write(
                self.style.WARNING(f'Skipping verify: {csv_path} not found')
            )
            return

        gazetteer = get_gazetteer()
        resolved = out_of_scope = unresolved = 0
        misses: dict[tuple[str, str], int] = {}

        with csv_path.open(encoding='utf-8-sig', newline='') as handle:
            for row in csv.DictReader(handle):
                city = (row.get('City') or '').strip()
                state = (row.get('State') or '').strip().upper()

                if state in CA_PROVINCES:
                    out_of_scope += 1
                elif gazetteer.lookup(city, state) is not None:
                    resolved += 1
                else:
                    unresolved += 1
                    misses[(city, state)] = misses.get((city, state), 0) + 1

        total = resolved + out_of_scope + unresolved
        in_scope = resolved + unresolved

        self.stdout.write('')
        self.stdout.write('Verification against the fuel price CSV')
        self.stdout.write(f'  rows total      {total:>7,}')
        self.stdout.write(
            f'  out of scope    {out_of_scope:>7,}   (Canadian provinces)'
        )
        self.stdout.write(f'  in scope (US)   {in_scope:>7,}')
        rate = resolved / in_scope * 100 if in_scope else 0.0
        self.stdout.write(
            f'  resolved        {resolved:>7,}   {rate:.2f}% of in-scope rows'
        )
        self.stdout.write(f'  unresolved      {unresolved:>7,}')

        if misses:
            self.stdout.write('')
            self.stdout.write('  Unresolved US cities:')
            ranked = sorted(misses.items(), key=lambda item: -item[1])
            for (city, state), count in ranked[:15]:
                self.stdout.write(f'    {city}, {state}  ({count} rows)')
            if len(ranked) > 15:
                self.stdout.write(f'    ... and {len(ranked) - 15} more')
