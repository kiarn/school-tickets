# SPDX-License-Identifier: GPL-3.0-or-later
import os

from django.core.wsgi import get_wsgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "school_tickets.settings")
application = get_wsgi_application()
