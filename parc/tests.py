# SPDX-License-Identifier: GPL-3.0-or-later
"""The estate's reconciliation, tested against real-world data.

``testdata/devices-sample.csv`` comes from a live linuxmuster development
server. It is kept because it is *awkward*: mixed MAC case, a host commented
out to clear an address collision, ``---`` used as an empty marker, and roles
that disagree with the PXE flag. Every one of those broke something.
"""

import json
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from django.db import IntegrityError, connection
from django.test import TestCase, SimpleTestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from accounts.authz import Role
from accounts.models import AuditLog, User
from parc import sources, tasks
from parc.inventory import apply_inventory
from parc.lmnapi import Client, LmnApiError
from parc.models import (
    Device,
    DeviceStatus,
    Room,
    SyncDecision,
    SyncRun,
    WorkerHeartbeat,
)

SAMPLE = Path(__file__).parent / "testdata" / "devices-sample.csv"
API_SAMPLE = Path(__file__).parent / "testdata" / "devices-api-sample.json"


def parse_sample():
    return sources.parse_devices_csv(SAMPLE.read_text())


class ParserTests(SimpleTestCase):
    def setUp(self):
        self.estate = parse_sample()

    def test_sample_shape(self):
        self.assertEqual(len(self.estate.rows), 45)
        self.assertEqual(self.estate.reasons(), {"disabled": 3})

    def test_commented_out_host_leaves_the_estate(self):
        """``#server;dockerhost;...`` is a disabled machine, not a comment.

        It shares 10.0.0.4 with the ``edu`` row, which is plainly why somebody
        commented it out. Splitting naively would invent a room called
        ``#server`` and re-create the collision the admin had just resolved.
        """
        self.assertNotIn("#server", {row.room for row in self.estate.rows})
        self.assertNotIn("dockerhost", {row.hostname for row in self.estate.rows})
        self.assertEqual(
            len([row for row in self.estate.rows if row.ip == "10.0.0.4"]), 1
        )

    def test_mac_case_is_normalised(self):
        """The sample mixes 38 upper-case and 7 lower-case addresses."""
        self.assertTrue(all(row.mac == row.mac.lower() for row in self.estate.rows))
        macs = {row.mac for row in self.estate.rows}
        self.assertIn("48:5b:39:0b:2e:c2", macs)
        self.assertIn("0c:dc:7e:f3:d5:e9", macs)

    def test_dashes_are_emptiness(self):
        shelly = next(r for r in self.estate.rows if r.hostname == "shelly8")
        self.assertEqual(shelly.pxe, 0)
        self.assertEqual(shelly.role, "iponly")
        self.assertEqual(shelly.comment, "MIGRATION")
        self.assertEqual(shelly.ip, "10.30.20.31")

    def test_pxe_flag_is_not_derivable_from_the_role(self):
        """Why ``pxe`` had to become a column of its own.

        Nothing in this application branches on a device's role -- but even if
        something wanted to, it could not stand in for the PXE flag: the two
        disagree outright on real data.
        """
        flags = {}
        for row in self.estate.rows:
            flags.setdefault(row.role, set()).add(row.pxe)
        self.assertEqual(flags["staffcomputer"], {0, 1})
        self.assertEqual(flags["classroom-studentcomputer"], {0, 1})
        self.assertEqual(flags["printer"], {0})

    def test_malformed_lines_cost_only_themselves(self):
        estate = sources.parse_devices_csv(
            "\n".join(
                [
                    "r1;good;allg;AA:BB:CC:DD:EE:01;10.0.0.1;;;;staffcomputer;1;1;;;;;",
                    "r1;short;allg;AA:BB:CC:DD:EE:02;10.0.0.2",
                    "r1;badmac;allg;not-a-mac;10.0.0.3;;;;staffcomputer;1;1;;;;;",
                    ";nameless;allg;AA:BB:CC:DD:EE:04;10.0.0.4;;;;staffcomputer;1;1;;;;;",
                    "r1;twin;allg;aa:bb:cc:dd:ee:01;10.0.0.5;;;;staffcomputer;1;1;;;;;",
                    "r1;noip;allg;AA:BB:CC:DD:EE:06;999.1.1.1;;;;staffcomputer;1;1;;;;;",
                ]
            )
        )
        self.assertEqual([r.hostname for r in estate.rows], ["good", "noip"])
        self.assertEqual(
            sorted(estate.reasons()),
            ["bad_mac", "duplicate_mac_of_good", "missing_key_field", "too_few_fields"],
        )
        # A bad address costs the address, never the machine.
        self.assertIsNone(estate.rows[1].ip)


