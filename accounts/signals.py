# SPDX-License-Identifier: GPL-3.0-or-later
"""What happens around a login.

``last_seen_at`` has been on the model since the first migration and nothing
ever wrote to it. It is the column that answers "is this account still in use?"
before an admin removes somebody at the end of a school year.
"""

from django.contrib.auth.signals import user_logged_in
from django.dispatch import receiver
from django.utils import timezone


@receiver(user_logged_in)
def stamp_last_seen(sender, request, user, **kwargs):
    # update() rather than save(): no signals, no race with a concurrent
    # request, and nothing else on the row is touched.
    type(user).objects.filter(pk=user.pk).update(last_seen_at=timezone.now())
