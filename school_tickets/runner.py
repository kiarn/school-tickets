# SPDX-License-Identifier: GPL-3.0-or-later
"""The test suite runs in English, and that is a rule, not a convenience.

``LANGUAGE_CODE`` is German (D-15): the school is German-speaking. Assertions,
on the other hand, are about **the strings this project writes** -- the msgid,
which are English (D-23). Before the catalogues existed the two coincided,
because an untranslated msgid comes back as itself; the day German was
translated, fourteen tests that had never mentioned a language started failing
on their own success.

Forcing the language here rather than decorating each test keeps the rule in
one place: a test that wants a translation asks for one explicitly, as
``CatalogueTests`` does by giving its account a language.
"""

from django.test.runner import DiscoverRunner
from django.test.utils import override_settings


class EnglishTestRunner(DiscoverRunner):
    def setup_test_environment(self, **kwargs):
        super().setup_test_environment(**kwargs)
        self._english = override_settings(LANGUAGE_CODE="en")
        self._english.enable()

    def teardown_test_environment(self, **kwargs):
        self._english.disable()
        super().teardown_test_environment(**kwargs)