class ApiAdapterTests(SimpleTestCase):
    """``devices-api-sample.json`` is the same estate as the CSV, read through
    ``GET /v1/devices/list/{school}`` on a linuxmuster 7.4.11 test server.

    Kept for the same reason as the CSV sample: it is awkward in exactly the
    same ways, because the API re-exports the file line by line without
    cleaning it.
    """

    def setUp(self):
        self.estate = sources.rows_from_api(json.loads(API_SAMPLE.read_text()))

    def test_the_two_sources_describe_the_same_estate(self):
        """doc 03's whole premise, and the only test that really checks it.

        The reconciliation must not be able to tell the sources apart. If one
        day the CSV and the API disagree on a single machine, this is what has
        to fail -- not a room quietly renamed in production.
        """
        csv_rows = {r.mac: r for r in parse_sample().rows}
        api_rows = {r.mac: r for r in self.estate.rows}

        self.assertEqual(sorted(csv_rows), sorted(api_rows))
        for mac, csv_row in csv_rows.items():
            api_row = api_rows[mac]
            for f in ("room", "hostname", "group", "ip", "role", "pxe", "comment"):
                self.assertEqual(
                    getattr(csv_row, f),
                    getattr(api_row, f),
                    msg=f"{mac}: {f} differs between the two sources",
                )

    def test_comment_lines_leave_the_estate(self):
        """``status`` names what the CSV made us detect by a leading ``#``.

        Four lines, and the awkward one is ``#server;dockerhost``: a host
        commented out to clear an address collision. It still carries a MAC,
        an IP and a role, so nothing but ``status`` marks it as gone.
        """
        self.assertEqual(self.estate.reasons(), {"disabled": 4})
        self.assertNotIn("#server", {r.room for r in self.estate.rows})
        self.assertNotIn("dockerhost", {r.hostname for r in self.estate.rows})

    def test_unregistered_machines_stay_in_the_estate(self):
        """A router absent from the directory is still a router that breaks."""
        self.assertIn("wlan-router", {r.hostname for r in self.estate.rows})

    def test_mac_case_is_normalised(self):
        for row in self.estate.rows:
            self.assertEqual(row.mac, row.mac.lower())

    def test_null_and_dashes_are_both_emptiness(self):
        """The API mixes JSON ``null``, ``""`` and sophomorix's ``---``."""
        self.assertEqual(
            sources._api_value({"a": None, "b": "---", "c": " x "}, "a"), ""
        )
        self.assertEqual(sources._api_value({"b": "---"}, "b"), "")
        self.assertEqual(sources._api_value({"c": " x "}, "c"), "x")

    def test_a_payload_that_is_not_a_list_is_refused(self):
        """An empty estate reads as "every machine disappeared"."""
        with self.assertRaises(sources.LmnApiPayloadError):
            sources.rows_from_api({"devices": []})


class DeviceModelTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.room = Room.objects.create(name="204")

    def _device(self, mac, hostname="pc", **kwargs):
        return Device.objects.create(
            room=self.room, mac=mac, hostname=hostname, **kwargs
        )

    def test_mac_is_lowercased_on_every_write(self):
        device = self._device("48:5B:39:0B:2E:C2")
        self.assertEqual(device.mac, "48:5b:39:0b:2e:c2")
        device.refresh_from_db()
        self.assertEqual(device.mac, "48:5b:39:0b:2e:c2")

    def test_recased_mac_can_no_longer_slip_past_the_constraint(self):
        """The whole point: without normalisation this created a second row.

        The MAC is the estate's only stable identity (doc 03); a duplicate of
        it is a machine that silently splits in two, taking half its tickets
        and its whole LINBO history with it.
        """
        self._device("aa:bb:cc:11:22:aa", hostname="drucker4")
        with self.assertRaises(IntegrityError):
            self._device("AA:BB:CC:11:22:AA", hostname="drucker4-again")

    def test_expects_linbo_reads_the_flag(self):
        self.assertTrue(self._device("aa:bb:cc:00:00:01", pxe=1).expects_linbo)
        self.assertFalse(self._device("aa:bb:cc:00:00:02", pxe=0).expects_linbo)
        # NULL means "the source did not say", which is not 0 -- but it is not
        # a reason to expect a status either.
        self.assertFalse(self._device("aa:bb:cc:00:00:03").expects_linbo)


