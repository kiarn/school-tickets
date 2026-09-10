# SPDX-License-Identifier: GPL-3.0-or-later
"""Bodies of the worker jobs.

The inventory pass is complete: ``GET /v1/devices/list/{school}`` has been read
on a live lmn 7.4.11 and ``sources.rows_from_api`` is written against it.

LINBO is read through two endpoints, and the difference is not a detail:

``GET /v1/linbo/hosts/image-status`` -- the whole estate in one call, keyed by
hostname and carrying no MAC. Used for the hourly sweep, where D-20's cost
argument rules: 300 machines must not become 300 calls. Filed by hostname, with
the benign risk that entails (see ``_index_by_hostname``).

``GET /v1/linbo/hosts/{hostname}/status`` -- one machine, and it **carries the
MAC**, so doc 05's rule ("address by what lmn can name, file under the MAC")
can be applied as written. Used on demand, when somebody presses refresh on a
machine they are looking at. It also answers per image, which is what makes
"up to date" measurable and what separates "never applied" from "no
information".

Both answer UTC, and the two agree -- checked against a live server rather than
assumed. ``_parse_last_sync`` accepts either serialisation an lmn may send, a
trailing ``Z`` or an explicit ``+00:00``, so an instance running an older
server is not left with unreadable dates.

Each function returns a dict of counters, which the heartbeat records.
"""

import collections
import logging
from datetime import datetime

from django.conf import settings
from django.utils import timezone

from parc import sources
from parc.inventory import apply_inventory
from parc.lmnapi import Client, Forbidden, LmnApiError, NotConfigured
from parc.models import Device, DeviceStatus

logger = logging.getLogger(__name__)


class LinboPayloadError(RuntimeError):
    """The LINBO endpoint answered, but not with something we can file."""


def _index_by_hostname(response) -> dict:
    """``{"hosts": {hostname: {lastSync, action, image, imageVersion}}, "total": n}``

    Keyed by hostname, and carrying no MAC -- and that is accepted rather than
    fought. Doc 03's "file under the MAC" governs the **inventory**, where
    mistaking one machine for another corrupts the referential and can detach
    a ticket from its device. This regime writes ``device_status`` and nothing
    else, rewritten whole on every hourly pass; doc 05 classes an error here
    as benign, "a stale value is displayed".

    What that costs is one narrow case: a hostname reassigned to a different
    machine between two nightly inventories, which would show the newcomer's
    state on the old row until the next inventory puts it right. The other
    case -- a machine renamed and not yet re-inventoried -- resolves to no
    match at all, which doc 05 already describes as the expected freshness
    gap after a rename.
    """
    hosts = response.get("hosts")
    if not isinstance(hosts, dict):
        raise LinboPayloadError(f"expected a 'hosts' object, got {list(response)}")
    return {str(hostname).strip().lower(): entry for hostname, entry in hosts.items()}


def _parse_last_sync(value):
    """A string, and an aware one -- in either serialisation lmn may send.

    ``"2026-08-07T12:40:00+00:00"`` from a current server, or
    ``"2026-08-07T14:40:00.000Z"`` from an older one. Both are read, so the
    application does not depend on which version an instance runs.

    A machine whose timestamp is unreadable is still a machine that answered:
    losing the date is right, losing the row is not (same rule as a malformed
    IP in ``sources``).
    """
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        logger.warning("unreadable lastSync: %r", value)
        return None


def _write_status(by_hostname: dict) -> int:
    now = timezone.now()
    # Only machines that actually boot over PXE have a LINBO state to miss.
    # Read from the flag and never inferred from the role: on real estate data
    # the two disagree (see Device.expects_linbo). Sweeping everything would
    # file an absence of data on every printer, router, NAS and server -- a
    # whole estate of them, which is exactly what doc 05 forbids.
    queryset = Device.objects.filter(is_active=True, pxe__gt=0)

    # A hostname is not unique in this table, and an ambiguous one is not
    # guessed: two machines answering to the same name means we cannot say
    # which the log belongs to, so neither gets it. Rare, and cheap to check.
    # Counted on the normalised name, the same one the lookup uses: "Client1"
    # and "client1" are one name to lmn and must be one name here too.
    ambiguous = {
        name
        for name, count in collections.Counter(
            h.strip().lower()
            for h in Device.objects.filter(is_active=True, pxe__gt=0).values_list(
                "hostname", flat=True
            )
        ).items()
        if count > 1
    }
    if ambiguous:
        logger.warning("hostname is not unique, so not filed: %s", sorted(ambiguous))

    written = 0
    for device in queryset.iterator():
        name = device.hostname.strip().lower()
        row = None if name in ambiguous else by_hostname.get(name)
        status, _created = DeviceStatus.objects.get_or_create(device=device)
        if row is None:
            # The server held nothing under this name. That is all it means.
            status.fetch_status = DeviceStatus.FetchStatus.NO_DATA
        else:
            status.fetch_status = DeviceStatus.FetchStatus.OK
            status.last_linbo_sync_at = _parse_last_sync(row.get("lastSync"))
            status.linbo_image = row.get("image") or ""
            status.raw = row
        status.observed_at = now
        status.refresh_requested_at = None
        status.save()
        written += 1
    return written


