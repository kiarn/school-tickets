# SPDX-License-Identifier: GPL-3.0-or-later
"""Ticket views.

Two non-negotiable rules.

**Reading** (D-21): every read starts from
``Ticket.objects.visible_to(request.user)``. A ticket you are not allowed to
see returns **404 and never 403** -- on sequential identifiers, a 403 would
reveal existence.

**Writing**: being able to read a ticket is not being able to change it. Each
write view names the predicate it needs, from ``accounts.authz``, and the
answer to a refused write is the same 404 -- an error page that distinguished
"not yours" from "does not exist" would leak the same thing.

State changes are ordinary forms that redirect, not HTMX fragments. Doc 08 asks
that every fragment be secured one by one; a POST that ends in a redirect has
one gate instead of two, and works with a dead battery's worth of JavaScript.
"""

import mimetypes
from pathlib import Path

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Case, IntegerField, Prefetch, Q, When
from django.http import FileResponse, Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

from accounts.authz import (
    ADMIN_ROLES,
    VISIBILITY_HELP,
    WORKING_ROLES,
    Role,
    can_widen,
    can_work_on,
    visibility_targets,
)
from accounts.models import AuditLog, User
from notifications import events
from parc.models import Device, Room

from . import attachments as photos
from .forms import CommentForm, TicketCorrectionForm, TicketForm, VisibilityForm
from .models import Attachment, Comment, Tag, Ticket, TicketAssignee, TicketTag


def _ticket_or_404(request, pk):
    return get_object_or_404(Ticket.objects.visible_to(request.user), pk=pk)


#: The list offers work views, not a status picker. Nothing here answers
#: "show me everything": the archive is reached through a room, never by
#: scrolling past it.
VIEWS = ("open", "in_progress", "urgent", "mine")
DEFAULT_VIEW = "open"
ACTIVE = (Ticket.Status.OPEN, Ticket.Status.IN_PROGRESS)
PAGE_SIZE = 20

#: Urgent first, in every view. Ordering on ``priority`` itself sorts
#: alphabetically -- high, low, normal, urgent -- which reads as correct until a
#: `low` turns up, and which puts the most urgent level last. Annotated rather
#: than passed to ``order_by`` directly: the expression then sits in the SELECT
#: list, which DISTINCT queries require.
PRIORITY_RANK = Case(
    When(priority=Ticket.Priority.URGENT, then=0),
    When(priority=Ticket.Priority.HIGH, then=1),
    When(priority=Ticket.Priority.NORMAL, then=2),
    default=3,
    output_field=IntegerField(),
)

#: What the "Urgent" view holds (D-37). Both levels above normal, not `urgent`
#: alone: the tab answers "what do I do next", and a `high` ticket that no view
#: shows is a level nobody would ever set.
HURRIED = (Ticket.Priority.URGENT, Ticket.Priority.HIGH)


