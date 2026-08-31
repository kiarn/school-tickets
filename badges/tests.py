# SPDX-License-Identifier: GPL-3.0-or-later
"""Badges (D-10, D-24). The tests that matter are the ones about what is NOT shown."""

from django.db import IntegrityError
from django.test import TestCase
from django.urls import reverse

from accounts.authz import Role, Visibility
from accounts.models import AuditLog, User
from parc.models import Room
from tickets.models import Ticket

from .models import Badge, BadgeAward


class CatalogTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.member = User.objects.enroll(
            cn="pupil", role=Role.MEMBER, display_name="Lea"
        )
        cls.admin = User.objects.enroll(
            cn="admin", role=Role.ADMIN, display_name="Admin"
        )
        cls.local = Badge.objects.create(
            slug="lycee-hdmi", name="First HDMI fault"
        )
        cls.shared = Badge.objects.create(
            slug="first-linbo", is_catalog=True, name="First LINBO sync"
        )

    def test_the_catalogue_shows_what_exists_and_never_who_holds_it(self):
        """Listing holders is a leaderboard as soon as you count them (D-24)."""
        BadgeAward.objects.create(badge=self.local, user=self.member, awarded_by=self.admin)
        # Read by somebody else, so that the only "Lea" a page could contain is
        # a holder -- the reader's own name is in the navbar of every screen.
        self.client.force_login(self.admin)
        response = self.client.get(reverse("badges:catalog"))
        self.assertContains(response, "First HDMI fault")
        self.assertContains(response, "First LINBO sync")
        self.assertNotContains(response, "Lea")

    def test_only_an_admin_is_offered_the_management_controls(self):
        self.client.force_login(self.member)
        response = self.client.get(reverse("badges:catalog"))
        self.assertNotContains(response, reverse("badges:create"))
        self.assertNotContains(response, reverse("badges:award"))

        self.client.force_login(self.admin)
        response = self.client.get(reverse("badges:catalog"))
        self.assertContains(response, reverse("badges:create"))

    def test_a_member_cannot_create_a_badge(self):
        self.client.force_login(self.member)
        response = self.client.post(reverse("badges:create"), {"name": "Mine"})
        self.assertEqual(response.status_code, 404)
        self.assertFalse(Badge.objects.filter(name="Mine").exists())

    def test_a_created_badge_is_never_shared(self):
        """A shared badge means a string in the Crowdin catalogues (D-15): not
        something decided here on an afternoon."""
        self.client.force_login(self.admin)
        self.client.post(reverse("badges:create"), {"name": "First full install"})
        badge = Badge.objects.get(name="First full install")
        self.assertFalse(badge.is_catalog)
        self.assertEqual(badge.slug, "first-full-install")

    def test_a_name_the_catalogue_already_uses_is_refused(self):
        """Until D-49 the two could coexist, one attached to a school and one
        not. They cannot now, and that is the intended tightening: two badges
        of the same name are indistinguishable on a profile page."""
        self.client.force_login(self.admin)
        self.client.post(reverse("badges:create"), {"name": "First LINBO sync"})
        self.assertEqual(Badge.objects.filter(name="First LINBO sync").count(), 1)


class AwardTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.room = Room.objects.create(name="204")
        cls.member = User.objects.enroll(
            cn="pupil", role=Role.MEMBER, display_name="Lea"
        )
        cls.reporter = User.objects.enroll(
            cn="teacher", role=Role.REPORTER, display_name="Sam"
        )
        cls.admin = User.objects.enroll(
            cn="admin", role=Role.ADMIN, display_name="Admin"
        )
        cls.badge = Badge.objects.create(
            slug="lycee-hdmi", name="First HDMI fault"
        )
        cls.ticket = Ticket.objects.create(
            room=cls.room, room_label="204", title="Black screen",
            visibility=Visibility.TEAM, created_by=cls.member,
        )

    def award(self, **data):
        return self.client.post(reverse("badges:award"), data)

    def test_an_admin_awards_and_the_act_is_recorded(self):
        self.client.force_login(self.admin)
        response = self.award(
            badge=self.badge.pk, user=self.member.pk, ticket=self.ticket.pk,
            motivation="Found the bent pin nobody else looked for.",
        )
        given = BadgeAward.objects.get()
        self.assertRedirects(
            response, reverse("accounts:profile_detail", args=[self.member.pk])
        )
        self.assertEqual(given.awarded_by, self.admin)
        self.assertEqual(given.ticket, self.ticket)
        entry = AuditLog.objects.get(action=AuditLog.Action.BADGE_AWARD)
        # Identifiers only: never the sentence written about somebody.
        self.assertEqual(entry.payload, {"badge": self.badge.pk, "user": self.member.pk})
        self.assertNotIn("bent pin", str(entry.payload))

    def test_a_member_awards_nothing(self):
        """Between peers a badge becomes currency; the adult is what gives it
        its worth (D-24)."""
        self.client.force_login(self.member)
        self.assertEqual(
            self.award(badge=self.badge.pk, user=self.member.pk).status_code, 404
        )
        self.assertEqual(BadgeAward.objects.count(), 0)

    def test_a_reporter_is_not_in_the_list(self):
        """A badge recognises a repair, and reporting is not repairing."""
        self.client.force_login(self.admin)
        self.award(badge=self.badge.pk, user=self.reporter.pk, motivation="x")
        self.assertEqual(BadgeAward.objects.count(), 0)

    def test_the_same_badge_is_never_awarded_twice(self):
        BadgeAward.objects.create(badge=self.badge, user=self.member, awarded_by=self.admin)
        self.client.force_login(self.admin)
        response = self.award(badge=self.badge.pk, user=self.member.pk, motivation="again")
        self.assertContains(response, "already hold")
        self.assertEqual(BadgeAward.objects.count(), 1)

    def test_the_form_arrives_pointed_at_the_person(self):
        self.client.force_login(self.admin)
        response = self.client.get(reverse("badges:award_to", args=[self.member.pk]))
        self.assertContains(response, "Lea")


class TallyTests(TestCase):
    """R-17: the award goes, the fact that it happened stays."""

    @classmethod
    def setUpTestData(cls):
        cls.member = User.objects.enroll(
            cn="pupil", role=Role.MEMBER, display_name="Lea"
        )
        cls.other = User.objects.enroll(
            cn="pupil2", role=Role.MEMBER, display_name="Nils"
        )
        cls.admin = User.objects.enroll(
            cn="admin", role=Role.ADMIN
        )
        cls.badge = Badge.objects.create(slug="lycee-hdmi", name="HDMI")

    def count(self):
        self.badge.refresh_from_db()
        return self.badge.award_count

    def test_awarding_counts(self):
        BadgeAward.objects.create(badge=self.badge, user=self.member, awarded_by=self.admin)
        BadgeAward.objects.create(badge=self.badge, user=self.other, awarded_by=self.admin)
        self.assertEqual(self.count(), 2)

    def test_taking_a_badge_back_uncounts_it(self):
        """A withdrawal is a correction: it should never have been counted."""
        given = BadgeAward.objects.create(
            badge=self.badge, user=self.member, awarded_by=self.admin
        )
        given.delete()
        self.assertEqual(self.count(), 0)

    def test_anonymising_removes_the_award_and_keeps_the_tally(self):
        BadgeAward.objects.create(badge=self.badge, user=self.member, awarded_by=self.admin)
        self.member.anonymize()
        self.assertEqual(BadgeAward.objects.filter(user=self.member).count(), 0)
        # The school keeps the fact that this badge was earned once; it just no
        # longer keeps by whom.
        self.assertEqual(self.count(), 1)

    def test_the_catalogue_shows_the_tally_and_still_no_name(self):
        BadgeAward.objects.create(badge=self.badge, user=self.member, awarded_by=self.admin)
        self.client.force_login(self.admin)
        response = self.client.get(reverse("badges:catalog"))
        self.assertContains(response, "awarded once")
        self.assertNotContains(response, "Lea")


