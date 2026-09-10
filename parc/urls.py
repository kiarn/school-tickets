# SPDX-License-Identifier: GPL-3.0-or-later
from django.urls import path

from . import views

app_name = "parc"

urlpatterns = [
    path("estate/", views.estate, name="estate"),
    path("estate/r/<int:pk>/", views.room, name="room"),
    path("estate/m/<int:pk>/", views.device, name="device"),
    path("estate/m/<int:pk>/refresh/", views.device_refresh, name="device_refresh"),
]
