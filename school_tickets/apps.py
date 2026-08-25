# SPDX-License-Identifier: GPL-3.0-or-later
from django.apps import AppConfig


class SchoolTicketsConfig(AppConfig):
    """The project package as an application, for one reason: its checks.

    It carries no model and no migration. Without it, ``checks.py`` would never
    be imported, and a registered check that is never imported is not a check.
    """

    name = "school_tickets"

    def ready(self):
        from . import checks  # noqa: F401  (registration is the import's point)
