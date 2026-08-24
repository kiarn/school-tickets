# SPDX-License-Identifier: GPL-3.0-or-later
"""The unread count, on every page.

One indexed count per request, for a badge that has to be right everywhere or
it is worse than absent. The index it uses is ``(recipient, read_at)``.
"""

from .models import Notification


def unread(request):
    user = getattr(request, "user", None)
    if not getattr(user, "is_authenticated", False):
        return {}
    return {"unread_count": Notification.objects.filter(
        recipient=user, read_at__isnull=True
    ).count()}
