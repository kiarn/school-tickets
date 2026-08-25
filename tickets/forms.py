# SPDX-License-Identifier: GPL-3.0-or-later
"""The write side of a ticket.

Every queryset here is bound to ``user.school`` at construction time. That is
not decoration: a form field is a list of primary keys the browser posts back,
so an unscoped queryset is an open door onto another school's estate. The rule
is the same one as ``visible_to()`` on the read side -- nothing global, ever.
"""

from django import forms
from django.db.models import Max
from django.urls import reverse_lazy
from django.utils.translation import gettext_lazy as _

from accounts.authz import VISIBILITY_HELP, Visibility, creation_visibilities, visibility_targets
from parc.models import Device, Room

from .models import Comment, Tag, Ticket

#: How many rooms the picker lifts to the top. Four is about what fits before
#: a phone's select turns into a wheel; past that, "recent" stops meaning
#: anything anyway.
RECENT_ROOMS = 4


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


class RoomAndDeviceMixin:
    """The room picker, and the machine list that follows it.

    Two forms need exactly this pair -- the one that opens a ticket and the one
    that corrects it (D-37) -- and what matters is the same in both: **the
    device queryset is narrowed server-side**. The HTMX partial that repaints
    the list when the room changes is a convenience; this queryset is what a
    posted value is checked against, and a machine from another room is
    refused here whatever the browser did.
    """

    def _bind_room_and_device(self, school):
        self.fields["room"].queryset = Room.objects.filter(school=school, is_active=True)
        # The machine list follows the room (D-17). htmx sends the select's own
        # value, so the parameter name is simply the field name.
        self.fields["room"].widget.attrs.update({
            "class": "select w-full",
            "hx-get": reverse_lazy("tickets:room_devices"),
            "hx-trigger": "change",
            "hx-target": "#id_device",
            "hx-swap": "innerHTML",
        })

        # Every device of the room is offered, printers and servers included: a
        # device's role gives it no standing here (see "Reporté" in
        # specs/01-decisions.md).
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


class TicketForm(VisibilityOptionsMixin, SchoolScopedMixin, RoomAndDeviceMixin, forms.ModelForm):
    """Two fields and a button, with everything else folded away (D-35).

    The corridor is where most reports start: somebody says "R102 does not
    work" between two doors, and whoever hears it has a room and a sentence --
    no hostname, no photo, no diagnosis, because they are not standing in front
    of the machine. Asking that person for seven fields is asking them not to
    report. So the form opens on the two the corridor can answer, and the rest
    waits behind a disclosure for the other case: a member, in the room, with
    the fault in front of them.

    Nothing is removed. Every field the full form had is still here, and the
    defaults that apply when they are left alone are the ones the model already
    carries: normal priority, team visibility, no machine, no tag.
    """

    photos = MultipleFileField(required=False, label=_("Photos"))

    #: What the disclosure hides. Named once because two things need it: the
    #: template, to open the block when one of them is in error -- a folded
    #: error is an error nobody can see -- and the reader, to know what "more
    #: fields" means without counting them.
    FOLDED = ("device", "description", "priority", "tags", "photos", "visibility")

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

        self._bind_room_and_device(school)
        self.fields["room"].empty_label = _("Choose a room")
        self._lift_recent_rooms()

        self.fields["priority"].widget.attrs.update({"class": "select w-full"})
        # Both carry a model default, and both are folded away (D-35). A
        # browser posts them anyway -- a closed <details> still submits its
        # inputs -- but a form whose quick path depends on that is a form one
        # fold away from breaking. Optional here, defaulted in clean_*.
        self.fields["priority"].required = False
        self.fields["visibility"].required = False

        self.fields["tags"].queryset = Tag.objects.filter(school=school)
        self.fields["tags"].required = False

        self.fields["visibility"].choices = creation_visibilities(self.user)
        self.fields["visibility"].widget = forms.RadioSelect(
            attrs={"class": "radio radio-primary mt-0.5"},
            choices=self.fields["visibility"].choices,
        )

    def _lift_recent_rooms(self):
        """Put the rooms this person last reported on at the top of the list.

        A school has twenty to forty rooms, which on a phone is a wheel to
        spin. The one being reported now is very often one of the last few:
        faults cluster, and whoever reports is usually walking the same
        corridor. Two `<optgroup>`s rather than a re-sorted flat list, because
        a list that is neither alphabetical nor grouped reads as broken.

        ``choices`` is replaced, ``queryset`` is not: validation still goes
        through the full, school-scoped queryset, so nothing here can widen
        what may be posted.
        """
        recent = list(
            self.fields["room"].queryset
            .filter(tickets__created_by=self.user)
            .annotate(last_reported=Max("tickets__created_at"))
            .order_by("-last_reported")[:RECENT_ROOMS]
        )
        if not recent:
            return
        seen = {room.pk for room in recent}
        others = [r for r in self.fields["room"].queryset if r.pk not in seen]
        self.fields["room"].choices = [
            ("", self.fields["room"].empty_label),
            (_("Recently"), [(room.pk, str(room)) for room in recent]),
            (_("All rooms"), [(room.pk, str(room)) for room in others]),
        ]

    def clean_priority(self):
        return self.cleaned_data.get("priority") or Ticket.Priority.NORMAL

    def clean_visibility(self):
        return self.cleaned_data.get("visibility") or Visibility.TEAM

    @property
    def folded_has_errors(self) -> bool:
        """Whether the disclosure must be open on this render."""
        return any(field in self.errors for field in self.FOLDED)

    @property
    def visibility_summary(self):
        """The audience as a sentence, for the state where the radios are hidden.

        Doc 08 asks that visibility never be implicit. Folding the *choice*
        away does not have to make the *answer* invisible: the quick form says
        who will read this, and the disclosure is where it is changed.
        """
        value = self["visibility"].value()
        try:
            return Visibility(int(value)).label
        except (TypeError, ValueError):
            return Visibility(Visibility.TEAM).label

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


class TicketCorrectionForm(SchoolScopedMixin, RoomAndDeviceMixin, forms.ModelForm):
    """What was mistyped, corrected afterwards (D-37).

    People fill this in under pressure, between two lessons, and they get the
    room wrong. A ticket filed on the wrong room is a ticket the next person
    cannot find, and until now nothing in the application could move it -- the
    room, the machine and the priority were settled at creation and never
    again. Only the Django admin could touch them, which is to say: not the
    people standing in the corridor.

    Three fields and no more. The title and the description are the reporter's
    own words and are corrected by adding a note, not by rewriting history;
    visibility has its own form and its own rule (doc 08); the status has its
    buttons.
    """

    class Meta:
        model = Ticket
        fields = ["room", "device", "priority"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._bind_room_and_device(self.user.school)
        # A ticket always has a room, so there is nothing empty to offer -- and
        # the rooms are listed plainly here, with no "recently reported" group:
        # the room being corrected is a fact to look up, not a habit to guess.
        self.fields["room"].empty_label = None
        self.fields["priority"].widget.attrs.update({"class": "select w-full"})

    def clean(self):
        cleaned = super().clean()
        # The label was frozen at creation so that a ticket from March still
        # reads "204" after the room was renamed (doc 03). A correction is the
        # other case entirely: the room was *wrong*, so the label was wrong
        # with it, and re-freezing is what makes the ticket findable again.
        room = cleaned.get("room")
        if room and room.pk != self.instance.room_id:
            self.instance.room_label = room.name
        return cleaned


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
