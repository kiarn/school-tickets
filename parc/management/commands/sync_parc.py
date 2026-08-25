# SPDX-License-Identifier: GPL-3.0-or-later
"""One inventory pass, by hand. The worker does the same thing in a loop.

``--from-file`` is not a development toy: doc 03 makes a hand-uploaded
``devices.csv`` a first-class source, the fallback where lmnapi
is out of reach. It goes through the very same reconciliation as the API, so
what is exercised here is what will run in production.
"""

from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from parc import sources, tasks
from parc.inventory import apply_inventory


class Command(BaseCommand):
    help = "Full snapshot of the estate from lmnapi (specs/03-parc-synchronisation.md)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--from-file",
            metavar="PATH",
            help="read a sophomorix devices.csv instead of calling lmnapi",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="parse and report, write nothing",
        )

    def handle(self, *args, **opts):
        if not opts["from_file"]:
            if opts["dry_run"]:
                raise CommandError("--dry-run only applies to --from-file")
            self.stdout.write(str(tasks.sync_inventory()))
            return

        path = Path(opts["from_file"])
        if not path.is_file():
            raise CommandError(f"no such file: {path}")

        # errors="replace": a stray byte in one comment must not cost the whole
        # estate. The rows themselves are ASCII in practice.
        estate = sources.parse_devices_csv(path.read_text(encoding="utf-8", errors="replace"))

        self.stdout.write(f"parsed {len(estate.rows)} device(s) from {path}")
        for reason, count in sorted(estate.reasons().items()):
            self.stdout.write(f"  skipped {count:>4}  {reason}")

        if opts["dry_run"]:
            rooms = sorted({row.room for row in estate.rows})
            self.stdout.write(f"{len(rooms)} room(s): {', '.join(rooms)}")
            self.stdout.write(self.style.WARNING("dry run -- nothing written"))
            return

        run = apply_inventory(estate.rows, source="csv_upload")

        line = f"{run.status}: {run.rooms_seen} room(s), {run.devices_seen} device(s)"
        if run.status == run.Status.REFUSED_GUARD:
            self.stdout.write(self.style.ERROR(line))
            self.stdout.write(self.style.ERROR(run.error))
            return

        pending = run.decisions.count()
        self.stdout.write(self.style.SUCCESS(line))
        if pending:
            self.stdout.write(f"{pending} decision(s) queued for review")