class ParcAdminTests(TestCase):
    """What `/admin/` may be asked about the estate (D-51).

    The estate is written by the synchronisation. A form that offers to edit
    what the next pass rewrites is a form that lies about who decides.
    """

    @classmethod
    def setUpTestData(cls):
        cls.boss = User.objects.enroll(cn="boss", role=Role.ADMIN)
        cls.room = Room.objects.create(name="204", building="A")
        cls.device = Device.objects.create(
            room=cls.room, mac="48:5b:39:0b:2e:c2", hostname="r204-01"
        )

    def setUp(self):
        self.client.force_login(self.boss)

    def test_a_device_is_read_and_never_edited(self):
        """Every field a form could offer is rewritten on the next pass, and
        the one that is not -- the MAC -- is the identity itself."""
        self.assertEqual(
            self.client.get(reverse("admin:parc_device_add")).status_code, 403
        )
        page = self.client.get(reverse("admin:parc_device_change", args=[self.device.pk]))
        self.assertEqual(page.context["adminform"].form.fields, {})

    def test_a_room_keeps_the_two_fields_no_source_carries(self):
        """`building` and `sort_key` come from nowhere else: no DeviceRow has
        them and `_ensure_rooms` never writes them."""
        offered = self.client.get(
            reverse("admin:parc_room_change", args=[self.room.pk])
        ).context["adminform"].form.fields
        self.assertEqual(sorted(offered), ["building", "sort_key"])

    def test_a_room_is_never_renamed_here(self):
        """`_ensure_rooms` matches by name: renamed here, the room comes back
        as a second one at the next pass and the tickets stay behind in the
        first."""
        self.assertNotIn(
            "name",
            self.client.get(
                reverse("admin:parc_room_change", args=[self.room.pk])
            ).context["adminform"].form.fields,
        )
        self.assertEqual(self.client.get(reverse("admin:parc_room_add")).status_code, 403)


    def test_a_decision_is_answered_not_rewritten(self):
        """`kind`, `payload` and `sync_run` are the run's account of what it
        saw; offering them for editing offered to rewrite the question (D-52)."""
        run = SyncRun.objects.create(source="csv_upload")
        decision = SyncDecision.objects.create(
            sync_run=run, kind=SyncDecision.Kind.ROOM_DISAPPEARED, payload={"key": "204"}
        )
        offered = self.client.get(
            reverse("admin:parc_syncdecision_change", args=[decision.pk])
        ).context["adminform"].form.fields
        self.assertEqual(list(offered), ["status"])

    def test_deciding_stamps_who_and_when(self):
        """A queue that records a decision without its author records half of
        one -- and nobody types their own name honestly at the third one."""
        run = SyncRun.objects.create(source="csv_upload")
        decision = SyncDecision.objects.create(
            sync_run=run, kind=SyncDecision.Kind.DEVICE_DISAPPEARED, payload={"key": "aa"}
        )
        self.client.post(
            reverse("admin:parc_syncdecision_change", args=[decision.pk]),
            {"status": SyncDecision.Status.DISMISSED},
        )
        decision.refresh_from_db()
        self.assertEqual(decision.status, SyncDecision.Status.DISMISSED)
        self.assertEqual(decision.decided_by, self.boss)
        self.assertIsNotNone(decision.decided_at)

    def test_applied_is_not_offered_while_nothing_applies(self):
        """Q-10. **Delete this test with the guard it protects**, the day the
        queue gets an executor -- and not before: choosing "applied" today
        retires no room and takes no machine out of service."""
        run = SyncRun.objects.create(source="csv_upload")
        decision = SyncDecision.objects.create(
            sync_run=run, kind=SyncDecision.Kind.ROOM_DISAPPEARED, payload={"key": "204"}
        )
        offered = self.client.get(
            reverse("admin:parc_syncdecision_change", args=[decision.pk])
        ).context["adminform"].form.fields["status"].choices
        self.assertNotIn(
            SyncDecision.Status.APPLIED, [value for value, _label in offered]
        )
        self.assertIn(SyncDecision.Status.DISMISSED, [value for value, _label in offered])

    def test_the_worker_has_a_window(self):
        """The one process nobody watches had no screen, while three tables it
        writes had one (D-52)."""
        WorkerHeartbeat.objects.create(at=timezone.now(), jobs_ran={"notify": 1})
        page = self.client.get(reverse("admin:parc_workerheartbeat_changelist"))
        self.assertEqual(page.status_code, 200)
        self.assertEqual(
            self.client.get(reverse("admin:parc_workerheartbeat_add")).status_code, 403
        )


