# SPDX-License-Identifier: GPL-3.0-or-later
"""Foundation: school, enrolled people, audit log. See specs/02-modele-donnees.md."""

from django.conf import settings
from django.contrib.auth.models import AbstractBaseUser, BaseUserManager, PermissionsMixin
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from .authz import ADMIN_ROLES, Role


class SchoolManager(models.Manager):
    def default(self):
        """The one school this instance serves (D-30).

        Resolved as ``manage.py enroll`` resolves it, minus the
        ``get_or_create``: every caller runs behind a login, which means an
        account exists, which means a school does. The fallback on the first
        row covers an instance whose ``ST_DEFAULT_SCHOOL`` was renamed after
        the fact rather than at install time.
        """
        return self.filter(slug=settings.ST_DEFAULT_SCHOOL_SLUG).first() or self.first()


class School(models.Model):
    """D-06: the school concept exists from the very first migration."""

    slug = models.SlugField(unique=True)
    name = models.CharField(max_length=200)
    created_at = models.DateTimeField(auto_now_add=True)

    objects = SchoolManager()

    def __str__(self):
        return self.name


class UserManager(BaseUserManager):
    def enroll(self, *, school, cn, role=Role.REPORTER, display_name="", email="",
               enrolled_by=None):
        """Enrol a person BEFORE their first login (D-22).

        The OIDC ``sub`` is unknown at this point: it gets bound on first
        login, by matching on the ``cn``.
        """
        user = self.model(
            school=school,
            cn=cn,
            role=role,
            display_name=display_name,
            email=email,
            enrolled_by=enrolled_by,
            enrolled_at=timezone.now(),
        )
        user.set_unusable_password()
        user.save(using=self._db)
        return user


