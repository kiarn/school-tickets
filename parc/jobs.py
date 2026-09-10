# SPDX-License-Identifier: GPL-3.0-or-later
"""The worker's four jobs: how often each comes round, and when it may run.

A cadence and a window are not the same thing. ``every`` says how often a job
comes back; the window says whether it is allowed to do anything when it does.
They are separate because the reason differs for each job (D-20):

``refresh`` -- every 30s, no window. The web service is forbidden from calling
lmnapi at all, so "refresh now" cannot be a request: the page writes
``refresh_requested_at`` and waits for the worker to notice. Thirty seconds is
what makes that button feel like a button.

``notify`` -- its own window, and pointedly not the estate's. It answers "may
a notification reach somebody's phone right now", which is a different
question from "are the machines on"; sharing one window would silence the
evening instructions the application exists to carry.

``linbo`` -- hourly, open hours only. A workstation that is switched off
reports no LINBO state, and reading that absence as a fault would post an
estate of false alarms every night and all weekend.

``inventory`` -- nightly, out of hours. A full snapshot of every room and
machine, and the one pass that can queue decisions for a human; nothing about
it is urgent enough to spend a school day on.
"""

import logging
from dataclasses import dataclass, field
from typing import Callable

from django.conf import settings
from django.utils import timezone

logger = logging.getLogger(__name__)


def is_open_hours() -> bool:
    """Workstations do not sync while the school is closed.

    ``localtime`` and not ``datetime.now``: otherwise the window drifts by an
    hour between winter and summer.
    """
    now = timezone.localtime()
    start, end = settings.ST_OPENING_HOURS
    return now.weekday() in settings.ST_OPENING_WEEKDAYS and start <= now.hour < end


def is_out_of_hours() -> bool:
    return not is_open_hours()


def is_notify_hours() -> bool:
    """When a notification may reach a phone -- not when machines are on.

    Deliberately a second window rather than a reuse of the first: the two
    answer different questions, and merging them would silence the evening
    instructions doc 00 describes. See specs/09-notifications.md §6.
    """
    now = timezone.localtime()
    start, end = settings.ST_NOTIFY_HOURS
    return now.weekday() in settings.ST_NOTIFY_WEEKDAYS and start <= now.hour < end


@dataclass
class Job:
    name: str
    every: int
    fn: Callable[[], dict]
    window: Callable[[], bool] = lambda: True
    next_run: float = 0.0
    backoff: float = 0.0
    failures: int = 0
    last_result: dict = field(default_factory=dict)

    def due(self, now: float) -> bool:
        return now >= self.next_run and self.window()


def build_jobs(only: str | None = None) -> list[Job]:
    from parc import tasks

    from notifications import delivery

    jobs = [
        Job("refresh", settings.ST_WORKER_REFRESH_EVERY, tasks.drain_refresh_queue),
        Job(
            "notify",
            settings.ST_WORKER_NOTIFY_EVERY,
            delivery.deliver_pending,
            window=is_notify_hours,
        ),
        Job("linbo", settings.ST_WORKER_LINBO_EVERY, tasks.sweep_linbo, window=is_open_hours),
        Job(
            "inventory",
            settings.ST_WORKER_INVENTORY_EVERY,
            tasks.sync_inventory,
            window=is_out_of_hours,
        ),
    ]
    if only:
        jobs = [j for j in jobs if j.name == only]
        if not jobs:
            raise ValueError(f"unknown job: {only}")
    return jobs