@login_required
def ticket_list(request):
    view = request.GET.get("view") or DEFAULT_VIEW
    # An unknown view, or "mine" asked by somebody who repairs nothing, falls
    # back to the default. Not a 404: no object is being hidden here, and an
    # error page would be a strange answer to a mistyped query string.
    if view not in VIEWS or (view == "mine" and not can_work_on(request.user)):
        view = DEFAULT_VIEW

    tickets = (
        Ticket.objects.visible_to(request.user)
        # ``resolution_comment`` joined here rather than read per card: the
        # whole point of hanging the mark on the ticket (D-29) is that the list
        # shows what worked without loading anybody's thread.
        .select_related("room", "created_by", "resolution_comment")
        .prefetch_related("tags")
    )

    # A search reaches the archive whether or not the box is ticked: "how did we
    # fix this last time" is answered by a resolved ticket, so restricting the
    # statuses here would silence the one case the search exists for.
    search = request.GET.get("q", "").strip()
    closed = request.GET.get("closed") == "1" or bool(search)
    if not closed:
        tickets = tickets.filter(
            status__in=[Ticket.Status.OPEN] if view == "open"
            else [Ticket.Status.IN_PROGRESS] if view == "in_progress"
            else ACTIVE
        )
    if view == "urgent":
        tickets = tickets.filter(priority__in=HURRIED)
    if view == "mine":
        tickets = tickets.filter(assignees=request.user)

    room = request.GET.get("room")
    if room and room.isdigit():
        tickets = tickets.filter(room_id=room)
    else:
        room = ""

    # Resolved before it filters, like `room` just above. A slug matching no tag
    # -- a typo, or a link shared before the tag was renamed -- used to narrow
    # the list to nothing while `selected_tag` stayed None: neither the chip
    # that removes the filter nor the "nothing under these filters" line was
    # drawn. An empty list with no visible cause is precisely what the chips
    # exist to prevent.
    tag = request.GET.get("tag") or ""
    selected_tag = (
        Tag.objects.filter(slug=tag).first() if tag else None
    )
    if selected_tag:
        tickets = tickets.filter(tags=selected_tag)

    if search:
        # Comments are searched, and they are the reason this exists: the note
        # saying what was tried lives there, not in the description. The join
        # multiplies rows, which the DISTINCT of visible_to() already absorbs.
        tickets = tickets.filter(
            Q(title__icontains=search)
            | Q(description__icontains=search)
            | Q(comments__body__icontains=search)
        )
        # Carry the matching notes along, so a hit inside a thread shows what
        # it matched instead of sending the reader ticket by ticket.
        tickets = tickets.prefetch_related(
            Prefetch(
                "comments",
                queryset=Comment.objects.filter(body__icontains=search).order_by(
                    "created_at"
                ),
                to_attr="matches",
            )
        )

    tickets = tickets.annotate(priority_rank=PRIORITY_RANK).order_by(
        "priority_rank", "-created_at"
    )

    page = Paginator(tickets, PAGE_SIZE).get_page(request.GET.get("page"))
    rooms = Room.objects.filter(is_active=True).order_by(
        "sort_key", "name"
    )
    return render(
        request,
        "tickets/list.html",
        {
            "page": page,
            "tickets": page.object_list,
            "view": view,
            "closed": closed,
            "search": search,
            "rooms": rooms,
            "tags": Tag.objects.order_by("name"),
            "selected_room": rooms.filter(pk=room).first() if room else None,
            "selected_tag": selected_tag,
            "can_work": can_work_on(request.user),
        },
    )


@login_required
def ticket_detail(request, pk):
    ticket = get_object_or_404(
        Ticket.objects.visible_to(request.user).select_related(
            "room", "device", "resolution_comment", "resolution_comment__author"
        ),
        pk=pk,
    )
    assignees = list(ticket.assignees.all())
    # One choice means nothing to choose: the block stays a plain statement of
    # who can read, rather than a form that can only re-submit the status quo.
    choices = visibility_targets(request.user, ticket)
    # The same predicate as reopening, and for the same reason: a mistyped room
    # is almost always the reporter's own (D-37). Built only for whoever may
    # use it -- the form costs two queries that a reader has no use for.
    may_correct = can_work_on(request.user) or ticket.created_by_id == request.user.pk
    return render(
        request,
        "tickets/detail.html",
        {
            "ticket": ticket,
            "comments": ticket.comments.select_related("author").prefetch_related("attachments"),
            # Only the photos hanging off the ticket itself: the ones posted
            # with a comment belong in the thread, next to what they show.
            "attachments": ticket.attachments.filter(comment__isnull=True),
            "assignees": assignees,
            "is_assignee": request.user in assignees,
            "comment_form": CommentForm(),
            "can_work": can_work_on(request.user),
            "can_reopen": may_correct,
            "correction_form": (
                TicketCorrectionForm(instance=ticket, user=request.user) if may_correct else None
            ),
            # Each level carries the sentence that says what it means (doc 08):
            # the wording is part of the rule, so it travels with the choice
            # rather than living in a template a translator never sees.
            "visibility_choices": [
                (value, label, VISIBILITY_HELP.get(value, "")) for value, label in choices
            ],
            "can_widen": can_widen(request.user),
            "assignable": _team(request.user) if _is_admin(request.user) else None,
            "taggable": (
                Tag.objects.order_by("name")
                if can_work_on(request.user)
                else None
            ),
        },
    )


@login_required
def ticket_create(request):
    """The first screen of the application that writes anything."""
    if request.method != "POST":
        return render(request, "tickets/form.html", {"form": TicketForm(user=request.user)})

    form = TicketForm(request.POST, request.FILES, user=request.user)
    if form.is_valid():
        try:
            with transaction.atomic():
                ticket = form.save()
                photos.store_all(
                    form.cleaned_data["photos"], ticket=ticket, user=request.user
                )
                # Written inside the event's transaction (doc 09 §5): a ticket
                # saved without its notifications would reach nobody, silently.
                events.ticket_opened(ticket)
        except ValidationError as error:
            # A rejected photo must not cost the text that came with it: the
            # transaction is gone, the filled-in form is not.
            form.add_error("photos", error)
        else:
            messages.success(request, _("Ticket opened."))
            return redirect("tickets:detail", pk=ticket.pk)
    return render(request, "tickets/form.html", {"form": form})


