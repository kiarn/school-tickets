# SPDX-License-Identifier: GPL-3.0-or-later
"""Notifications. See specs/09-notifications.md.

The tests worth having here are the ones about what does NOT get sent: a
notification that reaches somebody who lost access is a leak, and one that
reaches somebody with nothing to do about it is how a channel gets muted for
good.
"""

from datetime import timedelta
from unittest import mock

from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from accounts.authz import Role, Visibility
from accounts.models import PushSubscription, User
from notifications import delivery, events
from notifications.models import Delivery, Kind, Mute, Notification
from parc.models import Room
from tickets.models import Comment, Ticket, TicketAssignee


class Base(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.room = Room.objects.create(name="204")

        def person(cn, role):
            return User.objects.enroll(
                cn=cn, role=role, display_name=cn
            )

        cls.admin = person("admin", Role.ADMIN)
        cls.lea = person("lea", Role.MEMBER)
        cls.nils = person("nils", Role.MEMBER)
        cls.teacher = person("teacher", Role.REPORTER)

    def ticket(self, **kwargs):
        fields = {
            "room": self.room, "room_label": "204",
            "title": "Black screen", "visibility": Visibility.TEAM,
            "created_by": self.lea,
        }
        return Ticket.objects.create(**{**fields, **kwargs})

    def recipients(self, kind=None):
        rows = Notification.objects.all()
        if kind:
            rows = rows.filter(kind=kind)
        return set(rows.values_list("recipient__cn", flat=True))


class FanoutTests(Base):
    def test_a_new_ticket_calls_the_team_and_leaves_reporters_alone(self):
        """The recruitment notification -- and the one most exposed to flooding
        (doc 09 §3). A teacher who reported a projector has no use for the
        estate's traffic."""
        events.ticket_opened(self.ticket())
        self.assertEqual(self.recipients(), {"admin", "nils"})

    def test_nobody_is_notified_of_their_own_action(self):
        """The single rule that keeps the list readable, and it has no exception."""
        events.ticket_opened(self.ticket(created_by=self.admin))
        self.assertNotIn("admin", self.recipients())

    def test_a_new_ticket_never_reaches_somebody_who_may_not_read_it(self):
        events.ticket_opened(self.ticket(visibility=Visibility.ADMINS, created_by=self.teacher))
        self.assertEqual(self.recipients(), {"admin"})

    def test_a_resolution_reaches_the_person_who_reported_the_fault(self):
        """The notification that closes the teaching loop: only the author can
        say "no, it started again"."""
        ticket = self.ticket(created_by=self.teacher, visibility=Visibility.ALL)
        TicketAssignee.objects.create(ticket=ticket, user=self.lea, assigned_by=self.admin)
        ticket.status = Ticket.Status.RESOLVED
        ticket.save()
        events.status_changed(ticket, actor=self.lea, previous=Ticket.Status.OPEN)
        self.assertEqual(self.recipients(Kind.RESOLVED), {"teacher"})

    def test_withdrawing_a_ticket_notifies_nobody(self):
        """Nobody has anything to do about work that no longer exists (§4)."""
        ticket = self.ticket()
        ticket.status = Ticket.Status.CANCELLED
        events.status_changed(ticket, actor=self.admin, previous=Ticket.Status.OPEN)
        self.assertEqual(Notification.objects.count(), 0)

    def test_a_note_reaches_the_author_and_the_assignees_and_nobody_else(self):
        ticket = self.ticket(created_by=self.teacher, visibility=Visibility.ALL)
        TicketAssignee.objects.create(ticket=ticket, user=self.lea, assigned_by=self.admin)
        comment = Comment.objects.create(ticket=ticket, author=self.lea, body="Cable swapped.")
        events.commented(comment)
        self.assertEqual(self.recipients(Kind.COMMENT), {"teacher"})

    def test_an_escalation_calls_the_team_not_only_the_people_involved(self):
        """D-37. The involved of an unclaimed ticket are its author alone --
        usually the person raising the priority, whom the fan-out then removes.
        Sent to `_involved`, the notification would reach nobody."""
        events.escalated(self.ticket(created_by=self.lea), actor=self.lea)
        self.assertEqual(self.recipients(Kind.ESCALATED), {"admin", "nils"})

    def test_a_pending_queue_is_announced_once_not_every_hour(self):
        """Re-notifying about the same unread queue is how a channel gets muted."""
        events.sync_decisions_pending(count=3)
        events.sync_decisions_pending(count=3)
        self.assertEqual(Notification.objects.filter(kind=Kind.SYNC_DECISIONS).count(), 1)


class ViewWiringTests(Base):
    """The events are called from the views, in the event's transaction."""

    def test_opening_a_ticket_through_the_form_notifies_the_team(self):
        self.client.force_login(self.lea)
        self.client.post(reverse("tickets:create"), {
            "room": self.room.pk, "title": "No sound", "priority": "normal",
            "visibility": Visibility.TEAM,
        })
        self.assertEqual(self.recipients(Kind.TICKET_OPENED), {"admin", "nils"})

    def test_assigning_notifies_only_the_newcomers(self):
        ticket = self.ticket()
        self.client.force_login(self.admin)
        url = reverse("tickets:assignees", args=[ticket.pk])
        self.client.post(url, {"users": [self.nils.pk]})
        self.client.post(url, {"users": [self.nils.pk]})  # same list again
        self.assertEqual(Notification.objects.filter(kind=Kind.ASSIGNED).count(), 1)

    def test_raising_a_ticket_to_urgent_notifies_the_team(self):
        ticket = self.ticket(priority=Ticket.Priority.NORMAL)
        self.client.force_login(self.lea)
        self.client.post(reverse("tickets:correct", args=[ticket.pk]), {
            "room": self.room.pk, "priority": Ticket.Priority.URGENT,
        })
        ticket.refresh_from_db()
        self.assertEqual(ticket.priority, Ticket.Priority.URGENT)
        self.assertEqual(self.recipients(Kind.ESCALATED), {"admin", "nils"})

    def test_a_correction_that_leaves_the_priority_alone_notifies_nobody(self):
        """Moving a ticket to the room it should have been filed under is
        bookkeeping: nobody has anything to do about it (doc 09 §4)."""
        ticket = self.ticket(priority=Ticket.Priority.URGENT)
        other = Room.objects.create(name="205")
        self.client.force_login(self.lea)
        self.client.post(reverse("tickets:correct", args=[ticket.pk]), {
            "room": other.pk, "priority": Ticket.Priority.URGENT,
        })
        ticket.refresh_from_db()
        self.assertEqual(ticket.room, other)
        self.assertEqual(Notification.objects.count(), 0)

    def test_dropping_a_ticket_back_to_normal_notifies_nobody(self):
        """Only upwards: a notification nobody must act on is how a channel
        gets muted for good."""
        ticket = self.ticket(priority=Ticket.Priority.URGENT)
        self.client.force_login(self.lea)
        self.client.post(reverse("tickets:correct", args=[ticket.pk]), {
            "room": self.room.pk, "priority": Ticket.Priority.NORMAL,
        })
        self.assertEqual(Notification.objects.count(), 0)

    def test_the_badge_appears_on_every_page(self):
        events.ticket_opened(self.ticket())
        self.client.force_login(self.nils)
        response = self.client.get(reverse("tickets:list"))
        self.assertEqual(response.context["unread_count"], 1)

    def test_marking_read_empties_the_badge(self):
        events.ticket_opened(self.ticket())
        self.client.force_login(self.nils)
        self.client.post(reverse("notifications:read"))
        response = self.client.get(reverse("tickets:list"))
        self.assertEqual(response.context["unread_count"], 0)

    def test_following_one_lands_on_its_ticket_and_marks_it_read(self):
        ticket = self.ticket()
        events.ticket_opened(ticket)
        row = Notification.objects.filter(recipient=self.nils).get()
        self.client.force_login(self.nils)
        response = self.client.get(reverse("notifications:open", args=[row.pk]))
        self.assertRedirects(response, reverse("tickets:detail", args=[ticket.pk]))
        row.refresh_from_db()
        self.assertIsNotNone(row.read_at)

    def test_somebody_elses_notification_is_not_mine_to_open(self):
        events.ticket_opened(self.ticket())
        row = Notification.objects.filter(recipient=self.nils).get()
        self.client.force_login(self.admin)
        self.assertEqual(
            self.client.get(reverse("notifications:open", args=[row.pk])).status_code, 404
        )

    def test_the_service_worker_is_served_from_the_root(self):
        """Below /static/ it would control /static/ and nothing else, and no
        push would ever be displayed."""
        response = self.client.get("/sw.js")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/javascript")
        self.assertContains(response, "notificationclick")


@override_settings(ST_VAPID_PRIVATE_KEY="test-key", ST_VAPID_SUBJECT="mailto:a@b.c")
class DeliveryTests(Base):
    def setUp(self):
        self.sent = []

        def fake_send(subscription, payload):
            self.sent.append((subscription.endpoint, payload))
            return True

        patcher = mock.patch.object(delivery, "_send", side_effect=fake_send)
        patcher.start()
        self.addCleanup(patcher.stop)

    def device(self, user, endpoint="https://push.example/1"):
        return PushSubscription.objects.create(
            user=user, endpoint=endpoint, p256dh="p", auth="a"
        )

    def test_a_notification_whose_ticket_became_invisible_is_dropped_not_delayed(self):
        """THE test of doc 09 §2: restricting is open to everyone, so the
        visibility is replayed at delivery and not only at creation."""
        self.device(self.nils)
        ticket = self.ticket(visibility=Visibility.ALL)
        events.ticket_opened(ticket)
        ticket.visibility = Visibility.ADMINS
        ticket.save()

        delivery.deliver_pending()
        row = Notification.objects.get(recipient=self.nils)
        self.assertEqual(row.delivery, Delivery.DROPPED)
        self.assertEqual(self.sent, [])

    def test_the_payload_names_nobody_and_quotes_nothing(self):
        """It is read off a locked screen lying on a classroom table."""
        self.device(self.nils)
        ticket = self.ticket(title="Lea broke the projector", description="secret")
        events.ticket_opened(ticket)
        delivery.deliver_pending()

        payload = self.sent[0][1]
        self.assertIn("204", payload)
        self.assertNotIn("Lea", payload)
        self.assertNotIn("secret", payload)
        self.assertNotIn("projector", payload)

    def test_new_tickets_are_grouped_to_one_push_an_hour(self):
        self.device(self.nils)
        for _ in range(3):
            events.ticket_opened(self.ticket())
        delivery.deliver_pending()
        self.assertEqual(len(self.sent), 1)
        self.assertIn("3 new tickets", self.sent[0][1])

        # A fourth one within the hour waits -- it is held, not dropped, and it
        # is readable in the application the whole time.
        events.ticket_opened(self.ticket())
        delivery.deliver_pending()
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(
            Notification.objects.filter(
                recipient=self.nils, delivery=Delivery.PENDING
            ).count(), 1
        )

    def test_an_addressed_notification_is_never_held_back(self):
        """It carries an action, so grouping it would be losing it."""
        self.device(self.nils)
        ticket = self.ticket()
        events.assigned(ticket, [self.nils], by=self.admin)
        events.commented(
            Comment.objects.create(ticket=ticket, author=self.admin, body="x")
        )
        TicketAssignee.objects.create(ticket=ticket, user=self.nils, assigned_by=self.admin)
        delivery.deliver_pending()
        self.assertGreaterEqual(len(self.sent), 1)
        self.assertNotIn("new tickets", self.sent[0][1])

    def test_a_mute_silences_the_push_and_leaves_the_row(self):
        """Muting a reminder is not asking to be kept in the dark (§7)."""
        self.device(self.nils)
        Mute.objects.create(user=self.nils, kind=Kind.TICKET_OPENED)
        events.ticket_opened(self.ticket())
        delivery.deliver_pending()

        row = Notification.objects.get(recipient=self.nils)
        self.assertEqual(row.delivery, Delivery.MUTED)
        self.assertIsNone(row.read_at)          # still unread, still on the page
        self.assertEqual(self.sent, [])

    def test_without_a_device_nothing_is_sent_and_nothing_is_lost(self):
        events.ticket_opened(self.ticket())
        delivery.deliver_pending()
        self.assertEqual(
            Notification.objects.get(recipient=self.nils).delivery, Delivery.NO_DEVICE
        )

    def test_the_recipients_own_language_is_used(self):
        """The worker has no request, so without an override every push would
        go out in German (D-15)."""
        self.nils.language = "fr"
        self.nils.save()
        self.device(self.nils)
        events.assigned(self.ticket(), [self.nils], by=self.admin)
        TicketAssignee.objects.create(
            ticket=Ticket.objects.first(), user=self.nils, assigned_by=self.admin
        )
        delivery.deliver_pending()
        self.assertTrue(self.sent)

    def test_nothing_is_sent_at_all_without_a_configured_key(self):
        """And the rows stay PENDING, so configuring the keys later delivers
        the backlog rather than losing it."""
        self.device(self.nils)
        events.ticket_opened(self.ticket())
        with override_settings(ST_VAPID_PRIVATE_KEY=""):
            result = delivery.deliver_pending()
        self.assertIn("skipped", result)
        self.assertEqual(
            Notification.objects.get(recipient=self.nils).delivery, Delivery.PENDING
        )


class WindowTests(TestCase):
    """Doc 09 §6: not the window of the estate, and the difference is the point."""

    def test_the_notification_window_runs_later_than_the_estate_one(self):
        from django.conf import settings

        self.assertEqual(settings.ST_NOTIFY_HOURS, (7, 20))
        self.assertGreater(settings.ST_NOTIFY_HOURS[1], settings.ST_OPENING_HOURS[1])

    def test_the_worker_holds_notifications_outside_the_window(self):
        from parc import jobs

        evening = timezone.localtime().replace(hour=22, minute=0)
        with mock.patch.object(jobs.timezone, "localtime", return_value=evening):
            self.assertFalse(jobs.is_notify_hours())

        afternoon = timezone.localtime().replace(hour=17, minute=0)
        while afternoon.weekday() not in (0, 1, 2, 3, 4):
            afternoon += timedelta(days=1)
        with mock.patch.object(jobs.timezone, "localtime", return_value=afternoon):
            self.assertTrue(jobs.is_notify_hours())


@override_settings(ST_VAPID_PRIVATE_KEY="test-key", ST_VAPID_SUBJECT="mailto:a@b.c")
class HeldOvernightTests(Base):
    """What the delivery window actually does to a Friday evening.

    Nothing is lost: rows stay PENDING and go out at the next opening. But a
    Push that is a day late meets two situations the immediate case never does,
    and both are tested here.
    """

    def setUp(self):
        self.sent = []
        patcher = mock.patch.object(
            delivery, "_send",
            side_effect=lambda sub, payload: (self.sent.append(payload), True)[1],
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        PushSubscription.objects.create(
            user=self.nils, endpoint="https://push.example/1", p256dh="p", auth="a"
        )

    def weekend_of_notes(self):
        ticket = self.ticket()
        TicketAssignee.objects.create(ticket=ticket, user=self.nils, assigned_by=self.admin)
        for index in range(4):
            events.commented(
                Comment.objects.create(ticket=ticket, author=self.lea, body=f"note {index}")
            )
        return ticket

    def test_nothing_is_lost_while_the_window_is_shut(self):
        self.weekend_of_notes()
        self.assertEqual(
            Notification.objects.filter(
                recipient=self.nils, delivery=Delivery.PENDING
            ).count(), 4
        )

    def test_four_notes_on_one_ticket_ring_the_phone_once(self):
        """One Push per ticket, not per event: it is one thing to go and look at."""
        self.weekend_of_notes()
        delivery.deliver_pending()
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(
            Notification.objects.filter(recipient=self.nils, delivery=Delivery.SENT).count(), 4
        )

    def test_two_tickets_stay_two_pushes(self):
        """Grouping per ticket, never across: they are two different places."""
        for _ in range(2):
            ticket = self.ticket()
            TicketAssignee.objects.create(
                ticket=ticket, user=self.nils, assigned_by=self.admin
            )
            events.commented(
                Comment.objects.create(ticket=ticket, author=self.lea, body="note")
            )
        delivery.deliver_pending()
        self.assertEqual(len(self.sent), 2)

    def test_the_headline_is_the_most_recent_event(self):
        ticket = self.weekend_of_notes()
        ticket.status = Ticket.Status.RESOLVED
        ticket.save()
        events.status_changed(ticket, actor=self.lea, previous=Ticket.Status.OPEN)
        delivery.deliver_pending()
        # After a note and a resolution, what matters is that it was resolved.
        self.assertIn("resolved", self.sent[-1])

    def test_an_escalation_says_urgent_and_the_room_and_nothing_else(self):
        """D-37 sends this one; §2 decides what it may carry. A lock screen
        learns that something in A101 became urgent -- never the title, which
        is where a pupil's words and a machine's name would be."""
        ticket = self.ticket(title="Wasserschaden im Serverschrank")
        events.escalated(ticket, actor=self.lea)
        delivery.deliver_pending()
        self.assertIn("urgent", self.sent[-1].lower())
        self.assertIn("204", self.sent[-1])
        self.assertNotIn("Wasserschaden", self.sent[-1])

    def test_what_was_read_over_the_weekend_never_rings_on_monday(self):
        """The application has no window; somebody may well have opened it on
        Sunday. Announcing what they have already dealt with is the excess
        doc 09 §1 says cannot be undone."""
        self.weekend_of_notes()
        Notification.objects.filter(recipient=self.nils).update(read_at=timezone.now())

        delivery.deliver_pending()
        self.assertEqual(self.sent, [])
        self.assertEqual(
            Notification.objects.filter(recipient=self.nils, delivery=Delivery.READ).count(), 4
        )

    def test_only_the_unread_part_rings(self):
        ticket = self.weekend_of_notes()
        Notification.objects.filter(recipient=self.nils, ticket=ticket).update(
            read_at=timezone.now()
        )
        other = self.ticket(title="Still unread")
        TicketAssignee.objects.create(ticket=other, user=self.nils, assigned_by=self.admin)
        events.commented(
            Comment.objects.create(ticket=other, author=self.lea, body="new")
        )

        delivery.deliver_pending()
        self.assertEqual(len(self.sent), 1)