class User(AbstractBaseUser, PermissionsMixin):
    """Local cache of the directory, never a read dependency.

    A pupil who has left the school must still leave readable tickets and
    comments behind: nothing here may require an LDAP round trip.
    """

    school = models.ForeignKey(School, on_delete=models.PROTECT, related_name="users")

    # Pivot identity. NULL as long as an enrolled person has never logged in
    # (D-22); unique only when set.
    oidc_sub = models.CharField(max_length=255, unique=True, null=True, blank=True, default=None)
    # Used for the initial match, once only. **NULL** on a tombstone, and not
    # the empty string: several erased people share a school, and a unique
    # constraint counts every "" as the same value while it counts every NULL
    # as its own. That is what lets the constraint below drop its condition --
    # see the note there.
    cn = models.CharField(max_length=150, blank=True, null=True, default=None)

    display_name = models.CharField(max_length=200, blank=True)
    email = models.EmailField(blank=True)
    avatar_url = models.URLField(blank=True)

    # Display language (D-24). Empty means "no preference": the browser's
    # Accept-Language decides, then LANGUAGE_CODE. Django dropped the
    # session-backed language in 4.0, and a cookie does not survive a new
    # phone, so a durable preference needs a column -- plus the hook that
    # applies it, in accounts/middleware.py.
    language = models.CharField(
        max_length=10,
        blank=True,
        choices=settings.LANGUAGES,
        verbose_name=_("Display language"),
    )

    class Theme(models.TextChoices):
        LIGHT = "light", _("Light")
        DARK = "dark", _("Dark")

    # Empty means "follow the device", which is what an unset preference has
    # always meant here: the script in base.html then reads
    # prefers-color-scheme. Stored rather than left in localStorage for the
    # same reason as the language -- a preference that does not survive a new
    # phone is not a preference (D-24).
    theme = models.CharField(
        max_length=10, blank=True, choices=Theme.choices, verbose_name=_("Theme")
    )

    role = models.CharField(max_length=20, choices=Role.choices, default=Role.REPORTER)
    is_active = models.BooleanField(default=True)
    anonymized_at = models.DateTimeField(null=True, blank=True)

    enrolled_by = models.ForeignKey(
        "self", on_delete=models.SET_NULL, null=True, blank=True, related_name="enrolled"
    )
    enrolled_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    last_seen_at = models.DateTimeField(null=True, blank=True)

    objects = UserManager()

    USERNAME_FIELD = "oidc_sub"
    REQUIRED_FIELDS = []

    class Meta:
        constraints = [
            # No condition, and it took a while to see why that matters. The
            # rule wanted is "unique among the named", tombstones excepted --
            # which reads as a condition on ``anonymized_at``, and a condition
            # is a partial index. **MariaDB has none.** Django then emits a
            # `check` warning and creates nothing: the constraint held in
            # SQLite, under development and in the whole test suite, and was
            # absent in production (D-03). The tests proved a guarantee the
            # deployment did not have.
            #
            # Carrying the exception in the data instead of in the constraint
            # costs one NULL and works identically on both engines.
            models.UniqueConstraint(fields=["school", "cn"], name="unique_cn_per_school"),
        ]

    def __str__(self):
        return self.display_name or self.cn or str(_("Former member"))

    @property
    def is_admin(self) -> bool:
        """Administers *this application*: visibility, badges, assignments.

        This is the question the app asks. It is NOT the same question as
        ``is_staff`` below, even though D-27 makes the two answers coincide --
        one is about the ticket application, the other about the Django admin.
        """
        return Role(self.role) in ADMIN_ROLES

    @property
    def is_staff(self):
        """Django admin access: superusers, and nobody else (D-27).

        Deliberately NOT derived from ``role`` any more. Deriving it from the
        role overrode Django's own notion of staff, and left a trap: an account
        Django considered a superuser but whose role was not an administration
        one could not open ``/admin/`` at all.

        The link to the role is kept, but it now runs through ``is_superuser``,
        which ``save()`` maintains -- so ``has_perm()`` answers natively
        instead of returning False for everything.
        """
        return self.is_superuser

    def save(self, *args, **kwargs):
        """An administrator of the school is an administrator of the database.

        D-27. Kept here rather than at the one call site that creates accounts
        today: a role changed from the Django admin has to carry the flag with
        it, or the two drift invisibly.
        """
        fields = kwargs.get("update_fields")
        if fields is None or "role" in fields:
            self.is_superuser = Role(self.role) in ADMIN_ROLES
            if fields is not None:
                kwargs["update_fields"] = set(fields) | {"is_superuser"}
        super().save(*args, **kwargs)

    def bind_oidc_sub(self, sub: str) -> None:
        """Bind the ``sub`` on first login.

        Refused when a ``sub`` is already bound: a ``cn`` reassigned by the
        school to somebody else would otherwise attach a newcomer to a former
        member's history (D-22).
        """
        if self.oidc_sub and self.oidc_sub != sub:
            raise ValueError("a sub is already bound to this account")
        self.oidc_sub = sub
        self.save(update_fields=["oidc_sub"])

    def anonymize(self) -> None:
        """Tombstone (D-11). The row survives, emptied.

        Anonymising implies deactivating (D-22, redefined by D-31): without
        ``is_active = False`` and without clearing the ``sub``, the row would
        stay loginable.

        The three deletions below are the ones specs/06-rgpd.md tabulates.
        Only the first was ever written; the other two are listed in that
        table as ``supprimé``, were described as done in two code comments,
        and were not happening.
        """
        self.cn = None
        self.display_name = ""
        self.language = ""
        self.theme = ""
        self.email = ""
        self.avatar_url = ""
        self.oidc_sub = None
        self.is_active = False
        self.anonymized_at = timezone.now()
        self.set_unusable_password()
        self.save()
        # Badges go with the name (R-17), as assignments already did: a
        # nominative recognition detached from its
        # name means nothing, and keeping it would keep a trace of the path of
        # somebody who asked to be erased. The school's own record survives in
        # Badge.award_count, which is why the stamping above happens FIRST --
        # the receiver reads it to know not to decrement.
        self.badge_awards.all().delete()

        # "Plus aucune trace d'affectation nominative" (doc 06). The comment on
        # ``TicketAssignee.user`` claimed this already happened, and the one
        # above says "as assignments already did": both were describing a
        # cascade that only fires on a DELETE of the row, which a tombstone is
        # precisely not.
        self.tickets_assigned.clear()

        # The device stops receiving, and -- the half that matters here -- the
        # row stops holding a `user_agent`. "Pixel 7, Chrome 141" is device
        # data about somebody who has just been erased, kept in the chapter
        # that decides whether the project is allowed to run at all.
        self.push_subscriptions.all().delete()


class PushSubscription(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="push_subscriptions")
    endpoint = models.TextField()
    p256dh = models.CharField(max_length=255)
    auth = models.CharField(max_length=255)
    # So the pupil recognises which device to revoke.
    user_agent = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    last_used_at = models.DateTimeField(null=True, blank=True)


class AuditLog(models.Model):
    """A duty to account for actions, not a developer convenience."""

    class Action(models.TextChoices):
        ENROLL = "enroll", _("Enrolment")
        ROLE_CHANGE = "role_change", _("Role change")
        OIDC_BIND = "oidc_bind", _("OIDC binding")
        ANONYMIZE = "anonymize", _("Anonymisation")
        BADGE_AWARD = "badge_award", _("Badge award")
        # Its counterpart, and the asymmetry it closes was the telling one:
        # giving a recognition was accounted for, taking one back was not --
        # although that is the act somebody is owed an explanation for.
        BADGE_REVOKE = "badge_revoke", _("Badge withdrawn")
        SYNC_DECISION = "sync_decision", _("Sync decision")
        # Moves and hostname changes apply without asking (doc 03); this is
        # what keeps them reconstructible afterwards.
        SYNC_APPLIED = "sync_applied", _("Change applied by a sync")
        VISIBILITY_CHANGE = "visibility_change", _("Visibility change")

    actor = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, related_name="+")
    action = models.CharField(max_length=32, choices=Action.choices)
    target_type = models.CharField(max_length=64, blank=True)
    target_id = models.BigIntegerField(null=True, blank=True)
    # Never any identifying data about a third party.
    payload = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [models.Index(fields=["action", "created_at"])]
