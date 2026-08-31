# SPDX-License-Identifier: GPL-3.0-or-later
"""Recognition (D-10): awarded by an adult, never by an algorithm.

Accepted corollary: no leaderboard. Ranking pupils by tickets closed would push
towards taking the easy faults and skipping the full retest.
"""

from django.conf import settings
from django.db import models
from django.utils.translation import get_language
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
    # The words this school chose, per language: {"de": "...", "fr": "..."}.
    # Empty on a catalogue badge unless somebody here wanted other words, and
    # emptying one language restores the shipped, translated text for it.
    #
    # JSON rather than a column per language: the project ships three today
    # and Crowdin may bring more, and a language should not cost a migration
    # in two models. Nothing is required -- an admin who fills German and
    # leaves French empty gets German in French, which is their business and
    # is fixed by typing in the empty box.
    names = models.JSONField(default=dict, blank=True)
    descriptions = models.JSONField(default=dict, blank=True)
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
        ordering = ["order", "slug"]

    def __str__(self):
        return self.label

    @staticmethod
    def in_language(texts) -> str:
        """One string out of the per-language dictionary, or "".

        The reading language first, then the site's own, then whatever is
        filled. That last step is the one worth explaining: a badge invented
        here may exist in one language only, and answering a French reader
        with a slug because the French box is empty would be worse than
        answering them in German.
        """
        if not texts:
            return ""
        for code in (get_language(), settings.LANGUAGE_CODE):
            if code and texts.get(code):
                return texts[code]
        return next((text for text in texts.values() if text), "")

    def _shipped(self):
        return BY_SLUG.get(self.slug) if self.is_catalog else None

    @property
    def label(self) -> str:
        """This school's words, else the shipped ones, else the slug.

        The order is the design. An override wins because somebody here typed
        it on purpose; emptying it falls back through to the translated
        catalogue text, which is what makes the customisation reversible.

        The last fallback is not decoration: a badge whose slug has dropped out
        of a later version's catalogue still has to draw something on the
        profile of the person holding it.
        """
        chosen = self.in_language(self.names)
        if chosen:
            return chosen
        shipped = self._shipped()
        return str(shipped.name) if shipped else self.slug

    @property
    def summary(self) -> str:
        chosen = self.in_language(self.descriptions)
        if chosen:
            return chosen
        shipped = self._shipped()
        return str(shipped.description) if shipped else ""

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
