# SPDX-License-Identifier: GPL-3.0-or-later
"""The badges shipped with the project, defined in code rather than in rows.

They live here because of what D-15 asks of them: a catalogue badge's strings
are `msgid`s that travel to Crowdin, and ``makemessages`` only ever finds a
string written in a source file. Text typed into ``/admin`` cannot be
extracted, which is why a catalogue that lived only in the database could never
have been translated at all.

The database keeps the ``slug`` and what an establishment decides about it --
whether it is offered, and the words it prefers. The join between the two is
the slug, and only the slug: it is an identity, never a label, so a rung
carries its number rather than the name of its rank. Renaming a rank is then a
translation change and touches nothing that has already been awarded.

A slug that disappears from here in a later version leaves its row behind on
purpose: the awards made under it happened, and ``Badge.label`` falls back on
what the school last saw.
"""

from typing import NamedTuple

from django.db import models
from django.utils.translation import gettext_lazy as _


class Family(models.TextChoices):
    """A ladder -- or, for ``NONE``, a badge earned once.

    The value is the slug prefix, and the order of declaration is the order the
    catalogue is shown in. That order is copied to ``Badge.order`` when the
    catalogue is synchronised, because sorting on the displayed name would sort
    the German: `Anfänger, Experte, Fortgeschritten, Profi`.
    """

    NETWORK = "network", _("Network")
    WORKSTATION = "workstation", _("Workstation")
    PRESENTATION = "presentation", _("Presentation equipment")
    PRINTING = "printing", _("Printing")
    INVENTORY = "inventory", _("Workshop and inventory")
    METHOD = "method", _("Diagnosis and method")
    NONE = "", ""


# Level 0 is "not a rung": a badge earned once, which no metal applies to.
#
# The metal is derived and never stored. A level is a fact about the ladder; a
# colour is a decision about the theme, and storing the second would freeze it
# into every row on the day the palette changes.
METALS = {1: "bronze", 2: "silver", 3: "gold"}

# Shown on the card, and the reason the rung's number never is: "Silber" says
# where you stand without anybody having to know how many rungs there are.
METAL_LABELS = {1: _("Bronze"), 2: _("Silver"), 3: _("Gold")}


class Shipped(NamedTuple):
    slug: str
    family: str
    level: int
    icon: str
    name: str
    description: str


def _rung(family, level, icon, name, description):
    return Shipped(f"{family}-{level}", family, level, icon, name, description)


def _once(slug, icon, name, description):
    return Shipped(slug, Family.NONE.value, 0, icon, name, description)


