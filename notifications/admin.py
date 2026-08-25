# SPDX-License-Identifier: GPL-3.0-or-later
from django.contrib import admin

from .models import Notification


@admin.register(Notification)
class NotificationAdmin(admin.ModelAdmin):
    list_display = ("created_at", "kind", "recipient", "ticket", "delivery", "read_at")
    list_filter = ("kind", "delivery")
    readonly_fields = ("created_at", "pushed_at")

    def has_add_permission(self, request):
        """Notifications are consequences, never entries."""
        return False

    def has_change_permission(self, request, obj=None):
        """And a log is read, not corrected (D-50).

        The one field worth looking at is `delivery`: it says why a Push did or
        did not leave. Editing it would rewrite that answer.
        """
        return False


# `Mute` is not registered, deliberately (D-50): a mute is a preference its
# owner sets on their own profile, worded there as "send me" rather than as
# "mute" (D-24). A row here would let an administrator silence somebody's
# notifications on their behalf, which is the one thing that screen must not
# make easy -- and it would say nothing readable while doing it.
