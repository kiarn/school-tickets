# SPDX-License-Identifier: GPL-3.0-or-later
from django.contrib import admin
from django.urls import include, path

from notifications import views as notification_views

urlpatterns = [
    path("admin/", admin.site.urls),
    # At the root, not under /static/: a service worker only controls pages
    # below its own URL. See notifications.views.service_worker.
    path("sw.js", notification_views.service_worker, name="service_worker"),
    path("", include("accounts.urls")),
    path("", include("badges.urls")),
    path("", include("notifications.urls")),
    path("", include("tickets.urls")),
]
