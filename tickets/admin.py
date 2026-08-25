# SPDX-License-Identifier: GPL-3.0-or-later
from django.contrib import admin
from django.db.models import Count
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from . import attachments as photos
from .models import Attachment, Comment, Tag, Ticket, TicketAssignee


class AssigneeInline(admin.TabularInline):
    model = TicketAssignee
    extra = 0


@admin.register(Ticket)
class TicketAdmin(admin.ModelAdmin):
    """The title, and after that only reading (D-52).

    Everything else a form could offer already has a screen in the application,
    and going through this one instead **skips half of what that screen does**:
    setting `status` to "resolved" here leaves `resolved_by` and `resolved_at`
    empty and notifies nobody, reopening does not count the reopening. A ticket
    resolved by nobody is not a correction, it is a lie the list then repeats.

    `title` is the exception because it is the one field with no route in the
    application: D-42 kept it out of the correction form on purpose -- it is
    the line the list is scanned by and the one people bookmark. Somewhere it
    still has to be fixable, and behind a superuser is the right somewhere.

    Deletion stays too: it exists nowhere else, and an erasure sometimes has
    to be complete (doc 06).
    """

    list_display = ("id", "room_label", "title", "status", "priority", "visibility", "created_at")
    list_filter = ("status", "visibility", "priority")
    search_fields = ("title", "description", "room_label")
    inlines = [AssigneeInline]
    readonly_fields = tuple(
        f.name for f in Ticket._meta.fields if f.name not in ("id", "title")
    ) + ("tags",)

    def has_add_permission(self, request):
        """A ticket is opened by whoever saw the fault, in the application."""
        return False


@admin.register(Tag)
class TagAdmin(admin.ModelAdmin):
    """The vocabulary itself. A ticket's tags are set in the application.

    Deliberately no ``TicketTag`` inline beside ``AssigneeInline``: tagging is
    a conclusion drawn by whoever repaired the machine, on a phone, in the
    room -- not a row edited by a superuser here. That is ``t/<pk>/tags/``.
    """

    list_display = ("name", "slug", "color", "ticket_count")
    list_filter = ("color",)
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


@admin.register(Attachment)
class AttachmentAdmin(admin.ModelAdmin):
    """A photo is a consequence of an upload, never an entry.

    The same reason ``NotificationAdmin`` refuses additions -- and here it was
    not only untidy. ``storage_path`` was a free text box, and every reader
    joined it onto ``MEDIA_ROOT``: one typed field turned database access into
    arbitrary file read, and through the delete button into arbitrary file
    removal. ``attachments.resolved_path()`` refuses that on its own now; this
    stops it being offered in the first place.

    Deletion stays, and takes the bytes with it. Doc 06 asks that removing a
    photo be easy precisely because one may show a face -- and the admin used
    to remove only the row, leaving the file on disk.
    """

    list_display = ("filename", "ticket", "uploaded_by", "mime", "size_bytes", "created_at")
    list_filter = ("mime",)
    search_fields = ("filename",)
    readonly_fields = tuple(f.name for f in Attachment._meta.fields)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def delete_model(self, request, obj):
        photos.delete_file(obj)
        super().delete_model(request, obj)

    def delete_queryset(self, request, queryset):
        # The bulk action goes through a collector that never calls
        # ``delete_model``.
        for obj in queryset:
            photos.delete_file(obj)
        super().delete_queryset(request, queryset)


@admin.register(Comment)
class CommentAdmin(admin.ModelAdmin):
    """The body stays editable, and that is deliberate.

    Doc 06 lists it as an accepted blind spot: "j'ai vérifié après Lukas" names
    somebody in the clear and no anonymisation reaches it. Editing is the only
    remedy there is, so it stays -- but a thread rewritten with no mark is the
    team's technical memory quietly changing under them.
    """

    list_display = ("ticket", "author", "created_at", "edited_at")
    search_fields = ("body",)
    readonly_fields = ("ticket", "author", "created_at", "edited_at")

    def save_model(self, request, obj, form, change):
        if change and "body" in form.changed_data:
            obj.edited_at = timezone.now()
        super().save_model(request, obj, form, change)
