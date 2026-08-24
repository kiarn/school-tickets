# SPDX-License-Identifier: GPL-3.0-or-later
"""Enrol a person -- and solve the bootstrap problem (D-22).

On a fresh install nobody is enrolled: nobody can log in, so nobody can enrol
anybody. This command is the first step after installing the package.

    manage.py enroll --cn arnaud --role admin
"""

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from accounts.authz import Role
from accounts.models import AuditLog, School, User


class Command(BaseCommand):
    help = "Enrol an account, the only way to gain access to the application (D-22)."

    def add_arguments(self, parser):
        parser.add_argument("--cn", required=True, help="linuxmuster login")
        parser.add_argument("--role", default=Role.REPORTER, choices=[r.value for r in Role])
        parser.add_argument("--name", default="", help="display name")
        parser.add_argument("--email", default="")
        parser.add_argument("--school", default=None, help="school slug")
        parser.add_argument(
            "--password",
            default=None,
            help="development only: production authenticates through OIDC (D-05)",
        )

    @transaction.atomic
    def handle(self, *args, **opts):
        slug = opts["school"] or settings.ST_DEFAULT_SCHOOL_SLUG
        school, created = School.objects.get_or_create(
            slug=slug, defaults={"name": slug.replace("-", " ").title()}
        )
        if created:
            self.stdout.write(f"school created: {school.slug}")

        if User.objects.filter(school=school, cn=opts["cn"], anonymized_at__isnull=True).exists():
            raise CommandError(f"{opts['cn']} is already enrolled in {school.slug}")

        user = User.objects.enroll(
            school=school,
            cn=opts["cn"],
            role=Role(opts["role"]),
            display_name=opts["name"],
            email=opts["email"],
        )
        if opts["password"]:
            user.set_password(opts["password"])
            user.save(update_fields=["password"])
            self.stdout.write(self.style.WARNING("password set: development only"))

        AuditLog.objects.create(
            actor=None,
            action=AuditLog.Action.ENROLL,
            target_type="user",
            target_id=user.pk,
            payload={"role": user.role, "source": "manage.py enroll"},
        )
        self.stdout.write(self.style.SUCCESS(f"{user.cn} enrolled as {user.role}"))
        if user.is_superuser:
            # Not a side effect to discover later: an administration role means
            # full power over the Django admin as well (D-27).
            self.stdout.write(
                self.style.WARNING("This role is a Django superuser: full access to /admin/.")
            )
        self.stdout.write("The OIDC sub will be bound on first login.")
