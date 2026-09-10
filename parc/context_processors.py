# SPDX-License-Identifier: GPL-3.0-or-later
"""Whether to offer the estate in the menu.

No query: the answer is the role already on the request. A context processor
rather than a per-view variable because the menu is drawn by ``base.html`` on
every page, and a link that appears on some of them is worse than none.
"""

from accounts.authz import can_work_on


def estate_access(request):
    return {"can_see_estate": can_work_on(getattr(request, "user", None))}
