# SPDX-License-Identifier: GPL-3.0-or-later
from django.contrib import admin

from .models import Badge, BadgeAward


@admin.register(Badge)
class BadgeAdmin(admin.ModelAdmin):
    # award_count is editable here on purpose: it is a tally, and a tally
    # occasionally needs a human correction.
    list_display = ("slug", "name", "school", "is_catalog", "category", "award_count")
    list_filter = ("is_catalog", "school", "category")
    search_fields = ("slug", "name")


@admin.register(BadgeAward)
class BadgeAwardAdmin(admin.ModelAdmin):
    """Where an award is taken back.

    Deliberately not a button on a profile: revoking recognition in front of
    the person who holds it is an administrative correction, not a screen the
    application invites anyone to use.
    """

    list_display = ("badge", "user", "awarded_by", "awarded_at")
    list_filter = ("badge",)
    search_fields = ("user__cn", "user__display_name", "motivation")
