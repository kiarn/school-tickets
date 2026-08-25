# SPDX-License-Identifier: GPL-3.0-or-later
from django import forms
from django.contrib import admin, messages
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from .models import AuditLog, User


class EnrolmentForm(forms.ModelForm):
    """Enrolling asks two questions and no more: who, and with what role.

    Everything else about a person -- their name, their address, their
    photograph -- belongs to LDAP/Keycloak and is written by the first login
    (D-22). A field for it here would be an invitation to type a value the
    directory is about to overwrite, and a second answer to a question the
    directory has already answered.

    No password field either, deliberately: a local password is the stopgap of
    D-26 and ``manage.py set_password`` is where it is granted. The plain
    ``CharField`` the admin used to render for it wrote whatever was typed
    straight into the column, unhashed, which left the account unable to log
    in and said nothing about it.
    """

    class Meta:
        model = User
        fields = ["cn", "role"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # The column is ``blank=True`` because an anonymised row has no cn left
        # (D-11). An enrolment without one is another matter: the login looks
        # people up by cn, so it would create an account nobody can enter.
        self.fields["cn"].required = True
        self.fields["cn"].help_text = _(
            "The linuxmuster login, spelled as the directory spells it."
        )

    def clean_cn(self):
        cn = self.cleaned_data["cn"].strip()
        # ``unique_cn`` says the same thing and Django now checks it by itself
        # (the constraint stopped being scoped to a school in D-49). This stays
        # for the sentence: a constraint violation surfaces as "User with this
        # Cn already exists", where the person typing wants to read the login
        # they just typed. ``anonymized_at`` is in the filter for the reader,
        # not for the query -- a tombstone has no cn left to collide with.
        if User.objects.filter(cn=cn, anonymized_at__isnull=True).exists():
            raise forms.ValidationError(_("%(cn)s is already enrolled.") % {"cn": cn})
        return cn


@admin.register(User)
class UserAdmin(admin.ModelAdmin):
    """Who has access and with what role. Not identities, and not passwords.

    The division of labour, which is the whole point of this class:

    - **the directory owns the identity** -- name, address, avatar, ``sub``;
    - **the person owns their own preferences** -- language and theme, on the
      profile page, which is the only screen where they mean anything;
    - **the admin owns access** -- the role, and whether the account is live.

    Everything in the first two groups is displayed and locked, so the page
    still answers "who is this", without offering to answer it twice.
    """

    list_display = ("cn", "display_name", "role", "is_active", "anonymized_at")
    list_filter = ("role", "is_active")
    search_fields = ("cn", "display_name", "email")
    ordering = ("cn",)

    add_form = EnrolmentForm
    add_fieldsets = ((None, {"fields": ("cn", "role")}),)

    #: Written by the directory on login (D-22).
    _from_directory = ("display_name", "email", "avatar_url", "oidc_sub")
    #: Written by the person, on their profile (D-24).
    _from_the_person = ("language", "theme")
    #: Written by the application.
    _stamps = (
        "enrolled_by", "enrolled_at", "created_at", "last_seen_at", "last_login",
        "anonymized_at",
    )

    fieldsets = (
        (_("Access"), {"fields": ("cn", "role", "is_active")}),
        (_("From the directory"), {"fields": _from_directory}),
        (_("Chosen by this person"), {"fields": _from_the_person}),
        (_("History"), {"fields": _stamps}),
    )

    def get_fieldsets(self, request, obj=None):
        return self.add_fieldsets if obj is None else self.fieldsets

    def get_form(self, request, obj=None, **kwargs):
        if obj is None:
            kwargs["form"] = self.add_form
        return super().get_form(request, obj, **kwargs)

    def get_readonly_fields(self, request, obj=None):
        readonly = self._from_directory + self._from_the_person + self._stamps
        if obj is None:
            return readonly
        if obj.anonymized_at:
            # A tombstone is not a form (D-11).
            return readonly + ("cn", "role", "is_active")
        if obj.oidc_sub:
            # Before the first login a cn is a claim somebody typed, and a typo
            # in it enrols nobody -- so it stays correctable. Once a sub is
            # bound the claim has been answered and the identity is settled;
            # editing it then would point a bound account at another person.
            return readonly + ("cn",)
        return readonly

    def save_model(self, request, obj, form, change):
        if not change:
            # What ``UserManager.enroll()`` does, D-22: a row that exists
            # before its first login, with no password of its own.
            obj.enrolled_by = request.user
            obj.enrolled_at = timezone.now()
            obj.set_unusable_password()

        super().save_model(request, obj, form, change)

        if not change:
            AuditLog.objects.create(
                actor=request.user,
                action=AuditLog.Action.ENROLL,
                target_type="user",
                target_id=obj.pk,
                payload={"role": obj.role, "source": "admin"},
            )
            messages.info(request, _(
                "%(cn)s is enrolled. Their name and address arrive with the first "
                "login; `manage.py set_password` grants a local password until "
                "OIDC lands."
            ) % {"cn": obj.cn})
            if obj.is_superuser:
                # Not a side effect to discover later, as `manage.py enroll`
                # also says out loud.
                messages.warning(request, _(
                    "This role is a Django superuser: full access to /admin/ (D-27)."
                ))
        elif "role" in form.changed_data:
            # The one action the log promised and nobody wrote: the admin is
            # the only place a role changes, the application has no screen for
            # it.
            AuditLog.objects.create(
                actor=request.user,
                action=AuditLog.Action.ROLE_CHANGE,
                target_type="user",
                target_id=obj.pk,
                payload={"from": form.initial.get("role"), "to": obj.role},
            )


@admin.register(AuditLog)
class AuditLogAdmin(admin.ModelAdmin):
    list_display = ("created_at", "action", "actor", "target_type", "target_id")
    list_filter = ("action",)
    readonly_fields = tuple(f.name for f in AuditLog._meta.fields)

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
