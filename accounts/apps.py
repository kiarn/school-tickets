# SPDX-License-Identifier: GPL-3.0-or-later
from django.apps import AppConfig


class AccountsConfig(AppConfig):
    name = "accounts"

    def ready(self):
        from . import signals  # noqa: F401
