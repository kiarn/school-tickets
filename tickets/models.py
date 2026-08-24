# SPDX-License-Identifier: GPL-3.0-or-later
"""Interventions. See specs/02-modele-donnees.md and specs/08-visibilite.md."""

from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _

from accounts.authz import Visibility, clearance


class TicketQuerySet(models.QuerySet):
    def visible_to(self, user):
        """THE mandatory gate (D-21).

        No view queries ``Ticket.objects`` without coming through here. Three
        access paths, two of them independent of the floor:

        - the visibility floor, compared with the role's clearance;
        - the author, who never loses sight of their own report;
        - the assignees, because you see what you are meant to repair.
        """
        if not getattr(user, "is_authenticated", False) or not user.is_active:
            return self.none()
        return self.filter(
            models.Q(school=user.school)
            & (
                models.Q(visibility__gte=clearance(user))
                | models.Q(created_by=user)
                | models.Q(assignees=user)
            )
        ).distinct()


class Ticket(models.Model):
    class Status(models.TextChoices):
        OPEN = "open", _("Open")
        IN_PROGRESS = "in_progress", _("In progress")
        RESOLVED = "resolved", _("Resolved")
        CANCELLED = "cancelled", _("Cancelled")

    class Priority(models.TextChoices):
        LOW = "low", _("Low")
        NORMAL = "normal", _("Normal")
        HIGH = "high", _("High")

    school = models.ForeignKey("accounts.School", on_delete=models.PROTECT, related_name="tickets")
    room = models.ForeignKey("parc.Room", on_delete=models.PROTECT, related_name="tickets")
    device = models.ForeignKey(
        "parc.Device", on_delete=models.SET_NULL, null=True, blank=True, related_name="tickets"
    )
    # Label frozen at creation: a ticket from March will always read "Room 204"
    # even after a rename to A204, because that is what was true that day. It
    # does not cancel the foreign key, it accompanies it.
    room_label = models.CharField(max_length=200)

    title = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.OPEN)
    priority = models.CharField(max_length=10, choices=Priority.choices, default=Priority.NORMAL)

    visibility = models.PositiveSmallIntegerField(
        choices=Visibility.choices,
        default=Visibility.TEAM,
        help_text=_("Who may read this ticket. Widening retroactively publishes the thread."),
    )

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="tickets_created"
    )
    assignees = models.ManyToManyField(
        settings.AUTH_USER_MODEL,
        through="TicketAssignee",
        through_fields=("ticket", "user"),
        related_name="tickets_assigned",
    )
    tags = models.ManyToManyField("Tag", through="TicketTag", related_name="tickets", blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    resolved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="tickets_resolved",
    )
    resolved_at = models.DateTimeField(null=True, blank=True)
    # A reopened ticket signals an incomplete retest. Material for a
    # conversation with the pupil, never for a sanction, never shown publicly.
    reopened_count = models.PositiveIntegerField(default=0)

    objects = TicketQuerySet.as_manager()

    class Meta:
        indexes = [
            # The query behind every list.
            models.Index(fields=["school", "visibility", "status"]),
        ]
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.room_label} - {self.title}"

    def save(self, *args, **kwargs):
        if not self.room_label and self.room_id:
            self.room_label = self.room.name
        super().save(*args, **kwargs)


class TicketAssignee(models.Model):
    ticket = models.ForeignKey(Ticket, on_delete=models.CASCADE)
    # Deleted on anonymisation: no named assignment trace left behind.
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    assigned_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="+"
    )
    assigned_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["ticket", "user"], name="unique_ticket_assignee")
        ]


class Tag(models.Model):
    # Always local to a school and never translated: they therefore never enter
    # the Crowdin catalogues (D-15).
    school = models.ForeignKey("accounts.School", on_delete=models.CASCADE, related_name="tags")
    slug = models.SlugField()
    name = models.CharField(max_length=100)
    color = models.CharField(max_length=20, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["school", "slug"], name="unique_tag_per_school")
        ]

    def __str__(self):
        return self.name


class TicketTag(models.Model):
    ticket = models.ForeignKey(Ticket, on_delete=models.CASCADE)
    tag = models.ForeignKey(Tag, on_delete=models.CASCADE)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["ticket", "tag"], name="unique_ticket_tag")]


class Comment(models.Model):
    """The team's technical memory: what the previous person already tried."""

    ticket = models.ForeignKey(Ticket, on_delete=models.CASCADE, related_name="comments")
    # Kept after anonymisation, displayed as "Former member".
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="comments"
    )
    body = models.TextField()  # Markdown: code blocks and emoji, hence utf8mb4 (D-03)
    created_at = models.DateTimeField(auto_now_add=True)
    edited_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["created_at"]


class Attachment(models.Model):
    """A photo taken on the spot is worth three sentences thumbed on a phone.

    Never served by the web server: see ``tickets.views.attachment``.
    """

    ticket = models.ForeignKey(Ticket, on_delete=models.CASCADE, related_name="attachments")
    comment = models.ForeignKey(
        Comment, on_delete=models.CASCADE, null=True, blank=True, related_name="attachments"
    )
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    filename = models.CharField(max_length=255)
    mime = models.CharField(max_length=100)
    size_bytes = models.PositiveIntegerField()
    storage_path = models.CharField(max_length=500)
    width = models.PositiveIntegerField(null=True, blank=True)
    height = models.PositiveIntegerField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
