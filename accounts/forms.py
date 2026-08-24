# SPDX-License-Identifier: GPL-3.0-or-later
"""The profile form: the one thing a person may change about themselves.

Everything else on the profile comes from the directory or from an admin --
name, role, enrolment. Letting somebody edit their own display name would be
letting them edit a field the next OIDC login overwrites (D-22), and letting
them edit their own role needs no comment.
"""

from django import forms
from django.utils.translation import gettext_lazy as _

from .backends import LOCKOUT_SECONDS, locked_out
from .models import User


class ProfileForm(forms.ModelForm):
    class Meta:
        model = User
        fields = ["language", "theme"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["language"].required = False
        self.fields["theme"].required = False
        self.fields["theme"].widget = forms.Select(
            attrs={"class": "select w-full"},
            choices=[("", _("Follow my device"))] + list(User.Theme.choices),
        )
        # An explicit "follow the browser" beats an empty first line nobody
        # can read: the field has a meaning when unset, and it is this one.
        self.fields["language"].widget = forms.Select(
            attrs={"class": "select w-full"},
            choices=[("", _("Follow my browser"))] + list(self.fields["language"].choices)[1:],
        )


class LoginForm(forms.Form):
    """Deliberately not Django's ``AuthenticationForm``.

    That form is built around ``USERNAME_FIELD``, which here is ``oidc_sub`` --
    a value nobody knows and which is NULL for every account that has not been
    through Keycloak yet. The field people actually have is the ``cn``.
    """

    cn = forms.CharField(
        label=_("Login"),
        max_length=150,
        widget=forms.TextInput(attrs={
            "class": "input w-full",
            "autocomplete": "username",
            "autocapitalize": "none",
            "autofocus": True,
        }),
    )
    password = forms.CharField(
        label=_("Password"),
        widget=forms.PasswordInput(attrs={
            "class": "input w-full",
            "autocomplete": "current-password",
        }),
    )

    def __init__(self, request=None, *args, **kwargs):
        # LoginView hands the request in positionally: accepted and kept, so
        # that authenticate() gets it and any backend may look at it.
        self.request = request
        self.user = None
        super().__init__(*args, **kwargs)

    def clean(self):
        from django.contrib.auth import authenticate

        cleaned = super().clean()
        cn = (cleaned.get("cn") or "").strip()
        if not cn or not cleaned.get("password"):
            return cleaned

        if locked_out(cn):
            raise forms.ValidationError(
                _("Too many attempts. Try again in %(minutes)s minutes."),
                params={"minutes": LOCKOUT_SECONDS // 60},
                code="locked_out",
            )

        self.user = authenticate(self.request, cn=cn, password=cleaned["password"])
        if self.user is None:
            # One message for every failure, on purpose: "no such account" and
            # "wrong password" are the same sentence, or the form becomes a way
            # of asking who is enrolled. An account that has been removed or
            # anonymised lands here too, and that is the intended answer --
            # this path serves accounts that already exist, and losing access
            # is not an error to explain.
            raise forms.ValidationError(
                _("Wrong login or password, or this account has no access."),
                code="invalid_login",
            )
        return cleaned

    def get_user(self):
        return self.user


class MuteForm(forms.Form):
    """Which Push a person has chosen not to receive (doc 09 §7).

    Rendered as "send me", not as "mute": a checked box meaning silence is a
    box people tick by mistake. Absence of a Mute row means "send", so a new
    kind of event starts out audible for everybody.
    """

    def __init__(self, *args, user, **kwargs):
        from notifications.models import Kind, Mute

        super().__init__(*args, **kwargs)
        self.user = user
        self._kinds = Kind
        silenced = set(
            Mute.objects.filter(user=user).values_list("kind", flat=True)
        )
        for value, label in Kind.choices:
            self.fields[f"notify_{value}"] = forms.BooleanField(
                required=False,
                initial=value not in silenced,
                label=label,
                widget=forms.CheckboxInput(attrs={"class": "checkbox checkbox-sm"}),
            )

    def save(self):
        from notifications.models import Mute

        for value, _label in self._kinds.choices:
            wanted = self.cleaned_data.get(f"notify_{value}", False)
            if wanted:
                Mute.objects.filter(user=self.user, kind=value).delete()
            else:
                Mute.objects.get_or_create(user=self.user, kind=value)
