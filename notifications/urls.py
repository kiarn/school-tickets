# SPDX-License-Identifier: GPL-3.0-or-later
from django.urls import path

from . import views

app_name = "notifications"

urlpatterns = [
    path("notifications/", views.notification_list, name="list"),
    path("notifications/read/", views.mark_read, name="read"),
    path("notifications/<int:pk>/", views.open_notification, name="open"),
    path("notifications/subscribe/", views.subscribe, name="subscribe"),
    path("notifications/unsubscribe/", views.unsubscribe, name="unsubscribe"),
]
