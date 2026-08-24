# SPDX-License-Identifier: GPL-3.0-or-later
"""The write side of a ticket.

Every queryset here is bound to ``user.school`` at construction time. That is
not decoration: a form field is a list of primary keys the browser posts back,
so an unscoped queryset is an open door onto another school's estate. The rule
is the same one as ``visible_to()`` on the read side -- nothing global, ever.
"""

from django import forms
from django.urls import reverse_lazy
from django.utils.translation import gettext_lazy as _

from accounts.authz import VISIBILITY_HELP, creation_visibilities, visibility_targets
from parc.models import Device, Room

from .models import Comment, Tag, Ticket


class MultipleFileInput(forms.ClearableFileInput):
    """Django 5 refuses ``multiple`` on the stock widget; this is the documented
    way round it. ``capture`` is deliberately NOT set: it would force the camera
    and take away the gallery, and a photo taken a minute earlier is a photo."""

    allow_multiple_selected = True


class MultipleFileField(forms.FileField):
    def __init__(self, *args, **kwargs):
        kwargs.setdefault("widget", MultipleFileInput(attrs={
            "accept": "image/*",
            "multiple": True,
            "class": "file-input file-input-bordered w-full",
        }))
        super().__init__(*args, **kwargs)

    def clean(self, data, initial=None):
        single = super().clean
        if isinstance(data, (list, tuple)):
            return [single(item, initial) for item in data]
        return [single(data, initial)] if data else []


class VisibilityOptionsMixin:
    """Pair each radio with the sentence that says what it means.

    Kept out of the template because the same three sentences are needed twice:
    when a ticket is opened, and when its level is changed afterwards.
    """

    def visibility_options(self):
        for radio in self["visibility"]:
            yield radio, VISIBILITY_HELP.get(radio.data["value"], "")


class SchoolScopedMixin:
    """Bind the fields to the person filling the form in."""

    def __init__(self, *args, user, **kwargs):
        self.user = user
        super().__init__(*args, **kwargs)


class TicketForm(VisibilityOptionsMixin, SchoolScopedMixin, forms.ModelForm):
    photos = MultipleFileField(required=False, label=_("Photos"))

    class Meta:
        model = Ticket
        fields = ["room", "device", "title", "description", "priority", "visibility", "tags"]
        widgets = {
            "title": forms.TextInput(attrs={
                "class": "input w-full",
                "autofocus": True,
                "placeholder": _("Black screen on the teacher's desk"),
            }),
            "description": forms.Textarea(attrs={
                "class": "textarea w-full",
                "rows": 4,
                "placeholder": _("What you saw, and what you already tried."),
            }),
            "tags": forms.CheckboxSelectMultiple(attrs={"class": "checkbox checkbox-sm"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        school = self.user.school

        self.fields["room"].queryset = Room.objects.filter(school=school, is_active=True)
        self.fields["room"].empty_label = _("Choose a room")
        # The machine list follows the room (D-17). htmx sends the select's own
        # value, so the parameter name is simply the field name.
        self.fields["room"].widget.attrs.update({
            "class": "select w-full",
            "hx-get": reverse_lazy("tickets:room_devices"),
            "hx-trigger": "change",
            "hx-target": "#id_device",
            "hx-swap": "innerHTML",
        })

        # Narrowed to the chosen room, and to that room only. The HTMX partial
        # that repaints this list is a convenience; this queryset is the rule.
        # Every room is offered, printers and servers included: a device's role
        # gives it no standing here (see "Reporté" in specs/01-decisions.md).
        devices = Device.objects.filter(school=school, is_active=True)
        room = self.data.get("room") or self.initial.get("room")
        if room:
            devices = devices.filter(room_id=room)
        elif self.instance.pk:
            devices = devices.filter(room_id=self.instance.room_id)
        else:
            devices = devices.none()
        self.fields["device"].queryset = devices
        self.fields["device"].required = False
        self.fields["device"].empty_label = _("No particular machine")
        self.fields["device"].widget.attrs.update({"class": "select w-full"})

        self.fields["priority"].widget.attrs.update({"class": "select w-full"})

        self.fields["tags"].queryset = Tag.objects.filter(school=school)
        self.fields["tags"].required = False

        self.fields["visibility"].choices = creation_visibilities(self.user)
        self.fields["visibility"].widget = forms.RadioSelect(
            attrs={"class": "radio radio-primary mt-0.5"},
            choices=self.fields["visibility"].choices,
        )

    def clean(self):
        cleaned = super().clean()
        room, device = cleaned.get("room"), cleaned.get("device")
        # The queryset above already refuses a foreign device; this catches the
        # case where room and device are each valid but do not belong together.
        if device and room and device.room_id != room.pk:
            self.add_error("device", _("This machine is not in that room."))
        return cleaned

    def save(self, commit=True):
        ticket = super().save(commit=False)
        ticket.school = self.user.school
        ticket.created_by = self.user
        # Frozen at creation, on purpose: a ticket from March keeps reading
        # "204" after the room becomes A204 (doc 03).
        ticket.room_label = ticket.room.name
        if commit:
            ticket.save()
            self.save_m2m()
        return ticket


class CommentForm(forms.ModelForm):
    """The technical memory of the team, and the only field a phone must fill."""

    photos = MultipleFileField(required=False)

    class Meta:
        model = Comment
        fields = ["body"]
        widgets = {
            "body": forms.Textarea(attrs={
                "class": "textarea w-full",
                "rows": 3,
                "placeholder": _("What you tried, and what it did."),
            }),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Not required on its own: a photo with no words is a legitimate note,
        # and the pair is checked in clean(). Leaving `required` on the widget
        # would let the browser refuse a post the application accepts.
        self.fields["body"].required = False

    def clean(self):
        cleaned = super().clean()
        # A photo on its own is a legitimate note; an empty post is not.
        if not (cleaned.get("body") or "").strip() and not cleaned.get("photos"):
            raise forms.ValidationError(_("Write something, or attach a photo."))
        return cleaned


class VisibilityForm(VisibilityOptionsMixin, forms.Form):
    """Rebuilt per request from the ticket's current level: the set of legal
    targets depends on where the ticket is now, not only on who is asking."""

    visibility = forms.TypedChoiceField(
        coerce=int, widget=forms.RadioSelect(attrs={"class": "radio radio-primary mt-0.5"})
    )

    def __init__(self, *args, user, ticket, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["visibility"].choices = visibility_targets(user, ticket.visibility)