class InventoryTests(TestCase):
    def import_sample(self):
        return apply_inventory(parse_sample().rows, source="csv_upload")

    def rows_without(self, hostnames):
        return [r for r in parse_sample().rows if r.hostname not in hostnames]

    def test_first_import(self):
        run = self.import_sample()
        self.assertEqual(run.status, SyncRun.Status.SUCCESS)
        self.assertEqual(run.devices_seen, 45)
        self.assertEqual(run.rooms_seen, 18)
        self.assertEqual(Device.objects.count(), 45)
        self.assertEqual(Room.objects.count(), 18)
        self.assertEqual(SyncDecision.objects.count(), 0)

    def test_second_import_changes_nothing(self):
        """Idempotence is what a full snapshot buys (doc 03)."""
        self.import_sample()
        stamps = dict(Device.objects.values_list("mac", "id"))
        run = self.import_sample()
        self.assertEqual(run.status, SyncRun.Status.SUCCESS)
        self.assertEqual(Device.objects.count(), 45)
        self.assertEqual(SyncDecision.objects.count(), 0)
        self.assertEqual(AuditLog.objects.count(), 0)
        self.assertEqual(dict(Device.objects.values_list("mac", "id")), stamps)

    def test_move_and_rename_apply_silently(self):
        """Both at once, on the same machine, and nothing is lost.

        Same MAC, so same row, so the tickets and the LINBO history stay put.
        """
        self.import_sample()
        device = Device.objects.get(hostname="dienst05")
        original_id, original_room = device.pk, device.room.name

        rows = []
        for row in parse_sample().rows:
            if row.hostname == "dienst05":
                row = sources.DeviceRow(**{**row.__dict__, "room": "kissy", "hostname": "sfb11"})
            rows.append(row)
        apply_inventory(rows, source="csv_upload")

        device.refresh_from_db()
        self.assertEqual(device.pk, original_id)
        self.assertEqual(device.hostname, "sfb11")
        self.assertEqual(device.room.name, "kissy")
        self.assertEqual(SyncDecision.objects.count(), 0)

        trace = AuditLog.objects.get(target_id=original_id)
        self.assertEqual(trace.action, AuditLog.Action.SYNC_APPLIED)
        self.assertEqual(trace.payload["room"], [original_room, "kissy"])
        self.assertEqual(trace.payload["hostname"], ["dienst05", "sfb11"])

    def test_room_rename_is_proposed_never_applied(self):
        self.import_sample()
        rows = [
            sources.DeviceRow(**{**row.__dict__, "room": "B012"})
            if row.room == "dienst"
            else row
            for row in parse_sample().rows
        ]
        apply_inventory(rows, source="csv_upload")

        decision = SyncDecision.objects.get(kind=SyncDecision.Kind.ROOM_RENAMED_SUSPECTED)
        self.assertEqual(decision.status, SyncDecision.Status.PENDING)
        self.assertEqual(decision.payload["room"], "dienst")
        self.assertEqual(decision.payload["suspected_new_name"], "B012")
        self.assertEqual(decision.payload["confidence"], 1.0)
        self.assertFalse(decision.payload["weak_evidence"])
        # Proposed, not applied: the old room is untouched and still active.
        self.assertTrue(Room.objects.get(name="dienst").is_active)

    def test_a_single_machine_proves_nothing(self):
        """"Its one MAC turns up in Y" does not distinguish a rename from a
        workstation being carried down the corridor (doc 03)."""
        self.import_sample()
        rows = [
            sources.DeviceRow(**{**row.__dict__, "room": "B013"})
            if row.room == "etudemoy"
            else row
            for row in parse_sample().rows
        ]
        apply_inventory(rows, source="csv_upload")
        decision = SyncDecision.objects.get(kind=SyncDecision.Kind.ROOM_RENAMED_SUSPECTED)
        self.assertTrue(decision.payload["weak_evidence"])

    def test_moving_a_whole_room_into_an_existing_one_is_not_a_rename(self):
        """The guard that stops two real rooms being merged irreversibly.

        Emptying 204 into the very real 205 satisfies "its MACs reappear
        elsewhere" to the letter. Only a target name that did *not* exist
        before can be a rename.
        """
        self.import_sample()
        rows = [
            sources.DeviceRow(**{**row.__dict__, "room": "cuisine"})
            if row.room == "dienst"
            else row
            for row in parse_sample().rows
        ]
        apply_inventory(rows, source="csv_upload")

        self.assertEqual(SyncDecision.objects.count(), 0)
        # The origin room stays, empty -- the exact description of what happened.
        self.assertTrue(Room.objects.get(name="dienst").is_active)
        self.assertEqual(Room.objects.get(name="dienst").devices.count(), 0)
        self.assertEqual(Room.objects.get(name="cuisine").devices.count(), 7)

    def test_disappearance_is_queued_never_applied(self):
        self.import_sample()
        gone = {"dienst05", "dienst07", "dienst08", "dienst09"}
        apply_inventory(self.rows_without(gone), source="csv_upload")

        self.assertEqual(
            SyncDecision.objects.filter(kind=SyncDecision.Kind.DEVICE_DISAPPEARED).count(), 4
        )
        room_decision = SyncDecision.objects.get(kind=SyncDecision.Kind.ROOM_DISAPPEARED)
        self.assertEqual(room_decision.payload["room"], "dienst")
        self.assertEqual(room_decision.payload["survivors"], 0)
        # Nothing retired: a sync proposes, it never destroys.
        self.assertEqual(Device.objects.filter(is_active=True).count(), 45)
        self.assertTrue(Room.objects.get(name="dienst").is_active)

    def test_a_pending_decision_is_never_filed_twice(self):
        """Otherwise a nightly sync buries the queue in its own repetitions."""
        self.import_sample()
        gone = {"dienst05"}
        apply_inventory(self.rows_without(gone), source="csv_upload")
        apply_inventory(self.rows_without(gone), source="csv_upload")
        self.assertEqual(
            SyncDecision.objects.filter(kind=SyncDecision.Kind.DEVICE_DISAPPEARED).count(), 1
        )

    def test_a_returning_device_needs_no_decision(self):
        self.import_sample()
        apply_inventory(self.rows_without({"dienst05"}), source="csv_upload")
        Device.objects.filter(hostname="dienst05").update(is_active=False)

        apply_inventory(parse_sample().rows, source="csv_upload")
        self.assertTrue(Device.objects.get(hostname="dienst05").is_active)

    @override_settings(ST_SYNC_MAX_REMOVAL_RATIO=0.20)
    def test_the_volume_guard_applies_nothing(self):
        """A truncated export, an empty body behind a 200: how an estate is lost."""
        self.import_sample()
        run = apply_inventory([], source="csv_upload")

        self.assertEqual(run.status, SyncRun.Status.REFUSED_GUARD)
        self.assertIn("Nothing was applied", run.error)
        self.assertEqual(Device.objects.count(), 45)
        self.assertEqual(Device.objects.filter(is_active=True).count(), 45)
        self.assertEqual(SyncDecision.objects.count(), 0)

    @override_settings(ST_SYNC_MAX_REMOVAL_RATIO=0.20)
    def test_a_removal_under_the_threshold_still_goes_through(self):
        self.import_sample()
        gone = {r.hostname for r in parse_sample().rows[:8]}
        run = apply_inventory(self.rows_without(gone), source="csv_upload")
        self.assertEqual(run.status, SyncRun.Status.SUCCESS)


