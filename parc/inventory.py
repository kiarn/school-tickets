# SPDX-License-Identifier: GPL-3.0-or-later
"""Reconciling the local reflection with a full snapshot. See specs/03.

The one rule the whole module serves: **a sync never destroys, it proposes.**
Creations, moves and renames apply on their own; disappearances and suspected
room renames go to ``SyncDecision`` and wait for a human.
"""

import logging

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from accounts.models import AuditLog
from notifications import events
from parc.models import Device, Room, SyncDecision, SyncRun

logger = logging.getLogger(__name__)


def _trace(actor, device, changes: dict) -> None:
    """Silent to the user, never silent in the log (doc 03).

    Moves and renames apply without asking precisely because they are routine;
    that is exactly why they must stay reconstructible afterwards.
    """
    AuditLog.objects.create(
        actor=actor,
        action=AuditLog.Action.SYNC_APPLIED,
        target_type="parc.Device",
        target_id=device.pk,
        payload=changes,
    )


def apply_inventory(rows, *, source, triggered_by=None) -> SyncRun:
    """Confront the reflection with a full snapshot and write the differences.

    ``rows`` is a list of :class:`parc.sources.DeviceRow`; where it came from
    is none of this function's business (doc 03, "les deux sources").
    """
    run = SyncRun.objects.create(source=source, triggered_by=triggered_by)

    incoming = {row.mac: row for row in rows}
    incoming_rooms: dict[str, set[str]] = {}
    for row in rows:
        incoming_rooms.setdefault(row.room, set()).add(row.mac)

    previous = {d.mac: d for d in Device.objects.filter(is_active=True)}
    known_room_names = set(Room.objects.values_list("name", flat=True))

    vanished_macs = set(previous) - set(incoming)

    # --- The volume guard, before anything is written ------------------------
    # An expired token answering 200 with an empty body, a truncated export, a
    # format change: this is how a whole reference set is lost on a Tuesday.
    if previous and len(vanished_macs) / len(previous) > settings.ST_SYNC_MAX_REMOVAL_RATIO:
        run.status = SyncRun.Status.REFUSED_GUARD
        run.error = (
            f"{len(vanished_macs)} of {len(previous)} devices missing from the snapshot, "
            f"above ST_SYNC_MAX_REMOVAL_RATIO={settings.ST_SYNC_MAX_REMOVAL_RATIO}. "
            f"Nothing was applied."
        )
        run.rooms_seen = len(incoming_rooms)
        run.devices_seen = len(incoming)
        run.finished_at = timezone.now()
        run.save()
        logger.error("sync refused by the volume guard: %s", run.error)
        return run

    counters = {"rooms_created": 0, "devices_created": 0, "moved": 0, "renamed": 0}

    with transaction.atomic():
        rooms = _ensure_rooms(incoming_rooms, source, counters)
        _apply_devices(incoming, rooms, source, triggered_by, counters)
        decisions = _queue_decisions(
            run, incoming, incoming_rooms, previous, known_room_names, vanished_macs
        )
        # The one notification with no ticket behind it. In the transaction
        # like every other (doc 09 §5), and only to admins: they are the only
        # people who can act on the queue.
        events.sync_decisions_pending(count=decisions)

        run.status = SyncRun.Status.SUCCESS
        run.rooms_seen = len(incoming_rooms)
        run.devices_seen = len(incoming)
        run.finished_at = timezone.now()
        run.save()

    logger.info("sync %s: %s, %d decision(s) queued", run.pk, counters, decisions)
    return run


def _ensure_rooms(incoming_rooms, source, counters) -> dict:
    """Create what is new, revive what came back. Never touch what exists.

    A retired room whose name reappears is reactivated rather than duplicated:
    the row keeps its id, so the tickets filed in it stay attached (doc 03).
    """
    rooms = {}
    for name in incoming_rooms:
        room = Room.objects.filter(name=name).first()
        if room is None:
            room = Room.objects.create(name=name, source=source)
            counters["rooms_created"] += 1
        elif not room.is_active:
            room.is_active = True
            room.retired_at = None
        room.last_synced_at = timezone.now()
        room.save()
        rooms[name] = room
    return rooms


def _apply_devices(incoming, rooms, source, actor, counters) -> None:
    """Creations, moves and hostname changes -- all of them silent.

    A decision queue that asked to confirm every moved workstation would hold
    fifty entries by the end of the first week of term, and would stop being
    read just before the one entry that commits a reference set (doc 03).
    """
    now = timezone.now()
    existing = {d.mac: d for d in Device.objects.all()}

    for mac, row in incoming.items():
        room = rooms[row.room]
        device = existing.get(mac)

        if device is None:
            Device.objects.create(
                room=room,
                mac=mac,
                hostname=row.hostname,
                ip=row.ip,
                sophomorix_group=row.group,
                role=row.role,
                pxe=row.pxe,
                source=source,
                last_synced_at=now,
            )
            counters["devices_created"] += 1
            continue

        changes = {}
        if device.room_id != room.pk:
            changes["room"] = [device.room.name, room.name]
            device.room = room
            counters["moved"] += 1
        if device.hostname != row.hostname:
            changes["hostname"] = [device.hostname, row.hostname]
            device.hostname = row.hostname
            counters["renamed"] += 1
        if not device.is_active:
            # It was proposed for retirement, and it is back before anybody
            # acted on it. Coming back is not a decision to make.
            changes["is_active"] = [False, True]
            device.is_active = True
            device.retired_at = None

        device.ip = row.ip
        device.sophomorix_group = row.group
        device.role = row.role
        device.pxe = row.pxe
        device.last_synced_at = now
        device.save()

        if changes:
            _trace(actor, device, changes)


