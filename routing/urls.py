"""URL routing for the fuel route app."""

from django.urls import path

from .views import RouteView

app_name = 'routing'

urlpatterns = [
    path('route/', RouteView.as_view(), name='route'),
]
