# SPDX-License-Identifier: GPL-3.0-or-later
"""Profiles, and the language preference that had nowhere to live (D-24)."""

from django.contrib import admin as django_admin
from django.db import IntegrityError
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import NoReverseMatch, reverse

from accounts import backends
from accounts.authz import Role, Visibility
from accounts.models import AuditLog, PushSubscription, User
from badges.models import Badge, BadgeAward
from parc.models import Room
from tickets.models import Ticket, TicketAssignee


class ProfileTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.member = User.objects.enroll(
            cn="pupil", role=Role.MEMBER, display_name="Lea"
        )
        cls.other_member = User.objects.enroll(
            cn="pupil2", role=Role.MEMBER, display_name="Nils"
        )
        cls.admin = User.objects.enroll(
            cn="admin", role=Role.ADMIN, display_name="Admin"
        )

    def test_a_profile_shows_its_badges_with_the_sentence_that_came_with_them(self):
        badge = Badge.objects.create(
            slug="first-hdmi", name="First HDMI fault",
            icon="🔌",
        )
        BadgeAward.objects.create(
            badge=badge, user=self.member, awarded_by=self.admin,
            motivation="Found the bent pin nobody else looked for.",
        )
        self.client.force_login(self.member)
        response = self.client.get(reverse("accounts:profile"))
        self.assertContains(response, "First HDMI fault")
        self.assertContains(response, "bent pin")

    def test_an_admin_may_open_anybody(self):
        self.client.force_login(self.admin)
        response = self.client.get(
            reverse("accounts:profile_detail", args=[self.member.pk])
        )
        self.assertContains(response, "Lea")
        # Somebody else's profile is never a form.
        self.assertNotContains(response, "Display language")

    def test_a_member_may_not_open_somebody_elses(self):
        self.client.force_login(self.member)
        response = self.client.get(
            reverse("accounts:profile_detail", args=[self.other_member.pk])
        )
        self.assertEqual(response.status_code, 404)

    def test_my_own_id_lands_on_my_own_profile(self):
        self.client.force_login(self.member)
        response = self.client.get(reverse("accounts:profile_detail", args=[self.member.pk]))
        self.assertRedirects(response, reverse("accounts:profile"))


class LanguageTests(TestCase):
    """The column exists because Django has nowhere else durable to put this."""

    @classmethod
    def setUpTestData(cls):
        cls.person = User.objects.enroll(
            cn="pupil", role=Role.MEMBER, display_name="Lea"
        )

    def test_choosing_a_language_applies_to_the_next_page(self):
        self.client.force_login(self.person)
        self.client.post(reverse("accounts:profile"), {"language": "fr"})
        self.person.refresh_from_db()
        self.assertEqual(self.person.language, "fr")

        response = self.client.get(reverse("tickets:list"), HTTP_ACCEPT_LANGUAGE="de")
        # The preference outranks Accept-Language, which is the whole point:
        # a French-speaking teacher in a German school (D-15).
        self.assertContains(response, 'lang="fr"')

    def test_no_preference_leaves_the_browser_in_charge(self):
        self.client.force_login(self.person)
        response = self.client.get(reverse("tickets:list"), HTTP_ACCEPT_LANGUAGE="fr")
        self.assertContains(response, 'lang="fr"')
        response = self.client.get(reverse("tickets:list"), HTTP_ACCEPT_LANGUAGE="de")
        self.assertContains(response, 'lang="de"')

    def test_anonymising_takes_the_preference_with_it(self):
        self.person.language = "fr"
        self.person.save()
        self.person.anonymize()
        self.person.refresh_from_db()
        self.assertEqual(self.person.language, "")


