"""Admin registration, so the imported data can be eyeballed without SQL."""

from django.contrib import admin

from .models import FuelStation


@admin.register(FuelStation)
class FuelStationAdmin(admin.ModelAdmin):
    list_display = (
        'name',
        'city',
        'state',
        'price_per_gallon',
        'observation_count',
        'latitude',
        'longitude',
    )
    list_filter = ('state',)
    search_fields = ('name', 'city', 'opis_id')
    ordering = ('price_per_gallon',)
