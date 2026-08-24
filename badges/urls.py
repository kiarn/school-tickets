# SPDX-License-Identifier: GPL-3.0-or-later
from django.urls import path

from . import views

app_name = "badges"

urlpatterns = [
    path("badges/", views.catalog, name="catalog"),
    path("badges/new/", views.badge_create, name="create"),
    path("badges/award/", views.award, name="award"),
    path("badges/award/<int:pk>/", views.award, name="award_to"),
]
