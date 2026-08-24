# SPDX-License-Identifier: GPL-3.0-or-later
from django.contrib.auth import views as auth_views
from django.urls import path

from . import views

app_name = "accounts"

urlpatterns = [
    path("login/", views.Login.as_view(), name="login"),
    # POST only since Django 5: a link that logs you out is a link somebody
    # else's page can make your browser follow.
    path("logout/", auth_views.LogoutView.as_view(), name="logout"),
    path("me/", views.profile, name="profile"),
    path("me/theme/", views.theme, name="theme"),
    path("u/<int:pk>/", views.profile_detail, name="profile_detail"),
]
