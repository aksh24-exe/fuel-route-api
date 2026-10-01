"""Storage for the truckstop fuel price data.

One row per truckstop, geocoded at import time. The table is small - 6,738 rows
- and entirely read-only once loaded, so it is read into an in-memory spatial
index at process start and the database is not touched again during a request.
"""

from __future__ import annotations

from django.db import models


class FuelStation(models.Model):
    """A truckstop with a retail diesel price and a coordinate.

    Identity comes from the OPIS Truckstop ID. The supplied file holds 8,151
    rows against 6,738 distinct IDs, and 597 of the repeated IDs carry
    *different* prices for the same truckstop in the same city - the file is an
    OPIS retail feed with several price observations per site and no date column
    to separate them.

    So a row here is one truckstop, and its price is the mean of the
    observations for it. Taking the minimum instead would make every route look
    cheaper and the savings figure look better, but it would be a promise the
    data cannot keep: nothing says the truck arrives on the cheap day.
    ``observation_count`` keeps that collapsing visible rather than silent.
    """

    # --- identity --------------------------------------------------------
    opis_id = models.IntegerField(
        unique=True,
        help_text='OPIS Truckstop ID, the identifier used by the price feed.',
    )
    name = models.CharField(
        max_length=200,
        help_text='Truckstop name. Where the feed spells it several ways for '
                  'one ID, the longest form is kept.',
    )
    address = models.CharField(max_length=255, blank=True)
    city = models.CharField(max_length=120)
    state = models.CharField(max_length=2)
    rack_id = models.IntegerField(
        null=True,
        blank=True,
        help_text='OPIS rack (wholesale pricing region) identifier.',
    )

    # --- price -----------------------------------------------------------
    price_per_gallon = models.DecimalField(
        max_digits=7,
        decimal_places=4,
        help_text='Retail price per gallon in USD; the mean of all '
                  'observations for this truckstop.',
    )
    observation_count = models.PositiveSmallIntegerField(
        default=1,
        help_text='How many rows of the source file were averaged into '
                  'price_per_gallon.',
    )

    # --- location --------------------------------------------------------
    #
    # Resolved offline from the bundled gazetteer, which places a truckstop at
    # the centre of its city rather than at its interstate exit. Against a
    # 500-mile range that error does not change which stops are optimal, and
    # storing it as two plain floats keeps the spatial index a matter of
    # arithmetic rather than requiring PostGIS.
    latitude = models.FloatField()
    longitude = models.FloatField()

    class Meta:
        ordering = ('state', 'city', 'name')
        indexes = (
            # The corridor search loads stations by bounding box before the
            # in-memory index is built, and on every cold start.
            models.Index(fields=('latitude', 'longitude'), name='station_latlon_idx'),
            models.Index(fields=('state',), name='station_state_idx'),
            models.Index(fields=('price_per_gallon',), name='station_price_idx'),
        )
        verbose_name = 'fuel station'
        verbose_name_plural = 'fuel stations'

    def __str__(self) -> str:
        return f'{self.name} ({self.city}, {self.state}) ${self.price_per_gallon}/gal'

    @property
    def price(self) -> float:
        """Price as a float, for the arithmetic in the optimizer."""
        return float(self.price_per_gallon)
