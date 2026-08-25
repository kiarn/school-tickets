# SPDX-License-Identifier: GPL-3.0-or-later
"""Enrol a person -- and solve the bootstrap problem (D-22).

On a fresh install nobody is enrolled: nobody can log in, so nobody can enrol
anybody. This command is the first step after installing the package.

    manage.py enroll --cn arnaud --role admin

D-31 narrowed what enrolling means without changing a line of this command.
Access no longer comes from here -- whoever Keycloak authenticates gets an
account, `reporter` by default. What this still does, and nothing else does,
is hand out a role **above** that default: the repair team, and the first
administrator. The bootstrap argument above holds all the more for it, since
the first person to log in would be a reporter with nobody able to promote
them.
"""

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from accounts.authz import Role
from accounts.models import AuditLog, User


class Command(BaseCommand):
    help = "Create an account and give it a role, before its first login (D-22, D-31)."

    def add_arguments(self, parser):
        parser.add_argument("--cn", required=True, help="linuxmuster login")
        parser.add_argument("--role", default=Role.REPORTER, choices=[r.value for r in Role])
        parser.add_argument("--name", default="", help="display name")
        parser.add_argument("--email", default="")
        parser.add_argument(
            "--password",
            default=None,
            help="development only: production authenticates through OIDC (D-05)",
        )

    @transaction.atomic
    def handle(self, *args, **opts):
        if User.objects.filter(cn=opts["cn"], anonymized_at__isnull=True).exists():
            raise CommandError(f"{opts['cn']} is already enrolled")

        user = User.objects.enroll(
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
