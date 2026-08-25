# SPDX-License-Identifier: GPL-3.0-or-later
"""Sending the Push. Runs in the worker, never in the web process.

Doc 09 §5: the web process holds no VAPID private key, exactly as it holds no
lmnapi secret (D-09). Same two environment files, same separation, same reason
-- the service exposed to the network carries no outbound secret.

Three things happen to a pending row here, in this order, and none is optional:

1. **``visible_to()`` is replayed.** Visibility may have been restricted since
   the event, and restricting is open to everyone (doc 08). A row that no
   longer passes is dropped, never delayed.
2. **The recipient's mute is honoured** -- for the Push only. The row stays
   unread in the application.
3. **The payload is built poor on purpose**: kind and room, never a
   description, never a comment, never anybody's name. It is read off a locked
   screen lying on a classroom table (§2).
"""

import json
import logging
from datetime import timedelta

from django.conf import settings
from django.db import models
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext as _
from django.utils.translation import override

from accounts.models import PushSubscription
from tickets.models import Ticket

from .models import Delivery, GROUPED, Kind, Mute, Notification

log = logging.getLogger(__name__)

#: One grouped Push per recipient per hour (doc 09 §6). Addressed events --
#: assignment, note, resolution -- are never held back: they carry an action.
GROUP_WINDOW = timedelta(hours=1)


def _visible(notification) -> bool:
    if notification.ticket_id is None:
        return True
    return Ticket.objects.visible_to(notification.recipient).filter(
        pk=notification.ticket_id
    ).exists()


def _headline(kind, *, room, count=1) -> str:
    """Everything a lock screen is allowed to learn."""
    if kind == Kind.TICKET_OPENED:
        if count > 1:
            return _("%(count)s new tickets") % {"count": count}
        return _("New ticket · %(room)s") % {"room": room}
    if kind == Kind.ASSIGNED:
        return _("Assigned to you · %(room)s") % {"room": room}
    if kind == Kind.COMMENT:
        return _("New note · %(room)s") % {"room": room}
    if kind == Kind.RESOLVED:
        return _("Marked resolved · %(room)s") % {"room": room}
    if kind == Kind.REOPENED:
        return _("Reopened · %(room)s") % {"room": room}
    if kind == Kind.ESCALATED:
        return _("Now urgent · %(room)s") % {"room": room}
    return _("Estate changes to review")


def _payload(notification, *, count=1) -> str:
    """Built in the **recipient's** language, not the worker's.

    The worker has no request and therefore no locale; without this override
    every Push would go out in LANGUAGE_CODE, which is German (D-15).
    """
    with override(notification.recipient.language or settings.LANGUAGE_CODE):
        if notification.ticket_id and count == 1:
            url = reverse("tickets:detail", args=[notification.ticket_id])
        elif notification.ticket_id:
            url = reverse("tickets:list")
        else:
            url = reverse("notifications:list")
        # **No title.** The school's name is identical on every notification, so
        # shipping it in each encrypted payload is waste -- and it would make
        # ST_SITE_NAME a setting the worker and the web both hold and must keep
        # equal, which is the kind of pair that quietly drifts. ``sw.js`` fills
        # it in from ``data.title || brand_name``, and the web renders sw.js,
        # so the name is configured in exactly one place (D-30).
        #
        # The cost, such as it is: a browser caches its service worker, so a
        # school that renames itself keeps the old name on Push until sw.js is
        # fetched again.
        return json.dumps({
            "body": str(_headline(
                notification.kind,
                room=notification.ticket.room_label if notification.ticket_id else "",
                count=count,
            )),
            "url": url,
        })


def _send(subscription, payload: str) -> bool:
    """One device. Returns False when the subscription should be forgotten."""
    from pywebpush import WebPushException, webpush

    try:
        webpush(
            subscription_info={
                "endpoint": subscription.endpoint,
                "keys": {"p256dh": subscription.p256dh, "auth": subscription.auth},
            },
            data=payload,
            vapid_private_key=settings.ST_VAPID_PRIVATE_KEY,
            vapid_claims={"sub": settings.ST_VAPID_SUBJECT},
            timeout=10,
        )
        return True
    except WebPushException as error:
        status = getattr(error.response, "status_code", None)
        if status in (404, 410):
            # The normal answer of an uninstalled browser or a revoked
            # permission. Forgotten rather than retried -- it is also what
            # stops the table growing with every change of phone (§5).
            log.info("push subscription %s gone (%s), removing", subscription.pk, status)
            subscription.delete()
            return False
        log.warning("push to %s failed: %s", subscription.pk, error)
        raise


