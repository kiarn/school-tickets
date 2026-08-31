# SPDX-License-Identifier: GPL-3.0-or-later
"""Recognition (D-10): awarded by an adult, never by an algorithm.

Accepted corollary: no leaderboard. Ranking pupils by tickets closed would push
towards taking the easy faults and skipping the full retest.
"""

from django.conf import settings
from django.db import models
from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver
from django.utils.translation import gettext_lazy as _

from .catalog import BY_SLUG, METAL_LABELS, METALS, Family


class Badge(models.Model):
    """A row is an identity plus what one school decided about it.

    The words are not here. A catalogue badge's name and description are
    shipped in ``catalog.py``, looked up by ``slug`` -- and the two text
    columns below hold an **override**, empty by default. That indirection is
    the whole point: the name used to *be* the `msgid` the .po files were keyed
    on, so renaming a badge in /admin orphaned every translation of it without
    failing, and the admin had to lock the field to stop it. Keyed on the slug,
    a rename is local, reversible, and cannot reach the catalogues at all.
    """

    slug = models.SlugField(unique=True)
    # When true: the words come from `catalog.py` and travel to Crowdin (D-15).
    is_catalog = models.BooleanField(default=False)
    # Empty on a catalogue badge unless somebody here wanted other words.
    # Clearing it restores the shipped, translated text.
    name = models.CharField(max_length=200, blank=True, verbose_name=_("Name"))
    description = models.TextField(blank=True, verbose_name=_("Description"))
    icon = models.CharField(max_length=100, blank=True)
    # The ladder this badge belongs to, and which rung. Level 0 is a badge
    # earned once, which is not a rung and wears no metal.
    family = models.CharField(max_length=20, blank=True, choices=Family.choices)
    level = models.PositiveSmallIntegerField(default=0)
    # Pruning without deleting. Deleting cascades to `BadgeAward`, so removing
    # a badge from the list on offer used to mean erasing the recognitions
    # already given with it.
    is_active = models.BooleanField(default=True, verbose_name=_("Offered"))
    # Sorting on the name would sort the German, and a ladder read
    # `Anfänger, Experte, Fortgeschritten, Profi` is no longer a ladder.
    order = models.PositiveSmallIntegerField(default=0)
    # How many times this badge has been given, ever (R-17). It exists
    # because an award is DELETED when its holder is
    # anonymised: without a counter, an erasure would also erase the record
    # that the badge had ever been earned. Not a leaderboard --
    # counting badges says nothing about which pupil holds them (D-10).
    award_count = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        # `name` used to be unique, back when it was the badge's identity. It
        # is now an override that is blank on nearly every row, and a
        # non-conditional unique index -- the only kind MariaDB gives us
        # (D-03) -- would collide on the second empty one. The identity moved
        # to `slug`, which is unique already.
        ordering = ["order", "name", "slug"]

    def __str__(self):
        return self.label

    @property
    def label(self) -> str:
        """This school's words, else the shipped ones, else the slug.

        The order is the design. An override wins because somebody here typed
        it on purpose; emptying the field falls back through to the translated
        catalogue text, which is what makes the customisation reversible.

        The last fallback is not decoration: a badge whose slug has dropped out
        of a later version's catalogue still has to draw something on the
        profile of the person holding it.
        """
        if self.name:
            return self.name
        shipped = BY_SLUG.get(self.slug)
        if self.is_catalog and shipped is not None:
            return str(shipped.name)
        return self.slug

    @property
    def summary(self) -> str:
        if self.description:
            return self.description
        shipped = BY_SLUG.get(self.slug)
        if self.is_catalog and shipped is not None:
            return str(shipped.description)
        return ""

    @property
    def family_label(self) -> str:
        """The ladder's name, translated. Empty for a badge earned once.

        It replaced a free-text `category`: two fields saying the same thing,
        one of them closed and one of them not, made the admin screen a puzzle
        -- and a category typed by hand was never translated either.
        """
        return str(Family(self.family).label) if self.family else ""

    @property
    def metal(self) -> str:
        """bronze / silver / gold, or nothing for a badge earned once."""
        return METALS.get(self.level, "")

    @property
    def metal_label(self) -> str:
        return str(METAL_LABELS[self.level]) if self.level in METAL_LABELS else ""


class BadgeAward(models.Model):
    badge = models.ForeignKey(Badge, on_delete=models.CASCADE, related_name="awards")
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="badge_awards"
    )
    awarded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="+"
    )
    awarded_at = models.DateTimeField(auto_now_add=True)
    motivation = models.TextField(blank=True)  # the sentence the pupil will re-read
    # The intervention that earned it: turns the badge into visible evidence.
    ticket = models.ForeignKey(
        "tickets.Ticket", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["badge", "user"], name="unique_badge_per_user")
        ]


# Kept in models.py rather than a signals module: this app has one behaviour
# beyond its schema, and the receivers are the schema's other half.
#
# Signals rather than an override of ``save``/``delete``: the Django admin
# deletes through a collector, which never calls ``Model.delete()``. An award
# taken back there has to decrement too, or the count drifts.


@receiver(post_save, sender=BadgeAward)
def _count_award(sender, instance, created, **kwargs):
    if created:
        Badge.objects.filter(pk=instance.badge_id).update(
            award_count=models.F("award_count") + 1
        )


@receiver(post_delete, sender=BadgeAward)
def _uncount_award(sender, instance, **kwargs):
    """Decrement -- unless the holder was anonymised.

    That is the whole distinction R-17 rests on. An award withdrawn by an
    admin was a mistake, so it should never have been counted. An award that
    disappears because somebody exercised their right to erasure did happen,
    and the record that it happened survives -- just not the name.
    """
    holder = getattr(instance, "user", None)
    if holder is not None and holder.anonymized_at:
        return
    Badge.objects.filter(pk=instance.badge_id, award_count__gt=0).update(
        award_count=models.F("award_count") - 1
    )
