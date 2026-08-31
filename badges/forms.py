# SPDX-License-Identifier: GPL-3.0-or-later
"""Awarding, and creating a badge of one's own. Both admin-only (D-24)."""

from django import forms
from django.utils.text import slugify
from django.utils.translation import gettext_lazy as _

from accounts.authz import WORKING_ROLES
from accounts.models import User
from tickets.models import Ticket

from .catalog import METALS
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

        # Only what is on offer: a badge switched off in /admin is one this
        # school has decided not to give, and the list is where that decision
        # takes effect. Ordered by the ladder rather than by the name, or the
        # rungs would come back in alphabetical German.
        self.fields["badge"].queryset = Badge.objects.filter(is_active=True)
        self.fields["badge"].widget.attrs.update({"class": "select w-full"})

        # Reporters are not in the list: a badge recognises a repair (D-10),
        # and reporting is not repairing.
        people = User.objects.filter(
            is_active=True, role__in=[role.value for role in WORKING_ROLES]
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
        # "not a valid choice".
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
    """A badge invented here. Never a catalogue badge.

    ``is_catalog`` is not offered on purpose: a shared badge means a string in
    the Crowdin catalogues (D-15), which is a decision about the project, not
    about one afternoon in one establishment.
    """

    class Meta:
        model = Badge
        fields = ["name", "description", "icon", "family", "level"]
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
            "family": forms.Select(attrs={"class": "select w-full"}),
            "level": forms.Select(attrs={"class": "select w-full"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["description"].required = False
        self.fields["icon"].required = False
        # A badge invented here may join a shipped ladder rather than stand on
        # its own -- a school that wants a fourth rung on one of them should
        # not have to reinvent the other three.
        self.fields["family"].required = False
        self.fields["level"] = forms.TypedChoiceField(
            required=False, coerce=int, empty_value=0, initial=0,
            choices=[(0, _("Earned once"))] + [
                (level, _("Level %(level)d") % {"level": level}) for level in METALS
            ],
            widget=forms.Select(attrs={"class": "select w-full"}),
            label=_("Level"),
        )

    def clean_name(self):
        name = self.cleaned_data["name"]
        if Badge.objects.filter(name=name).exists():
            raise forms.ValidationError(_("A badge by that name already exists."))
        return name

    def save(self, commit=True):
        badge = super().save(commit=False)
        badge.is_catalog = False
        badge.slug = unique_slug(badge.name)
        if commit:
            badge.save()
        return badge


def unique_slug(name: str) -> str:
    """``Badge.slug`` is unique across the instance, catalogue included.

    It used to carry the school as a prefix, so that two establishments
    inventing "Premier poste installé" would not collide (D-49 removed the
    school). The numeric suffix below already covered the remaining case: a
    locally invented badge landing on the slug of a shipped one.
    """
    base = slugify(name)[:45] or "badge"
    candidate, suffix = base, 2
    while Badge.objects.filter(slug=candidate).exists():
        candidate = f"{base}-{suffix}"
        suffix += 1
    return candidate
