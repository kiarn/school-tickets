# SPDX-License-Identifier: GPL-3.0-or-later
from django.contrib import admin

from .models import AuditLog, School, User


@admin.register(School)
class SchoolAdmin(admin.ModelAdmin):
    list_display = ("slug", "name", "created_at")


@admin.register(User)
class UserAdmin(admin.ModelAdmin):
    list_display = ("cn", "display_name", "role", "school", "is_active", "anonymized_at")
    list_filter = ("role", "school", "is_active")
    search_fields = ("cn", "display_name", "email")
    readonly_fields = ("oidc_sub", "anonymized_at", "created_at", "last_seen_at")


@admin.register(AuditLog)
class AuditLogAdmin(admin.ModelAdmin):
    list_display = ("created_at", "action", "actor", "target_type", "target_id")
    list_filter = ("action",)
    readonly_fields = tuple(f.name for f in AuditLog._meta.fields)

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
