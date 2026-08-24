# SPDX-License-Identifier: GPL-3.0-or-later
"""Where notifications are born. See specs/09-notifications.md §3 and §5.

Called explicitly from the views, **inside the transaction of the event**, and
deliberately not from signals. Two reasons:

- a signal on ``Ticket`` cannot tell *resolved* from *reopened* without
  re-deriving the state it was not given;
- these calls are the map of §3. ``grep -rn "notifications.events"`` should
  answer "what notifies whom" without reading a single receiver.

Every recipient is filtered through ``Ticket.objects.visible_to()`` here, and
filtered again at delivery -- the second pass is the guarantee (§2), this one
only avoids writing rows that would be dropped a second later.
"""

from accounts.authz import ADMIN_ROLES, WORKING_ROLES
from tickets.models import Ticket

from .models import Kind, Notification


def _may_read(user, ticket) -> bool:
    """The one authorisation rule, asked the one way.

    One query per person. A cheaper predicate in Python is easy to write and
    would be a second implementation of doc 08 -- and two implementations of an
    authorisation rule drift. The team is a handful of people.
    """
    return Ticket.objects.visible_to(user).filter(pk=ticket.pk).exists()


def _fanout(kind, *, recipients, ticket=None, actor=None) -> int:
    """Write one row per recipient, minus the person who caused the event.

    Nobody is notified of their own action: it is the single rule that keeps
    the list readable, and it has no exception.
    """
    rows = [
        Notification(recipient=person, kind=kind, ticket=ticket, actor=actor)
        for person in recipients
        if actor is None or person.pk != actor.pk
    ]
    Notification.objects.bulk_create(rows)
    return len(rows)


def _team(school):
    """The people who repair. `reporter` is absent, and that is doc 09 §3:
    a teacher who reported a projector has no use for the estate's traffic."""
    from accounts.models import User

    return User.objects.filter(
        school=school, is_active=True, role__in=[role.value for role in WORKING_ROLES]
    )


def ticket_opened(ticket) -> int:
    """The recruitment notification: without it a ticket reaches nobody."""
    recipients = [
        person for person in _team(ticket.school) if _may_read(person, ticket)
    ]
    return _fanout(Kind.TICKET_OPENED, recipients=recipients, ticket=ticket,
                   actor=ticket.created_by)


def assigned(ticket, people, *, by) -> int:
    """Someone put this ticket in your hands. The strongest signal there is."""
    recipients = [person for person in people if _may_read(person, ticket)]
    return _fanout(Kind.ASSIGNED, recipients=recipients, ticket=ticket, actor=by)


def commented(comment) -> int:
    ticket = comment.ticket
    return _fanout(
        Kind.COMMENT,
        recipients=_involved(ticket),
        ticket=ticket,
        actor=comment.author,
    )


def status_changed(ticket, *, actor, previous) -> int:
    """Only two transitions carry an action; the rest is bookkeeping (§4)."""
    if ticket.status == Ticket.Status.RESOLVED:
        kind = Kind.RESOLVED
    elif previous == Ticket.Status.RESOLVED and ticket.status == Ticket.Status.OPEN:
        kind = Kind.REOPENED
    else:
        return 0
    return _fanout(kind, recipients=_involved(ticket), ticket=ticket, actor=actor)


def _involved(ticket):
    """The author and the assignees.

    The author matters most on a resolution: it is what closes the teaching
    loop, since only they can say "no, it started again" (§3).
    """
    people = {person.pk: person for person in ticket.assignees.all()}
    people.setdefault(ticket.created_by_id, ticket.created_by)
    return [person for person in people.values() if _may_read(person, ticket)]


def sync_decisions_pending(school, *, count) -> int:
    """The only event with no ticket behind it. Admins only: they are the only
    people who can act on the queue (doc 03)."""
    from accounts.models import User

    if not count:
        return 0
    admins = User.objects.filter(
        school=school, is_active=True, role__in=[role.value for role in ADMIN_ROLES]
    )
    # One pending queue, one notification per pass: re-notifying every hour
    # about the same unread queue is how a channel gets muted.
    fresh = [
        person for person in admins
        if not Notification.objects.filter(
            recipient=person, kind=Kind.SYNC_DECISIONS, read_at__isnull=True
        ).exists()
    ]
    return _fanout(Kind.SYNC_DECISIONS, recipients=fresh)