class BadgeAdminTests(TestCase):
    """The dividing line of D-15, enforced rather than merely intended.

    The catalogue belongs to the project: its strings are `msgid`s shipped
    with the code. A school's own badges belong to the school. The admin used
    to let somebody build the two impossible things in between, and nothing --
    no form, no constraint -- said no.
    """

    @classmethod
    def setUpTestData(cls):
        cls.boss = User.objects.enroll(cn="boss", role=Role.ADMIN)
        cls.own = Badge.objects.create(
            slug="default-school-hdmi", name="HDMI"
        )
        cls.catalog = Badge.objects.create(
            slug="first-linbo", is_catalog=True, name="First LINBO sync"
        )

    def setUp(self):
        self.client.force_login(self.boss)

    def test_creating_a_badge_asks_only_what_a_school_decides(self):
        page = self.client.get(reverse("admin:badges_badge_add"))
        self.assertEqual(
            list(page.context["adminform"].form.fields),
            ["name", "description", "icon", "is_active"],
        )

    def test_a_badge_made_here_is_never_shared(self):
        self.client.post(
            reverse("admin:badges_badge_add"),
            {"name": "Werkstatt-Helfer", "description": "", "icon": "", "is_active": "on"},
        )
        badge = Badge.objects.get(name="Werkstatt-Helfer")
        self.assertFalse(badge.is_catalog)
        self.assertEqual(badge.slug, "werkstatt-helfer")

    def test_a_catalogue_badge_keeps_its_identity_and_lends_its_words(self):
        adminform = self.client.get(
            reverse("admin:badges_badge_change", args=[self.catalog.pk])
        ).context["adminform"]
        readonly = adminform.model_admin.get_readonly_fields(None, self.catalog)
        # The identity, and the ladder the project defines it in. Renaming a
        # rung here used to orphan its translations; it cannot any more, since
        # the shipped text is keyed on the slug -- which is exactly why the
        # slug, and not the name, is what stays locked.
        for field in ("slug", "is_catalog", "family", "level"):
            self.assertIn(field, readonly)
        for field in ("name", "description", "is_active", "award_count"):
            self.assertNotIn(field, readonly)
        # `school` is not among them and is not on the page either: the column
        # itself is gone since D-49.
        self.assertNotIn("school", adminform.form.fields)

    def test_renaming_a_catalogue_badge_is_local_and_reversible(self):
        """The whole reason the lock could be lifted.

        The shipped text is found by slug, so an override changes what this
        school reads and nothing else -- and emptying the field brings the
        translated wording back, rather than leaving a hole.
        """
        # Already there: the catalogue arrives with the code, so the data
        # migration put it in this database as it would in a new install.
        badge = Badge.objects.get(slug="network-1")
        self.assertEqual(badge.label, "Network · Beginner")
        self.assertEqual(badge.name, "")

        badge.name = "Kabelfuchs"
        self.assertEqual(badge.label, "Kabelfuchs")

        badge.name = ""
        self.assertEqual(badge.label, "Network · Beginner")

    def test_a_badge_whose_slug_left_the_catalogue_still_draws(self):
        """A later version may drop a rung. The awards made under it happened."""
        badge = Badge.objects.create(slug="withdrawn-rung", is_catalog=True)
        self.assertEqual(badge.label, "withdrawn-rung")

    def test_a_locally_made_badge_keeps_its_text_editable(self):
        adminform = self.client.get(
            reverse("admin:badges_badge_change", args=[self.own.pk])
        ).context["adminform"]
        self.assertNotIn("name", adminform.model_admin.get_readonly_fields(None, self.own))
        self.assertNotIn("school", adminform.form.fields)

    def test_the_identity_is_the_slug_and_names_may_now_repeat(self):
        """`name` used to be unique, back when it was the identity.

        It is an override now, blank on nearly every row, and MariaDB gives us
        no conditional index (D-03): a unique one would have collided on the
        second empty name.
        """
        Badge.objects.create(slug="hdmi-again", name="HDMI")
        self.assertEqual(Badge.objects.filter(name="HDMI").count(), 2)

        with self.assertRaises(IntegrityError):
            Badge.objects.create(slug="hdmi-again", name="Something else")