class LinboSweepTests(TestCase):
    """Doc 05's rule: never show a fault where there is only absent data."""

    @classmethod
    def setUpTestData(cls):
        apply_inventory(parse_sample().rows, source="csv_upload")

    def test_only_pxe_machines_are_swept(self):
        """31 of the sample's 45 rows are printers, routers, NAS and servers.

        Sweeping them would file a permanent ``no_data`` on each -- an
        estate of false alarms on a screen a pupil is meant to trust.
        """
        written = tasks._write_status({})
        self.assertEqual(written, 14)
        self.assertEqual(DeviceStatus.objects.count(), 14)
        self.assertFalse(DeviceStatus.objects.filter(device__pxe=0).exists())

    def test_a_swept_machine_carries_both_ages(self):
        device = Device.objects.filter(pxe__gt=0).first()
        tasks._write_status(
            {
                device.hostname.lower(): {
                    "lastSync": "2026-08-07T14:40:00.000Z",
                    "action": "applied",
                    "image": "data-jammy.qcow2",
                    "imageVersion": None,
                }
            }
        )
        status = DeviceStatus.objects.get(device=device)
        self.assertEqual(status.fetch_status, DeviceStatus.FetchStatus.OK)
        self.assertIsNotNone(status.observed_at)
        self.assertIsNotNone(status.last_linbo_sync_at)
        self.assertLess(status.last_linbo_sync_at, timezone.now())
        self.assertEqual(status.linbo_image, "data-jammy.qcow2")

    def test_an_unreadable_timestamp_costs_the_date_not_the_machine(self):
        device = Device.objects.filter(pxe__gt=0).first()
        tasks._write_status({device.hostname.lower(): {"lastSync": "never"}})
        status = DeviceStatus.objects.get(device=device)
        self.assertEqual(status.fetch_status, DeviceStatus.FetchStatus.OK)
        self.assertIsNone(status.last_linbo_sync_at)

    def test_an_absent_machine_is_missing_data_not_a_fault(self):
        """Doc 05's rule, in the one place it can be broken.

        The server holds nothing under this name -- never booted, renamed, log
        purged, all indistinguishable from here. Saying "unreachable" would
        report a fault nobody observed, and "never synchronised" would state a
        conclusion the server cannot support.
        """
        tasks._write_status({})
        statuses = DeviceStatus.objects.values_list("fetch_status", flat=True)
        self.assertEqual(set(statuses), {DeviceStatus.FetchStatus.NO_DATA})

    def test_a_response_is_filed_by_hostname(self):
        device = Device.objects.filter(pxe__gt=0).first()
        written = tasks._write_status(
            tasks._index_by_hostname(
                {
                    "hosts": {
                        device.hostname.upper(): {
                            "lastSync": "2026-08-07T14:40:00.000Z",
                            "action": "applied",
                            "image": "win11.qcow2",
                            "imageVersion": None,
                        }
                    },
                    "total": 1,
                }
            )
        )
        self.assertEqual(written, 14)
        status = DeviceStatus.objects.get(device=device)
        self.assertEqual(status.fetch_status, DeviceStatus.FetchStatus.OK)
        self.assertEqual(status.linbo_image, "win11.qcow2")

    def test_an_ambiguous_hostname_is_filed_on_neither_machine(self):
        """Two machines under one name: the log belongs to one, unknowably.

        Guessing would put a repair history on the wrong device. Both are left
        as missing data instead, and the collision is logged.
        """
        first, second = Device.objects.filter(pxe__gt=0)[:2]
        second.hostname = first.hostname
        second.save()

        tasks._write_status(
            tasks._index_by_hostname(
                {"hosts": {first.hostname: {"image": "win11.qcow2"}}, "total": 1}
            )
        )
        for device in (first, second):
            status = DeviceStatus.objects.get(device=device)
            self.assertEqual(status.fetch_status, DeviceStatus.FetchStatus.NO_DATA)

    def test_a_payload_without_hosts_is_refused(self):
        with self.assertRaises(tasks.LinboPayloadError):
            tasks._index_by_hostname({"devices": []})


