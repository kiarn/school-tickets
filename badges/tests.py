# SPDX-License-Identifier: GPL-3.0-or-later
"""Badges (D-10, D-24). The tests that matter are the ones about what is NOT shown."""

from django.db import IntegrityError
from django.test import TestCase
from django.urls import reverse

from accounts.authz import Role, Visibility
from accounts.models import AuditLog, School, User
from parc.models import Room
from tickets.models import Ticket

from .models import Badge, BadgeAward


class CatalogTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.school = School.objects.create(slug="lycee", name="Lycee")
        cls.other_school = School.objects.create(slug="other", name="Other")
        cls.member = User.objects.enroll(
            school=cls.school, cn="pupil", role=Role.MEMBER, display_name="Lea"
        )
        cls.admin = User.objects.enroll(
            school=cls.school, cn="admin", role=Role.ADMIN, display_name="Admin"
        )
        cls.local = Badge.objects.create(
            slug="lycee-hdmi", school=cls.school, name="First HDMI fault"
        )
        cls.shared = Badge.objects.create(
            slug="first-linbo", school=None, is_catalog=True, name="First LINBO sync"
        )
        cls.foreign = Badge.objects.create(
            slug="other-thing", school=cls.other_school, name="Somebody else's badge"
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

    def test_another_school_s_badges_stay_there(self):
        self.client.force_login(self.member)
        response = self.client.get(reverse("badges:catalog"))
        self.assertNotContains(response, "Somebody else&#x27;s badge")

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

    def test_a_created_badge_belongs_to_the_school_and_is_never_shared(self):
        """A shared badge means a string in the Crowdin catalogues (D-15): not
        something one school decides on an afternoon."""
        self.client.force_login(self.admin)
        self.client.post(reverse("badges:create"), {"name": "First full install"})
        badge = Badge.objects.get(name="First full install")
        self.assertEqual(badge.school, self.school)
        self.assertFalse(badge.is_catalog)
        self.assertTrue(badge.slug.startswith("lycee-"))

    def test_two_schools_may_invent_the_same_badge(self):
        """`slug` is unique across every school, catalogue included."""
        self.client.force_login(self.admin)
        self.client.post(reverse("badges:create"), {"name": "Somebody else's badge"})
        self.assertEqual(Badge.objects.filter(name="Somebody else's badge").count(), 2)


class AwardTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.school = School.objects.create(slug="lycee", name="Lycee")
        cls.room = Room.objects.create(school=cls.school, name="204")
        cls.member = User.objects.enroll(
            school=cls.school, cn="pupil", role=Role.MEMBER, display_name="Lea"
        )
        cls.reporter = User.objects.enroll(
            school=cls.school, cn="teacher", role=Role.REPORTER, display_name="Sam"
        )
        cls.admin = User.objects.enroll(
            school=cls.school, cn="admin", role=Role.ADMIN, display_name="Admin"
        )
        cls.badge = Badge.objects.create(
            slug="lycee-hdmi", school=cls.school, name="First HDMI fault"
        )
        cls.ticket = Ticket.objects.create(
            school=cls.school, room=cls.room, room_label="204", title="Black screen",
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
        cls.school = School.objects.create(slug="lycee", name="Lycee")
        cls.member = User.objects.enroll(
            school=cls.school, cn="pupil", role=Role.MEMBER, display_name="Lea"
        )
        cls.other = User.objects.enroll(
            school=cls.school, cn="pupil2", role=Role.MEMBER, display_name="Nils"
        )
        cls.admin = User.objects.enroll(
            school=cls.school, cn="admin", role=Role.ADMIN
        )
        cls.badge = Badge.objects.create(slug="lycee-hdmi", school=cls.school, name="HDMI")

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
        cls.school = School.objects.create(slug="default-school", name="Lycee")
        cls.boss = User.objects.enroll(school=cls.school, cn="boss", role=Role.ADMIN)
        cls.own = Badge.objects.create(
            slug="default-school-hdmi", school=cls.school, name="HDMI"
        )
        cls.catalog = Badge.objects.create(
            slug="first-linbo", school=None, is_catalog=True, name="First LINBO sync"
        )

    def setUp(self):
        self.client.force_login(self.boss)

    def test_creating_a_badge_asks_only_what_a_school_decides(self):
        page = self.client.get(reverse("admin:badges_badge_add"))
        self.assertEqual(
            list(page.context["adminform"].form.fields),
            ["name", "description", "icon", "category"],
        )

    def test_a_badge_made_here_is_the_school_s_own_and_never_shared(self):
        self.client.post(
            reverse("admin:badges_badge_add"),
            {"name": "Werkstatt-Helfer", "description": "", "icon": "", "category": ""},
        )
        badge = Badge.objects.get(name="Werkstatt-Helfer")
        self.assertEqual(badge.school, self.school)
        self.assertFalse(badge.is_catalog)
        # The school goes into the slug: two schools inventing the same name
        # must not collide, `Badge.slug` being unique everywhere.
        self.assertTrue(badge.slug.startswith("default-school-"))

    def test_a_catalogue_badge_is_read_only_except_its_tally(self):
        readonly = self.client.get(
            reverse("admin:badges_badge_change", args=[self.catalog.pk])
        ).context["adminform"].model_admin.get_readonly_fields(None, self.catalog)
        # Its name is the msgid the .po files are keyed on: editing it here
        # would silently orphan every translation of it.
        for field in ("name", "description", "school", "is_catalog", "slug"):
            self.assertIn(field, readonly)
        self.assertNotIn("award_count", readonly)

    def test_a_school_badge_keeps_its_text_editable(self):
        readonly = self.client.get(
            reverse("admin:badges_badge_change", args=[self.own.pk])
        ).context["adminform"].model_admin.get_readonly_fields(None, self.own)
        self.assertNotIn("name", readonly)
        self.assertIn("school", readonly)

    def test_the_database_refuses_a_catalogue_badge_attached_to_a_school(self):
        with self.assertRaises(IntegrityError):
            Badge.objects.create(
                slug="hybrid", is_catalog=True, school=self.school, name="Hybrid"
            )

    def test_the_database_refuses_a_school_badge_attached_to_none(self):
        """It would surface in every school's catalogue, untranslated."""
        with self.assertRaises(IntegrityError):
            Badge.objects.create(slug="orphan", is_catalog=False, school=None, name="Orphan")

    def test_two_badges_of_one_school_may_not_share_a_name(self):
        with self.assertRaises(IntegrityError):
            Badge.objects.create(slug="hdmi-again", school=self.school, name="HDMI")


class BadgeAwardAdminTests(TestCase):
    """Awarding has a screen (D-24). This has a delete button, and that is all."""

    @classmethod
    def setUpTestData(cls):
        cls.school = School.objects.create(slug="default-school", name="Lycee")
        cls.boss = User.objects.enroll(school=cls.school, cn="boss", role=Role.ADMIN)
        cls.pupil = User.objects.enroll(school=cls.school, cn="pupil", role=Role.MEMBER)
        cls.badge = Badge.objects.create(
            slug="default-school-hdmi", school=cls.school, name="HDMI"
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
        second = User.objects.enroll(school=self.school, cn="other", role=Role.MEMBER)
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
