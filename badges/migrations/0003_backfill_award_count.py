# SPDX-License-Identifier: GPL-3.0-or-later
"""Set the tally to what the awards already say (R-17).

The column arrives after the awards do, on an installation that has been in
use. Counting once here is the only moment the two can be reconciled: from now
on the receivers in ``badges/models.py`` keep them in step, and an award lost
to an anonymisation is deliberately not subtracted.
"""

from django.db import migrations
from django.db.models import Count


def count_existing(apps, schema_editor):
    Badge = apps.get_model("badges", "Badge")
    for row in Badge.objects.annotate(total=Count("awards")).filter(total__gt=0):
        Badge.objects.filter(pk=row.pk).update(award_count=row.total)


def forget(apps, schema_editor):
    apps.get_model("badges", "Badge").objects.update(award_count=0)


class Migration(migrations.Migration):
    dependencies = [("badges", "0002_badge_award_count")]
    operations = [migrations.RunPython(count_existing, forget)]
