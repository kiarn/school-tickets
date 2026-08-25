# SPDX-License-Identifier: GPL-3.0-or-later
"""Local reflection of the estate (D-08). See specs/03-parc-synchronisation.md."""

from django.db import models
from django.utils.translation import gettext_lazy as _


class Source(models.TextChoices):
    MANUAL = "manual", _("Manual entry")
    IMPORT = "import", _("CSV import")
    SYNC = "sync", _("lmnapi sync")


class DeviceRole(models.TextChoices):
    """Column 9 of ``devices.csv``, reflected and nothing more.

    **A device's role gives it no standing in this application.** A jammed
    printer and a dead workstation are the same thing here -- something a pupil
    repairs and files a ticket about. Nothing may branch on this field: no
    filtering, no eligibility, no sorting into first and second class.

    Kept because doc 05's argument for ``raw`` applies to it as well -- what we
    want to show will change faster than our reading of the source, and the
    estate has already handed us the value.

    Not enforced at database level either: sophomorix accepts roles this list
    does not know, and an unknown one must simply pass through.
    """

    CLASSROOM_STUDENT = "classroom-studentcomputer", _("Classroom computer (pupil)")
    CLASSROOM_TEACHER = "classroom-teachercomputer", _("Classroom computer (teacher)")
    STAFF = "staffcomputer", _("Staff computer")
    SERVER = "server", _("Server")
    PRINTER = "printer", _("Printer")
    ROUTER = "router", _("Router")
    IP_ONLY = "iponly", _("IP reservation only")


class Room(models.Model):
    """A room is never deleted, only retired."""

    name = models.CharField(max_length=100)
    building = models.CharField(max_length=100, blank=True)
    sort_key = models.CharField(max_length=100, blank=True)  # human order, not alphabetical
    source = models.CharField(max_length=10, choices=Source.choices, default=Source.SYNC)
    is_active = models.BooleanField(default=True)
    retired_at = models.DateTimeField(null=True, blank=True)
    last_synced_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["sort_key", "name"]

    def __str__(self):
        return self.name


class Device(models.Model):
    room = models.ForeignKey(Room, on_delete=models.PROTECT, related_name="devices")
    # Stable identity: survives both a hostname change AND a move to another
    # room. This is the estate's external key -- never the hostname (doc 03).
    mac = models.CharField(max_length=17)
    hostname = models.CharField(max_length=100)  # changes more often than one thinks
    ip = models.GenericIPAddressField(null=True, blank=True)
    # Column 3: the LINBO group / hardware class (``allg``, ``101``, ``nopxe``).
    sophomorix_group = models.CharField(max_length=100, blank=True)
    # Column 9. Reflected for display only -- nothing branches on it.
    role = models.CharField(max_length=40, choices=DeviceRole.choices, blank=True)
    # Column 11, the PXE flag. NULL means "the source did not say", which is
    # NOT the same as 0 and must not be read as one.
    pxe = models.PositiveSmallIntegerField(null=True, blank=True)
    source = models.CharField(max_length=10, choices=Source.choices, default=Source.SYNC)
    is_active = models.BooleanField(default=True)
    retired_at = models.DateTimeField(null=True, blank=True)
    last_synced_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            # On the MAC, and emphatically NOT on the hostname.
            models.UniqueConstraint(fields=["mac"], name="unique_device_mac")
        ]
        ordering = ["hostname"]

    def __str__(self):
        return self.hostname or self.mac

    @staticmethod
    def normalize_mac(value: str) -> str:
        """Lower case, always.

        Real ``devices.csv`` files mix both cases in the same file -- a sample
        from a live server carried 38 upper-case and 7 lower-case addresses.
        Without normalisation the unique constraint on `mac` protects
        nothing: the same machine re-cased creates a second row, and the MAC
        stops being the stable identity the whole design rests on (doc 03).
        """
        return (value or "").strip().lower()

    def save(self, *args, **kwargs):
        """Normalisation belongs here, not in the callers.

        Every write path -- sync, CSV import, admin, shell -- goes through
        ``save()``. Anything narrower would leave a door open.
        """
        self.mac = self.normalize_mac(self.mac)
        return super().save(*args, **kwargs)

    @property
    def expects_linbo(self) -> bool:
        """Whether a missing LINBO status means a fault or means nothing.

        This is not a judgement on the machine, it is a fact about it: a device
        that does not boot over PXE has no LINBO state, so the absence of one
        means nothing. Without the distinction every printer, router, NAS and
        server carries a permanent ``unreachable`` -- the false-alarm flood
        doc 05 forbids.

        Read from the flag itself, never guessed from the role: on real estate
        data the two disagree outright, ``staffcomputer`` appearing with the
        flag at both 0 and 1.
        """
        return bool(self.pxe)


