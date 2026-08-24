# SPDX-License-Identifier: GPL-3.0-or-later
from django.contrib import admin

from .models import Mute, Notification


@admin.register(Notification)
class NotificationAdmin(admin.ModelAdmin):
    list_display = ("created_at", "kind", "recipient", "ticket", "delivery", "read_at")
    list_filter = ("kind", "delivery")
    readonly_fields = ("created_at", "pushed_at")

    def has_add_permission(self, request):
        """Notifications are consequences, never entries."""
        return False


admin.site.register(Mute)