class HostStatusTests(TestCase):
    """The per-host endpoint: addressed by name, filed under the MAC.

    Shapes come from ``GET /v1/linbo/hosts/{hostname}/status`` on a live
    lmn 7.4.11 -- one entry per image of the host's ``start.conf`` group, each
    carrying the date it was last applied or ``null`` if it never was.
    """

    @classmethod
    def setUpTestData(cls):
        apply_inventory(parse_sample().rows, source="csv_upload")

    def setUp(self):
        self.device = Device.objects.filter(pxe__gt=0).first()

    def _payload(self, *, mac=None, images=None):
        return {
            "hostname": self.device.hostname,
            "school": "default-school",
            "mac": mac if mac is not None else self.device.mac.upper(),
            "ip": "10.0.0.100",
            "pxeEnabled": True,
            "online": None,
            "osState": None,
            "images": images
            if images is not None
            else [
                {"image": "jammy.qcow2", "name": "Ubuntu Mate", "partition": 2, "lastSync": None},
                {
                    "image": "data-jammy.qcow2",
                    "name": "Data",
                    "partition": 3,
                    "lastSync": "2026-08-07T12:40:00+00:00",
                },
            ],
        }

    def _refresh(self, payload):
        with patch.object(Client, "linbo_status_host", return_value=payload):
            return tasks.refresh_device(self.device)

    def test_the_most_recently_applied_image_is_the_machine_s_state(self):
        """Two images in the group, one never applied: the dated one wins."""
        self.assertEqual(self._refresh(self._payload()), "ok")
        status = DeviceStatus.objects.get(device=self.device)
        self.assertEqual(status.fetch_status, DeviceStatus.FetchStatus.OK)
        self.assertEqual(status.linbo_image, "data-jammy.qcow2")
        self.assertEqual(status.last_linbo_sync_at.year, 2026)
        # The whole answer is kept: a per-image screen needs the undated ones.
        self.assertEqual(len(status.raw["images"]), 2)

    def test_a_reassigned_hostname_files_nothing(self):
        """Doc 05's reason for wanting the MAC in the response, exercised.

        The name still resolves, but it now belongs to another machine. Writing
        its state here would put one machine's history on another -- so the
        answer is refused, and that is the point of the whole rule.
        """
        outcome = self._refresh(self._payload(mac="aa:bb:cc:dd:ee:99"))
        self.assertEqual(outcome, "reassigned")
        status = DeviceStatus.objects.get(device=self.device)
        self.assertEqual(status.fetch_status, DeviceStatus.FetchStatus.NO_DATA)
        self.assertEqual(status.linbo_image, "")
        self.assertIsNone(status.last_linbo_sync_at)

    def test_a_machine_that_never_applied_an_image_is_not_a_fault(self):
        """The group's images are known; none has ever landed on this host."""
        images = [{"image": "jammy.qcow2", "name": "Ubuntu Mate", "lastSync": None}]
        self.assertEqual(self._refresh(self._payload(images=images)), "never_applied")
        status = DeviceStatus.objects.get(device=self.device)
        self.assertEqual(status.fetch_status, DeviceStatus.FetchStatus.NO_DATA)

    def test_mac_case_does_not_decide_identity(self):
        """The API answers upper-case; ``Device.mac`` is always lower."""
        self.assertEqual(self._refresh(self._payload(mac=self.device.mac.upper())), "ok")

    def test_the_queue_is_drained_one_machine_at_a_time(self):
        """The button clears even when a machine's own call fails.

        A request left in place is a button that stays stuck: the next pass
        would retry the same failure for ever, and the person watching would
        never be told anything.
        """
        first, second = Device.objects.filter(pxe__gt=0)[:2]
        for device in (first, second):
            DeviceStatus.objects.create(device=device, refresh_requested_at=timezone.now())

        def answer(hostname, **kwargs):
            if hostname == first.hostname:
                raise LmnApiError("boom")
            return {"mac": second.mac, "images": []}

        with patch.object(Client, "linbo_status_host", side_effect=answer):
            outcomes = tasks.drain_refresh_queue()

        self.assertEqual(outcomes, {"failed": 1, "never_applied": 1})
        self.assertFalse(
            DeviceStatus.objects.filter(refresh_requested_at__isnull=False).exists()
        )


