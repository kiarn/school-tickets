# SPDX-License-Identifier: GPL-3.0-or-later
"""Put the shipped catalogue in the database, or bring it up to date.

    manage.py sync_badges

A fresh install gets it from the migration and never needs this. It is the
upgrade that does: a version that adds a rung ships the definition in
`catalog.py`, and this is what turns the definition into rows.

Nothing an establishment decided is touched -- not the names it typed, not the
badges it switched off, not one award. Running it twice changes nothing the
second time.
"""

from django.core.management.base import BaseCommand

from badges.catalog import CATALOG, sync


class Command(BaseCommand):
    help = "Create or refresh the badges shipped with the project (D-15)."

    def handle(self, *args, **opts):
        created = sync()
        self.stdout.write(
            f"{len(CATALOG)} badges in the catalogue, {created} created."
        )
