# SPDX-License-Identifier: GPL-3.0-or-later
"""Visibility is the rule we cannot afford to break.

These tests beat a careful re-read: the project's query count will only grow,
and every new view is a chance to forget ``visible_to()``.
"""

import io
import shutil
import tempfile
from pathlib import Path

from django.core.exceptions import SuspiciousFileOperation, ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from PIL import Image
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from accounts.authz import (
    VISIBILITY_HELP,
    Role,
    Visibility,
    can_widen,
    visibility_targets,
)
from accounts.models import AuditLog, School, User
from parc.models import Device, Room
from tickets import attachments
from tickets.forms import TicketForm
from tickets.models import Attachment, Comment, Tag, Ticket, TicketAssignee, TicketTag


class VisibilityTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.school = School.objects.create(slug="lycee", name="Lycee")
        cls.other_school = School.objects.create(slug="other", name="Other")
        cls.room = Room.objects.create(school=cls.school, name="204")

        def person(cn, role, school=None):
            return User.objects.enroll(school=school or cls.school, cn=cn, role=role)

        cls.admin = person("admin", Role.ADMIN)
        cls.member = person("pupil", Role.MEMBER)
        cls.reporter = person("teacher", Role.REPORTER)
        cls.other_member = person("pupil2", Role.MEMBER)
        cls.outsider = person("elsewhere", Role.ADMIN, school=cls.other_school)

        def ticket(visibility, author=None):
            return Ticket.objects.create(
                school=cls.school,
                room=cls.room,
                room_label="204",
                title=f"t{visibility}",
                visibility=visibility,
                created_by=author or cls.admin,
            )

        cls.t_admins = ticket(Visibility.ADMINS)
        cls.t_team = ticket(Visibility.TEAM)
        cls.t_all = ticket(Visibility.ALL)

    def visible(self, user):
        return set(Ticket.objects.visible_to(user).values_list("title", flat=True))

    def test_reporter_only_sees_everyone_enrolled_level(self):
        self.assertEqual(self.visible(self.reporter), {"t30"})

    def test_member_sees_team_and_above(self):
        self.assertEqual(self.visible(self.member), {"t20", "t30"})

    def test_admin_sees_everything(self):
        self.assertEqual(self.visible(self.admin), {"t10", "t20", "t30"})

    def test_author_keeps_their_own_ticket(self):
        """Without this clause a cautious default would blind a reporting teacher."""
        mine = Ticket.objects.create(
            school=self.school,
            room=self.room,
            room_label="204",
            title="mine",
            visibility=Visibility.ADMINS,
            created_by=self.reporter,
        )
        self.assertIn(mine.title, self.visible(self.reporter))

    def test_assigning_grants_access(self):
        TicketAssignee.objects.create(
            ticket=self.t_admins, user=self.member, assigned_by=self.admin
        )
        self.assertIn("t10", self.visible(self.member))

    def test_no_duplicate_rows_with_several_assignees(self):
        for user in (self.member, self.other_member):
            TicketAssignee.objects.create(ticket=self.t_all, user=user, assigned_by=self.admin)
        titles = list(Ticket.objects.visible_to(self.member).values_list("title", flat=True))
        self.assertEqual(len(titles), len(set(titles)))

    def test_anonymous_and_deactivated_see_nothing(self):
        from django.contrib.auth.models import AnonymousUser

        self.assertEqual(self.visible(AnonymousUser()), set())
        self.member.is_active = False
        self.assertEqual(self.visible(self.member), set())

    def test_schools_are_partitioned(self):
        """A global admin of another school sees nothing here (D-06)."""
        self.assertEqual(self.visible(self.outsider), set())

    def test_anonymising_cuts_access(self):
        self.member.anonymize()
        self.assertEqual(self.visible(self.member), set())


class ViewTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.school = School.objects.create(slug="lycee", name="Lycee")
        cls.room = Room.objects.create(school=cls.school, name="204")
        cls.reporter = User.objects.enroll(school=cls.school, cn="teacher", role=Role.REPORTER)
        cls.reporter.oidc_sub = "sub-teacher"
        cls.reporter.save()
        cls.admin = User.objects.enroll(school=cls.school, cn="admin", role=Role.ADMIN)
        cls.hidden = Ticket.objects.create(
            school=cls.school,
            room=cls.room,
            room_label="204",
            title="classified",
            visibility=Visibility.ADMINS,
            created_by=cls.admin,
        )

    def setUp(self):
        self.client.force_login(self.reporter)

    def test_404_and_never_403(self):
        """A 403 on a sequential identifier would reveal existence."""
        response = self.client.get(reverse("tickets:detail", args=[self.hidden.pk]))
        self.assertEqual(response.status_code, 404)

    def test_attachment_revalidates_its_parent_ticket(self):
        item = Attachment.objects.create(
            ticket=self.hidden,
            uploaded_by=self.admin,
            filename="photo.jpg",
            mime="image/jpeg",
            size_bytes=1,
            storage_path="photo.jpg",
        )
        response = self.client.get(reverse("tickets:attachment", args=[item.pk]))
        self.assertEqual(response.status_code, 404)

    def test_list_does_not_show_the_invisible(self):
        response = self.client.get(reverse("tickets:list"))
        self.assertNotContains(response, "classified")


class AsymmetryTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.school = School.objects.create(slug="lycee", name="Lycee")
        cls.member = User.objects.enroll(school=cls.school, cn="pupil", role=Role.MEMBER)
        cls.admin = User.objects.enroll(school=cls.school, cn="admin", role=Role.ADMIN)

    def test_widening_is_reserved_to_admins(self):
        self.assertFalse(can_widen(self.member))
        self.assertTrue(can_widen(self.admin))

    def test_only_the_author_and_the_admins_may_move_a_level(self):
        """D-41 narrowed this: reading a ticket is not being responsible for it.

        Any reader used to be able to restrict any ticket, which made a
        stranger's report vanish from the team's list with nothing said.
        """
        author = User.objects.enroll(school=self.school, cn="teacher", role=Role.REPORTER)
        room = Room.objects.create(school=self.school, name="204")
        ticket = Ticket.objects.create(
            school=self.school, room=room, room_label="204", title="Damage",
            visibility=Visibility.TEAM, created_by=author,
        )
        # The author restricts, and down to admins-only: the author clause of
        # visible_to() keeps it in front of them whatever floor they pick.
        self.assertEqual(
            [value for value, _ in visibility_targets(author, ticket)],
            [Visibility.ADMINS, Visibility.TEAM],
        )
        # An admin moves it either way.
        self.assertEqual(len(visibility_targets(self.admin, ticket)), 3)
        # Anybody else, on somebody else's ticket: nothing to offer.
        self.assertEqual(visibility_targets(self.member, ticket), [])


class EnrolmentTests(TestCase):
    def setUp(self):
        self.school = School.objects.create(slug="lycee", name="Lycee")

    def test_enrolled_without_sub_then_bound(self):
        user = User.objects.enroll(school=self.school, cn="arnaud", role=Role.ADMIN)
        self.assertIsNone(user.oidc_sub)
        user.bind_oidc_sub("sub-123")
        self.assertEqual(User.objects.get(pk=user.pk).oidc_sub, "sub-123")

    def test_a_bound_sub_never_reattaches_to_somebody_else(self):
        """A reassigned cn must not inherit a former member's history."""
        user = User.objects.enroll(school=self.school, cn="arnaud")
        user.bind_oidc_sub("sub-123")
        with self.assertRaises(ValueError):
            user.bind_oidc_sub("sub-456")

    def test_anonymising_unenrols(self):
        user = User.objects.enroll(school=self.school, cn="pupil")
        user.bind_oidc_sub("sub-789")
        user.anonymize()
        user.refresh_from_db()
        self.assertIsNone(user.oidc_sub)
        self.assertFalse(user.is_active)
        self.assertIsNone(user.cn)
        self.assertIsNotNone(user.anonymized_at)


