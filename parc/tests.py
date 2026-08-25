# SPDX-License-Identifier: GPL-3.0-or-later
"""The estate's reconciliation, tested against real-world data.

``testdata/devices-sample.csv`` comes from a live linuxmuster development
server. It is kept because it is *awkward*: mixed MAC case, a host commented
out to clear an address collision, ``---`` used as an empty marker, and roles
that disagree with the PXE flag. Every one of those broke something.
"""

from pathlib import Path

from django.db import IntegrityError
from django.test import TestCase, SimpleTestCase, override_settings
from django.utils import timezone

from accounts.models import AuditLog
from parc import sources, tasks
from parc.inventory import apply_inventory
from parc.models import Device, DeviceStatus, Room, SyncDecision, SyncRun

SAMPLE = Path(__file__).parent / "testdata" / "devices-sample.csv"


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

        Sweeping them would file a permanent ``unreachable`` on each -- an
        estate of false alarms on a screen a pupil is meant to trust.
        """
        written = tasks._write_status({})
        self.assertEqual(written, 14)
        self.assertEqual(DeviceStatus.objects.count(), 14)
        self.assertFalse(DeviceStatus.objects.filter(device__pxe=0).exists())

    def test_a_swept_machine_carries_both_ages(self):
        device = Device.objects.filter(pxe__gt=0).first()
        tasks._write_status(
            {device.mac: {"mac": device.mac, "last_sync_at": "2026-08-20T09:00:00Z"}}
        )
        status = DeviceStatus.objects.get(device=device)
        self.assertEqual(status.fetch_status, DeviceStatus.FetchStatus.OK)
        self.assertIsNotNone(status.observed_at)
        self.assertIsNotNone(status.last_linbo_sync_at)
        self.assertLess(status.last_linbo_sync_at, timezone.now())
