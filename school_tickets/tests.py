# SPDX-License-Identifier: GPL-3.0-or-later
"""The name and the crest of the instance (D-30).

`school-tickets` was written into four places, the login page among them --
and the login page is the one that renders before anybody is known, which used
to make it the awkward case. One instance per school is what dissolves it.
"""

import tempfile
from pathlib import Path

from django.test import TestCase, override_settings
from django.urls import reverse

from accounts.authz import Role
from accounts.models import School, User


class SiteNameTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.school = School.objects.create(slug="lycee", name="Lycee")
        cls.person = User.objects.enroll(school=cls.school, cn="boss", role=Role.ADMIN)

    @override_settings(ST_SITE_NAME="LGB")
    def test_the_name_reaches_the_page_before_login(self):
        self.assertContains(self.client.get(reverse("accounts:login")), "LGB")

    @override_settings(ST_SITE_NAME="LGB")
    def test_the_name_reaches_the_title_and_the_header(self):
        self.client.force_login(self.person)
        body = self.client.get(reverse("tickets:list")).content.decode()
        self.assertIn("· LGB</title>", body)
        self.assertIn(">LGB</span>", body)

    @override_settings(ST_SITE_NAME="LGB")
    def test_the_name_reaches_the_service_worker(self):
        self.assertContains(self.client.get("/sw.js"), "LGB")

    def test_the_push_payload_carries_no_title_so_sw_js_supplies_it(self):
        """The name is the same on every notification, so it is not sent.

        It also keeps ST_SITE_NAME out of the worker's environment: a setting
        the two processes had to hold equal is a setting that drifts.
        """
        import json

        from notifications.delivery import _payload
        from notifications.models import Notification

        note = Notification.objects.create(
            recipient=self.person, kind=Notification._meta.get_field("kind").choices[0][0]
        )
        self.assertNotIn("title", json.loads(_payload(note)))
        self.assertIn('data.title || "', self.client.get("/sw.js").content.decode())


class LogoTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.school = School.objects.create(slug="lycee", name="Lycee")
        cls.person = User.objects.enroll(school=cls.school, cn="boss", role=Role.ADMIN)

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(__import__("shutil").rmtree, self.dir, ignore_errors=True)
        self.crest = Path(self.dir) / "crest.svg"
        self.crest.write_text('<svg xmlns="http://www.w3.org/2000/svg"/>')

    def test_a_fresh_install_has_no_crest_and_says_so_with_a_404(self):
        """Empty is the normal state, not a misconfiguration."""
        with override_settings(ST_LOGO=""):
            self.assertEqual(self.client.get(reverse("logo")).status_code, 404)

    def test_the_crest_is_served_to_anybody(self):
        """It is drawn on the login page, which has no session yet."""
        with override_settings(ST_LOGO=str(self.crest)):
            response = self.client.get(reverse("logo"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "image/svg+xml")

    def test_only_svg_and_png_are_served(self):
        other = Path(self.dir) / "crest.txt"
        other.write_text("not a crest")
        with override_settings(ST_LOGO=str(other)):
            self.assertEqual(self.client.get(reverse("logo")).status_code, 404)

    def test_a_configured_path_that_is_not_there_is_a_404_not_a_crash(self):
        with override_settings(ST_LOGO=str(Path(self.dir) / "missing.png")):
            self.assertEqual(self.client.get(reverse("logo")).status_code, 404)

    def test_the_crest_becomes_the_favicon_the_header_and_the_push_icon(self):
        """The three things the application had none of."""
        with override_settings(ST_LOGO=str(self.crest)):
            # Signed out first: `LoginView` redirects an authenticated visitor.
            login = self.client.get(reverse("accounts:login")).content.decode()
            self.client.force_login(self.person)
            page = self.client.get(reverse("tickets:list")).content.decode()
            worker = self.client.get("/sw.js").content.decode()
        self.assertIn('<link rel="icon" href="/logo">', page)
        self.assertIn('<img src="/logo"', page)
        self.assertIn('icon: "/logo"', worker)
        self.assertIn('<img src="/logo"', login)

    def test_without_a_crest_nothing_points_at_a_404(self):
        self.client.force_login(self.person)
        with override_settings(ST_LOGO=""):
            page = self.client.get(reverse("tickets:list")).content.decode()
            worker = self.client.get("/sw.js").content.decode()
        # Not a bare "/logo": it is a substring of the sign-out form's /logout/.
        self.assertNotIn('"/logo"', page)
        self.assertNotIn('"/logo"', worker)