# Eight failed attempts means eight password hashes, and the real hasher is
# slow by design. Nothing here tests the hasher.
@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class LoginTests(TestCase):
    """D-26. The rule being tested is not "the password matches" but
    "authenticating is not the same thing as having access" (D-22)."""

    @classmethod
    def setUpTestData(cls):
        cls.person = User.objects.enroll(
            cn="pupil", role=Role.MEMBER, display_name="Lea"
        )
        cls.person.set_password("correct horse")
        cls.person.save()

    def setUp(self):
        cache.clear()

    def post(self, **data):
        return self.client.post(reverse("accounts:login"), data)

    def test_an_enrolled_person_gets_in_by_their_cn(self):
        """USERNAME_FIELD is oidc_sub, which is NULL for everybody today: the
        login has to work on the identifier people actually have."""
        response = self.post(cn="pupil", password="correct horse")
        self.assertRedirects(response, reverse("tickets:list"))
        self.assertEqual(self.client.session["_auth_user_id"], str(self.person.pk))

    def test_logging_in_stamps_last_seen(self):
        self.post(cn="pupil", password="correct horse")
        self.person.refresh_from_db()
        self.assertIsNotNone(self.person.last_seen_at)

    def test_a_wrong_password_and_an_unknown_login_read_the_same(self):
        """Two different messages would turn the form into a way of asking who
        is enrolled."""
        wrong = self.post(cn="pupil", password="nope")
        unknown = self.post(cn="nobody", password="nope")
        self.assertContains(wrong, "Wrong login or password")
        self.assertContains(unknown, "Wrong login or password")

    def test_a_deactivated_account_no_longer_comes_in(self):
        self.person.is_active = False
        self.person.save()
        self.post(cn="pupil", password="correct horse")
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_an_anonymised_account_no_longer_comes_in(self):
        """Anonymising implies un-enrolling (D-22)."""
        self.person.anonymize()
        self.post(cn="pupil", password="correct horse")
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_an_account_with_no_password_cannot_be_guessed_into(self):
        """enroll() sets an unusable password; that must not match "" either."""
        other = User.objects.enroll(cn="fresh", role=Role.MEMBER)
        for attempt in ("", "!", other.password):
            self.post(cn="fresh", password=attempt)
            self.assertNotIn("_auth_user_id", self.client.session)

    def test_repeated_failures_stop_answering(self):
        for _ in range(backends.MAX_ATTEMPTS):
            self.post(cn="pupil", password="nope")
        # Even the right password, now: the lockout is on the cn, not on the
        # guess. Per cn and not per IP, because a school sits behind one NAT
        # address and an IP counter would lock out the building.
        response = self.post(cn="pupil", password="correct horse")
        self.assertContains(response, "Too many attempts")
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_a_good_password_clears_the_count(self):
        self.post(cn="pupil", password="nope")
        self.post(cn="pupil", password="correct horse")
        self.client.logout()
        self.assertFalse(backends.locked_out("pupil"))

    def test_signing_out_works_and_only_by_post(self):
        self.client.force_login(self.person)
        self.assertEqual(self.client.get(reverse("accounts:logout")).status_code, 405)
        self.client.post(reverse("accounts:logout"))
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_an_anonymous_visitor_is_sent_to_the_login_page(self):
        response = self.client.get(reverse("tickets:list"))
        self.assertRedirects(
            response, f"{reverse('accounts:login')}?next={reverse('tickets:list')}"
        )

    def test_there_is_nothing_to_sign_up_for(self):
        """Access is by explicit enrolment; the page says so rather than
        letting somebody find out by failing (D-22)."""
        response = self.client.get(reverse("accounts:login"))
        self.assertContains(response, "nothing to sign up for")


class ThemeTests(TestCase):
    """A dark screen is an accessibility need, not a decoration: it has to
    survive a new device, so it lives on the account (D-24, D-26)."""

    @classmethod
    def setUpTestData(cls):
        cls.person = User.objects.enroll(
            cn="admin", role=Role.ADMIN, display_name="Admin"
        )

    def test_no_preference_leaves_the_device_in_charge(self):
        self.client.force_login(self.person)
        response = self.client.get(reverse("tickets:list"))
        # No account source attribute: the script in base.html then reads
        # prefers-color-scheme, as it always did.
        self.assertNotContains(response, "data-theme-source")

    def test_choosing_dark_applies_it_server_side(self):
        self.client.force_login(self.person)
        self.client.post(
            reverse("accounts:theme"), {"theme": "dark", "next": reverse("tickets:list")}
        )
        self.person.refresh_from_db()
        self.assertEqual(self.person.theme, "dark")

        response = self.client.get(reverse("tickets:list"))
        # Settled before the first byte of CSS, so there is no white flash to
        # look at on the way in.
        self.assertContains(response, 'data-theme="dark"')
        self.assertContains(response, 'data-theme-source="account"')

    def test_the_toggle_comes_back_where_it_was(self):
        self.client.force_login(self.person)
        response = self.client.post(
            reverse("accounts:theme"), {"theme": "dark", "next": reverse("badges:catalog")}
        )
        self.assertRedirects(response, reverse("badges:catalog"))

    def test_an_off_site_return_address_is_refused(self):
        self.client.force_login(self.person)
        response = self.client.post(
            reverse("accounts:theme"), {"theme": "dark", "next": "https://elsewhere.example/"}
        )
        self.assertRedirects(response, "/")

    def test_an_invented_theme_goes_nowhere(self):
        self.client.force_login(self.person)
        response = self.client.post(reverse("accounts:theme"), {"theme": "midnight"})
        self.assertEqual(response.status_code, 404)

    def test_anonymising_takes_the_preference_with_it(self):
        self.person.theme = "dark"
        self.person.save()
        self.person.anonymize()
        self.person.refresh_from_db()
        self.assertEqual(self.person.theme, "")


