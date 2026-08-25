# SPDX-License-Identifier: GPL-3.0-or-later
from django.contrib import admin


from .models import Device, DeviceStatus, Room, SyncDecision, SyncRun


@admin.register(Room)
class RoomAdmin(admin.ModelAdmin):
    list_display = ("name", "building", "is_active", "last_synced_at")
    list_filter = ("is_active",)


@admin.register(Device)
class DeviceAdmin(admin.ModelAdmin):
    list_display = ("hostname", "mac", "room", "is_active", "last_synced_at")
    list_filter = ("room", "is_active")
    search_fields = ("hostname", "mac")


@admin.register(SyncDecision)
class SyncDecisionAdmin(admin.ModelAdmin):
    """The queue the administrator must read. Its worth lies in its rarity."""

    list_display = ("kind", "status", "sync_run", "decided_by", "decided_at")
    list_filter = ("kind", "status")


admin.site.register([DeviceStatus, SyncRun])
