# SPDX-License-Identifier: GPL-3.0-or-later
from django.contrib import admin
from django.urls import include, path

from notifications import views as notification_views

from . import branding

urlpatterns = [
    path("admin/", admin.site.urls),
    # At the root, not under /static/: a service worker only controls pages
    # below its own URL. See notifications.views.service_worker.
    path("sw.js", notification_views.service_worker, name="service_worker"),
    # Also the favicon and the Push icon: one file answers all three.
    path("logo", branding.logo, name="logo"),
    # At the root for the same reason as sw.js -- a manifest's scope cannot
    # reach above the directory it is served from.
    path("manifest.webmanifest", branding.manifest, name="manifest"),
    path("icon/<int:size>.png", branding.icon, name="icon"),
    path("", include("accounts.urls")),
    path("", include("badges.urls")),
    path("", include("notifications.urls")),
    path("", include("parc.urls")),
    path("", include("tickets.urls")),
]