class AdminRoleTests(TestCase):
    """D-27: one administration role, and it is a Django superuser.

    The bug these tests exist for is silent: `is_staff` followed the role while
    permissions did not, so an administrator opened /admin/ and found an empty
    page -- no error, no refusal, just nothing.
    """

    def person(self, cn, role):
        return User.objects.enroll(cn=cn, role=role)

    def test_an_admin_is_a_django_superuser_and_can_actually_do_something_there(self):
        admin = self.person("boss", Role.ADMIN)
        self.assertTrue(admin.is_superuser)
        self.assertTrue(admin.is_staff)
        self.assertTrue(admin.is_admin)
        # The half that used to be missing: permissions, not just the door.
        self.assertTrue(admin.has_perm("badges.add_badgeaward"))
        self.assertTrue(admin.has_module_perms("parc"))

    def test_nobody_else_gets_near_the_django_admin(self):
        for cn, role in (("pupil", Role.MEMBER), ("teacher", Role.REPORTER)):
            person = self.person(cn, role)
            self.assertFalse(person.is_superuser)
            self.assertFalse(person.is_staff)
            self.assertFalse(person.has_perm("badges.add_badgeaward"))

    def test_the_flag_follows_a_role_change_made_anywhere(self):
        """Including from the Django admin, which is why it lives in save()."""
        person = self.person("pupil", Role.MEMBER)
        person.role = Role.ADMIN
        person.save()
        person.refresh_from_db()
        self.assertTrue(person.is_superuser)

        person.role = Role.MEMBER
        person.save(update_fields=["role"])
        person.refresh_from_db()
        self.assertFalse(person.is_superuser)

    def test_saving_something_else_leaves_the_flag_alone(self):
        person = self.person("boss", Role.ADMIN)
        person.theme = "dark"
        person.save(update_fields=["theme"])
        person.refresh_from_db()
        self.assertTrue(person.is_superuser)

    def test_the_admin_site_lets_an_admin_in_and_shows_them_something(self):
        from django.contrib import admin as django_admin
        from django.test import RequestFactory

        admin_user = self.person("boss", Role.ADMIN)
        request = RequestFactory().get("/admin/")
        request.user = admin_user
        self.assertTrue(django_admin.site.has_permission(request))
        self.assertTrue(django_admin.site.get_app_list(request))

    def test_the_admin_site_turns_a_member_away(self):
        from django.contrib import admin as django_admin
        from django.test import RequestFactory

        request = RequestFactory().get("/admin/")
        request.user = self.person("pupil", Role.MEMBER)
        self.assertFalse(django_admin.site.has_permission(request))


class AdminSurfaceTests(TestCase):
    """What `/admin/` offers at all (D-50).

    The screens that answer nothing are the ones that cost the most: somebody
    uses them, believes something happened, and nothing did.
    """

    @classmethod
    def setUpTestData(cls):
        cls.boss = User.objects.enroll(cn="boss", role=Role.ADMIN)

    def setUp(self):
        self.client.force_login(self.boss)

    def test_groups_are_not_offered(self):
        """The permission machinery is short-circuited: `role == admin` sets
        `is_superuser` (D-27) and a superuser passes every check without Django
        reading a group. A group here could grant nothing to anybody."""
        with self.assertRaises(NoReverseMatch):
            reverse("admin:auth_group_changelist")

    def test_mutes_are_not_offered(self):
        """A mute belongs to the person, on their profile (D-24)."""
        with self.assertRaises(NoReverseMatch):
            reverse("admin:notifications_mute_changelist")

    def test_what_the_worker_writes_is_read_and_not_edited(self):
        """Machine output: a LINBO status and the trace of a sync pass. Both
        stay visible -- `SyncRun.error` carries the volume guard's refusal --
        and neither takes an entry."""
        for url in ("admin:parc_devicestatus_add", "admin:parc_syncrun_add",
                    "admin:notifications_notification_add"):
            with self.subTest(url=url):
                self.assertEqual(self.client.get(reverse(url)).status_code, 403)

    def test_the_audit_log_takes_no_entry_and_no_deletion(self):
        """A duty to account for actions is not a table somebody keeps."""
        self.assertEqual(
            self.client.get(reverse("admin:accounts_auditlog_add")).status_code, 403
        )


