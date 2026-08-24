# SPDX-License-Identifier: GPL-3.0-or-later
"""Recognition (D-10): awarded by an adult, never by an algorithm.

Accepted corollary: no leaderboard. Ranking pupils by tickets closed would push
towards taking the easy faults and skipping the full retest.
"""

from django.conf import settings
from django.db import models
from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver
from django.utils.translation import gettext


class Badge(models.Model):
    slug = models.SlugField(unique=True)
    # When true: label resolved through gettext and translated via Crowdin (D-15).
    is_catalog = models.BooleanField(default=False)
    # NULL = catalogue badge, shared by every school.
    school = models.ForeignKey(
        "accounts.School", on_delete=models.CASCADE, null=True, blank=True, related_name="badges"
    )
    name = models.CharField(max_length=200, blank=True)
    description = models.TextField(blank=True)
    icon = models.CharField(max_length=100, blank=True)
    category = models.CharField(max_length=100, blank=True)
    # How many times this badge has been given, ever (R-17, answered
    # 2026-08-24). It exists because an award is DELETED when its holder is
    # anonymised: without a counter, an erasure would also erase the school's
    # own record that the badge had ever been earned. Not a leaderboard --
    # counting badges says nothing about which pupil holds them (D-10).
    award_count = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.label

    @property
    def label(self) -> str:
        """A catalogue badge is translated; a school's own badge never is.

        The distinction is D-15's: catalogue strings travel to Crowdin, and a
        badge invented by one school has no business in a shared catalogue --
        nor would anybody translate it.
        """
        if self.is_catalog and self.name:
            return gettext(self.name)
        return self.name or self.slug

    @property
    def summary(self) -> str:
        if self.is_catalog and self.description:
            return gettext(self.description)
        return self.description


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
    and the school keeps the fact that it happened -- just not the name.
    """
    holder = getattr(instance, "user", None)
    if holder is not None and holder.anonymized_at:
        return
    Badge.objects.filter(pk=instance.badge_id, award_count__gt=0).update(
        award_count=models.F("award_count") - 1
    )
