# SPDX-License-Identifier: GPL-3.0-or-later
from django.urls import path

from . import views

app_name = "tickets"

urlpatterns = [
    path("", views.ticket_list, name="list"),
    path("new/", views.ticket_create, name="create"),
    path("rooms/devices/", views.room_devices, name="room_devices"),
    path("t/<int:pk>/", views.ticket_detail, name="detail"),
    path("t/<int:pk>/comment/", views.comment_create, name="comment"),
    path("t/<int:pk>/status/", views.ticket_status, name="status"),
    path("t/<int:pk>/claim/", views.ticket_claim, name="claim"),
    path("t/<int:pk>/device/refresh/", views.device_refresh, name="device_refresh"),
    path("t/<int:pk>/assignees/", views.ticket_assignees, name="assignees"),
    path("t/<int:pk>/tags/", views.ticket_tags, name="tags"),
    path("t/<int:pk>/resolution/", views.ticket_resolution, name="resolution"),
    path("t/<int:pk>/correct/", views.ticket_correct, name="correct"),
    path("t/<int:pk>/visibility/", views.ticket_visibility, name="visibility"),
    path("a/<int:pk>/", views.attachment, name="attachment"),
    path("a/<int:pk>/delete/", views.attachment_delete, name="attachment_delete"),
]