@login_required
def room_devices(request):
    """Repaint the machine list when the room changes (D-17).

    A convenience only. The form rebuilds the same queryset on POST, so a
    browser that posts a device from another room is refused there.
    """
    devices = Device.objects.none()
    room = request.GET.get("room")
    if room:
        devices = Device.objects.filter(
            is_active=True, room_id=room
        )
    return render(request, "tickets/_device_options.html", {"devices": devices})


@require_POST
@login_required
def comment_create(request, pk):
    ticket = _ticket_or_404(request, pk)
    form = CommentForm(request.POST, request.FILES)
    if not form.is_valid():
        for error in form.errors.values():
            messages.error(request, error[0])
        return redirect("tickets:detail", pk=ticket.pk)

    try:
        with transaction.atomic():
            comment = form.save(commit=False)
            comment.ticket = ticket
            comment.author = request.user
            comment.save()
            photos.store_all(
                form.cleaned_data["photos"], ticket=ticket, user=request.user, comment=comment
            )
            events.commented(comment)
    except ValidationError as error:
        messages.error(request, error.messages[0])
    return redirect("tickets:detail", pk=ticket.pk)


#: Where a ticket may go from where it is, and who may take it there.
#: ``reopen`` is not a status but a transition, and it is the one the person
#: who reported the fault must be able to make: "it is not fixed" is their
#: sentence to say, whatever their role.
_TRANSITIONS = {
    Ticket.Status.OPEN,
    Ticket.Status.IN_PROGRESS,
    Ticket.Status.RESOLVED,
    Ticket.Status.CANCELLED,
}


@require_POST
@login_required
def ticket_status(request, pk):
    ticket = _ticket_or_404(request, pk)
    target = request.POST.get("status")
    if target not in _TRANSITIONS:
        raise Http404

    author = ticket.created_by_id == request.user.pk
    if not can_work_on(request.user) and not author:
        raise Http404
    # A reporter may withdraw or reopen their own report, and nothing else:
    # marking somebody else's repair done is not theirs to do.
    if not can_work_on(request.user) and target not in (
        Ticket.Status.CANCELLED, Ticket.Status.OPEN
    ):
        raise Http404

    if ticket.status == Ticket.Status.RESOLVED and target != Ticket.Status.RESOLVED:
        # The count is about the ticket, never about the person who closed it:
        # doc 01 (D-10) wants material for a conversation, not for a reproach.
        # Which is exactly why ``resolved_by`` is cleared rather than kept.
        ticket.reopened_count += 1
        ticket.resolved_by = None
        ticket.resolved_at = None

    if target == Ticket.Status.RESOLVED:
        ticket.resolved_by = request.user
        ticket.resolved_at = timezone.now()

    previous = ticket.status
    ticket.status = target
    with transaction.atomic():
        ticket.save()
        # Only resolution and reopening carry an action; the rest is
        # bookkeeping and notifies nobody (doc 09 §4).
        events.status_changed(ticket, actor=request.user, previous=previous)
    messages.success(request, _("Status updated."))
    return redirect("tickets:detail", pk=ticket.pk)


@require_POST
@login_required
def ticket_claim(request, pk):
    """Pick a ticket up, or put it back down. Yourself, and only yourself.

    Assigning grants access (the assignee clause of ``visible_to``), so letting
    anyone assign anyone would be a way of widening a ticket without being an
    admin. Self-assignment grants nothing: you are already reading the page.
    """
    ticket = _ticket_or_404(request, pk)
    if not can_work_on(request.user):
        raise Http404
    existing = TicketAssignee.objects.filter(ticket=ticket, user=request.user)
    if existing.exists():
        existing.delete()
    else:
        TicketAssignee.objects.create(ticket=ticket, user=request.user, assigned_by=request.user)
    return redirect("tickets:detail", pk=ticket.pk)


def _is_admin(user):
    return Role(user.role) in ADMIN_ROLES


def _team(user):
    """The people an admin may put on a ticket: those who repair."""
    return User.objects.filter(
        is_active=True,
        role__in=[role.value for role in WORKING_ROLES],
    ).order_by("display_name", "cn")


