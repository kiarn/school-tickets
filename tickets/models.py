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
            models.Q(visibility__gte=clearance(user))
            | models.Q(created_by=user)
            | models.Q(assignees=user)
        ).distinct()


class Ticket(models.Model):
    class Status(models.TextChoices):
        OPEN = "open", _("Open")
        IN_PROGRESS = "in_progress", _("In progress")
        RESOLVED = "resolved", _("Resolved")
        CANCELLED = "cancelled", _("Cancelled")

    class Priority(models.TextChoices):
        """Four levels, and the fourth was asked for by name (D-37).

        ``urgent`` sits above ``high`` because two degrees of hurry exist in a
        school, and they are not the same sentence: "a class cannot happen right
        now" against "do not let this wait a fortnight". The list view named
        "Urgent" shows both -- it answers "what do I do next", and a `high`
        ticket with nowhere to be seen would be a level nobody ever sets.

        The order of declaration is the order of the picker. It is also the
        only order: `priority` is a text column, so sorting on it sorts
        alphabetically -- see ``PRIORITY_RANK`` in the views.
        """

        LOW = "low", _("Low")
        NORMAL = "normal", _("Normal")
        HIGH = "high", _("High")
        URGENT = "urgent", _("Urgent")

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
    # Set when the description is rewritten, never by any other save (D-42).
    # The same marker `Comment.edited_at` carries, and for the same reason: the
    # thread below may answer a sentence that no longer reads the same way, and
    # the reader is owed the fact that it changed.
    description_edited_at = models.DateTimeField(null=True, blank=True)
    resolved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="tickets_resolved",
    )
    resolved_at = models.DateTimeField(null=True, blank=True)
    # The note that says what actually worked (D-29). A foreign key on the
    # ticket rather than a flag on Comment: a ticket has one resolution or
    # none, which the key says by itself, and the list can draw it without
    # reading a single comment. SET_NULL because a deleted note must not take
    # the ticket with it.
    resolution_comment = models.ForeignKey(
        "Comment",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        verbose_name=_("Resolution"),
    )
    # A reopened ticket signals an incomplete retest. Material for a
    # conversation with the pupil, never for a sanction, never shown publicly.
    reopened_count = models.PositiveIntegerField(default=0)

    objects = TicketQuerySet.as_manager()

    class Meta:
        indexes = [
            # The query behind every list.
            models.Index(fields=["visibility", "status"]),
        ]
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.room_label} - {self.title}"

    def save(self, *args, **kwargs):
        if not self.room_label and self.room_id:
            self.room_label = self.room.name
        super().save(*args, **kwargs)

    @property
    def status_class(self) -> str:
        """How the state is drawn, decided here rather than in two templates.

        The colour is the state and nothing else (D-36): it is the one chip
        left in the header, so no tag and no priority competes with it. These
        are daisyUI's own component classes, not Tailwind utilities, which is
        why they survive being written in Python -- the scanner only reads
        ``templates/`` (D-18) but ``badge.css`` is imported whole.
        """
        return {
            self.Status.OPEN: "badge-warning",
            self.Status.IN_PROGRESS: "badge-info",
            self.Status.RESOLVED: "badge-success",
        }.get(self.status, "badge-ghost")

    @property
    def resolution_missing(self) -> bool:
        """Closed with nothing written down about what worked.

        Shown, never prevented (D-29). A required field would not teach anyone
        to write a resolution, it would teach them to type "ok"; and it would
        block the tickets that legitimately have none -- a duplicate, a false
        alarm, a machine replaced. So the gap is drawn on the page and in the
        list, and the form still closes.
        """
        return self.status == self.Status.RESOLVED and self.resolution_comment_id is None


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
    # Local to this instance and never translated: they therefore never enter
    # the Crowdin catalogues (D-15).

    class Color(models.TextChoices):
        """The theme's palette, by name rather than by value.

        These are daisyUI's semantic colours, and the value **is** the CSS
        class suffix -- which is why this is a fixed list and not the free
        text it used to be. Free text made a tag whose colour silently did
        nothing: the template composes ``badge-<value>``, and a typed
        "orange" composes a class that does not exist.

        No hex code, deliberately: a colour picked here has to stay readable
        in the light theme and in the dark one, and only the palette knows how
        (D-18).
        """

        PRIMARY = "primary", _("Primary")
        SECONDARY = "secondary", _("Secondary")
        ACCENT = "accent", _("Accent")
        NEUTRAL = "neutral", _("Neutral")
        INFO = "info", _("Info")
        SUCCESS = "success", _("Success")
        WARNING = "warning", _("Warning")
        ERROR = "error", _("Error")

    slug = models.SlugField()
    name = models.CharField(max_length=100)
    color = models.CharField(
        max_length=20,
        blank=True,
        choices=Color.choices,
        verbose_name=_("Colour"),
        help_text=_("Leave empty for a plain outline."),
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["slug"], name="unique_tag_slug")
        ]

    def __str__(self):
        return self.name

    @property
    def badge_class(self) -> str:
        """How the chip is drawn, decided here rather than in three templates.

        A tinted tag is *soft* and never solid: a row of saturated chips
        competes with the priority badge next to it, which is the one thing on
        a card that has to be read first.

        No colour keeps the outline rather than falling back to a grey tint --
        "none chosen" has to stay distinguishable from ``neutral``, which is a
        deliberate choice somebody can make.
        """
        return f"badge-soft badge-{self.color}" if self.color else "badge-outline"


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
