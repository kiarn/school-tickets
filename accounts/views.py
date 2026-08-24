# SPDX-License-Identifier: GPL-3.0-or-later
"""Profiles (D-24).

Two rules, and the second is the one that matters:

- **everyone sees their own profile**, and an admin may open anybody's;
- **the badge catalogue never lists holders** -- "who has what" lives here, one
  person at a time. Listing holders under a badge would be a leaderboard by
  another route, and it only takes counting; D-10 rules that out.
"""

from django.contrib import messages
from django.contrib.auth import views as auth_views
from django.contrib.auth.decorators import login_required
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

from .authz import ADMIN_ROLES, Role
from .forms import LoginForm, MuteForm, ProfileForm
from .models import User


def _may_read(viewer, target) -> bool:
    """Yourself, or anybody in your school if you administer it."""
    if viewer.pk == target.pk:
        return True
    return Role(viewer.role) in ADMIN_ROLES and viewer.school_id == target.school_id


def _profile_context(person, *, editable, form=None):
    return {
        "person": person,
        "editable": editable,
        "form": form,
        # The badge, the sentence that came with it, and the intervention that
        # earned it: a badge with its motivation is evidence, without it a
        # sticker (D-24).
        "awards": person.badge_awards.select_related("badge", "ticket", "awarded_by"),
    }


@login_required
def profile(request):
    """Mine, and the only profile that is also a form."""
    person = request.user
    form = ProfileForm(request.POST or None, instance=person)
    mutes = MuteForm(request.POST or None, user=person)
    if request.method == "POST" and form.is_valid() and mutes.is_valid():
        form.save()
        mutes.save()
        # Saved, then acted upon: the redirect below is the first response the
        # new language applies to, because the middleware reads the column.
        messages.success(request, _("Profile saved."))
        return redirect("accounts:profile")
    context = _profile_context(person, editable=True, form=form)
    context["mutes"] = mutes
    return render(request, "accounts/profile.html", context)


@login_required
def profile_detail(request, pk):
    person = get_object_or_404(User, pk=pk)
    if not _may_read(request.user, person):
        # 404 rather than 403, as everywhere else: a 403 on a sequential
        # identifier says "this person exists here".
        raise Http404
    if person.pk == request.user.pk:
        return redirect("accounts:profile")
    return render(request, "accounts/profile.html", _profile_context(person, editable=False))


class Login(auth_views.LoginView):
    """The stopgap of D-26, until the OIDC flow exists.

    Nothing here creates an account. Under D-31 the OIDC path does, on first
    login; this stopgap does not, so a person with no row has nothing to
    authenticate against -- which is why there is no "refused" page on *this*
    path. The refusal is a failed login, indistinguishable from a wrong
    password, and deliberately so.
    """

    template_name = "accounts/login.html"
    authentication_form = LoginForm
    redirect_authenticated_user = True


@require_POST
@login_required
def theme(request):
    """The header toggle, stored on the account rather than in the browser.

    A theme kept in localStorage is a theme you set again on every device, and
    somebody who needs a dark screen needs it on all of them.
    """
    wanted = request.POST.get("theme", "")
    if wanted not in dict(User.Theme.choices) and wanted != "":
        raise Http404
    request.user.theme = wanted
    request.user.save(update_fields=["theme"])

    # Come back where we were. Checked, because the value comes from the page.
    target = request.POST.get("next") or ""
    if not url_has_allowed_host_and_scheme(
        target, allowed_hosts={request.get_host()}, require_https=request.is_secure()
    ):
        target = "/"
    return redirect(target)