class EstateViewTests(TestCase):
    """The estate seen by a person. A work tool, and open to those who work.

    Doc 04 asks that surfaces be minimised; this one opens no wider than its
    use. A refusal is a 404 like every other refusal in this application, so
    the answer never distinguishes "not for you" from "does not exist".
    """

    @classmethod
    def setUpTestData(cls):
        cls.room = Room.objects.create(name="204")
        cls.empty_room = Room.objects.create(name="Corridor")
        cls.pxe = Device.objects.create(
            room=cls.room, mac="48:5b:39:0b:2e:c2", hostname="dienst05", pxe=1
        )
        cls.printer = Device.objects.create(
            room=cls.empty_room, mac="48:5b:39:0b:2e:c3", hostname="drucker4", pxe=0
        )
        cls.member = User.objects.enroll(cn="lea", role=Role.MEMBER, display_name="Lea")
        cls.reporter = User.objects.enroll(
            cn="max", role=Role.REPORTER, display_name="Max"
        )

    def test_a_reporter_reaches_none_of_it(self):
        self.client.force_login(self.reporter)
        for name, args in (
            ("parc:estate", []),
            ("parc:room", [self.room.pk]),
            ("parc:device", [self.pxe.pk]),
        ):
            with self.subTest(name=name):
                self.assertEqual(self.client.get(reverse(name, args=args)).status_code, 404)

    def test_a_reporter_is_not_offered_the_link(self):
        """Hidden rather than left to refuse: a menu entry that always says no
        teaches the reader to distrust the menu."""
        self.client.force_login(self.reporter)
        response = self.client.get(reverse("tickets:list"))
        self.assertNotContains(response, reverse("parc:estate"))

    def test_whoever_repairs_is_offered_the_link(self):
        self.client.force_login(self.member)
        response = self.client.get(reverse("tickets:list"))
        self.assertContains(response, reverse("parc:estate"))

    def test_a_room_with_nothing_to_ask_is_not_listed(self):
        """A room of printers is not a room with nothing to report.

        It is a room with nothing to *ask*, and "0 of 0" beside its name would
        invite the reader to worry about it (doc 05).
        """
        self.client.force_login(self.member)
        response = self.client.get(reverse("parc:estate"))
        self.assertContains(response, "204")
        self.assertNotContains(response, "Corridor")

    def test_the_estate_counts_without_a_query_per_room(self):
        """Forty rooms must not cost forty queries to draw one page."""
        for n in range(10):
            room = Room.objects.create(name=f"R{n}")
            Device.objects.create(
                room=room, mac=f"aa:bb:cc:dd:ee:{n:02x}", hostname=f"pc{n}", pxe=1
            )
        self.client.force_login(self.member)
        with CaptureQueriesContext(connection) as queries:
            self.client.get(reverse("parc:estate"))
        # Session, user, the annotated room query, the unread count: a handful
        # that does not grow with the estate.
        self.assertLess(len(queries), 10)

    def test_a_room_shows_each_machine_with_its_state(self):
        now = timezone.now()
        DeviceStatus.objects.create(
            device=self.pxe,
            fetch_status=DeviceStatus.FetchStatus.OK,
            last_linbo_sync_at=now - timedelta(days=3),
            linbo_image="jammy.qcow2",
            observed_at=now - timedelta(minutes=5),
        )
        self.client.force_login(self.member)
        response = self.client.get(reverse("parc:room", args=[self.room.pk]))
        self.assertContains(response, "dienst05")
        self.assertContains(response, "Last synchronised")
        self.assertContains(response, "checked")

    def test_a_room_offers_no_refresh_button_per_row(self):
        """A row of buttons invites pressing every one, and each press is a
        call on the school's server. The request lives on the machine's page."""
        self.client.force_login(self.member)
        response = self.client.get(reverse("parc:room", args=[self.room.pk]))
        self.assertNotContains(response, "Check again")

    def test_a_machine_that_does_not_boot_over_the_network_says_so(self):
        """Not a gap in the data, and it must not read as one."""
        self.client.force_login(self.member)
        response = self.client.get(reverse("parc:room", args=[self.empty_room.pk]))
        self.assertContains(response, "Does not boot over the network")
        self.assertNotContains(response, "never been checked")

    def test_the_machine_page_carries_the_request(self):
        self.client.force_login(self.member)
        response = self.client.get(reverse("parc:device", args=[self.pxe.pk]))
        self.assertContains(response, "dienst05")
        self.assertContains(response, "Check again")

    def test_pressing_queues_the_machine(self):
        self.client.force_login(self.member)
        response = self.client.post(reverse("parc:device_refresh", args=[self.pxe.pk]))
        self.assertEqual(response.status_code, 302)
        self.assertIsNotNone(
            DeviceStatus.objects.get(device=self.pxe).refresh_requested_at
        )

    def test_a_printer_cannot_be_asked_about(self):
        self.client.force_login(self.member)
        response = self.client.post(
            reverse("parc:device_refresh", args=[self.printer.pk])
        )
        self.assertEqual(response.status_code, 404)
        self.assertFalse(DeviceStatus.objects.exists())

    def test_a_reporter_cannot_press_either(self):
        self.client.force_login(self.reporter)
        response = self.client.post(reverse("parc:device_refresh", args=[self.pxe.pk]))
        self.assertEqual(response.status_code, 404)
        self.assertFalse(DeviceStatus.objects.exists())

    def test_reaching_a_machine_does_not_open_threads_doc_08_keeps_shut(self):
        """The estate must not become a side door into a ticket.

        A member may not read an admins-only ticket, and finding it through
        the machine it was filed against changes nothing about that.
        """
        from tickets.models import Ticket
        from accounts.authz import Visibility

        # Authored by somebody else: `visible_to` lets an author keep sight of
        # their own report whatever its floor, so a ticket of one's own would
        # prove nothing here.
        boss = User.objects.enroll(cn="chief", role=Role.ADMIN, display_name="Chief")
        Ticket.objects.create(
            room=self.room, device=self.pxe, room_label="204",
            title="Secret matter", description="...",
            visibility=Visibility.ADMINS, created_by=boss,
        )
        self.client.force_login(self.member)
        response = self.client.get(reverse("parc:device", args=[self.pxe.pk]))
        self.assertNotContains(response, "Secret matter")