def _queue_decisions(
    run, incoming, incoming_rooms, previous, known_room_names, vanished_macs
) -> int:
    """Everything a machine may not settle on its own."""
    queued = 0

    for mac in sorted(vanished_macs):
        device = previous[mac]
        queued += _queue(
            run,
            SyncDecision.Kind.DEVICE_DISAPPEARED,
            key=f"device:{mac}",
            payload={
                "device_id": device.pk,
                "mac": mac,
                "hostname": device.hostname,
                "room": device.room.name,
            },
        )

    # Rooms present in the reflection but absent from the snapshot.
    previous_rooms: dict[str, set[str]] = {}
    for mac, device in previous.items():
        previous_rooms.setdefault(device.room.name, set()).add(mac)

    for name, old_macs in previous_rooms.items():
        if name in incoming_rooms:
            continue
        queued += _judge_vanished_room(run, name, old_macs, incoming, known_room_names)

    return queued


def _judge_vanished_room(run, name, old_macs, incoming, known_room_names) -> int:
    """Renamed, emptied, or gone -- and the difference matters enormously.

    The condition that separates them: **a room is only a rename candidate if
    the target name was absent from the previous reflection.** Without it, the
    most banal event of a school year -- machines moved from 204 to the very
    real 205 -- satisfies "its MACs reappear elsewhere" to the letter, and a
    hurried administrator merges two reference sets irreversibly.
    """
    survivors = {mac for mac in old_macs if mac in incoming}

    if not survivors:
        return _queue(
            run,
            SyncDecision.Kind.ROOM_DISAPPEARED,
            key=f"room:{name}",
            payload={"room": name, "devices": len(old_macs), "survivors": 0},
        )

    destinations: dict[str, int] = {}
    for mac in survivors:
        destinations[incoming[mac].room] = destinations.get(incoming[mac].room, 0) + 1

    new_names = {r: n for r, n in destinations.items() if r not in known_room_names}
    if not new_names:
        # Collective move into rooms that already exist. The devices have been
        # moved one by one already; the origin room stays, empty. That is the
        # exact description of what happened, and it needs no decision.
        logger.info("room %s emptied into %s -- no decision", name, sorted(destinations))
        return 0

    target = max(new_names, key=lambda r: new_names[r])
    overlap = new_names[target]
    confidence = overlap / len(old_macs)

    if confidence >= settings.ST_ROOM_RENAME_THRESHOLD:
        return _queue(
            run,
            SyncDecision.Kind.ROOM_RENAMED_SUSPECTED,
            key=f"room:{name}",
            payload={
                "room": name,
                "suspected_new_name": target,
                "overlap": overlap,
                "of": len(old_macs),
                "confidence": round(confidence, 2),
                # A one-machine room proves nothing: "the single MAC of X turns
                # up alone in Y" is indistinguishable from a workstation being
                # carried down the corridor (doc 03). Say so, rather than
                # present it with the same assurance as a match on eighteen.
                "weak_evidence": len(old_macs) < 2,
            },
        )

    # Rename and redistribution at once: the overlap falls below the threshold
    # and the rename is no longer certain. Present the room as gone -- a room
    # retired by mistake is reversible, a merge of two reference sets is not --
    # and mention the partial overlap for information only.
    return _queue(
        run,
        SyncDecision.Kind.ROOM_DISAPPEARED,
        key=f"room:{name}",
        payload={
            "room": name,
            "devices": len(old_macs),
            "survivors": len(survivors),
            "partial_overlap": {"name": target, "shared": overlap, "of": len(old_macs)},
        },
    )


def _queue(run: SyncRun, kind: str, *, key: str, payload: dict) -> int:
    """Queue a decision unless the identical one is already pending.

    Without this, a nightly sync re-files the same disappearance every night
    and the queue's whole value -- being short enough to be read -- is gone by
    the end of the week (doc 03: "sa valeur tient à sa rareté").
    """
    if SyncDecision.objects.filter(
        kind=kind,
        status=SyncDecision.Status.PENDING,
        payload__key=key,
    ).exists():
        return 0
    SyncDecision.objects.create(sync_run=run, kind=kind, payload={"key": key, **payload})
    return 1
