# SPDX-License-Identifier: GPL-3.0-or-later
"""The estate, read by a person rather than by the worker.

**Who may look.** Those who repair, and nobody else. The estate is a work tool:
its freshness is what one decides on, and reporting a fault needs none of it.
Doc 04 asks that surfaces be minimised, so this one opens no wider than its use.
A refused read answers 404 like every other refusal here -- an error page that
distinguished "not for you" from "does not exist" would leak the same thing.

**What it is for.** The question this application was born from, and the one
`/admin` could never answer for somebody standing in a corridor: when did this
machine last synchronise, and is that information still worth anything?

Nothing here calls lmnapi. Every value is a column the worker filled; only the
refresh button costs the school's server a request, and only when pressed
(D-09).
"""

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Count, Q
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

from accounts.authz import can_work_on
from parc.models import Device, DeviceStatus, Room
from tickets.models import Ticket


def _repairer_or_404(request):
    if not can_work_on(request.user):
        raise Http404


@login_required
def estate(request):
    """The rooms, and how much of each is worth looking at.

    Counted in the database rather than per room in the template: a school with
    forty rooms would otherwise cost forty queries to draw one page.

    Only PXE machines are counted. A room of printers is not a room with
    nothing to report -- it is a room with nothing to ask, and showing "0 of 0"
    beside it would invite the reader to worry about it.
    """
    _repairer_or_404(request)
    rooms = (
        Room.objects.filter(is_active=True)
        .annotate(
            machines=Count("devices", filter=Q(devices__is_active=True, devices__pxe__gt=0)),
            known=Count(
                "devices",
                filter=Q(
                    devices__is_active=True,
                    devices__pxe__gt=0,
                    devices__status__fetch_status=DeviceStatus.FetchStatus.OK,
                ),
            ),
        )
        .filter(machines__gt=0)
        .order_by("sort_key", "name")
    )
    return render(request, "parc/estate.html", {"rooms": rooms})


@login_required
def room(request, pk):
    """One room's machines, each with its state. The scanning view.

    "When did the machines in 204 last synchronise" is the question, and it is
    answered by reading down a column rather than by opening twelve pages.
    """
    _repairer_or_404(request)
    the_room = get_object_or_404(Room, pk=pk, is_active=True)
    devices = (
        Device.objects.filter(room=the_room, is_active=True)
        .select_related("status")
        .order_by("hostname")
    )
    return render(
        request,
        "parc/room.html",
        {"room": the_room, "devices": devices, "can_work": True},
    )


@login_required
def device(request, pk):
    """One machine: what it is, what it last did, and what is open on it.

    The tickets are filtered through ``visible_to``: reaching a machine from
    the estate must not become a way of reading a thread doc 08 keeps shut.
    """
    _repairer_or_404(request)
    the_device = get_object_or_404(
        Device.objects.select_related("room", "status"), pk=pk
    )
    return render(
        request,
        "parc/device.html",
        {
            "device": the_device,
            "linbo": getattr(the_device, "status", None),
            "expects_linbo": the_device.expects_linbo,
            "can_work": True,
            "refresh_url": reverse("parc:device_refresh", args=[the_device.pk]),
            "tickets": (
                Ticket.objects.visible_to(request.user)
                .filter(device=the_device)
                .select_related("room")
                .order_by("-created_at")[:20]
            ),
        },
    )


@require_POST
@login_required
def device_refresh(request, pk):
    """Ask the worker for a fresh reading. On demand, never on page load."""
    _repairer_or_404(request)
    the_device = get_object_or_404(Device, pk=pk)
    if DeviceStatus.request_refresh(the_device) is None:
        # No PXE, so no LINBO state to ask for: the request would return an
        # absence for ever.
        raise Http404
    messages.info(request, _("Asked for a fresh reading of this machine."))
    return redirect("parc:device", pk=the_device.pk)
