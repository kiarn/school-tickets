# SPDX-License-Identifier: GPL-3.0-or-later
from django.contrib import admin

from .models import Attachment, Comment, Tag, Ticket, TicketAssignee


class AssigneeInline(admin.TabularInline):
    model = TicketAssignee
    extra = 0


@admin.register(Ticket)
class TicketAdmin(admin.ModelAdmin):
    list_display = ("id", "room_label", "title", "status", "visibility", "created_at")
    list_filter = ("status", "visibility", "priority", "school")
    search_fields = ("title", "description", "room_label")
    inlines = [AssigneeInline]


admin.site.register([Tag, Comment, Attachment])