# The rungs read from the bottom up, and the top one is deliberately not the
# largest intervention: pupils have no access to the switches, to the server or
# to an image. What a ladder can ask of them at the top is the quality of the
# diagnosis and of the hand-over -- knowing where their reach ends, and passing
# the fault on well enough that nobody has to test it again.
CATALOG = [
    _rung(Family.NETWORK, 1, "🔌", _("Network · Beginner"),
          _("You say whether the fault is at the machine or at the socket: another "
            "cable, another machine, and you describe what you see.")),
    _rung(Family.NETWORK, 2, "🔌", _("Network · Advanced"),
          _("You follow a line from the socket to the patch panel, find the dead "
            "socket and swap a patch cable.")),
    _rung(Family.NETWORK, 3, "🔌", _("Network · Pro"),
          _("You recognise a fault that reaches beyond one workstation, and hand it "
            "on precisely enough that it can be acted on without new tests.")),

    _rung(Family.WORKSTATION, 1, "🧰", _("Workstation · Beginner"),
          _("You replace a device at a workstation and check the whole place "
            "afterwards.")),
    _rung(Family.WORKSTATION, 2, "🧰", _("Workstation · Advanced"),
          _("You open a machine, replace a component, put it back together and test "
            "it.")),
    _rung(Family.WORKSTATION, 3, "🧰", _("Workstation · Pro"),
          _("You set up a complete workstation, sync it and hand it over ready to "
            "use.")),

    _rung(Family.PRESENTATION, 1, "📽️", _("Presentation equipment · Beginner"),
          _("You get the picture back: source, cable, input.")),
    _rung(Family.PRESENTATION, 2, "📽️", _("Presentation equipment · Advanced"),
          _("You set resolution and sound, and find a cable that only fails now and "
            "then.")),
    _rung(Family.PRESENTATION, 3, "📽️", _("Presentation equipment · Pro"),
          _("You mount or realign a projector and bring a whole room back into "
            "service.")),

    _rung(Family.PRINTING, 1, "🖨️", _("Printing · Beginner"),
          _("Paper, toner, a simple jam: you get the printer running again and check "
            "it with a real printout.")),
    _rung(Family.PRINTING, 2, "🖨️", _("Printing · Advanced"),
          _("A stuck queue, one machine that no longer prints while the others do, a "
            "jam deep inside.")),
    _rung(Family.PRINTING, 3, "🖨️", _("Printing · Pro"),
          _("You tell the printer apart from the print server, and say what you have "
            "already ruled out.")),

    _rung(Family.INVENTORY, 1, "📋", _("Workshop and inventory · Beginner"),
          _("You label a device and enter it in the inventory: number, room, "
            "condition.")),
    _rung(Family.INVENTORY, 2, "📋", _("Workshop and inventory · Advanced"),
          _("You make new computers and tablets ready for service, and record what "
            "was never in the inventory — switches, access points.")),
    _rung(Family.INVENTORY, 3, "📋", _("Workshop and inventory · Pro"),
          _("You retire a device properly: wipe the disk, remove what identifies the "
            "school, and write down that you did both.")),

    _rung(Family.METHOD, 1, "🔍", _("Diagnosis and method · Beginner"),
          _("You reproduce the fault before touching anything, and describe what you "
            "see.")),
    _rung(Family.METHOD, 2, "🔍", _("Diagnosis and method · Advanced"),
          _("You test the whole workstation once more before closing, and write the "
            "solution down for whoever comes next.")),
    _rung(Family.METHOD, 3, "🔍", _("Diagnosis and method · Pro"),
          _("You take over a ticket whose first lead was wrong, and find the real "
            "cause.")),

    _once("first-ticket", "★", _("First solved ticket"),
          _("Your first fault, from the report all the way to the hand-over.")),
    _once("well-explained", "🤝", _("Well explained"),
          _("You handed the workstation back and explained what had happened — so "
            "that it was understood.")),
    _once("regular", "🗓️", _("Always there"),
          _("You are in the workshop regularly over a long stretch, whatever the "
            "faults happen to be.")),
]

BY_SLUG = {entry.slug: entry for entry in CATALOG}


def sync(model=None):
    """Bring the shipped catalogue into the database, without ever overriding.

    Idempotent, and deliberately conservative: it creates what is missing and
    refreshes only what the project owns -- the ladder's structure. It never
    touches ``name``, ``description`` or ``is_active``, which is where an
    establishment's own decisions live. Deactivating a badge, or renaming it,
    therefore survives every upgrade.

    It also never deletes: a badge dropped from a later version keeps its row,
    and with it the awards already made under it.

    ``model`` is how the data migration passes its historical ``Badge`` in,
    rather than this reaching for the live one behind its back.
    """
    if model is None:
        from .models import Badge as model

    created = 0
    for order, entry in enumerate(CATALOG):
        structure = {
            "is_catalog": True,
            "family": entry.family,
            "level": entry.level,
            "order": order,
        }
        # The icon is set once and then left alone: it is drawn on the card
        # next to the name, so it belongs with the words a school is free to
        # change, not with the structure the project owns.
        _, is_new = model.objects.update_or_create(
            slug=entry.slug,
            defaults=structure,
            create_defaults={**structure, "icon": entry.icon},
        )
        created += is_new
    return created
