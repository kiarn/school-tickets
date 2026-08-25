# SPDX-License-Identifier: GPL-3.0-or-later
from django.contrib import admin
from django.utils import timezone


from .models import (
    Device,
    DeviceStatus,
    Room,
    SyncDecision,
    SyncRun,
    WorkerHeartbeat,
)


class ReadOnlyAdmin(admin.ModelAdmin):
    """A window onto what the worker writes, and nothing more (D-50).

    Both models below are machine output: a LINBO status observed on a device,
    and the trace of a synchronisation pass. Neither is edited by anybody --
    the bare ``admin.site.register()`` they used to get offered "add" and
    "save" buttons on rows that only the worker has any business writing.

    Not unregistered either, and that is the deliberate half: `SyncRun.error`
    carries the volume guard's refusal, and `DeviceStatus` is currently the
    only place a LINBO answer can be read at all -- the device page's block
    waits on the endpoint semantics (doc 05). Removing the screen would remove
    the only window onto the one job that runs unattended.
    """

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(Room)
class RoomAdmin(admin.ModelAdmin):
    """Two fields are a human's, the rest is the sync's (D-51).

    ``building`` and ``sort_key`` are **not carried by any source**: no
    ``DeviceRow`` has them, and ``_ensure_rooms`` never writes them. The
    ordering one especially -- "human order, not alphabetical" -- is a decision
    nothing but a person can make, and this is where it is made.

    Everything else is read-only, and ``name`` is the reason this class exists
    rather than a bare registration: **``_ensure_rooms`` matches rooms by
    name**. Renamed here, a room does not come back corrected at the next pass
    -- the sync finds no room under the old name, creates a second one, and the
    tickets of the first stay behind in a room no source feeds any more. Doc 03
    detects renames from the MACs of the *incoming* estate; it cannot see an
    edit made in this form.

    No add either: a room enters through a source, never through this page.
    """

    list_display = ("name", "building", "sort_key", "is_active", "last_synced_at")
    list_filter = ("is_active",)
    search_fields = ("name", "building")
    readonly_fields = ("name", "source", "is_active", "retired_at", "last_synced_at")

    def has_add_permission(self, request):
        return False


@admin.register(Device)
class DeviceAdmin(ReadOnlyAdmin):
    """Read-only, and Arnaud's objection is the whole reason (D-51).

    "Modifier un device serait alors nécessaire après chaque sync" -- exactly:
    ``room``, ``hostname``, ``ip``, ``sophomorix_group``, ``role``, ``pxe`` and
    ``is_active`` are **all rewritten on every pass**, so a correction typed
    here survives until the next hour and no longer. The only field the sync
    never touches is ``mac``, and that one must not be touched at all: it is
    the stable identity (doc 03), the key every lookup goes through, and the
    thread that ties a ticket to the machine it was filed against.

    A form where every field is either overwritten or forbidden is a form with
    nothing to offer. What is left is the list, which answers "what does the
    estate look like right now" -- and that is worth keeping.
    """

    list_display = ("hostname", "mac", "room", "pxe", "is_active", "last_synced_at")
    list_filter = ("room", "is_active")
    search_fields = ("hostname", "mac")


@admin.register(SyncDecision)
class SyncDecisionAdmin(admin.ModelAdmin):
    """The queue the administrator must read. Its worth lies in its rarity.

    One field is answered here -- ``status`` -- and the rest is the run's own
    account of what it saw (D-52). ``kind``, ``payload`` and ``sync_run`` were
    editable, which offered to rewrite the question rather than answer it.

    Who decided and when are stamped rather than typed: a queue that records a
    decision without its author records half of one.
    """

    list_display = ("kind", "status", "sync_run", "decided_by", "decided_at")
    list_filter = ("kind", "status")
    readonly_fields = ("sync_run", "kind", "payload", "decided_by", "decided_at")

    def has_add_permission(self, request):
        """A decision is raised by a synchronisation pass, never typed."""
        return False

    def formfield_for_choice_field(self, db_field, request, **kwargs):
        """"Applied" is not offered while nothing applies (Q-10).

        **Temporary, and to be deleted the day the queue gets an executor.**
        Nothing anywhere acts on a `SyncDecision`: marking one "applied" retires
        no room, merges none, takes no machine out of service. Leaving the word
        in the menu is the trap D-50 to D-52 spent three decisions removing --
        somebody chooses it, believes the estate was corrected, and nothing was.

        What remains is honest: "dismissed" means *read, nothing to do*, which
        is the only answer this screen can actually carry out today.
        """
        if db_field.name == "status":
            kwargs["choices"] = [
                (value, label)
                for value, label in SyncDecision.Status.choices
                if value != SyncDecision.Status.APPLIED
            ]
        return super().formfield_for_choice_field(db_field, request, **kwargs)

    def save_model(self, request, obj, form, change):
        if "status" in form.changed_data and obj.status != SyncDecision.Status.PENDING:
            obj.decided_by = request.user
            obj.decided_at = timezone.now()
        super().save_model(request, obj, form, change)


@admin.register(WorkerHeartbeat)
class WorkerHeartbeatAdmin(ReadOnlyAdmin):
    """The window that was missing (D-52).

    The one process that runs with nobody watching had no screen at all, while
    three tables it writes had one. "The worker has been down since Tuesday" is
    exactly what this answers -- and D-20 put the heartbeat there for that
    question.
    """

    list_display = ("at", "jobs_ran")


@admin.register(DeviceStatus)
class DeviceStatusAdmin(ReadOnlyAdmin):
    list_display = ("device", "fetch_status", "linbo_image", "last_linbo_sync_at", "observed_at")
    list_filter = ("fetch_status",)
    search_fields = ("device__hostname", "device__mac")


@admin.register(SyncRun)
class SyncRunAdmin(ReadOnlyAdmin):
    list_display = ("started_at", "source", "status", "rooms_seen", "devices_seen")
    list_filter = ("source", "status")
