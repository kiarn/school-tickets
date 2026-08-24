# SPDX-License-Identifier: GPL-3.0-or-later
"""What we send people, and whether it got there. See specs/09-notifications.md.

A fifth application, which D-16 did not foresee. The line it draws: ``accounts``
owns *who a person is and which devices are theirs* -- ``PushSubscription``
stays there, next to the row anonymisation wipes. This application owns *the
messages* and their delivery. Nothing here is a property of a person; every row
is an event that happened to them.
"""

from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _


class Kind(models.TextChoices):
    """The events of doc 09 §3, and only those.

    Adding one means answering §3's question first -- *what does this person
    have to do about it?* -- not merely noticing that something happened.
    """

    TICKET_OPENED = "ticket_opened", _("New ticket")
    ASSIGNED = "assigned", _("Assigned to you")
    COMMENT = "comment", _("New note")
    RESOLVED = "resolved", _("Marked resolved")
    REOPENED = "reopened", _("Reopened")
    SYNC_DECISIONS = "sync_decisions", _("Estate changes to review")


#: Grouped rather than sent one by one (doc 09 §6). Only the untargeted one:
#: a notification that carries an action for *you* is never held back.
GROUPED = frozenset({Kind.TICKET_OPENED})


class Delivery(models.TextChoices):
    """Why a Push did or did not leave. Never why a row exists.

    The row is the truth and stays readable in the application whatever
    happens here: "le Push est un rappel, jamais le seul exemplaire" (§5).
    """

    PENDING = "pending", _("Waiting")
    SENT = "sent", _("Sent")
    MUTED = "muted", _("Muted by the recipient")
    # Read in the application before the Push ever left -- which happens
    # whenever the delivery window held it back. Pushing then would be
    # announcing something the person has already dealt with.
    READ = "read", _("Already read in the application")
    # The visibility of the ticket was restricted between the event and the
    # delivery. Dropped, never delayed: doc 09 §2.
    DROPPED = "dropped", _("No longer visible")
    NO_DEVICE = "no_device", _("No device subscribed")
    FAILED = "failed", _("Delivery failed")


class NotificationQuerySet(models.QuerySet):
    def unread(self):
        return self.filter(read_at__isnull=True)

    def awaiting_push(self):
        return self.filter(delivery=Delivery.PENDING)


class Notification(models.Model):
    recipient = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="notifications"
    )
    kind = models.CharField(max_length=20, choices=Kind.choices)
    # NULL for the one event that is not about a ticket (pending sync
    # decisions), which is why this column may not be made required.
    ticket = models.ForeignKey(
        "tickets.Ticket", on_delete=models.CASCADE, null=True, blank=True,
        related_name="notifications",
    )
    # Who caused it. Recorded for the in-app list; **never** put in a Push
    # payload, which is read off a lock screen (doc 09 §2).
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    read_at = models.DateTimeField(null=True, blank=True)

    delivery = models.CharField(
        max_length=12, choices=Delivery.choices, default=Delivery.PENDING
    )
    pushed_at = models.DateTimeField(null=True, blank=True)

    objects = NotificationQuerySet.as_manager()

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            # The badge on every page: unread, for one person.
            models.Index(fields=["recipient", "read_at"]),
            # The worker's one query.
            models.Index(fields=["delivery", "created_at"]),
        ]

    def __str__(self):
        return f"{self.get_kind_display()} -> {self.recipient_id}"


class Mute(models.Model):
    """A silence somebody put on one kind of event.

    It silences the **Push only**. The row still appears in the application,
    which is the direct consequence of doc 09 §5: muting a reminder is not
    asking to be kept in the dark.

    Absence of a row means "send", so a new kind starts out audible for
    everybody -- the alternative would have new events arrive silently.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="notification_mutes"
    )
    kind = models.CharField(max_length=20, choices=Kind.choices)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["user", "kind"], name="unique_mute_per_kind")
        ]
