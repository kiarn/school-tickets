# SPDX-License-Identifier: GPL-3.0-or-later
from django.contrib import admin
from django.db.models import Count
from django.utils.translation import gettext_lazy as _

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


@admin.register(Tag)
class TagAdmin(admin.ModelAdmin):
    """The vocabulary itself. A ticket's tags are set in the application.

    Deliberately no ``TicketTag`` inline beside ``AssigneeInline``: tagging is
    a conclusion drawn by whoever repaired the machine, on a phone, in the
    room -- not a row edited by a superuser here. That is ``t/<pk>/tags/``.
    """

    list_display = ("name", "slug", "school", "color", "ticket_count")
    list_filter = ("school", "color")
    search_fields = ("name", "slug")
    ordering = ("name",)
    # The slug ends up in an URL (`?tag=hdmi`) and nothing generates it. Typed
    # by hand it invites the fault that has no symptom: `hdmy` is a perfectly
    # valid slug, simply one no ticket carries.
    prepopulated_fields = {"slug": ("name",)}

    def get_queryset(self, request):
        return super().get_queryset(request).annotate(_tickets=Count("tickets"))

    @admin.display(description=_("Tickets"), ordering="_tickets")
    def ticket_count(self, obj):
        """Shown because deleting a tag is silent.

        ``TicketTag.tag`` cascades, so removing a tag untags every ticket that
        carried it, with no warning. This column is what turns that into an
        informed decision.
        """
        return obj._tickets


admin.site.register([Comment, Attachment])
