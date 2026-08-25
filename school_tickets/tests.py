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

from unittest import skipUnless

from django.conf import settings
from django.test import TestCase, override_settings
from django.urls import reverse
from PIL import Image

from accounts.authz import Role
from accounts.models import School, User
from school_tickets.checks import catalogues_are_compiled, crest_is_a_square_png


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
        self.crest = Path(self.dir) / "crest.png"
        Image.new("RGBA", (512, 512), (10, 20, 30, 255)).save(self.crest)

    def test_a_fresh_install_has_no_crest_and_says_so_with_a_404(self):
        """Empty is the normal state, not a misconfiguration."""
        with override_settings(ST_LOGO=""):
            self.assertEqual(self.client.get(reverse("logo")).status_code, 404)

    def test_the_crest_is_served_to_anybody(self):
        """It is drawn on the login page, which has no session yet."""
        with override_settings(ST_LOGO=str(self.crest)):
            response = self.client.get(reverse("logo"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "image/png")

    def test_nothing_but_a_png_is_served(self):
        """SVG was served for a while and cost more than it gave (D-33): iOS
        reads none, so a crest given as one installed everywhere but on the
        phones this application is for."""
        for name, content in (("crest.svg", '<svg xmlns="http://www.w3.org/2000/svg"/>'),
                              ("crest.txt", "not a crest")):
            other = Path(self.dir) / name
            other.write_text(content)
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
        self.png = Path(self.dir) / "crest.png"
        Image.new("RGBA", (512, 512), (10, 20, 30, 255)).save(self.png)
        # A crest that is not square is refused nowhere -- it is fitted. The
        # check of the same name is what tells its owner (D-33).
        self.wide = Path(self.dir) / "wide.png"
        Image.new("RGBA", (300, 100), (10, 20, 30, 255)).save(self.wide)

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
            self.assertEqual(icon.size, (192, 192))

    def test_a_crest_that_is_not_square_is_fitted_rather_than_cropped(self):
        """The requirement is square (D-33) and `manage.py check` says so, but
        a warning does not stop a service: what is served has to stay square
        anyway, or the manifest declares 192x192 and hands over 300x100."""
        with override_settings(ST_LOGO=str(self.wide)):
            response = self.client.get("/icon/192.png")
        with Image.open(io.BytesIO(response.content)) as icon:
            self.assertEqual(icon.size, (192, 192))
            # Centred on transparency: the corners stay empty, the middle does not.
            self.assertEqual(icon.getpixel((1, 1))[3], 0)
            self.assertEqual(icon.getpixel((96, 96))[3], 255)

    def test_an_svg_crest_is_no_crest_at_all(self):
        """PNG only (D-33). An SVG is not half-supported, it is refused: iOS
        reads none, and half-support is what leaves an empty square on the one
        kind of phone this application is meant for."""
        with override_settings(ST_LOGO=str(self.svg)):
            self.assertEqual(self.manifest()["icons"], [])
            self.assertEqual(self.client.get(reverse("logo")).status_code, 404)
            self.client.force_login(self.person)
            page = self.client.get(reverse("tickets:list")).content.decode()
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


class CrestCheckTests(TestCase):
    """The requirement, said out loud at startup instead of never.

    A crest that is not a square PNG is refused by `branding.logo_path`, and
    the refusal is silent by nature: the header simply draws no crest and the
    manifest declares no icon. This is what tells its owner why.
    """

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(__import__("shutil").rmtree, self.dir, ignore_errors=True)

    def warn(self, value):
        with override_settings(ST_LOGO=value):
            return [warning.id for warning in crest_is_a_square_png(None)]

    def png(self, name, size):
        path = Path(self.dir) / name
        Image.new("RGBA", size, (10, 20, 30, 255)).save(path)
        return str(path)

    def test_no_crest_is_not_a_misconfiguration(self):
        """Empty is the normal state of a fresh install (D-30)."""
        self.assertEqual(self.warn(""), [])

    def test_a_square_png_large_enough_says_nothing(self):
        self.assertEqual(self.warn(self.png("crest.png", (512, 512))), [])

    def test_an_svg_is_named_as_the_wrong_format(self):
        path = Path(self.dir) / "crest.svg"
        path.write_text('<svg xmlns="http://www.w3.org/2000/svg"/>')
        self.assertEqual(self.warn(str(path)), ["school_tickets.W001"])

    def test_a_path_that_is_not_there_is_named(self):
        """The likeliest mistake of all: configured on the host, not mounted
        into the container."""
        self.assertEqual(
            self.warn(str(Path(self.dir) / "missing.png")), ["school_tickets.W002"]
        )

    def test_a_png_suffix_is_not_a_png_file(self):
        path = Path(self.dir) / "lie.png"
        path.write_text("not an image")
        self.assertEqual(self.warn(str(path)), ["school_tickets.W003"])

    def test_a_crest_that_is_not_square_is_named_with_its_size(self):
        with override_settings(ST_LOGO=self.png("wide.png", (300, 100))):
            warnings = crest_is_a_square_png(None)
        self.assertEqual(warnings[0].id, "school_tickets.W004")
        self.assertIn("300x100", warnings[0].msg)

    def test_a_crest_too_small_for_a_home_screen_is_named(self):
        self.assertEqual(
            self.warn(self.png("small.png", (128, 128))), ["school_tickets.W005"]
        )


class CatalogueTests(TestCase):
    """The catalogues themselves (D-23, D-34).

    Two of these read the `.po` files rather than the compiled `.mo`, because
    the `.po` are what the repository carries: a string added and never
    translated has to fail here, on a fresh clone, before anybody has run
    `compilemessages`.
    """

    LOCALE = Path(settings.BASE_DIR) / "locale"

    def entries(self, language):
        """(msgid, msgstr-is-empty) for every entry but the header."""
        text = (self.LOCALE / language / "LC_MESSAGES" / "django.po").read_text()
        for block in text.split("\n\n"):
            lines = block.split("\n")
            msgid = [l for l in lines if l.startswith("msgid ")]
            if not msgid or msgid[0] == 'msgid ""':
                continue  # the header, whose msgid is empty by definition
            body = [l for l in lines if l.startswith("msgstr")]
            filled = any(l not in ('msgstr ""', 'msgstr[0] ""', 'msgstr[1] ""') for l in body)
            yield msgid[0], filled

    def test_german_and_french_are_complete(self):
        """Untranslated is not a state this project ships in: the school is
        German-speaking, and a half-translated page is worse than an English
        one -- it reads as broken rather than as untranslated."""
        for language in ("de", "fr"):
            with self.subTest(language=language):
                missing = [msgid for msgid, filled in self.entries(language) if not filled]
                self.assertEqual(missing, [], f"{len(missing)} untranslated in {language}")

    def test_the_english_catalogue_is_deliberately_empty(self):
        """The msgid **are** the English (D-23), so every msgstr falls back to
        them. The catalogue exists for Crowdin to have a source, not to hold a
        second copy of the same sentences."""
        filled = [msgid for msgid, is_filled in self.entries("en") if is_filled]
        # Only the plural entries carry text: an empty msgstr[0] would leave
        # gettext to guess a plural rule it has not been given.
        self.assertTrue(all("once" in msgid for msgid in filled), filled)

    def test_the_french_plural_rule_is_not_the_english_one(self):
        """makemessages writes `n != 1` for every language it creates. French
        counts zero as singular, so the header has to be corrected by hand --
        and this is what remembers it after the next `makemessages`."""
        header = (self.LOCALE / "fr" / "LC_MESSAGES" / "django.po").read_text()
        self.assertIn("plural=(n > 1)", header)
        german = (self.LOCALE / "de" / "LC_MESSAGES" / "django.po").read_text()
        self.assertIn("plural=(n != 1)", german)

    @skipUnless(
        (Path(settings.BASE_DIR) / "locale/de/LC_MESSAGES/django.mo").exists(),
        "catalogues not compiled: run manage.py compilemessages",
    )
    def test_a_page_really_comes_out_in_german(self):
        """`.mo` files are build artefacts and stay out of the repository, so
        this one skips rather than fails on a fresh clone. It is still the only
        test that proves the whole chain -- extraction, translation, LocaleMiddleware."""
        school = School.objects.create(slug="lgb", name="LGB")
        person = User.objects.enroll(school=school, cn="lena", role=Role.MEMBER)
        person.language = "de"
        person.save(update_fields=["language"])
        self.client.force_login(person)
        page = self.client.get(reverse("tickets:list")).content.decode()
        self.assertIn("Störung melden", page)      # Report a fault
        self.assertIn("Kein sichtbares Ticket.", page)
        self.assertNotIn("Report a fault", page)


class CatalogueCheckTests(TestCase):
    """`.mo` files are build artefacts (D-34), and this is what that costs.

    Deploy without `compilemessages` and every page comes out in English, with
    no error and no log line. Keeping the binaries out of the repository is
    only defensible if their absence is loud.
    """

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(__import__("shutil").rmtree, self.dir, ignore_errors=True)

    def catalogue(self, language, *, compiled):
        path = Path(self.dir) / language / "LC_MESSAGES"
        path.mkdir(parents=True)
        (path / "django.po").write_text('msgid ""\nmsgstr ""\n')
        if compiled:
            (path / "django.mo").write_bytes(b"")

    def warn(self):
        with override_settings(LOCALE_PATHS=[self.dir]):
            return [warning.id for warning in catalogues_are_compiled(None)]

    def test_a_compiled_catalogue_says_nothing(self):
        self.catalogue("de", compiled=True)
        self.catalogue("fr", compiled=True)
        self.assertEqual(self.warn(), [])

    def test_a_catalogue_left_uncompiled_is_named(self):
        self.catalogue("de", compiled=False)
        self.catalogue("fr", compiled=True)
        with override_settings(LOCALE_PATHS=[self.dir]):
            warnings = catalogues_are_compiled(None)
        self.assertEqual([w.id for w in warnings], ["school_tickets.W006"])
        self.assertIn("de", warnings[0].msg)
        self.assertNotIn("fr", warnings[0].msg)

    def test_english_is_not_expected_to_be_compiled(self):
        """Its msgstr are empty by design: the msgid are the English (D-23)."""
        self.catalogue("en", compiled=False)
        self.assertEqual(self.warn(), [])

    def test_a_language_with_no_catalogue_at_all_is_not_a_warning(self):
        """LANGUAGES may list one nobody has started. That is a translation
        that does not exist, not a build step somebody forgot."""
        self.assertEqual(self.warn(), [])