class DeviceStatus(models.Model):
    """Deliberately split from Device: incompatible write rhythms.

    The two timestamps do not say the same thing, and conflating them produces
    a lying display. See specs/05-linbo-derniere-synchro.md.
    """

    class FetchStatus(models.TextChoices):
        OK = "ok", _("Observed")
        UNREACHABLE = "unreachable", _("Unreachable")
        FORBIDDEN = "forbidden", _("Forbidden")

    device = models.OneToOneField(Device, on_delete=models.CASCADE, related_name="status")
    # When THE MACHINE last synced.
    last_linbo_sync_at = models.DateTimeField(null=True, blank=True)
    linbo_image = models.CharField(max_length=200, blank=True)
    # When SCHOOL-TICKETS observed that value. Never show freshness without the
    # age of the observation.
    observed_at = models.DateTimeField(null=True, blank=True)
    fetch_status = models.CharField(max_length=20, choices=FetchStatus.choices, blank=True)
    raw = models.JSONField(null=True, blank=True)
    # Written by the web front end, picked up by the worker: the front end
    # holds no lmnapi secret and calls nothing itself (D-09).
    refresh_requested_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [models.Index(fields=["refresh_requested_at"])]


class SyncRun(models.Model):
    class Status(models.TextChoices):
        SUCCESS = "success", _("Succeeded")
        FAILED = "failed", _("Failed")
        REFUSED_GUARD = "refused_guard", _("Refused by the volume guard")

    source = models.CharField(max_length=20, choices=[("lmnapi", "lmnapi"), ("csv_upload", "CSV")])
    started_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, blank=True)
    rooms_seen = models.PositiveIntegerField(default=0)
    devices_seen = models.PositiveIntegerField(default=0)
    triggered_by = models.ForeignKey(
        "accounts.User", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    error = models.TextField(blank=True)


class SyncDecision(models.Model):
    """The queue of changes a machine may not settle on its own.

    Its worth lies in its rarity: device moves and renames apply silently and
    never land here (doc 03).
    """

    class Kind(models.TextChoices):
        ROOM_DISAPPEARED = "room_disappeared", _("Room disappeared")
        ROOM_RENAMED_SUSPECTED = "room_renamed_suspected", _("Suspected room rename")
        DEVICE_DISAPPEARED = "device_disappeared", _("Device disappeared")

    class Status(models.TextChoices):
        PENDING = "pending", _("Pending")
        APPLIED = "applied", _("Applied")
        DISMISSED = "dismissed", _("Dismissed")

    sync_run = models.ForeignKey(SyncRun, on_delete=models.CASCADE, related_name="decisions")
    kind = models.CharField(max_length=32, choices=Kind.choices)
    # The old, the new, the overlapping MACs, the confidence score.
    payload = models.JSONField(default=dict)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING)
    decided_by = models.ForeignKey(
        "accounts.User", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    decided_at = models.DateTimeField(null=True, blank=True)


class WorkerHeartbeat(models.Model):
    """One turn of the worker loop (D-20).

    Without this heartbeat the application cannot tell "this machine has not
    synced" from "I no longer know anything about this machine", and a pupil
    concludes there is a fault when the worker has simply been down since
    Tuesday.
    """

    at = models.DateTimeField()
    jobs_ran = models.JSONField(default=dict, blank=True)

    class Meta:
        get_latest_by = "at"
