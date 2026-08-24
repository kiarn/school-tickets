# SPDX-License-Identifier: GPL-3.0-or-later
"""Awarding, and creating a badge of one's own. Both admin-only (D-24)."""

from django import forms
from django.db import models
from django.utils.text import slugify
from django.utils.translation import gettext_lazy as _

from accounts.authz import WORKING_ROLES
from accounts.models import User
from tickets.models import Ticket

from .models import Badge, BadgeAward


class AwardForm(forms.ModelForm):
    """One badge, one person, one sentence, and the repair that earned it."""

    class Meta:
        model = BadgeAward
        fields = ["badge", "user", "motivation", "ticket"]
        widgets = {
            "motivation": forms.Textarea(attrs={
                "class": "textarea w-full",
                "rows": 3,
                "placeholder": _("What they did, in the words you would say to them."),
            }),
        }

    def __init__(self, *args, awarded_by, person=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.awarded_by = awarded_by
        school = awarded_by.school

        self.fields["badge"].queryset = Badge.objects.filter(
            models.Q(school=school) | models.Q(school__isnull=True)
        ).order_by("-is_catalog", "name")
        self.fields["badge"].widget.attrs.update({"class": "select w-full"})

        # Reporters are not in the list: a badge recognises a repair (D-10),
        # and reporting is not repairing.
        people = User.objects.filter(
            school=school, is_active=True, role__in=[role.value for role in WORKING_ROLES]
        ).order_by("display_name", "cn")
        self.fields["user"].queryset = people
        self.fields["user"].widget.attrs.update({"class": "select w-full"})
        if person is not None:
            self.fields["user"].initial = person.pk

        # The intervention that earned it, which is what turns a badge into
        # evidence rather than a sticker. Optional, and never automatic (D-24).
        #
        # Not sliced, and that is not an oversight: a sliced queryset still
        # renders its options, but `ModelChoiceField` validates by filtering
        # it, and filtering a slice is refused -- so every choice comes back
        # "not a valid choice". Caught by a test on 2026-08-24.
        #
        # It does mean the list grows with the archive. The narrowing that
        # would make sense is by person -- the intervention that earned a badge
        # is one they worked on -- and that needs the person to be known
        # first: an htmx pass like the room/machine pair, the day it hurts.
        self.fields["ticket"].queryset = Ticket.objects.visible_to(awarded_by).order_by(
            "-created_at"
        )
        self.fields["ticket"].required = False
        self.fields["ticket"].empty_label = _("No particular ticket")
        self.fields["ticket"].widget.attrs.update({"class": "select w-full"})

    def clean(self):
        cleaned = super().clean()
        badge, person = cleaned.get("badge"), cleaned.get("user")
        if badge and person and BadgeAward.objects.filter(badge=badge, user=person).exists():
            # The database constraint says the same thing; saying it here is
            # what turns a 500 into a sentence somebody can read.
            raise forms.ValidationError(_("They already hold this badge."))
        return cleaned

    def save(self, commit=True):
        award = super().save(commit=False)
        award.awarded_by = self.awarded_by
        if commit:
            award.save()
        return award


class BadgeForm(forms.ModelForm):
    """A badge invented by one school. Never a catalogue badge.

    ``is_catalog`` and ``school = NULL`` are not offered on purpose: a shared
    badge means a string in the Crowdin catalogues (D-15), which is a decision
    about the project, not about one school's afternoon.
    """

    class Meta:
        model = Badge
        fields = ["name", "description", "icon", "category"]
        widgets = {
            "name": forms.TextInput(attrs={
                "class": "input w-full", "placeholder": _("First complete install"),
            }),
            "description": forms.Textarea(attrs={
                "class": "textarea w-full", "rows": 2,
                "placeholder": _("What has to be done to earn it."),
            }),
            "icon": forms.TextInput(attrs={
                "class": "input w-full", "placeholder": "🛠",
            }),
            "category": forms.TextInput(attrs={"class": "input w-full"}),
        }

    def __init__(self, *args, school, **kwargs):
        super().__init__(*args, **kwargs)
        self.school = school
        self.fields["description"].required = False
        self.fields["icon"].required = False
        self.fields["category"].required = False

    def clean_name(self):
        name = self.cleaned_data["name"]
        if Badge.objects.filter(school=self.school, name=name).exists():
            raise forms.ValidationError(_("This school already has a badge by that name."))
        return name

    def save(self, commit=True):
        badge = super().save(commit=False)
        badge.school = self.school
        badge.is_catalog = False
        badge.slug = unique_slug(badge.name, self.school)
        if commit:
            badge.save()
        return badge


def unique_slug(name: str, school) -> str:
    """``Badge.slug`` is unique across every school, catalogue included.

    Two schools inventing "Premier poste installé" must not collide, so the
    school's own slug goes into the value.
    """
    base = f"{school.slug}-{slugify(name)}"[:45] or f"{school.slug}-badge"
    candidate, suffix = base, 2
    while Badge.objects.filter(slug=candidate).exists():
        candidate = f"{base}-{suffix}"
        suffix += 1
    return candidate
