# SPDX-License-Identifier: GPL-3.0-or-later
"""Give an already-enrolled account a password (D-26).

``enroll --password`` covers the moment somebody is added. This covers every
moment after it: a forgotten password, or an account enrolled before the login
page existed.

Django's own ``changepassword`` is no use here -- it looks accounts up by
``USERNAME_FIELD``, which is ``oidc_sub``, the one identifier nobody knows.

    manage.py set_password --cn arnaud
"""

from getpass import getpass

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from accounts.models import User


class Command(BaseCommand):
    help = "Set the local password of an enrolled account (D-26, until OIDC)."

    def add_arguments(self, parser):
        parser.add_argument("--cn", required=True, help="linuxmuster login")
        parser.add_argument("--school", default=None, help="school slug")
        parser.add_argument(
            "--password",
            default=None,
            help="read from the terminal when omitted, which keeps it out of the shell history",
        )
        parser.add_argument(
            "--clear",
            action="store_true",
            help="remove the password: the account can then only come in through OIDC",
        )

    def handle(self, *args, **opts):
        slug = opts["school"] or settings.ST_DEFAULT_SCHOOL_SLUG
        try:
            user = User.objects.get(
                school__slug=slug, cn=opts["cn"], anonymized_at__isnull=True
            )
        except User.DoesNotExist:
            raise CommandError(
                f"{opts['cn']} is not enrolled in {slug} -- run `manage.py enroll` first."
            )

        if opts["clear"]:
            user.set_unusable_password()
            user.save(update_fields=["password"])
            self.stdout.write(self.style.SUCCESS(f"{user.cn}: password removed"))
            return

        password = opts["password"] or getpass("Password: ")
        if not password:
            raise CommandError("empty password")
        user.set_password(password)
        user.save(update_fields=["password"])
        self.stdout.write(self.style.SUCCESS(f"{user.cn}: password set"))
        self.stdout.write("Local passwords are a stopgap until OIDC (D-05, D-26).")
