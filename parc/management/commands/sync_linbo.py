# SPDX-License-Identifier: GPL-3.0-or-later
"""One LINBO state pass, by hand."""

from django.core.management.base import BaseCommand

from parc import tasks


class Command(BaseCommand):
    help = "Collects LINBO state for the whole estate in one collective call (doc 05)."

    def handle(self, *args, **opts):
        self.stdout.write(str(tasks.sweep_linbo()))
