# SPDX-License-Identifier: GPL-3.0-or-later
"""Apply the display language somebody chose (D-24).

``LocaleMiddleware`` is wired up, but nothing hands it a person's preference.
It reads, in order: the URL prefix, the language cookie, ``Accept-Language``,
then ``LANGUAGE_CODE``. Django 4.0 removed the session-backed language, and a
cookie does not survive a new phone -- so a stored preference has to be
activated by hand, and this is where.

Placed **after** ``AuthenticationMiddleware``: ``LocaleMiddleware`` runs before
the user is known, so it could not do this itself. Its ``process_response``
still reads ``get_language()``, so the ``Content-Language`` header and the
``Vary`` follow what is activated here.
"""

from django.utils import translation


class UserLanguageMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        language = getattr(getattr(request, "user", None), "language", "")
        if language:
            translation.activate(language)
            # LocaleMiddleware set this from the request; the person's own
            # choice outranks it, and templates read it for <html lang="...">.
            request.LANGUAGE_CODE = language
        return self.get_response(request)
