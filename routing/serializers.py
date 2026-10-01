"""Request validation for the route endpoint.

Only the incoming query string is validated with a serializer. The response is
assembled explicitly in :mod:`routing.views` rather than through nested
serializers: it is a computed report, not a view of model instances, and
building it in one readable place keeps the shape of the JSON obvious to anyone
reading the code alongside the API docs.
"""

from __future__ import annotations

from django.conf import settings
from rest_framework import serializers


class RouteQuerySerializer(serializers.Serializer):
    """The query parameters accepted by ``GET /api/route/``."""

    start = serializers.CharField(
        max_length=120,
        help_text='Where the trip begins: "Chicago, IL", "Chicago", '
                  'or "41.8781,-87.6298".',
    )
    finish = serializers.CharField(
        max_length=120,
        help_text='Where the trip ends, in the same forms as start.',
    )
    start_gallons = serializers.FloatField(
        required=False,
        min_value=0.0,
        help_text='Fuel in the tank at departure. Defaults to a full tank; '
                  'the brief does not specify it, so it is adjustable.',
    )
    max_detour_miles = serializers.FloatField(
        required=False,
        min_value=0.0,
        max_value=50.0,
        help_text='How far off the route a truckstop may sit. Default 5.',
    )

    def validate_start_gallons(self, value: float) -> float:
        config = settings.FUEL_ROUTE
        capacity = config['MAX_RANGE_MILES'] / config['MILES_PER_GALLON']
        if value > capacity:
            raise serializers.ValidationError(
                f'The tank holds {capacity:.0f} gallons '
                f'({config["MAX_RANGE_MILES"]:.0f} miles at '
                f'{config["MILES_PER_GALLON"]:.0f} mpg), so it cannot start '
                f'with {value:.0f}.'
            )
        return value
