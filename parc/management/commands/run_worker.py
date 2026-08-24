# SPDX-License-Identifier: GPL-3.0-or-later
"""The worker (D-20): synchronisation loop, no listening port.

Started by ``school-tickets-worker.service``. It is the only process holding
the lmnapi secret; the web front end shares this code but not the environment
variable.

    manage.py run_worker              # loop, systemd service
    manage.py run_worker --once       # one pass of every due job, then exit
    manage.py run_worker --job linbo  # a single job
"""

import logging
import time

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import close_old_connections
from django.utils import timezone

from parc.jobs import build_jobs
from parc.lmnapi import RateLimited
from parc.models import WorkerHeartbeat

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "lmnapi synchronisation loop. Serves no page."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true")
        parser.add_argument("--job", default=None)

    def handle(self, *args, **opts):
        jobs = build_jobs(opts["job"])
        logger.info("worker started: %s", ", ".join(j.name for j in jobs))

        while True:
            # Outside the request/response cycle Django never closes its
            # connection, and MariaDB drops it after wait_timeout. The fault
            # only shows after eight hours: never during tests, always on the
            # first morning in production.
            close_old_connections()

            now = time.monotonic()
            ran = {}
            for job in jobs:
                if not job.due(now):
                    continue
                ran[job.name] = self._run(job, now)

            WorkerHeartbeat.objects.create(at=timezone.now(), jobs_ran=ran)

            if opts["once"]:
                return
            time.sleep(settings.ST_WORKER_TICK)

    def _run(self, job, now):
        try:
            result = job.fn() or {}
        except RateLimited as exc:
            # State lives between passes because the process lives between
            # passes: that is the whole difference with a scheduled task.
            job.backoff = max(exc.retry_after, min((job.backoff * 2) or 60, 3600))
            job.next_run = now + job.backoff
            logger.warning("%s: 429, next attempt in %.0fs", job.name, job.backoff)
            return {"rate_limited": job.backoff}
        except Exception:
            job.failures += 1
            job.next_run = now + min(60 * 2**job.failures, 3600)
            logger.exception("%s: failure #%d", job.name, job.failures)
            return {"failed": job.failures}

        job.failures = 0
        job.backoff = 0
        # The next due time starts from the END of the pass: two runs can never
        # overlap, where a timer would start a second one on top of the first.
        job.next_run = time.monotonic() + job.every
        job.last_result = result
        return result
