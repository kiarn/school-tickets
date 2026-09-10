# SPDX-License-Identifier: GPL-3.0-or-later
"""Bodies of the worker jobs.

The inventory pass is complete: ``GET /v1/devices/list/{school}`` has been read
on a live lmn 7.4.11 and ``sources.rows_from_api`` is written against it.

The LINBO sweep is not, and cannot be: ``GET /v1/linbo/hosts/image-status``
keys its answer by hostname and carries no MAC, so there is nothing to file
under (Q-03). ``_index_by_mac`` refuses rather than degrade -- see there for
why silence would be the dangerous option. Two points of semantics are also
still open: ``imageVersion`` was null on every row, and ``action`` was
``applied`` on every row, so doc 05's question -- last successful sync, or
last contact? -- is not settled either.

Each function returns a dict of counters, which the heartbeat records.
"""

import logging
from datetime import datetime

from django.conf import settings
from django.utils import timezone

from parc import sources
from parc.inventory import apply_inventory
from parc.lmnapi import Client, Forbidden, NotConfigured
from parc.models import Device, DeviceStatus

logger = logging.getLogger(__name__)


class LinboPayloadError(RuntimeError):
    """The LINBO endpoint answered, but not with something we can file."""


def _index_by_mac(response) -> dict:
    """The response is filed under the MAC, never under the key we sent.

    A hostname remembered at the last inventory sync may have changed since;
    the MAC has not (doc 03).

    ``GET /v1/linbo/hosts/image-status`` answers
    ``{"hosts": {hostname: {lastSync, action, image, imageVersion}}, "total": n}``
    -- keyed by hostname, and **carrying no MAC**. That is the one thing Q-03
    asked the API for and the one thing it does not yet give.

    Raising here is the whole point. Filing by hostname instead would break
    doc 03's rule; returning an empty index would be worse still, because
    ``_write_status`` reads "no row" as "machine unreachable" and would post a
    false alarm on every PXE machine in the estate, silently, on every sweep.
    """
    hosts = response.get("hosts")
    if not isinstance(hosts, dict):
        raise LinboPayloadError(f"expected a 'hosts' object, got {list(response)}")

    by_mac = {}
    for hostname, entry in hosts.items():
        mac = (entry.get("mac") or "").lower()
        if not mac:
            raise LinboPayloadError(
                f"{hostname}: the LINBO response carries no MAC, so nothing can "
                "be filed under one (specs/07, Q-03)"
            )
        by_mac[mac] = entry
    return by_mac


def _parse_last_sync(value):
    """``"2026-08-07T14:40:00.000Z"`` -- a string, and an aware one.

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


def _write_status(by_mac: dict, *, macs=None) -> int:
    now = timezone.now()
    # Only machines that actually boot over PXE have a LINBO state to miss.
    # Read from the flag and never inferred from the role: on real estate data
    # the two disagree (see Device.expects_linbo). Sweeping everything would
    # mark every printer, router, NAS and server permanently "unreachable" --
    # a whole estate of false alarms, which is exactly what doc 05 forbids.
    queryset = Device.objects.filter(is_active=True, pxe__gt=0)
    if macs is not None:
        queryset = queryset.filter(mac__in=macs)

    written = 0
    for device in queryset.iterator():
        row = by_mac.get(device.mac.lower())
        status, _created = DeviceStatus.objects.get_or_create(device=device)
        if row is None:
            status.fetch_status = DeviceStatus.FetchStatus.UNREACHABLE
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
    """One collective call for the whole estate, then filing by MAC."""
    client = Client()
    try:
        response = client.linbo_status_all()
    except NotConfigured as exc:
        logger.warning("lmnapi not configured: %s", exc)
        return {"skipped": "not_configured"}
    except Forbidden:
        DeviceStatus.objects.update(fetch_status=DeviceStatus.FetchStatus.FORBIDDEN)
        raise

    return {"devices": _write_status(_index_by_mac(response))}


def drain_refresh_queue() -> dict:
    """The queue is only a trigger, not a variant.

    No outgoing addressing key: we repeat the same collective call as
    ``sweep_linbo``, which removes the whole class of bugs tied to stale names.
    """
    pending = DeviceStatus.objects.filter(refresh_requested_at__isnull=False)
    if not pending.exists():
        return {}
    logger.info("refresh requested on %d device(s)", pending.count())
    return sweep_linbo()


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