@require_POST
@login_required
def ticket_assignees(request, pk):
    """Admins only, for the reason spelled out in ``ticket_claim``."""
    ticket = _ticket_or_404(request, pk)
    if not _is_admin(request.user):
        raise Http404

    wanted = set(_team(request.user).filter(pk__in=request.POST.getlist("users")))
    current = set(ticket.assignees.all())
    with transaction.atomic():
        TicketAssignee.objects.filter(ticket=ticket, user__in=current - wanted).delete()
        TicketAssignee.objects.bulk_create([
            TicketAssignee(ticket=ticket, user=user, assigned_by=request.user)
            for user in wanted - current
        ])
        # Only the newcomers: re-saving the same list must not re-notify.
        events.assigned(ticket, wanted - current, by=request.user)
    messages.success(request, _("Assignments updated."))
    return redirect("tickets:detail", pk=ticket.pk)


@require_POST
@login_required
def ticket_tags(request, pk):
    """Re-tag: the half of tagging that had no door.

    A tag here is a **conclusion** -- `hdmi`, `linbo`, `drucker` -- and the
    person who opens a ticket is the person who has not diagnosed it yet. They
    see a black screen and tick `bildschirm`; the fault was a cable, and the
    repairer learns it an hour later. Offering tags only on the creation form
    recorded a guess and gave the answer nowhere to go -- and the same applies
    to the ticket opened before a tag existed, which no filter would ever
    reach again.

    ``can_work_on``, for the reason that decides ``status`` and ``claim``: what
    is being written down is the diagnosis, and reporting is not repairing.

    Neither audited nor notified, unlike visibility and assignment. Those two
    change who may read a ticket; this one changes how it is filed. The audit
    log is a duty to account for actions, not a changelog.
    """
    ticket = _ticket_or_404(request, pk)
    if not can_work_on(request.user):
        raise Http404

    wanted = set(Tag.objects.filter(pk__in=request.POST.getlist("tags")))
    current = set(ticket.tags.all())
    if wanted != current:
        with transaction.atomic():
            TicketTag.objects.filter(ticket=ticket, tag__in=current - wanted).delete()
            TicketTag.objects.bulk_create(
                [TicketTag(ticket=ticket, tag=tag) for tag in wanted - current]
            )
        messages.success(request, _("Tags updated."))
    return redirect("tickets:detail", pk=ticket.pk)


@require_POST
@login_required
def ticket_resolution(request, pk):
    """Point at the note that says what actually worked, or take the mark off.

    The teaching point comes first here: a ticket closed without anybody
    writing down *what worked* is worth nothing to the next person, while the
    thread is precisely "the team's technical memory". Marking one note forces
    the sentence to be written, and makes it findable without re-reading twenty
    others.

    ``can_work_on``, for the reason that decides ``status`` and ``tags``: what
    is being named is the repair, and reporting is not repairing.

    **The mark survives a reopening**, deliberately. A fault that starts again
    does not make the note untrue; it makes it the first thing to read, at the
    exact moment somebody needs to know what has already been tried. The
    ticket's own ``resolved_by`` and ``resolved_at`` are cleared on reopening
    because they answer "who closed this, when" -- a question a reopened ticket
    no longer has. "What worked last time" is a different question, and it
    keeps its answer.

    Neither audited nor notified, like ``ticket_tags`` and for the same reason:
    this changes how a ticket is documented, not who may read it.
    """
    ticket = _ticket_or_404(request, pk)
    if not can_work_on(request.user):
        raise Http404

    wanted = request.POST.get("comment") or ""
    if wanted.isdigit():
        # Looked up inside this ticket's own thread, never in Comment at large:
        # an identifier from elsewhere would otherwise hang a stranger's note
        # -- possibly from a ticket this reader may not even open -- at the top
        # of this page.
        ticket.resolution_comment = get_object_or_404(ticket.comments, pk=wanted)
        messages.success(request, _("Marked as what worked."))
    elif wanted:
        # Not a number, so not an identifier: 404 rather than a silent unmark.
        raise Http404
    else:
        ticket.resolution_comment = None
        messages.success(request, _("Resolution mark removed."))
    ticket.save(update_fields=["resolution_comment", "updated_at"])
    return redirect("tickets:detail", pk=ticket.pk)