class BadgeAwardAdminTests(TestCase):
    """Awarding has a screen (D-24). This has a delete button, and that is all."""

    @classmethod
    def setUpTestData(cls):
        cls.boss = User.objects.enroll(cn="boss", role=Role.ADMIN)
        cls.pupil = User.objects.enroll(cn="pupil", role=Role.MEMBER)
        cls.badge = Badge.objects.create(
            slug="default-school-hdmi", name="HDMI"
        )
        cls.given = BadgeAward.objects.create(
            badge=cls.badge, user=cls.pupil, awarded_by=cls.boss
        )

    def setUp(self):
        self.client.force_login(self.boss)

    def test_nothing_is_awarded_from_here(self):
        """It went around every rule `AwardForm` enforces, silently.

        An arbitrary `awarded_by` where the application forces the signed-in
        admin; people the application keeps out of the list, reporters
        included, since a badge recognises a repair (D-10); another school's
        pupil; and no `BADGE_AWARD` line where the application writes one.
        """
        self.assertEqual(self.client.get(reverse("admin:badges_badgeaward_add")).status_code, 403)

    def test_the_sentence_somebody_was_given_is_not_rewritten_months_later(self):
        page = self.client.get(
            reverse("admin:badges_badgeaward_change", args=[self.given.pk])
        )
        self.assertFalse(page.context["adminform"].form.fields)

    def test_taking_one_back_is_accounted_for(self):
        """The asymmetry that mattered: giving was logged, taking back was not."""
        self.client.post(
            reverse("admin:badges_badgeaward_delete", args=[self.given.pk]), {"post": "yes"}
        )
        entry = AuditLog.objects.get(action=AuditLog.Action.BADGE_REVOKE)
        self.assertEqual(entry.actor, self.boss)
        self.assertEqual(entry.target_id, self.given.pk)
        self.assertEqual(entry.payload, {"badge": self.badge.pk, "user": self.pupil.pk})

    def test_the_bulk_action_accounts_for_every_row_it_erases(self):
        """It goes through a collector that never calls `delete_model`."""
        second = User.objects.enroll(cn="other", role=Role.MEMBER)
        BadgeAward.objects.create(badge=self.badge, user=second, awarded_by=self.boss)
        self.client.post(
            reverse("admin:badges_badgeaward_changelist"),
            {
                "action": "delete_selected",
                "_selected_action": [
                    str(pk) for pk in BadgeAward.objects.values_list("pk", flat=True)
                ],
                "post": "yes",
            },
        )
        self.assertEqual(BadgeAward.objects.count(), 0)
        self.assertEqual(
            AuditLog.objects.filter(action=AuditLog.Action.BADGE_REVOKE).count(), 2
        )

    def test_an_erasure_is_not_a_withdrawal(self):
        """`anonymize()` deletes awards too (R-17), and that is another act.

        It has its own line. A per-badge trail pointing at somebody exercising
        their right to be forgotten would be the opposite of what the erasure
        is for.
        """
        self.pupil.anonymize()
        self.assertFalse(AuditLog.objects.filter(action=AuditLog.Action.BADGE_REVOKE).exists())

    def test_taking_one_back_is_still_possible_and_still_uncounts(self):
        self.assertContains(
            self.client.get(
                reverse("admin:badges_badgeaward_change", args=[self.given.pk])
            ),
            "deletelink",
        )
        self.client.post(
            reverse("admin:badges_badgeaward_delete", args=[self.given.pk]), {"post": "yes"}
        )
        self.assertFalse(BadgeAward.objects.filter(pk=self.given.pk).exists())
        self.badge.refresh_from_db()
        self.assertEqual(self.badge.award_count, 0)