class ScreenTests(TestCase):
    """The templates must actually render, with content in them.

    The visibility tests above only reach ``detail.html`` through its 404 path,
    so a broken tag in that template would sail through the whole suite. These
    two render the real thing, with a comment, an attachment and a device
    attached -- the branches a bare ticket never exercises.
    """

    @classmethod
    def setUpTestData(cls):
        cls.school = School.objects.create(slug="lycee", name="Lycee")
        cls.room = Room.objects.create(school=cls.school, name="204")
        cls.device = Device.objects.create(
            school=cls.school, room=cls.room, mac="48:5B:39:0B:2E:C2",
            hostname="dienst05", pxe=1,
        )
        cls.user = User.objects.enroll(
            school=cls.school, cn="pupil", role=Role.MEMBER, display_name="Lea"
        )
        cls.ticket = Ticket.objects.create(
            school=cls.school, room=cls.room, device=cls.device, room_label="204",
            title="Black screen", description="Nothing on boot.",
            status=Ticket.Status.OPEN, priority=Ticket.Priority.HIGH,
            visibility=Visibility.TEAM, created_by=cls.user,
        )
        Comment.objects.create(ticket=cls.ticket, author=cls.user, body="Cable swapped.")
        Attachment.objects.create(
            ticket=cls.ticket, uploaded_by=cls.user, filename="photo.jpg",
            mime="image/jpeg", size_bytes=1, storage_path="photo.jpg",
        )

    def setUp(self):
        self.client.force_login(self.user)

    def test_list_renders(self):
        response = self.client.get(reverse("tickets:list"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Black screen")
        self.assertContains(response, "204")

    def test_detail_renders_its_thread_and_photos(self):
        response = self.client.get(reverse("tickets:detail", args=[self.ticket.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Cable swapped.")
        self.assertContains(response, "dienst05")
        # Photos go through the view, never through MEDIA_URL (D-21).
        self.assertContains(response, reverse("tickets:attachment", args=[
            self.ticket.attachments.get().pk
        ]))

    def test_the_state_decides_the_colour_in_one_place(self):
        """Two templates draw that chip, so the mapping lives in the model and
        they cannot drift apart (D-36)."""
        for status, expected in (
            (Ticket.Status.OPEN, "badge-warning"),
            (Ticket.Status.IN_PROGRESS, "badge-info"),
            (Ticket.Status.RESOLVED, "badge-success"),
            (Ticket.Status.CANCELLED, "badge-ghost"),
        ):
            with self.subTest(status=status):
                self.ticket.status = status
                self.assertEqual(self.ticket.status_class, expected)

    def test_status_priority_and_tags_are_told_apart_on_sight(self):
        """D-36. The three shared one row of chips and colour did not separate
        them: a tag the school painted `error` was drawn exactly like "High",
        and one painted `warning` like "Open". The state keeps the chip, the
        priority is named in words, the tag carries a `#`."""
        TicketTag.objects.create(
            ticket=self.ticket,
            tag=Tag.objects.create(
                school=self.school, slug="hdmi", name="HDMI", color=Tag.Color.ERROR
            ),
        )
        page = self.client.get(
            reverse("tickets:detail", args=[self.ticket.pk])
        ).content.decode()
        self.assertIn("#</span>HDMI", page)
        self.assertIn("High priority", page)
        # The chip that is left says the state and nothing else. The priority
        # no longer has one, which is what used to read as a fourth tag.
        self.assertIn("badge badge-warning", page)
        self.assertNotIn("badge badge-error", page)

    def test_both_screens_mark_every_priority_including_normal(self):
        """The flag stands left of the room and the title, on the list and on
        the ticket alike (D-39, extended to the ticket the same day).

        All four levels are drawn, "normal" included. What made the default
        worth hiding was a chip that came and went; a mark always in the same
        place is read as a scale instead, and that is what took the priority out
        of the row it shared with the tags.

        The flag is a colour, so the level has to reach a screen reader some
        other way: the accessible name is what this asserts, never the class.
        """
        screens = (reverse("tickets:list"), reverse("tickets:detail", args=[self.ticket.pk]))
        for level, name in (
            (Ticket.Priority.NORMAL, "Normal priority"),
            (Ticket.Priority.LOW, "Low priority"),
            (Ticket.Priority.HIGH, "High priority"),
            (Ticket.Priority.URGENT, "Urgent"),
        ):
            self.ticket.priority = level
            self.ticket.save(update_fields=["priority"])
            for url in screens:
                with self.subTest(priority=level, url=url):
                    page = self.client.get(url).content.decode()
                    self.assertIn(f'title="{name}"', page)

    def test_the_reader_is_told_who_else_can_see_this(self):
        """Visibility is never implicit on screen (doc 08)."""
        response = self.client.get(reverse("tickets:detail", args=[self.ticket.pk]))
        self.assertContains(response, "Visibility")
        self.assertContains(response, str(Visibility.TEAM.label))


class JpegMetadataTests(TestCase):
    """The stripping of EXIF is worth testing on bytes we built ourselves.

    A photo taken in a classroom carries the coordinates of that classroom, and
    a marker walk that gets one length wrong produces a file no browser opens.
    """

    @staticmethod
    def exif_block(*, orientation=None, gps=True) -> bytes:
        """A TIFF block with the two tags that matter to us, and nothing else.

        0x0112 is the orientation, the one tag worth carrying over. 0x8825 is
        the GPS IFD pointer -- the reason any of this code exists. The pointer
        is made to land on a real (empty) IFD appended after the block, so that
        the fixture is a file Pillow reads without complaint rather than one it
        merely tolerates.
        """
        count = (orientation is not None) + bool(gps)
        # header 8 + entry count 2 + entries + "no next IFD" 4
        gps_offset = 14 + 12 * count
        entries = b""
        if orientation is not None:
            entries += (
                b"\x01\x12\x00\x03\x00\x00\x00\x01"
                + orientation.to_bytes(2, "big") + b"\x00\x00"
            )
        if gps:
            entries += (
                b"\x88\x25\x00\x04\x00\x00\x00\x01" + gps_offset.to_bytes(4, "big")
            )
        block = (
            b"MM\x00\x2a\x00\x00\x00\x08"
            + count.to_bytes(2, "big") + entries + b"\x00\x00\x00\x00"
        )
        return block + (b"\x00\x00" + b"\x00\x00\x00\x00" if gps else b"")

    @staticmethod
    def photo(*, orientation=None, gps=True, size=(64, 48)) -> bytes:
        """A real JPEG, the kind that reaches ``scrub``.

        The hand-built one below is enough to exercise the marker walk, but it
        holds no decodable image: Pillow refuses it, and rightly so.
        """
        image = Image.new("RGB", size)
        image.putpixel((0, 0), (255, 0, 0))
        buffer = io.BytesIO()
        # Pillow wants the whole APP1 payload, prefix included: hand it the
        # bare TIFF block and it writes no EXIF at all, in silence.
        image.save(
            buffer, "JPEG",
            exif=b"Exif\x00\x00" + JpegMetadataTests.exif_block(
                orientation=orientation, gps=gps
            ),
        )
        return buffer.getvalue()

    @staticmethod
    def jpeg(*, orientation=None, gps=True):
        """A minimal but structurally valid JPEG: APP0, an APP1 EXIF, a scan."""
        parts = [b"\xff\xd8"]
        jfif = b"JFIF\x00\x01\x02\x00\x00\x01\x00\x01\x00\x00"
        parts.append(b"\xff\xe0" + (len(jfif) + 2).to_bytes(2, "big") + jfif)

        exif = b"Exif\x00\x00" + JpegMetadataTests.exif_block(
            orientation=orientation, gps=gps
        )
        parts.append(b"\xff\xe1" + (len(exif) + 2).to_bytes(2, "big") + exif)
        parts.append(b"\xff\xda\x00\x08\x01\x01\x00\x00\x3f\x00" + b"payload" + b"\xff\xd9")
        return b"".join(parts)

    def test_gps_is_gone(self):
        out = attachments.strip_jpeg_metadata(self.jpeg())
        self.assertNotIn(b"Exif\x00\x00", out)
        self.assertNotIn(b"\x88\x25", out)

    def test_the_picture_itself_survives(self):
        out = attachments.strip_jpeg_metadata(self.jpeg())
        self.assertTrue(out.startswith(b"\xff\xd8"))
        self.assertIn(b"JFIF", out)          # APP0 is about pixels, it stays
        self.assertIn(b"payload", out)       # so does the scan
        self.assertTrue(out.endswith(b"\xff\xd9"))

    def test_orientation_is_carried_over(self):
        """Stripping EXIF wholesale would lay every portrait photo on its side."""
        out = attachments.strip_jpeg_metadata(self.jpeg(orientation=6))
        self.assertEqual(attachments.orientation_of_jpeg(out), 6)
        self.assertNotIn(b"\x88\x25", out)   # ...without carrying the GPS along

    def test_an_upright_photo_gains_no_exif_at_all(self):
        out = attachments.strip_jpeg_metadata(self.jpeg(orientation=1))
        self.assertNotIn(b"Exif", out)

    def test_a_broken_file_is_returned_untouched(self):
        """Refusing it would leave a pupil unable to file what they photographed."""
        broken = b"\xff\xd8\xff\xe1\xff\xff nonsense"
        self.assertEqual(attachments.strip_jpeg_metadata(broken), broken)

    def test_a_real_photo_loses_its_gps_and_keeps_its_size(self):
        out, mime, width, height = attachments.scrub(self.photo(), "image/jpeg")
        self.assertEqual(mime, "image/jpeg")
        self.assertEqual((width, height), (64, 48))
        self.assertNotIn(b"\x88\x25", out)      # the GPS pointer
        self.assertLess(len(out), len(self.photo()))

    def test_an_oversized_photo_is_brought_down(self):
        """Doc 06 names photos as the one volume that grows without bound."""
        big = self.photo(size=(attachments.MAX_DIMENSION + 800, 1000))
        out, _mime, width, height = attachments.scrub(big, "image/jpeg")
        self.assertEqual(max(width, height), attachments.MAX_DIMENSION)
        self.assertNotIn(b"Exif", out)
        self.assertLess(len(out), len(big))

    def test_a_portrait_photo_stays_upright(self):
        """Stripping EXIF removes the tag browsers rotate by. Whichever path
        runs, the picture must still come out the way it was taken."""
        upright, _m, width, height = attachments.scrub(
            self.photo(orientation=6, size=(64, 48)), "image/jpeg"
        )
        # Not resized: the tag is kept rather than the pixels rewritten, which
        # costs no re-compression -- but the recorded size is what is shown.
        self.assertEqual((width, height), (48, 64))
        self.assertEqual(attachments.orientation_of_jpeg(upright), 6)

        resized, _m, width, height = attachments.scrub(
            self.photo(orientation=6, size=(3000, 2000)), "image/jpeg"
        )
        # Resized: the rotation is baked into the pixels, so no tag is needed
        # and none is left.
        self.assertEqual(width, 2 * height // 3)
        self.assertEqual(attachments.orientation_of_jpeg(resized), 1)

    def test_a_file_we_cannot_decode_is_refused_not_stored(self):
        """The point of taking the Pillow dependency (R-18) was to be able to
        promise that what we keep has been scrubbed."""
        with self.assertRaises(ValidationError):
            attachments.scrub(self.jpeg(), "image/jpeg")

    def test_the_type_is_read_from_the_bytes(self):
        self.assertEqual(attachments.sniff(self.jpeg())[0], "image/jpeg")
        self.assertEqual(attachments.sniff(self.photo())[0], "image/jpeg")
        self.assertEqual(attachments.sniff(b"\x89PNG\r\n\x1a\n...")[0], "image/png")
        self.assertEqual(attachments.sniff(b"RIFF????WEBPVP8 ")[0], "image/webp")
        self.assertIsNone(attachments.sniff(b"#!/bin/sh\nrm -rf /"))
        # Nothing out of a camera is a GIF, and an animated format would have
        # to be flattened by the resize below for no benefit.
        self.assertIsNone(attachments.sniff(b"GIF89a" + b"\x00" * 16))


class WriteTests(TestCase):
    """Reading a ticket is not writing to it, and the tests say which is which."""

    @classmethod
    def setUpTestData(cls):
        cls.school = School.objects.create(slug="lycee", name="Lycee")
        cls.room = Room.objects.create(school=cls.school, name="204")
        cls.other_room = Room.objects.create(school=cls.school, name="205")
        cls.device = Device.objects.create(
            school=cls.school, room=cls.room, mac="48:5b:39:0b:2e:c2", hostname="r204-01"
        )
        cls.foreign_device = Device.objects.create(
            school=cls.school, room=cls.other_room, mac="48:5b:39:0b:2e:c3", hostname="r205-01"
        )

        def person(cn, role):
            return User.objects.enroll(school=cls.school, cn=cn, role=role, display_name=cn)

        cls.admin = person("admin", Role.ADMIN)
        cls.member = person("pupil", Role.MEMBER)
        cls.other_member = person("pupil2", Role.MEMBER)
        cls.reporter = person("teacher", Role.REPORTER)

    def setUp(self):
        self.media = tempfile.mkdtemp()
        override = override_settings(MEDIA_ROOT=self.media)
        override.enable()
        self.addCleanup(override.disable)
        self.addCleanup(shutil.rmtree, self.media, True)

    def open_ticket(self, **kwargs):
        fields = {
            "school": self.school, "room": self.room, "room_label": "204",
            "title": "Black screen", "visibility": Visibility.TEAM,
            "created_by": self.member,
        }
        return Ticket.objects.create(**{**fields, **kwargs})

    def post(self, name, ticket, data=None):
        return self.client.post(reverse(name, args=[ticket.pk]), data or {})

    # --- opening ------------------------------------------------------------

    def test_a_reporter_may_open_a_ticket(self):
        """D-07: anyone with access may report. The overflow is a teaching matter."""
        self.client.force_login(self.reporter)
        response = self.client.post(reverse("tickets:create"), {
            "room": self.room.pk, "title": "No sound", "description": "",
            "priority": "normal", "visibility": Visibility.TEAM,
        })
        ticket = Ticket.objects.get(title="No sound")
        self.assertRedirects(response, reverse("tickets:detail", args=[ticket.pk]))
        self.assertEqual(ticket.created_by, self.reporter)
        self.assertEqual(ticket.school, self.school)

    def test_the_room_label_is_frozen_at_creation(self):
        self.client.force_login(self.member)
        self.client.post(reverse("tickets:create"), {
            "room": self.room.pk, "title": "Frozen", "priority": "normal",
            "visibility": Visibility.TEAM,
        })
        self.room.name = "A204"
        self.room.save()
        self.assertEqual(Ticket.objects.get(title="Frozen").room_label, "204")

    def test_a_machine_from_another_room_is_refused(self):
        self.client.force_login(self.member)
        response = self.client.post(reverse("tickets:create"), {
            "room": self.room.pk, "device": self.foreign_device.pk, "title": "Mismatch",
            "priority": "normal", "visibility": Visibility.TEAM,
        })
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Ticket.objects.filter(title="Mismatch").exists())

    def test_a_reporter_may_open_an_admins_only_ticket(self):
        """The author clause keeps it visible to them, so restricting is safe."""
        self.client.force_login(self.reporter)
        self.client.post(reverse("tickets:create"), {
            "room": self.room.pk, "title": "Names a pupil", "priority": "normal",
            "visibility": Visibility.ADMINS,
        })
        ticket = Ticket.objects.get(title="Names a pupil")
        self.assertEqual(ticket.visibility, Visibility.ADMINS)
        self.assertIn(ticket, Ticket.objects.visible_to(self.reporter))
        self.assertNotIn(ticket, Ticket.objects.visible_to(self.member))

    def test_a_photo_is_stored_scrubbed_and_never_under_its_own_name(self):
        self.client.force_login(self.member)
        photo = SimpleUploadedFile(
            "../../evil.jpg", JpegMetadataTests.photo(), content_type="image/jpeg"
        )
        self.client.post(reverse("tickets:create"), {
            "room": self.room.pk, "title": "With a photo", "priority": "normal",
            "visibility": Visibility.TEAM, "photos": photo,
        })
        item = Ticket.objects.get(title="With a photo").attachments.get()
        self.assertEqual(item.mime, "image/jpeg")
        self.assertNotIn("..", item.storage_path)
        self.assertTrue((Path(self.media) / item.storage_path).is_file())
        self.assertNotIn(b"Exif", (Path(self.media) / item.storage_path).read_bytes())

    def test_a_file_that_is_not_an_image_is_refused(self):
        self.client.force_login(self.member)
        response = self.client.post(reverse("tickets:create"), {
            "room": self.room.pk, "title": "Not a photo", "priority": "normal",
            "visibility": Visibility.TEAM,
            "photos": SimpleUploadedFile("x.jpg", b"#!/bin/sh\n", content_type="image/jpeg"),
        })
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Ticket.objects.filter(title="Not a photo").exists())

    # --- commenting ---------------------------------------------------------

    def test_an_unreadable_ticket_takes_no_comment(self):
        hidden = self.open_ticket(visibility=Visibility.ADMINS, created_by=self.admin)
        self.client.force_login(self.reporter)
        self.assertEqual(self.post("tickets:comment", hidden, {"body": "hello"}).status_code, 404)
        self.assertEqual(hidden.comments.count(), 0)

    def test_an_empty_note_is_refused(self):
        ticket = self.open_ticket()
        self.client.force_login(self.member)
        self.post("tickets:comment", ticket, {"body": "   "})
        self.assertEqual(ticket.comments.count(), 0)

    def test_a_note_carries_its_photos(self):
        ticket = self.open_ticket()
        self.client.force_login(self.member)
        self.post("tickets:comment", ticket, {
            "body": "Swapped the cable.",
            "photos": SimpleUploadedFile(
                "p.jpg", JpegMetadataTests.photo(), content_type="image/jpeg"
            ),
        })
        comment = ticket.comments.get()
        self.assertEqual(comment.attachments.count(), 1)
        self.assertEqual(ticket.attachments.count(), 1)

    # --- correcting ---------------------------------------------------------

    def test_the_author_may_correct_the_room_they_mistyped(self):
        """D-37. Rooms are mistyped in a hurry, and until now nothing short of
        the Django admin could move a ticket. The label moves with it: it was
        frozen against a *rename* (doc 03), which is not this case -- here it
        was simply wrong."""
        ticket = self.open_ticket(created_by=self.reporter)
        self.client.force_login(self.reporter)
        self.post("tickets:correct", ticket, {
            "room": self.other_room.pk, "priority": Ticket.Priority.NORMAL,
        })
        ticket.refresh_from_db()
        self.assertEqual(ticket.room, self.other_room)
        self.assertEqual(ticket.room_label, "205")

    def test_the_priority_can_be_raised_after_the_fact(self):
        """The one the corridor could not know: a fault becomes urgent when the
        room is needed tomorrow, and that is learnt after the report."""
        ticket = self.open_ticket()
        self.client.force_login(self.member)
        self.post("tickets:correct", ticket, {
            "room": self.room.pk, "priority": Ticket.Priority.URGENT,
        })
        ticket.refresh_from_db()
        self.assertEqual(ticket.priority, Ticket.Priority.URGENT)

    def test_the_description_can_be_rewritten_and_says_that_it_was(self):
        """D-42 reversed D-37 on this one: a description dictated in a corridor
        is often wrong, and leaving the wrong words at the top of the page for
        good was the worse half of the trade. The cost is recorded rather than
        prevented -- the thread may be answering a sentence that has changed."""
        ticket = self.open_ticket(description="Screen off.")
        self.client.force_login(self.member)
        self.post("tickets:correct", ticket, {
            "room": self.room.pk, "priority": Ticket.Priority.NORMAL,
            "description": "Screen off, and the cable is missing.",
        })
        ticket.refresh_from_db()
        self.assertEqual(ticket.description, "Screen off, and the cable is missing.")
        self.assertIsNotNone(ticket.description_edited_at)

    def test_a_correction_that_leaves_the_words_alone_leaves_no_mark(self):
        """Moving a ticket to the right room is not an edit of what was written."""
        ticket = self.open_ticket(description="Screen off.")
        self.client.force_login(self.member)
        self.post("tickets:correct", ticket, {
            "room": self.other_room.pk, "priority": Ticket.Priority.NORMAL,
            "description": "Screen off.",
        })
        ticket.refresh_from_db()
        self.assertEqual(ticket.room, self.other_room)
        self.assertIsNone(ticket.description_edited_at)

    def test_a_reader_who_neither_repairs_nor_reported_may_not_correct(self):
        ticket = self.open_ticket(visibility=Visibility.ALL)
        self.client.force_login(self.reporter)
        response = self.post("tickets:correct", ticket, {
            "room": self.other_room.pk, "priority": Ticket.Priority.URGENT,
        })
        self.assertEqual(response.status_code, 404)
        ticket.refresh_from_db()
        self.assertEqual(ticket.room, self.room)

    def test_a_machine_from_another_room_is_refused(self):
        """The HTMX repaint is a convenience; the queryset is the rule."""
        ticket = self.open_ticket()
        self.client.force_login(self.member)
        self.post("tickets:correct", ticket, {
            "room": self.room.pk, "device": self.foreign_device.pk,
            "priority": Ticket.Priority.NORMAL,
        })
        ticket.refresh_from_db()
        self.assertIsNone(ticket.device)

    def test_a_room_from_another_school_is_refused(self):
        elsewhere = School.objects.create(slug="ailleurs", name="Ailleurs")
        foreign = Room.objects.create(school=elsewhere, name="Z999")
        ticket = self.open_ticket()
        self.client.force_login(self.member)
        self.post("tickets:correct", ticket, {
            "room": foreign.pk, "priority": Ticket.Priority.NORMAL,
        })
        ticket.refresh_from_db()
        self.assertEqual(ticket.room, self.room)

    # --- status -------------------------------------------------------------

    def test_a_pupil_closes_their_own_repair(self):
        """The pedagogical point of the project, in one assertion (doc 00)."""
        ticket = self.open_ticket()
        self.client.force_login(self.member)
        self.post("tickets:status", ticket, {"status": "resolved"})
        ticket.refresh_from_db()
        self.assertEqual(ticket.status, Ticket.Status.RESOLVED)
        self.assertEqual(ticket.resolved_by, self.member)
        self.assertIsNotNone(ticket.resolved_at)

    def test_a_reporter_may_not_resolve_somebody_elses_repair(self):
        ticket = self.open_ticket(visibility=Visibility.ALL)
        self.client.force_login(self.reporter)
        self.assertEqual(
            self.post("tickets:status", ticket, {"status": "resolved"}).status_code, 404
        )

    def test_the_person_who_reported_it_may_say_it_is_not_fixed(self):
        ticket = self.open_ticket(
            created_by=self.reporter, visibility=Visibility.ALL,
            status=Ticket.Status.RESOLVED, resolved_by=self.member,
        )
        self.client.force_login(self.reporter)
        self.post("tickets:status", ticket, {"status": "open"})
        ticket.refresh_from_db()
        self.assertEqual(ticket.status, Ticket.Status.OPEN)
        self.assertEqual(ticket.reopened_count, 1)
        # Cleared on purpose: the count is about the ticket, never about who
        # closed it too early (D-10).
        self.assertIsNone(ticket.resolved_by)

    def test_an_unknown_status_goes_nowhere(self):
        ticket = self.open_ticket()
        self.client.force_login(self.member)
        self.assertEqual(
            self.post("tickets:status", ticket, {"status": "archived"}).status_code, 404
        )

    # --- assignment ---------------------------------------------------------

    def test_claiming_is_a_toggle(self):
        ticket = self.open_ticket()
        self.client.force_login(self.member)
        self.post("tickets:claim", ticket)
        self.assertIn(self.member, ticket.assignees.all())
        self.post("tickets:claim", ticket)
        self.assertNotIn(self.member, ticket.assignees.all())

    def test_a_reporter_cannot_claim(self):
        ticket = self.open_ticket(visibility=Visibility.ALL)
        self.client.force_login(self.reporter)
        self.assertEqual(self.post("tickets:claim", ticket).status_code, 404)

    def test_only_an_admin_puts_somebody_else_on_a_ticket(self):
        """Assigning grants read access; letting a member do it would be a way
        of widening a ticket without being an admin."""
        ticket = self.open_ticket()
        self.client.force_login(self.member)
        response = self.post("tickets:assignees", ticket, {"users": [self.other_member.pk]})
        self.assertEqual(response.status_code, 404)

        self.client.force_login(self.admin)
        self.post("tickets:assignees", ticket, {"users": [self.other_member.pk]})
        self.assertEqual(list(ticket.assignees.all()), [self.other_member])

    def test_a_reporter_is_never_put_on_a_ticket(self):
        ticket = self.open_ticket()
        self.client.force_login(self.admin)
        self.post("tickets:assignees", ticket, {"users": [self.reporter.pk]})
        self.assertEqual(ticket.assignees.count(), 0)

    # --- visibility ---------------------------------------------------------

    def test_a_member_may_restrict_but_not_widen(self):
        ticket = self.open_ticket(visibility=Visibility.TEAM)
        self.client.force_login(self.member)
        self.assertEqual(
            self.post("tickets:visibility", ticket, {"visibility": Visibility.ALL}).status_code,
            404,
        )
        ticket.refresh_from_db()
        self.assertEqual(ticket.visibility, Visibility.TEAM)

    def test_a_member_may_not_restrict_below_their_own_clearance(self):
        """Or they would hide from themselves a ticket that is not theirs."""
        ticket = self.open_ticket(created_by=self.admin)
        self.client.force_login(self.member)
        self.assertEqual(
            self.post("tickets:visibility", ticket, {"visibility": Visibility.ADMINS}).status_code,
            404,
        )

    def test_an_admin_widens_and_the_move_is_recorded(self):
        ticket = self.open_ticket(visibility=Visibility.TEAM)
        self.client.force_login(self.admin)
        self.post("tickets:visibility", ticket, {"visibility": Visibility.ALL})
        ticket.refresh_from_db()
        self.assertEqual(ticket.visibility, Visibility.ALL)
        entry = AuditLog.objects.get(action=AuditLog.Action.VISIBILITY_CHANGE)
        self.assertEqual(entry.actor, self.admin)
        self.assertEqual(entry.payload, {"from": Visibility.TEAM, "to": Visibility.ALL})

    # --- removing a photo ---------------------------------------------------

    def test_whoever_took_the_photo_may_remove_it(self):
        """Doc 06 asks for this to be easy: a face may be in the background."""
        ticket = self.open_ticket()
        self.client.force_login(self.member)
        self.post("tickets:comment", ticket, {
            "body": "here", "photos": SimpleUploadedFile(
                "p.jpg", JpegMetadataTests.photo(), content_type="image/jpeg"
            ),
        })
        item = ticket.attachments.get()
        path = Path(self.media) / item.storage_path

        self.client.force_login(self.other_member)
        self.assertEqual(self.post("tickets:attachment_delete", item).status_code, 404)

        self.client.force_login(self.member)
        self.post("tickets:attachment_delete", item)
        self.assertEqual(ticket.attachments.count(), 0)
        self.assertFalse(path.exists())

    # --- the device list ----------------------------------------------------

    def test_the_machine_list_follows_the_room(self):
        self.client.force_login(self.member)
        response = self.client.get(reverse("tickets:room_devices"), {"room": self.room.pk})
        self.assertContains(response, "r204-01")
        self.assertNotContains(response, "r205-01")

    # --- what the screens actually put in front of a pupil -------------------

    def test_the_composer_names_every_audience_and_says_what_it_means(self):
        """Doc 08 asks that visibility never be implicit -- least of all at the
        moment somebody is deciding it."""
        self.client.force_login(self.member)
        response = self.client.get(reverse("tickets:create"))
        self.assertEqual(response.status_code, 200)
        for value, label in Visibility.choices:
            self.assertContains(response, str(label))
        self.assertContains(response, str(VISIBILITY_HELP[Visibility.ALL]))
        # enctype is what makes a photo arrive rather than its file name.
        self.assertContains(response, "multipart/form-data")

    def test_the_detail_screen_offers_the_actions_the_reader_may_take(self):
        # At "everyone", a member has somewhere to move it to: down to the team.
        # On a team ticket they have neither -- and the control is then absent
        # rather than drawn as a form that can only re-submit the status quo.
        ticket = self.open_ticket(visibility=Visibility.ALL)
        self.client.force_login(self.member)
        response = self.client.get(reverse("tickets:detail", args=[ticket.pk]))
        self.assertContains(response, reverse("tickets:comment", args=[ticket.pk]))
        self.assertContains(response, reverse("tickets:claim", args=[ticket.pk]))
        # A member may restrict, so the control is there...
        self.assertContains(response, reverse("tickets:visibility", args=[ticket.pk]))
        # ...but never the picker that grants access to somebody else.
        self.assertNotContains(response, reverse("tickets:assignees", args=[ticket.pk]))

    def test_a_reader_is_told_the_level_even_when_they_may_not_move_it(self):
        """The half of doc 08 that D-41 did **not** narrow.

        Whoever writes in a thread has to know who will read them, so the value
        is drawn for everybody. What narrowed is the control: on somebody
        else's ticket, a member is now offered nothing -- until D-41 any reader
        could restrict any ticket, and a stranger's report would vanish from
        the team's list with nothing said.
        """
        ticket = self.open_ticket(created_by=self.admin, visibility=Visibility.ALL)
        self.client.force_login(self.other_member)
        response = self.client.get(reverse("tickets:detail", args=[ticket.pk]))
        self.assertContains(response, "Visibility")
        self.assertContains(response, str(Visibility.ALL.label))
        self.assertNotContains(response, reverse("tickets:visibility", args=[ticket.pk]))

    def test_the_status_menu_offers_only_the_moves_that_are_legal(self):
        """D-38: the chip is the menu, and it lists moves, not states. From a
        resolved ticket the only move is back out of it -- offering "resolved"
        again would be a menu entry that does nothing."""
        ticket = self.open_ticket(status=Ticket.Status.RESOLVED)
        self.client.force_login(self.member)
        page = self.client.get(reverse("tickets:detail", args=[ticket.pk])).content.decode()
        self.assertIn('name="status" value="open"', page)
        self.assertNotIn('name="status" value="resolved"', page)
        self.assertNotIn('name="status" value="in_progress"', page)
        # The wording is the teaching, and it survives the move to a menu.
        self.assertIn("Reopen: not fixed", page)

    def test_a_reporter_sees_no_control_they_may_not_use(self):
        """A button that answers "you may not" is a button that should not be drawn."""
        ticket = self.open_ticket(visibility=Visibility.ALL, created_by=self.member)
        self.client.force_login(self.reporter)
        response = self.client.get(reverse("tickets:detail", args=[ticket.pk]))
        self.assertNotContains(response, reverse("tickets:claim", args=[ticket.pk]))
        self.assertNotContains(response, reverse("tickets:status", args=[ticket.pk]))
        # Their own report is another matter: they may still withdraw it.
        mine = self.open_ticket(created_by=self.reporter, visibility=Visibility.ALL)
        response = self.client.get(reverse("tickets:detail", args=[mine.pk]))
        self.assertContains(response, reverse("tickets:status", args=[mine.pk]))


class ListViewTests(TestCase):
    """The list is the screen people actually use: what it hides matters."""

    @classmethod
    def setUpTestData(cls):
        cls.school = School.objects.create(slug="lycee2", name="Lycee")
        cls.a101 = Room.objects.create(school=cls.school, name="A101", sort_key="1-101")
        cls.b204 = Room.objects.create(school=cls.school, name="B204", sort_key="2-204")
        cls.admin = User.objects.enroll(school=cls.school, cn="a", role=Role.ADMIN)
        cls.member = User.objects.enroll(school=cls.school, cn="m", role=Role.MEMBER)
        cls.reporter = User.objects.enroll(school=cls.school, cn="r", role=Role.REPORTER)
        cls.tag = Tag.objects.create(school=cls.school, slug="hdmi", name="HDMI")

        def ticket(title, *, status=Ticket.Status.OPEN, priority=Ticket.Priority.NORMAL,
                   room=None, visibility=Visibility.TEAM, tags=()):
            t = Ticket.objects.create(
                school=cls.school, room=room or cls.a101, title=title, status=status,
                priority=priority, visibility=visibility, created_by=cls.admin,
            )
            for tag in tags:
                TicketTag.objects.create(ticket=t, tag=tag)
            return t

        cls.open_normal = ticket("open normal")
        cls.open_high = ticket("open high", priority=Ticket.Priority.HIGH, tags=[cls.tag])
        cls.open_urgent = ticket("open urgent", priority=Ticket.Priority.URGENT)
        cls.working = ticket("in progress", status=Ticket.Status.IN_PROGRESS)
        cls.done = ticket("resolved", status=Ticket.Status.RESOLVED, room=cls.b204)
        cls.secret = ticket("admins only", visibility=Visibility.ADMINS)
        cls.assigned = ticket("assigned", room=cls.b204)
        TicketAssignee.objects.create(ticket=cls.assigned, user=cls.member)

    def titles(self, user, query=""):
        self.client.force_login(user)
        response = self.client.get(reverse("tickets:list") + query)
        self.assertEqual(response.status_code, 200)
        return [t.title for t in response.context["tickets"]]

    def test_default_view_is_open_only(self):
        titles = self.titles(self.member)
        self.assertIn("open normal", titles)
        self.assertNotIn("in progress", titles)
        self.assertNotIn("resolved", titles)

    def test_urgent_view_holds_both_levels_above_normal(self):
        """D-37: the tab answers "what do I do next", so it shows `urgent` and
        `high`. A level no view ever shows is a level nobody would set."""
        self.assertEqual(
            self.titles(self.member, "?view=urgent"), ["open urgent", "open high"]
        )

    def test_the_most_urgent_comes_first_in_every_view(self):
        """`priority` is a text column: sorted as it stands, "urgent" would come
        last of the four. PRIORITY_RANK is what stops that."""
        titles = self.titles(self.member)
        self.assertEqual(titles[:2], ["open urgent", "open high"])

    def test_mine_is_for_those_who_repair(self):
        self.assertEqual(self.titles(self.member, "?view=mine"), ["assigned"])

    def test_a_reporter_asking_for_mine_lands_on_the_default_view(self):
        self.client.force_login(self.reporter)
        response = self.client.get(reverse("tickets:list") + "?view=mine")
        self.assertEqual(response.context["view"], "open")

    def test_the_mine_tab_is_absent_for_a_reporter(self):
        self.client.force_login(self.reporter)
        self.assertNotContains(self.client.get(reverse("tickets:list")), "view=mine")
        self.client.force_login(self.member)
        self.assertContains(self.client.get(reverse("tickets:list")), "view=mine")

    def test_an_unknown_view_falls_back(self):
        self.assertIn("open normal", self.titles(self.member, "?view=nonsense"))

    def test_closed_are_reachable_but_never_by_default(self):
        self.assertNotIn("resolved", self.titles(self.member, "?room=%d" % self.b204.pk))
        self.assertIn(
            "resolved", self.titles(self.member, "?room=%d&closed=1" % self.b204.pk)
        )

    def test_room_and_tag_narrow_the_list(self):
        self.assertEqual(self.titles(self.member, "?tag=hdmi"), ["open high"])
        self.assertNotIn("open normal", self.titles(self.member, "?room=%d" % self.b204.pk))

    def test_no_filter_ever_widens_visibility(self):
        # The admins-only ticket sits in A101 and carries no tag: neither the
        # room filter nor "closed" may hand it to a member.
        for query in ("", "?room=%d" % self.a101.pk, "?closed=1", "?view=urgent"):
            self.assertNotIn("admins only", self.titles(self.member, query))
        self.assertIn("admins only", self.titles(self.admin))

    def test_a_bad_room_is_ignored_rather_than_crashing(self):
        self.assertIn("open normal", self.titles(self.member, "?room=nonsense"))

    def test_a_slug_matching_no_tag_is_ignored_rather_than_emptying_the_list(self):
        """A typo, or a link shared before the tag was renamed.

        It used to narrow the list to nothing while `selected_tag` stayed None,
        so neither the chip that removes the filter nor the "nothing under
        these filters" line was drawn: an empty screen with no visible cause,
        which is the one thing the chips exist to prevent.
        """
        self.assertIn("open normal", self.titles(self.member, "?tag=hdmy"))

        self.client.force_login(self.member)
        response = self.client.get(reverse("tickets:list") + "?tag=hdmy")
        self.assertIsNone(response.context["selected_tag"])

    def test_a_tag_of_another_school_narrows_nothing(self):
        elsewhere = School.objects.create(slug="ailleurs", name="Ailleurs")
        Tag.objects.create(school=elsewhere, slug="fremd", name="Fremd")
        self.assertIn("open normal", self.titles(self.member, "?tag=fremd"))

    def test_pagination_splits_the_archive(self):
        for i in range(25):
            Ticket.objects.create(
                school=self.school, room=self.a101, title=f"bulk {i}",
                visibility=Visibility.TEAM, created_by=self.admin,
            )
        # 25 bulk tickets plus the four open ones this class already has.
        self.assertEqual(len(self.titles(self.member)), 20)
        self.assertEqual(len(self.titles(self.member, "?page=2")), 9)


class SearchTests(TestCase):
    """The search exists for one question: how did we fix this last time?"""

    @classmethod
    def setUpTestData(cls):
        cls.school = School.objects.create(slug="lycee3", name="Lycee")
        cls.room = Room.objects.create(school=cls.school, name="C300")
        cls.admin = User.objects.enroll(school=cls.school, cn="a3", role=Role.ADMIN)
        cls.member = User.objects.enroll(school=cls.school, cn="m3", role=Role.MEMBER)

        def ticket(title, **kwargs):
            return Ticket.objects.create(
                school=cls.school, room=cls.room, title=title,
                created_by=cls.admin, **kwargs,
            )

        cls.solved = ticket(
            "Beamer", status=Ticket.Status.RESOLVED, visibility=Visibility.TEAM
        )
        Comment.objects.create(
            ticket=cls.solved, author=cls.admin,
            body="Port 23 am Patchfeld nachgezogen, danach Link-LED.",
        )
        cls.secret = ticket("Confidential", visibility=Visibility.ADMINS)
        Comment.objects.create(
            ticket=cls.secret, author=cls.admin, body="Auch am Patchfeld gesehen.",
        )
        cls.plain = ticket("Maus fehlt", visibility=Visibility.TEAM)

    def titles(self, user, query):
        self.client.force_login(user)
        response = self.client.get(reverse("tickets:list") + query)
        return [t.title for t in response.context["tickets"]]

    def test_a_word_from_a_note_finds_the_ticket(self):
        self.assertEqual(self.titles(self.member, "?q=patchfeld"), ["Beamer"])

    def test_a_search_reaches_resolved_tickets_without_asking(self):
        # The default view is "open"; the answer is in a resolved thread, and
        # the search would be useless if the status filter still applied.
        self.assertNotIn("Beamer", self.titles(self.member, ""))
        self.assertIn("Beamer", self.titles(self.member, "?q=beamer"))

    def test_a_note_never_surfaces_a_ticket_one_may_not_read(self):
        self.assertEqual(self.titles(self.member, "?q=patchfeld"), ["Beamer"])
        self.assertEqual(
            sorted(self.titles(self.admin, "?q=patchfeld")), ["Beamer", "Confidential"]
        )

    def test_the_title_is_searched_too(self):
        self.assertEqual(self.titles(self.member, "?q=maus"), ["Maus fehlt"])

    def test_the_matching_note_is_shown_on_the_card(self):
        self.client.force_login(self.member)
        response = self.client.get(reverse("tickets:list") + "?q=patchfeld")
        self.assertContains(response, "Port 23 am Patchfeld")

    def test_a_ticket_matching_by_title_carries_no_note(self):
        self.client.force_login(self.member)
        response = self.client.get(reverse("tickets:list") + "?q=maus")
        self.assertEqual(response.context["tickets"][0].matches, [])


class RetaggingTests(TestCase):
    """`t/<pk>/tags/`, the door that was missing.

    A tag is a conclusion -- `hdmi`, `linbo`, `drucker` -- and whoever opens a
    ticket has not diagnosed it yet: they see a black screen and tick
    `bildschirm` where the fault was a cable. Setting tags only on the creation
    form recorded a guess and gave the answer nowhere to go.
    """

    @classmethod
    def setUpTestData(cls):
        cls.school = School.objects.create(slug="lycee3", name="Lycee")
        cls.room = Room.objects.create(school=cls.school, name="A101", sort_key="1-101")
        cls.member = User.objects.enroll(school=cls.school, cn="m", role=Role.MEMBER)
        cls.reporter = User.objects.enroll(school=cls.school, cn="r", role=Role.REPORTER)
        cls.screen = Tag.objects.create(school=cls.school, slug="bildschirm", name="Bildschirm")
        cls.hdmi = Tag.objects.create(school=cls.school, slug="hdmi", name="HDMI")

    def setUp(self):
        # Opened by the reporter, tagged with what it looked like.
        self.ticket = Ticket.objects.create(
            school=self.school, room=self.room, title="Schwarzer Bildschirm",
            visibility=Visibility.ALL, created_by=self.reporter,
        )
        TicketTag.objects.create(ticket=self.ticket, tag=self.screen)

    def retag(self, user, pks):
        self.client.force_login(user)
        return self.client.post(
            reverse("tickets:tags", args=[self.ticket.pk]), {"tags": pks}
        )

    def slugs(self):
        return sorted(self.ticket.tags.values_list("slug", flat=True))

    def test_the_repairer_replaces_the_guess_with_the_diagnosis(self):
        self.retag(self.member, [self.hdmi.pk])
        self.assertEqual(self.slugs(), ["hdmi"])

    def test_a_ticket_that_was_never_tagged_can_be_filed_later(self):
        """The case D-31 multiplies: most tickets arrive with nothing ticked."""
        self.ticket.tags.clear()
        self.retag(self.member, [self.hdmi.pk, self.screen.pk])
        self.assertEqual(self.slugs(), ["bildschirm", "hdmi"])

    def test_sending_nothing_clears_them(self):
        self.retag(self.member, [])
        self.assertEqual(self.slugs(), [])

    def test_reporting_is_not_repairing_even_on_ones_own_ticket(self):
        """Same rule as `status` and `claim`: what is written is a diagnosis."""
        self.assertEqual(self.retag(self.reporter, [self.hdmi.pk]).status_code, 404)
        self.assertEqual(self.slugs(), ["bildschirm"])

    def test_a_tag_from_another_school_is_refused(self):
        elsewhere = School.objects.create(slug="ailleurs2", name="Ailleurs")
        foreign = Tag.objects.create(school=elsewhere, slug="fremd", name="Fremd")
        self.retag(self.member, [foreign.pk, self.hdmi.pk])
        self.assertEqual(self.slugs(), ["hdmi"])

    def test_a_ticket_one_may_not_read_is_a_404_not_a_403(self):
        self.ticket.visibility = Visibility.ADMINS
        self.ticket.save(update_fields=["visibility"])
        self.assertEqual(self.retag(self.member, [self.hdmi.pk]).status_code, 404)

    def test_the_route_answers_no_get(self):
        self.client.force_login(self.member)
        response = self.client.get(reverse("tickets:tags", args=[self.ticket.pk]))
        self.assertEqual(response.status_code, 405)

    def test_the_button_is_drawn_for_the_team_and_for_nobody_else(self):
        self.client.force_login(self.member)
        self.assertContains(
            self.client.get(reverse("tickets:detail", args=[self.ticket.pk])),
            "tags_modal",
        )
        self.client.force_login(self.reporter)
        self.assertNotContains(
            self.client.get(reverse("tickets:detail", args=[self.ticket.pk])),
            "tags_modal",
        )


class TagColourTests(TestCase):
    """`Tag.color` was filled by the demo seed and read by no template."""

    @classmethod
    def setUpTestData(cls):
        cls.school = School.objects.create(slug="lycee4", name="Lycee")

    def test_a_tinted_tag_is_soft_and_never_solid(self):
        tag = Tag.objects.create(
            school=self.school, slug="hdmi", name="HDMI", color=Tag.Color.WARNING
        )
        self.assertEqual(tag.badge_class, "badge-soft badge-warning")

    def test_no_colour_keeps_the_outline_rather_than_a_grey_tint(self):
        """"None chosen" has to stay distinguishable from `neutral`."""
        tag = Tag.objects.create(school=self.school, slug="plain", name="Plain")
        self.assertEqual(tag.badge_class, "badge-outline")
        neutral = Tag.objects.create(
            school=self.school, slug="grey", name="Grey", color=Tag.Color.NEUTRAL
        )
        self.assertEqual(neutral.badge_class, "badge-soft badge-neutral")

    def test_every_choice_names_a_class_the_stylesheet_defines(self):
        """The trap of D-18: a class composed in a template is never generated.

        daisyUI is imported as plain CSS rather than as a plugin, so the
        modifiers ship whether or not a template mentions them -- which is what
        makes `badge-{{ color }}` safe here. This test is what will notice the
        day that stops being true.
        """
        from django.conf import settings

        css = (settings.BASE_DIR / "static" / "app.css").read_text()
        for value, _label in Tag.Color.choices:
            self.assertIn(f".badge-{value}", css, f"badge-{value} missing from app.css")
        self.assertIn(".badge-soft", css)
        self.assertIn(".badge-outline", css)


class AttachmentPathTests(TestCase):
    """`storage_path` is a text column, and every reader joined it to a root.

    The visibility gate in front of the serving view was correct all along and
    answered the wrong question: it says who may read the row, not whether the
    row points at a photo.
    """

    @classmethod
    def setUpTestData(cls):
        cls.school = School.objects.create(slug="lycee", name="Lycee")
        cls.room = Room.objects.create(school=cls.school, name="204")
        cls.boss = User.objects.enroll(school=cls.school, cn="boss", role=Role.ADMIN)
        cls.ticket = Ticket.objects.create(
            school=cls.school, room=cls.room, title="Beamer",
            visibility=Visibility.TEAM, created_by=cls.boss,
        )

    def setUp(self):
        self.media = tempfile.mkdtemp()
        override = override_settings(MEDIA_ROOT=self.media)
        override.enable()
        self.addCleanup(override.disable)
        self.addCleanup(shutil.rmtree, self.media, ignore_errors=True)

        self.outside = Path(self.media).parent / "secret.txt"
        self.outside.write_text("not yours")
        self.addCleanup(self.outside.unlink, missing_ok=True)

        self.client.force_login(self.boss)

    def attachment(self, storage_path):
        return Attachment.objects.create(
            ticket=self.ticket, uploaded_by=self.boss, filename="innocent.jpg",
            mime="image/jpeg", size_bytes=1, storage_path=storage_path,
        )

    def test_an_absolute_path_is_refused_and_needs_no_dot_dot(self):
        """`Path(root) / "/etc/hostname"` is `/etc/hostname`: pathlib drops the root."""
        obj = self.attachment(str(self.outside))
        with self.assertRaises(SuspiciousFileOperation):
            attachments.resolved_path(obj)

    def test_climbing_out_with_dot_dot_is_refused(self):
        obj = self.attachment("../secret.txt")
        with self.assertRaises(SuspiciousFileOperation):
            attachments.resolved_path(obj)

    def test_a_real_photo_is_still_served(self):
        inside = Path(self.media) / "attachments" / "lycee" / "2026" / "08"
        inside.mkdir(parents=True)
        (inside / "abc.jpg").write_bytes(b"\xff\xd8\xff bytes")
        obj = self.attachment("attachments/lycee/2026/08/abc.jpg")
        response = self.client.get(reverse("tickets:attachment", args=[obj.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(b"".join(response.streaming_content), b"\xff\xd8\xff bytes")

    def test_nothing_outside_the_root_is_ever_unlinked(self):
        """The same join deleted, so the same field removed arbitrary files."""
        obj = self.attachment(str(self.outside))
        attachments.delete_file(obj)
        self.assertTrue(self.outside.exists())

    def test_the_admin_no_longer_offers_the_field_at_all(self):
        obj = self.attachment("attachments/x.jpg")
        self.assertEqual(
            self.client.get(reverse("admin:tickets_attachment_add")).status_code, 403
        )
        page = self.client.get(reverse("admin:tickets_attachment_change", args=[obj.pk]))
        self.assertFalse(page.context["adminform"].form.fields)

    def test_removing_a_photo_from_the_admin_takes_the_bytes_with_it(self):
        """Doc 06 asks that this be easy, because a photo may show a face."""
        inside = Path(self.media) / "attachments"
        inside.mkdir(parents=True)
        (inside / "face.jpg").write_bytes(b"jpeg")
        obj = self.attachment("attachments/face.jpg")
        self.client.post(
            reverse("admin:tickets_attachment_delete", args=[obj.pk]), {"post": "yes"}
        )
        self.assertFalse((inside / "face.jpg").exists())
        self.assertFalse(Attachment.objects.filter(pk=obj.pk).exists())


class CommentAdminTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.school = School.objects.create(slug="lycee", name="Lycee")
        cls.room = Room.objects.create(school=cls.school, name="204")
        cls.boss = User.objects.enroll(school=cls.school, cn="boss", role=Role.ADMIN)
        cls.ticket = Ticket.objects.create(
            school=cls.school, room=cls.room, title="Beamer",
            visibility=Visibility.TEAM, created_by=cls.boss,
        )
        cls.comment = Comment.objects.create(
            ticket=cls.ticket, author=cls.boss, body="j'ai vérifié après Lukas"
        )

    def test_rewriting_a_thread_leaves_a_mark(self):
        """It stays editable -- doc 06 has no other remedy for a name in the
        clear -- but the team's memory does not change under them silently."""
        self.client.force_login(self.boss)
        self.client.post(
            reverse("admin:tickets_comment_change", args=[self.comment.pk]),
            {"body": "vérifié une seconde fois"},
        )
        self.comment.refresh_from_db()
        self.assertEqual(self.comment.body, "vérifié une seconde fois")
        self.assertIsNotNone(self.comment.edited_at)


class ResolutionTests(TestCase):
    """The note that says what worked (D-29).

    A ticket closed without anybody writing down what fixed it is worth nothing
    to the next person, while the thread is the team's technical memory. The
    mark makes the sentence get written -- and makes it findable without
    re-reading twenty notes.
    """

    @classmethod
    def setUpTestData(cls):
        cls.school = School.objects.create(slug="lycee5", name="Lycee")
        cls.room = Room.objects.create(school=cls.school, name="A102", sort_key="1-102")
        cls.boss = User.objects.enroll(school=cls.school, cn="chef", role=Role.ADMIN)
        cls.member = User.objects.enroll(school=cls.school, cn="mm", role=Role.MEMBER)
        cls.reporter = User.objects.enroll(school=cls.school, cn="rr", role=Role.REPORTER)

    def setUp(self):
        self.ticket = Ticket.objects.create(
            school=self.school, room=self.room, title="Kein Bild",
            visibility=Visibility.ALL, created_by=self.reporter,
        )
        self.guess = Comment.objects.create(
            ticket=self.ticket, author=self.member, body="Monitor getauscht, nichts."
        )
        self.fix = Comment.objects.create(
            ticket=self.ticket, author=self.member, body="HDMI-Kabel war lose."
        )

    def mark(self, user, comment):
        self.client.force_login(user)
        data = {"comment": comment.pk} if comment is not None else {}
        return self.client.post(
            reverse("tickets:resolution", args=[self.ticket.pk]), data
        )

    def resolve(self):
        self.ticket.status = Ticket.Status.RESOLVED
        self.ticket.save(update_fields=["status"])

    def test_the_repairer_names_the_note_that_worked(self):
        self.mark(self.member, self.fix)
        self.ticket.refresh_from_db()
        self.assertEqual(self.ticket.resolution_comment_id, self.fix.pk)

    def test_marking_another_note_moves_the_mark_rather_than_adding_one(self):
        """Unicity comes free from the foreign key: a flag on Comment would
        have accepted two marked notes on the same ticket."""
        self.mark(self.member, self.guess)
        self.mark(self.member, self.fix)
        self.ticket.refresh_from_db()
        self.assertEqual(self.ticket.resolution_comment_id, self.fix.pk)

    def test_sending_nothing_takes_the_mark_off(self):
        self.mark(self.member, self.fix)
        self.mark(self.member, None)
        self.ticket.refresh_from_db()
        self.assertIsNone(self.ticket.resolution_comment_id)

    def test_a_note_from_another_thread_is_refused(self):
        """The lookup is scoped to this ticket's own comments: an identifier
        from elsewhere would hang a stranger's note at the top of this page."""
        other = Ticket.objects.create(
            school=self.school, room=self.room, title="Drucker",
            visibility=Visibility.ADMINS, created_by=self.boss,
        )
        elsewhere = Comment.objects.create(
            ticket=other, author=self.boss, body="Toner gewechselt."
        )
        self.assertEqual(self.mark(self.member, elsewhere).status_code, 404)
        self.ticket.refresh_from_db()
        self.assertIsNone(self.ticket.resolution_comment_id)

    def test_reporting_is_not_repairing(self):
        """Same rule as `status` and `tags`, on one's own ticket included."""
        self.assertEqual(self.mark(self.reporter, self.fix).status_code, 404)
        self.ticket.refresh_from_db()
        self.assertIsNone(self.ticket.resolution_comment_id)

    def test_a_ticket_one_may_not_read_is_a_404_not_a_403(self):
        self.ticket.visibility = Visibility.ADMINS
        self.ticket.save(update_fields=["visibility"])
        self.assertEqual(self.mark(self.member, self.fix).status_code, 404)

    def test_the_route_answers_no_get(self):
        self.client.force_login(self.member)
        response = self.client.get(reverse("tickets:resolution", args=[self.ticket.pk]))
        self.assertEqual(response.status_code, 405)

    def test_closing_never_demands_a_resolution(self):
        """D-29, taken by the other end: a required field would teach "ok",
        and would block the tickets that legitimately have none."""
        self.client.force_login(self.member)
        response = self.client.post(
            reverse("tickets:status", args=[self.ticket.pk]), {"status": "resolved"}
        )
        self.assertEqual(response.status_code, 302)
        self.ticket.refresh_from_db()
        self.assertEqual(self.ticket.status, Ticket.Status.RESOLVED)
        self.assertTrue(self.ticket.resolution_missing)

    def test_the_gap_is_shown_on_the_page_and_in_the_list(self):
        self.resolve()
        self.client.force_login(self.member)
        self.assertContains(
            self.client.get(reverse("tickets:detail", args=[self.ticket.pk])),
            "no note says what worked",
            status_code=200,
        )
        self.assertContains(
            self.client.get(reverse("tickets:list") + "?closed=1"),
            "No note says what worked.",
        )

    def test_the_resolution_rises_to_the_top_of_the_thread(self):
        self.mark(self.member, self.fix)
        self.resolve()
        response = self.client.get(reverse("tickets:detail", args=[self.ticket.pk]))
        page = response.content.decode()
        self.assertIn("What worked", page)
        # And keeps its chronological place below: twice on the page, not once.
        self.assertEqual(page.count("HDMI-Kabel war lose."), 2)

    def test_the_card_shows_it_without_reading_the_thread(self):
        """What the foreign key on the ticket buys: drawing the resolution on
        every card costs not one query more than not drawing it."""
        self.resolve()
        self.client.force_login(self.member)
        with CaptureQueriesContext(connection) as plain:
            self.client.get(reverse("tickets:list") + "?closed=1")
        self.mark(self.member, self.fix)
        with CaptureQueriesContext(connection) as marked:
            response = self.client.get(reverse("tickets:list") + "?closed=1")
        self.assertEqual(len(marked), len(plain))
        self.assertContains(response, "HDMI-Kabel war lose.")

    def test_the_mark_survives_a_reopening(self):
        """A fault that starts again does not make the note untrue: it makes it
        the first thing the next person should read. ``resolved_by`` answers a
        question a reopened ticket no longer has; this one does not."""
        self.mark(self.member, self.fix)
        self.resolve()
        self.client.force_login(self.reporter)
        self.client.post(
            reverse("tickets:status", args=[self.ticket.pk]), {"status": "open"}
        )
        self.ticket.refresh_from_db()
        self.assertEqual(self.ticket.status, Ticket.Status.OPEN)
        self.assertIsNone(self.ticket.resolved_by_id)
        self.assertEqual(self.ticket.resolution_comment_id, self.fix.pk)
        # And it is presented as what worked *last time*, the ticket being open.
        response = self.client.get(reverse("tickets:detail", args=[self.ticket.pk]))
        self.assertContains(response, "What worked last time")

    def test_a_deleted_note_does_not_take_the_ticket_with_it(self):
        self.mark(self.member, self.fix)
        self.fix.delete()
        self.ticket.refresh_from_db()
        self.assertIsNone(self.ticket.resolution_comment_id)

    def test_the_buttons_are_drawn_for_the_team_and_for_nobody_else(self):
        self.client.force_login(self.member)
        self.assertContains(
            self.client.get(reverse("tickets:detail", args=[self.ticket.pk])),
            "This is what worked",
        )
        self.client.force_login(self.reporter)
        self.assertNotContains(
            self.client.get(reverse("tickets:detail", args=[self.ticket.pk])),
            "This is what worked",
        )


class QuickCaptureTests(TestCase):
    """The corridor (D-35).

    Most reports start as a sentence heard between two doors -- "R102 does not
    work" -- from somebody who is not in front of the machine and has no
    diagnosis. The form has to accept that and stop asking.
    """

    @classmethod
    def setUpTestData(cls):
        cls.school = School.objects.create(slug="lycee6", name="Lycee")
        cls.a = Room.objects.create(school=cls.school, name="A101", sort_key="1-101")
        cls.b = Room.objects.create(school=cls.school, name="B204", sort_key="2-204")
        cls.c = Room.objects.create(school=cls.school, name="C305", sort_key="3-305")
        cls.reporter = User.objects.enroll(school=cls.school, cn="lehrer", role=Role.REPORTER)
        cls.device = Device.objects.create(
            school=cls.school, room=cls.b, mac="48:5b:39:0b:2e:c4", hostname="b204-01"
        )

    def setUp(self):
        self.client.force_login(self.reporter)

    def test_a_room_and_a_sentence_are_enough(self):
        """No machine, no description, no priority, no visibility, no photo --
        which is everything the person in the corridor actually has."""
        response = self.client.post(
            reverse("tickets:create"), {"room": self.a.pk, "title": "DVD geht nicht"}
        )
        self.assertEqual(response.status_code, 302)
        ticket = Ticket.objects.get()
        self.assertEqual(ticket.room, self.a)
        self.assertEqual(ticket.title, "DVD geht nicht")
        self.assertEqual(ticket.description, "")
        self.assertIsNone(ticket.device_id)
        # The model's own defaults, applied rather than demanded.
        self.assertEqual(ticket.priority, Ticket.Priority.NORMAL)
        self.assertEqual(ticket.visibility, Visibility.TEAM)

    def test_the_audience_is_stated_even_when_the_choice_is_folded(self):
        """Doc 08: visibility is never implicit. Folding the radios away does
        not make the answer invisible."""
        page = self.client.get(reverse("tickets:create")).content.decode()
        self.assertIn("Team", page.split("<button")[-2])

    def test_the_folded_block_opens_when_it_holds_an_error(self):
        """A folded error is an error nobody can see."""
        response = self.client.post(reverse("tickets:create"), {
            "room": self.a.pk, "title": "Kein Bild", "device": self.device.pk,
        })
        page = response.content.decode()
        # Whichever refusal fires -- the narrowed queryset or the cross-check
        # in clean() -- the message lands inside the disclosure, so what the
        # test is about is that the disclosure is open around it.
        self.assertIn("text-error", page)
        self.assertIn("open>", page)
        self.assertEqual(Ticket.objects.count(), 0)

    def test_the_block_stays_shut_on_a_first_visit(self):
        self.assertNotIn("open>", self.client.get(reverse("tickets:create")).content.decode())

    def test_nothing_was_removed_from_the_form(self):
        """The fields moved behind a disclosure; they did not go away. This is
        what a later "simplification" has to break to drop one."""
        page = self.client.get(reverse("tickets:create")).content.decode()
        for name in ("device", "description", "priority", "visibility", "photos"):
            with self.subTest(field=name):
                self.assertIn(f'name="{name}"', page)

    def test_the_rooms_last_reported_come_first(self):
        """Faults cluster, and whoever reports walks the same corridor twice."""
        for room in (self.c, self.b):
            self.client.post(reverse("tickets:create"), {"room": room.pk, "title": "x"})
        form = TicketForm(user=self.reporter)
        groups = dict(form.fields["room"].choices[1:])
        self.assertEqual(
            [str(label) for _pk, label in groups["Recently"]], ["B204", "C305"]
        )
        self.assertEqual([str(label) for _pk, label in groups["All rooms"]], ["A101"])

    def test_a_first_ticket_leaves_the_plain_alphabetical_list(self):
        """Nobody has reported anything yet: two groups of which one is empty
        would be worse than no grouping at all."""
        form = TicketForm(user=self.reporter)
        self.assertEqual([str(label) for _pk, label in form.fields["room"].choices[1:]],
                         ["A101", "B204", "C305"])

    def test_the_grouping_does_not_widen_what_may_be_posted(self):
        """`choices` is replaced, `queryset` is not -- and the queryset is the
        rule (see the module docstring of tickets.forms)."""
        elsewhere = School.objects.create(slug="ailleurs3", name="Ailleurs")
        foreign = Room.objects.create(school=elsewhere, name="Z999")
        self.client.post(reverse("tickets:create"), {"room": self.a.pk, "title": "x"})
        response = self.client.post(
            reverse("tickets:create"), {"room": foreign.pk, "title": "Fremd"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Ticket.objects.filter(title="Fremd").count(), 0)
