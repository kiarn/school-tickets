# SPDX-License-Identifier: GPL-3.0-or-later
"""Bodies of the worker jobs.

State: **skeleton**. The loop, the cadences and the filing-by-MAC are the
decided part (D-20); the exact shape of lmnapi responses is not (Q-03, the
semantics of the timestamps). Each function returns a dict of counters, which
the heartbeat records.
"""

import logging

from django.conf import settings
from django.utils import timezone

from parc import sources
from parc.inventory import apply_inventory
from parc.lmnapi import Client, Forbidden, NotConfigured
from parc.models import Device, DeviceStatus

logger = logging.getLogger(__name__)


def _index_by_mac(rows) -> dict:
    """The response is filed under the MAC, never under the key we sent.

    A hostname remembered at the last inventory sync may have changed since;
    the MAC has not (doc 03).
    """
    return {row["mac"].lower(): row for row in rows if row.get("mac")}


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
            status.last_linbo_sync_at = row.get("last_sync_at")
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
        response = client.linbo_status_all(school=settings.ST_DEFAULT_SCHOOL_SLUG)
    except NotConfigured as exc:
        logger.warning("lmnapi not configured: %s", exc)
        return {"skipped": "not_configured"}
    except Forbidden:
        DeviceStatus.objects.update(fetch_status=DeviceStatus.FetchStatus.FORBIDDEN)
        raise

    return {"devices": _write_status(_index_by_mac(response.get("devices", [])))}


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
    """Full snapshot from lmnapi, then the reconciliation of doc 03.

    Blocked one step short of the end, and knowingly so: ``apply_inventory``
    and the volume guard are written and tested, but ``rows_from_api`` cannot
    be written without one real ``/v1/devices`` response (Q-03). Until then
    the CSV path -- ``sync_parc --from-file`` -- exercises exactly the same
    reconciliation code.
    """
    client = Client()
    try:
        payload = client.inventory(school=settings.ST_DEFAULT_SCHOOL_SLUG)
    except NotConfigured as exc:
        logger.warning("lmnapi not configured: %s", exc)
        return {"skipped": "not_configured"}

    try:
        rows = sources.rows_from_api(payload)
    except NotImplementedError as exc:
        logger.warning("inventory sync held: %s", exc)
        return {"skipped": "awaiting_api_shape"}

    run = apply_inventory(rows, source="lmnapi")
    return {"status": run.status, "rooms": run.rooms_seen, "devices": run.devices_seen}