def sweep_linbo() -> dict:
    """One collective call for the whole estate, then filing by hostname."""
    client = Client()
    try:
        response = client.linbo_status_all()
    except NotConfigured as exc:
        logger.warning("lmnapi not configured: %s", exc)
        return {"skipped": "not_configured"}
    except Forbidden:
        DeviceStatus.objects.update(fetch_status=DeviceStatus.FetchStatus.FORBIDDEN)
        raise

    return {"devices": _write_status(_index_by_hostname(response))}


def refresh_device(device, *, probe: bool = False) -> str:
    """One machine, addressed by name, filed under its MAC.

    Doc 05's rule verbatim -- "on adresse l'API par ce que lmn sait nommer, on
    écrit en base par la MAC" -- and the first place it can be honoured, since
    this response carries the MAC and the collective one does not.

    So the name is treated as an address and nothing more. If the machine that
    answers is not the machine we asked about, its state is **not** written:
    that is the reassigned-hostname case, and filing it would put one
    machine's repair history on another.

    Returns the outcome as a short string, for the counters the heartbeat keeps.
    """
    payload = Client().linbo_status_host(
        device.hostname, school=settings.ST_DEFAULT_SCHOOL_SLUG, probe=probe
    )

    answered = Device.normalize_mac(payload.get("mac") or "")
    status, _created = DeviceStatus.objects.get_or_create(device=device)
    status.observed_at = timezone.now()
    status.refresh_requested_at = None

    if answered and answered != device.mac:
        logger.warning(
            "%s now answers for %s, not %s: state not filed",
            device.hostname, answered, device.mac,
        )
        status.fetch_status = DeviceStatus.FetchStatus.NO_DATA
        status.save()
        return "reassigned"

    # One entry per image of the group; the applied ones carry a date. The most
    # recent of those is what "last synchronised" means for the machine as a
    # whole -- the rest stays in `raw`, where a per-image screen can find it.
    applied = [
        (_parse_last_sync(image.get("lastSync")), image)
        for image in payload.get("images") or []
        if image.get("lastSync")
    ]
    applied = [(when, image) for when, image in applied if when is not None]

    status.raw = payload
    if applied:
        when, image = max(applied, key=lambda pair: pair[0])
        status.fetch_status = DeviceStatus.FetchStatus.OK
        status.last_linbo_sync_at = when
        status.linbo_image = image.get("image") or ""
        outcome = "ok"
    else:
        # The group's images are known and none has ever been applied here.
        # Still an absence of information about a synchronisation, not a fault.
        status.fetch_status = DeviceStatus.FetchStatus.NO_DATA
        status.last_linbo_sync_at = None
        status.linbo_image = ""
        outcome = "never_applied"

    status.save()
    return outcome


def drain_refresh_queue() -> dict:
    """The queue is a trigger, and now one call per machine rather than a sweep.

    Somebody pressed a button on a machine and is watching that machine: the
    per-host endpoint answers about it alone, carries its MAC, and costs lmn
    one read instead of the whole estate's. ``probe`` stays off -- contacting
    the machine is a separate, explicit act, not a side effect of a button
    that asks what the logs say.
    """
    pending = list(
        DeviceStatus.objects.filter(refresh_requested_at__isnull=False).select_related(
            "device"
        )
    )
    if not pending:
        return {}

    logger.info("refresh requested on %d device(s)", len(pending))
    outcomes: dict = {}
    for status in pending:
        try:
            outcome = refresh_device(status.device)
        except Forbidden:
            DeviceStatus.objects.update(fetch_status=DeviceStatus.FetchStatus.FORBIDDEN)
            raise
        except LmnApiError as exc:
            # One machine's failure must not strand the rest of the queue: the
            # request is cleared either way, or the button stays stuck for good.
            logger.warning("refresh failed for %s: %s", status.device.hostname, exc)
            DeviceStatus.objects.filter(pk=status.pk).update(refresh_requested_at=None)
            outcome = "failed"
        outcomes[outcome] = outcomes.get(outcome, 0) + 1
    return outcomes


def sync_inventory() -> dict:
    """Full snapshot from lmnapi, then the reconciliation of doc 03."""
    client = Client()
    try:
        payload = client.inventory(school=settings.ST_DEFAULT_SCHOOL_SLUG)
    except NotConfigured as exc:
        logger.warning("lmnapi not configured: %s", exc)
        return {"skipped": "not_configured"}

    estate = sources.rows_from_api(payload)

    # What was dropped is logged rather than counted silently: a source that
    # starts skipping rows is how an estate quietly shrinks, and the volume
    # guard downstream only ever sees the survivors.
    if estate.skipped:
        logger.info("inventory: %d row(s) skipped %s", len(estate.skipped), estate.reasons())

    run = apply_inventory(estate.rows, source="lmnapi")
    return {
        "status": run.status,
        "rooms": run.rooms_seen,
        "devices": run.devices_seen,
        "skipped": estate.reasons(),
    }
