# SPDX-License-Identifier: GPL-3.0-or-later
"""The catalogue, and the act of awarding (D-24).

One rule shapes both screens:

> **The catalogue never says who holds what.**

Not an omission -- a rule. Listing holders under a badge *is* a leaderboard: it
only takes counting. D-10 rules that out because a ranking pushes pupils
towards the easy faults and away from the full retest. "Who has what" lives on
one profile at a time, which is a page you open about a person, not a table you
sort.

The other rule is D-24's: **only an admin awards**. A badge exchanged between
pupils becomes currency, and it is the adult's judgement that gives the gesture
its worth.
"""

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import models, transaction
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import gettext as _

from accounts.authz import ADMIN_ROLES, Role
from accounts.models import AuditLog, User

from .forms import AwardForm, BadgeForm
from .models import Badge


def _admin_or_404(user):
    if Role(user.role) not in ADMIN_ROLES:
        # 404 and never 403, as everywhere else in this application.
        raise Http404


@login_required
def catalog(request):
    """What exists and how it is earned. Nothing about people."""
    badges = Badge.objects.filter(
        models.Q(school=request.user.school) | models.Q(school__isnull=True)
    ).order_by("-is_catalog", "category", "name")
    is_admin = Role(request.user.role) in ADMIN_ROLES
    return render(request, "badges/catalog.html", {
        "badges": badges,
        "is_admin": is_admin,
        "form": BadgeForm(school=request.user.school) if is_admin else None,
    })


@login_required
def badge_create(request):
    _admin_or_404(request.user)
    if request.method != "POST":
        return redirect("badges:catalog")
    form = BadgeForm(request.POST, school=request.user.school)
    if not form.is_valid():
        badges = Badge.objects.filter(
            models.Q(school=request.user.school) | models.Q(school__isnull=True)
        ).order_by("-is_catalog", "category", "name")
        return render(request, "badges/catalog.html", {
            "badges": badges, "is_admin": True, "form": form,
        })
    badge = form.save()
    messages.success(request, _("Badge “%(name)s” created.") % {"name": badge.label})
    return redirect("badges:catalog")


@login_required
def award(request, pk=None):
    """Awarding is a deliberate act, so it gets a screen of its own.

    ``pk`` pre-selects a person: the natural way in is from their profile,
    having just read what they did.
    """
    _admin_or_404(request.user)
    person = None
    if pk is not None:
        person = get_object_or_404(User, pk=pk, school=request.user.school)

    form = AwardForm(
        request.POST or None, awarded_by=request.user, person=person
    )
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            given = form.save()
            AuditLog.objects.create(
                actor=request.user,
                action=AuditLog.Action.BADGE_AWARD,
                target_type="badge_award",
                target_id=given.pk,
                # Identifiers only: the audit log never carries a third
                # party's name or the sentence written about them.
                payload={"badge": given.badge_id, "user": given.user_id},
            )
        messages.success(request, _("Badge awarded."))
        return redirect("accounts:profile_detail", pk=given.user_id)

    return render(request, "badges/award.html", {"form": form, "person": person})