def deliver_pending(limit: int = 200) -> dict:
    """The worker's job. Returns a summary for the log line."""
    if not settings.ST_VAPID_PRIVATE_KEY:
        # Said once per pass rather than per notification, and the rows stay
        # PENDING: configuring the keys later delivers the backlog.
        return {"skipped": "no VAPID key configured"}

    pending = list(
        Notification.objects.awaiting_push()
        .select_related("recipient", "ticket")
        .order_by("created_at")[:limit]
    )
    if not pending:
        return {"pending": 0}

    counts = {
        "sent": 0, "dropped": 0, "muted": 0, "read": 0,
        "no_device": 0, "held": 0, "failed": 0,
    }
    muted = {
        (mute.user_id, mute.kind)
        for mute in Mute.objects.filter(user__in={n.recipient_id for n in pending})
    }
    now = timezone.now()

    grouped: dict[int, list] = {}
    addressed: dict[tuple, list] = {}
    for notification in pending:
        if not _visible(notification):
            notification.delivery = Delivery.DROPPED
            notification.save(update_fields=["delivery"])
            counts["dropped"] += 1
            continue
        if notification.read_at is not None:
            # The delivery window means a Push can be a day late. Somebody who
            # opened the application in the meantime has already dealt with it;
            # ringing their phone about it is the excess doc 09 §1 warns is not
            # recoverable.
            notification.delivery = Delivery.READ
            notification.save(update_fields=["delivery"])
            counts["read"] += 1
            continue
        if (notification.recipient_id, notification.kind) in muted:
            notification.delivery = Delivery.MUTED
            notification.save(update_fields=["delivery"])
            counts["muted"] += 1
            continue
        if notification.kind in GROUPED:
            grouped.setdefault(notification.recipient_id, []).append(notification)
        else:
            # One Push per ticket, not per event. Four notes left on the same
            # ticket over a weekend are one thing to go and look at. The
            # service worker collapses them on the phone anyway (same `tag`);
            # doing it here makes it deterministic instead of incidental.
            addressed.setdefault(
                (notification.recipient_id, notification.ticket_id), []
            ).append(notification)

    for batch in addressed.values():
        counts[_push_addressed(batch, now)] += len(batch)

    for recipient_id, batch in grouped.items():
        counts[_push_group(recipient_id, batch, now)] += len(batch)

    return {key: value for key, value in counts.items() if value}


def _devices(recipient_id):
    return list(PushSubscription.objects.filter(user_id=recipient_id))


def _mark(rows, state, when=None):
    Notification.objects.filter(pk__in=[row.pk for row in rows]).update(
        delivery=state, pushed_at=when
    )


def _push_addressed(batch, now) -> str:
    """Everything pending about one ticket, for one person, in one Push.

    The headline is the most recent event: after a note and a resolution, what
    matters is that it was resolved.
    """
    devices = _devices(batch[0].recipient_id)
    if not devices:
        _mark(batch, Delivery.NO_DEVICE)
        return "no_device"
    return _deliver(batch, devices, _payload(batch[-1]), now)


def _push_group(recipient_id, batch, now) -> str:
    """At most one grouped Push per recipient per hour (§6).

    When the window has not elapsed the rows are **left PENDING**, not dropped:
    they will go out together with whatever arrives next, and they are readable
    in the application the whole time.
    """
    last = (
        Notification.objects.filter(
            recipient_id=recipient_id, kind__in=GROUPED, delivery=Delivery.SENT
        )
        .aggregate(models.Max("pushed_at"))["pushed_at__max"]
    )
    if last and now - last < GROUP_WINDOW:
        return "held"

    devices = _devices(recipient_id)
    if not devices:
        _mark(batch, Delivery.NO_DEVICE)
        return "no_device"
    return _deliver(batch, devices, _payload(batch[-1], count=len(batch)), now)


def _deliver(rows, devices, payload, now) -> str:
    reached = False
    for device in devices:
        try:
            reached = _send(device, payload) or reached
        except Exception:  # noqa: BLE001 - one bad device must not stop the pass
            log.exception("push failed for subscription %s", device.pk)
    if reached:
        _mark(rows, Delivery.SENT, now)
        return "sent"
    _mark(rows, Delivery.FAILED)
    return "failed"