@require_POST
@login_required
def ticket_correct(request, pk):
    """Fix what was mistyped: the room, the machine, how urgent it is (D-37).

    People report faults under pressure, between two lessons, and they get
    these wrong -- routinely. A ticket filed on the wrong room is a ticket the
    next person cannot find, and nothing short of the Django admin could move
    it. The author may correct their own report: the wrong room is almost
    always theirs, and forbidding them the fix guarantees it stays wrong.

    Nothing here touches what somebody wrote. The title and the description are
    their words; a correction adds a note, it does not rewrite the account.
    """
    ticket = _ticket_or_404(request, pk)
    if not can_work_on(request.user) and ticket.created_by_id != request.user.pk:
        raise Http404

    # Read before the form is bound: validation writes the posted values onto
    # the instance, so after `is_valid()` there is no "before" left to compare.
    previous = ticket.priority

    form = TicketCorrectionForm(request.POST, instance=ticket, user=request.user)
    if not form.is_valid():
        # Reachable without hostility: pick a room, post before HTMX has
        # repainted the machine list, and the machine belongs to the old room.
        # Refusing outright would throw the correction away without a word.
        messages.error(request, _("Correction refused: check the room and the machine."))
        return redirect("tickets:detail", pk=ticket.pk)

    with transaction.atomic():
        form.save()
        # Escalation is the one correction somebody has to hear about, and it
        # is written inside the same transaction as the change (doc 09 §5).
        # Only upwards: dropping a ticket back to normal asks nothing of
        # anybody, and a notification nobody must act on is how a channel gets
        # muted for good.
        if ticket.priority == Ticket.Priority.URGENT and previous != Ticket.Priority.URGENT:
            events.escalated(ticket, actor=request.user)

    messages.success(request, _("Ticket corrected."))
    return redirect("tickets:detail", pk=ticket.pk)


@require_POST
@login_required
def ticket_visibility(request, pk):
    ticket = _ticket_or_404(request, pk)
    form = VisibilityForm(request.POST, user=request.user, ticket=ticket)
    if not form.is_valid():
        # The only way here is a level the rules of doc 08 refuse.
        raise Http404

    target = form.cleaned_data["visibility"]
    if target != ticket.visibility:
        AuditLog.objects.create(
            actor=request.user,
            action=AuditLog.Action.VISIBILITY_CHANGE,
            target_type="ticket",
            target_id=ticket.pk,
            payload={"from": ticket.visibility, "to": target},
        )
        ticket.visibility = target
        ticket.save(update_fields=["visibility", "updated_at"])
        messages.success(request, _("Visibility updated."))
    return redirect("tickets:detail", pk=ticket.pk)


@login_required
def attachment(request, pk):
    """The main leak, closed here.

    Attachments are never served from MEDIA_URL: Nginx knows no visibility
    rule. We re-check the parent ticket, then hand the bytes over through
    ``X-Accel-Redirect`` when a prefix is configured.
    """
    obj = get_object_or_404(Attachment, pk=pk)
    if not Ticket.objects.visible_to(request.user).filter(pk=obj.ticket_id).exists():
        raise Http404

    # Before either branch: the visibility gate above answers "may this person
    # read this row", and answered it correctly all along. It says nothing
    # about whether the row points at a photo or at /etc/hostname.
    path = photos.resolved_path(obj)

    if settings.ST_X_ACCEL_PREFIX:
        response = HttpResponse(status=200)
        # The resolved path, relative again: nginx serves the media root and
        # must be handed something that cannot climb out of it either.
        inside = path.relative_to(Path(settings.MEDIA_ROOT).resolve())
        response["X-Accel-Redirect"] = f"{settings.ST_X_ACCEL_PREFIX}/{inside}"
        response["Content-Type"] = obj.mime or mimetypes.guess_type(obj.filename)[0] or ""
        response["Content-Disposition"] = f'inline; filename="{obj.filename}"'
        return response

    if not path.is_file():
        raise Http404
    return FileResponse(path.open("rb"), content_type=obj.mime, filename=obj.filename)


@require_POST
@login_required
def attachment_delete(request, pk):
    """Docs 06 asks for this to be easy: a photo may show a face.

    Whoever took it, and any admin. Not the ticket's author -- they did not
    take the photo, and a thread is not a possession.
    """
    obj = get_object_or_404(Attachment, pk=pk)
    if not Ticket.objects.visible_to(request.user).filter(pk=obj.ticket_id).exists():
        raise Http404
    if obj.uploaded_by_id != request.user.pk and not _is_admin(request.user):
        raise Http404

    photos.delete_file(obj)
    ticket_pk = obj.ticket_id
    obj.delete()
    messages.success(request, _("Photo removed."))
    return redirect("tickets:detail", pk=ticket_pk)
