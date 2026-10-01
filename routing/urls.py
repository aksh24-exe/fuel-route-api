"""URL routing for the fuel route app."""

from django.urls import path

from .views import PlacesView, RouteView

app_name = 'routing'

urlpatterns = [
    path('places/', PlacesView.as_view(), name='places'),
    path('route/', RouteView.as_view(), name='route'),
]
