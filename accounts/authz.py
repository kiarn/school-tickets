# SPDX-License-Identifier: GPL-3.0-or-later
"""The authorisation scale, in one place.

See specs/08-visibilite.md and D-21. Two numeric scales answer each other:

    Visibility  = how far down the audience a ticket reaches.
    clearance   = how far down a role is allowed to read.

A role sees a ticket when ``ticket.visibility >= clearance(role)``.

Values are spaced by ten so a level can be slipped in without a data migration.
"""

from django.db import models
from django.utils.translation import gettext_lazy as _


class Visibility(models.IntegerChoices):
    ADMINS = 10, _("Admins only")
    TEAM = 20, _("Team")
    ALL = 30, _("Everyone enrolled")


class Role(models.TextChoices):
    # One administration role, not two (D-27). `global_admin` and
    # `teacher_admin` were meant to differ on who may promote whom; that
    # asymmetry was never written, and nothing in the code ever branched on it.
    ADMIN = "admin", _("Administrator")
    MEMBER = "member", _("Team member")
    REPORTER = "reporter", _("Reporter")


#: Most restrictive visibility a role is allowed to read.
CLEARANCE = {
    Role.ADMIN: Visibility.ADMINS,
    Role.MEMBER: Visibility.TEAM,
    Role.REPORTER: Visibility.ALL,
}

#: A set of one, kept as a set: every call site asks "does this role
#: administer?", which is the question that survives if a second
#: administration role ever earns its place (D-27).
ADMIN_ROLES = frozenset({Role.ADMIN})


def clearance(user) -> int:
    """The visibility floor this user may read.

    An anonymous or deactivated user reads nothing: the value returned sits
    above every existing visibility, so no ticket passes. There is no public
    level (dropped, see specs/08-visibilite.md).
    """
    if not getattr(user, "is_authenticated", False) or not user.is_active:
        return Visibility.ALL + 10
    return CLEARANCE[Role(user.role)]


def can_widen(user) -> bool:
    """Widening retroactively publishes a whole thread: admins only."""
    return getattr(user, "is_authenticated", False) and Role(user.role) in ADMIN_ROLES


#: Roles that repair: they pick tickets up and move their status.
#: ``reporter`` is deliberately absent -- reporting is not repairing.
WORKING_ROLES = ADMIN_ROLES | frozenset({Role.MEMBER})


def can_work_on(user) -> bool:
    """Whether this user may claim a ticket and move its status.

    Pupils are in: closing a ticket themselves is the pedagogical point (D-01),
    not a privilege granted to adults.
    """
    return getattr(user, "is_authenticated", False) and Role(user.role) in WORKING_ROLES


def creation_visibilities(user):
    """Every level, for everybody, when a ticket is being opened.

    Deliberately NOT ``can_restrict_to``. That rule guards against hiding a
    ticket from oneself, which cannot happen here: the author clause of
    ``visible_to()`` keeps a ticket visible to whoever opened it, whatever
    floor they picked. Applying the edit rule here would forbid a reporting
    teacher the only level they may actually want -- admins only, for a fault
    that names a pupil.
    """
    return list(Visibility.choices) if getattr(user, "is_authenticated", False) else []


def visibility_targets(user, ticket):
    """Levels an *existing* ticket may be moved to, current one included.

    Three rules meet here (doc 08, narrowed by D-41):

    - **admins move it either way.** Widening retroactively publishes every
      comment and every photo of the thread, which is why it stops there;
    - **the author may restrict their own ticket**, and only restrict. It is
      the direction that protects, and it is the one that cannot wait: a
      teacher who realises their report about a pupil is being read by the
      repair team should not have to find an admin while the thread stays open;
    - **nobody else touches it.** Until D-41 any reader could restrict any
      ticket, which made a stranger's report vanish from the team's list with
      nothing said. Reading a ticket is not being responsible for it.

    No lower bound is needed for the author: the author clause of
    ``visible_to()`` keeps a ticket visible to whoever opened it whatever floor
    they pick, so restricting to "admins only" cannot hide it from them. That
    is the same reasoning ``creation_visibilities`` already rests on.
    """
    if not getattr(user, "is_authenticated", False):
        return []
    if can_widen(user):
        return list(Visibility.choices)
    if getattr(ticket, "created_by_id", None) != user.pk:
        return []
    return [
        (value, label)
        for value, label in Visibility.choices
        if value <= ticket.visibility
    ]


#: One sentence per level, for the selector. The wording is part of the rule:
#: a pupil has to be able to tell who will read them without opening doc 08 --
#: and "everyone" has to say out loud that it does not mean the public.
VISIBILITY_HELP = {
    Visibility.ADMINS: _("Only the teacher administrators."),
    Visibility.TEAM: _("The repair team and the administrators."),
    Visibility.ALL: _("Everybody with an account here. Never the public."),
}