class AdminEnrolmentTests(TestCase):
    """What ``/admin/`` may be asked about a person, and what it may not.

    The bug at the root of these tests was silent and total: the change page
    rendered ``password`` as an ordinary text box, so a teacher resetting
    somebody's password wrote it into the column unhashed and locked the
    account out of an application that then said nothing about it.
    """

    @classmethod
    def setUpTestData(cls):
        cls.boss = User.objects.enroll(cn="boss", role=Role.ADMIN)

    def setUp(self):
        self.client.force_login(self.boss)

    def test_enrolling_asks_for_a_login_and_a_role_and_nothing_else(self):
        page = self.client.get(reverse("admin:accounts_user_add"))
        self.assertEqual(list(page.context["adminform"].form.fields), ["cn", "role"])

    def test_the_change_page_never_offers_a_password_or_the_superuser_flag(self):
        page = self.client.get(
            reverse("admin:accounts_user_change", args=[self.boss.pk])
        )
        offered = set(page.context["adminform"].form.fields)
        # `is_superuser` is derived from the role (D-27): a checkbox for it
        # would be overwritten by save() the moment it was ticked.
        self.assertFalse(
            offered & {"password", "is_superuser", "groups", "user_permissions"}
        )

    def test_the_directory_and_the_person_own_their_own_fields(self):
        readonly = self.client.get(
            reverse("admin:accounts_user_change", args=[self.boss.pk])
        ).context["adminform"].model_admin.get_readonly_fields(None, self.boss)
        for field in ("display_name", "email", "avatar_url", "oidc_sub",
                      "language", "theme"):
            self.assertIn(field, readonly)

    def test_an_enrolment_made_here_is_the_same_one_manage_py_makes(self):
        self.client.post(
            reverse("admin:accounts_user_add"), {"cn": "neu", "role": Role.MEMBER}
        )
        person = User.objects.get(cn="neu")
        self.assertEqual(person.enrolled_by, self.boss)
        self.assertIsNotNone(person.enrolled_at)
        # No password, rather than one nobody can use: `manage.py set_password`
        # grants it, until OIDC (D-26).
        self.assertFalse(person.has_usable_password())

    def test_enrolling_and_changing_a_role_both_reach_the_audit_log(self):
        self.client.post(
            reverse("admin:accounts_user_add"), {"cn": "neu", "role": Role.MEMBER}
        )
        person = User.objects.get(cn="neu")
        entry = AuditLog.objects.get(action=AuditLog.Action.ENROLL)
        self.assertEqual((entry.actor, entry.target_id), (self.boss, person.pk))

        self.client.post(
            reverse("admin:accounts_user_change", args=[person.pk]),
            {"cn": "neu", "role": Role.ADMIN, "is_active": "on"},
        )
        entry = AuditLog.objects.get(action=AuditLog.Action.ROLE_CHANGE)
        self.assertEqual(entry.payload, {"from": Role.MEMBER, "to": Role.ADMIN})
        person.refresh_from_db()
        self.assertTrue(person.is_superuser)

    def test_a_second_enrolment_under_the_same_login_is_a_sentence_not_a_500(self):
        User.objects.enroll(cn="neu", role=Role.MEMBER)
        page = self.client.post(
            reverse("admin:accounts_user_add"), {"cn": "neu", "role": Role.MEMBER}
        )
        self.assertEqual(page.status_code, 200)
        self.assertFormError(page.context["adminform"].form, "cn",
                             "neu is already enrolled.")

    def test_a_bound_account_keeps_its_cn_and_a_tombstone_keeps_everything(self):
        person = User.objects.enroll(cn="neu", role=Role.MEMBER)
        admin_class = django_admin.site._registry[User]
        # Correctable while it is only a claim somebody typed.
        self.assertNotIn("cn", admin_class.get_readonly_fields(None, person))

        person.bind_oidc_sub("sub-123")
        self.assertIn("cn", admin_class.get_readonly_fields(None, person))

        person.anonymize()
        for field in ("cn", "role", "is_active"):
            self.assertIn(field, admin_class.get_readonly_fields(None, person))


