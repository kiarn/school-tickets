# SPDX-License-Identifier: GPL-3.0-or-later
"""The name and the crest of the instance (D-30).

`school-tickets` was written into four places, the login page among them --
and the login page is the one that renders before anybody is known, which used
to make it the awkward case. One instance per school is what dissolves it.
"""

import io
import json
import tempfile
from pathlib import Path

from django.conf import settings
from django.test import TestCase, override_settings
from django.urls import reverse
from PIL import Image

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


class ManifestTests(TestCase):
    """What makes the application installable on a home screen (D-33).

    The service worker and the Push worked already; without a manifest the
    thing they belong to could not be installed, so the notifications landed in
    a browser tab like any web page.
    """

    @classmethod
    def setUpTestData(cls):
        cls.school = School.objects.create(slug="lycee", name="Lycee")
        cls.person = User.objects.enroll(school=cls.school, cn="boss", role=Role.ADMIN)

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(__import__("shutil").rmtree, self.dir, ignore_errors=True)
        self.svg = Path(self.dir) / "crest.svg"
        self.svg.write_text('<svg xmlns="http://www.w3.org/2000/svg"/>')
        # Deliberately not square, and wider than tall: a crest usually is.
        self.png = Path(self.dir) / "crest.png"
        Image.new("RGBA", (300, 100), (10, 20, 30, 255)).save(self.png)

    def manifest(self):
        response = self.client.get(reverse("manifest"))
        self.assertEqual(response.status_code, 200)
        return json.loads(response.content)

    def test_it_is_served_to_anybody_as_a_manifest(self):
        """A browser reads it on the login page, before there is a session."""
        response = self.client.get(reverse("manifest"))
        self.assertEqual(response["Content-Type"], "application/manifest+json")
        self.assertEqual(response.status_code, 200)

    def test_it_carries_the_school_name_and_opens_on_the_work(self):
        with override_settings(ST_SITE_NAME="LGB", ST_SITE_SHORT_NAME="LGB"):
            manifest = self.manifest()
        self.assertEqual(manifest["name"], "LGB")
        self.assertEqual(manifest["short_name"], "LGB")
        self.assertEqual(manifest["start_url"], "/")
        self.assertEqual(manifest["display"], "standalone")

    def test_a_fresh_install_has_a_short_name_without_configuring_one(self):
        """The fallback lives in settings.py, at import: overriding the long
        name at runtime cannot move it, which is the point of testing it."""
        self.assertEqual(settings.ST_SITE_SHORT_NAME, settings.ST_SITE_NAME)

    def test_a_png_crest_becomes_the_two_sizes_a_browser_asks_for(self):
        with override_settings(ST_LOGO=str(self.png)):
            manifest = self.manifest()
            sizes = [icon["sizes"] for icon in manifest["icons"]]
            self.assertEqual(sizes, ["192x192", "512x512"])
            response = self.client.get(manifest["icons"][0]["src"])
        self.assertEqual(response["Content-Type"], "image/png")
        with Image.open(io.BytesIO(response.content)) as icon:
            # Square, whatever shape the crest was: a manifest that declares
            # 192x192 and serves 300x100 declares something false.
            self.assertEqual(icon.size, (192, 192))
            # Fitted and centred, never cropped -- the corners stay empty.
            self.assertEqual(icon.getpixel((1, 1))[3], 0)
            self.assertEqual(icon.getpixel((96, 96))[3], 255)

    def test_an_svg_crest_is_declared_once_at_any_size(self):
        """No rasteriser here, so the file goes as it is -- which installs
        everywhere but on iOS, and the apple-touch-icon is what says so."""
        with override_settings(ST_LOGO=str(self.svg)):
            manifest = self.manifest()
            self.client.force_login(self.person)
            page = self.client.get(reverse("tickets:list")).content.decode()
        self.assertEqual(manifest["icons"], [{
            "src": "/logo", "sizes": "any", "type": "image/svg+xml",
        }])
        self.assertNotIn("apple-touch-icon", page)

    def test_a_png_crest_reaches_ios_through_the_apple_link(self):
        with override_settings(ST_LOGO=str(self.png)):
            self.client.force_login(self.person)
            page = self.client.get(reverse("tickets:list")).content.decode()
            self.assertIn('<link rel="apple-touch-icon" href="/icon/180.png">', page)
            self.assertEqual(self.client.get("/icon/180.png").status_code, 200)

    def test_a_fresh_install_is_still_served_a_manifest_without_icons(self):
        """Empty is the normal state of a crest (D-30), so it cannot be an
        error here either -- the name and the colours are worth serving."""
        with override_settings(ST_LOGO=""):
            manifest = self.manifest()
        self.assertEqual(manifest["icons"], [])
        self.assertEqual(self.client.get("/icon/192.png").status_code, 404)

    def test_only_the_declared_sizes_are_ever_computed(self):
        """This endpoint resizes on demand and is public: an open size would
        be a way of spending the server's CPU from the outside."""
        with override_settings(ST_LOGO=str(self.png)):
            self.assertEqual(self.client.get("/icon/9999.png").status_code, 404)
            self.assertEqual(self.client.get("/icon/64.png").status_code, 404)

    def test_an_svg_crest_answers_no_png_icon(self):
        with override_settings(ST_LOGO=str(self.svg)):
            self.assertEqual(self.client.get("/icon/192.png").status_code, 404)

    def test_the_page_points_at_the_manifest_before_and_after_login(self):
        self.assertContains(
            self.client.get(reverse("accounts:login")),
            '<link rel="manifest" href="/manifest.webmanifest">',
        )
        self.client.force_login(self.person)
        self.assertContains(
            self.client.get(reverse("tickets:list")),
            '<link rel="manifest" href="/manifest.webmanifest">',
        )

    def test_the_status_bar_follows_the_device_when_the_account_has_no_theme(self):
        self.client.force_login(self.person)
        page = self.client.get(reverse("tickets:list")).content.decode()
        self.assertIn('media="(prefers-color-scheme: dark)"', page)
        self.assertIn("#262c33", page)

    def test_the_status_bar_follows_the_account_when_it_chose_one(self):
        """Same rule as `data-theme`: a preference is answered server-side, so
        an installed application does not open on a white bar and correct it."""
        self.person.theme = "dark"
        self.person.save(update_fields=["theme"])
        self.client.force_login(self.person)
        page = self.client.get(reverse("tickets:list")).content.decode()
        self.assertIn('<meta name="theme-color" content="#262c33">', page)
        # One tag, not three: the media-scoped pair would let the device
        # contradict the account on the one strip the account cannot see.
        self.assertEqual(page.count('name="theme-color"'), 1)