class ErasureTests(TestCase):
    """What `anonymize()` actually removes, against what doc 06 tabulates.

    The table listed three deletions and the code performed one. Two code
    comments described the missing ones as already done -- ``TicketAssignee``
    said "Deleted on anonymisation", and `anonymize()` itself said badges go
    "as assignments already did". Both were describing a cascade that fires on
    a DELETE of the row, which a tombstone is precisely not.
    """

    @classmethod
    def setUpTestData(cls):
        cls.room = Room.objects.create(name="A101", sort_key="1-101")
        cls.boss = User.objects.enroll(cn="boss", role=Role.ADMIN)

    def setUp(self):
        self.pupil = User.objects.enroll(
            cn="pupil", role=Role.MEMBER, display_name="Lena"
        )
        self.ticket = Ticket.objects.create(
            room=self.room, title="Beamer",
            visibility=Visibility.TEAM, created_by=self.boss,
        )
        TicketAssignee.objects.create(ticket=self.ticket, user=self.pupil)
        BadgeAward.objects.create(
            badge=Badge.objects.create(slug="lycee-x", name="X"),
            user=self.pupil, awarded_by=self.boss,
        )
        PushSubscription.objects.create(
            user=self.pupil, endpoint="https://push/x", p256dh="k", auth="a",
            user_agent="Pixel 7, Chrome 141",
        )

    def test_no_named_assignment_is_left_behind(self):
        self.pupil.anonymize()
        self.assertFalse(TicketAssignee.objects.filter(user=self.pupil).exists())

    def test_the_device_stops_receiving_and_stops_being_described(self):
        """A `user_agent` is device data about somebody just erased."""
        self.pupil.anonymize()
        self.assertFalse(PushSubscription.objects.filter(user=self.pupil).exists())

    def test_the_badge_goes_with_the_name(self):
        self.pupil.anonymize()
        self.assertFalse(BadgeAward.objects.filter(user=self.pupil).exists())

    def test_what_the_table_says_to_keep_is_kept(self):
        """The thread is the team's technical memory: it survives, unnamed."""
        self.pupil.anonymize()
        self.ticket.refresh_from_db()
        self.assertTrue(Ticket.objects.filter(pk=self.ticket.pk).exists())
        self.assertEqual(str(User.objects.get(pk=self.pupil.pk)), "Former member")


class TombstoneUniquenessTests(TestCase):
    """Why a tombstone's `cn` is NULL and not the empty string.

    "Unique among the named" reads as a condition on `anonymized_at`, and a
    condition is a partial index. MariaDB has none: Django warned and created
    nothing, so the rule held in SQLite -- development, and this very suite --
    and was absent in production (D-03). Carrying the exception in the data
    instead behaves identically on both engines.
    """

    def test_an_erased_account_keeps_no_cn_at_all(self):
        person = User.objects.enroll(cn="lena", role=Role.MEMBER)
        person.anonymize()
        person.refresh_from_db()
        self.assertIsNone(person.cn)

    def test_more_than_one_person_may_be_buried(self):
        """The case that forced the condition, and now needs none."""
        for cn in ("lena", "jonas", "mira"):
            User.objects.enroll(cn=cn, role=Role.MEMBER).anonymize()
        self.assertEqual(User.objects.filter(cn__isnull=True).count(), 3)

    def test_two_live_accounts_still_may_not_share_a_login(self):
        User.objects.enroll(cn="lena", role=Role.MEMBER)
        with self.assertRaises(IntegrityError):
            User.objects.enroll(cn="lena", role=Role.MEMBER)

    def test_the_constraint_carries_no_condition_so_mariadb_creates_it(self):
        """The regression this whole change exists to prevent."""
        constraint = next(
            c for c in User._meta.constraints if c.name == "unique_cn"
        )
        self.assertIsNone(constraint.condition)

    def test_a_login_freed_by_an_erasure_can_be_enrolled_again(self):
        """The school reassigns a cn; the newcomer inherits no history (D-22)."""
        User.objects.enroll(cn="lena", role=Role.MEMBER).anonymize()
        newcomer = User.objects.enroll(cn="lena", role=Role.MEMBER)
        self.assertEqual(newcomer.badge_awards.count(), 0)
