# SPDX-License-Identifier: GPL-3.0-or-later
"""The words a school chose, per language rather than once.

A single override was served in all three: renaming a rung in German renamed
it in French too. The two columns become dictionaries keyed by language code,
and what was already typed is carried over as the site's own language --
which is what it was, implicitly.

The order below is the point: the new columns are filled from the old ones
before those are dropped. Generated the other way round, the migration was
correct and lost every name.
"""

from django.conf import settings
from django.db import migrations, models


def spread(apps, schema_editor):
    Badge = apps.get_model("badges", "Badge")
    for badge in Badge.objects.exclude(name="", description=""):
        badge.names = {settings.LANGUAGE_CODE: badge.name} if badge.name else {}
        badge.descriptions = (
            {settings.LANGUAGE_CODE: badge.description} if badge.description else {}
        )
        badge.save(update_fields=["names", "descriptions"])


def collapse(apps, schema_editor):
    """Backwards: keep the site's language, or the first thing filled."""
    Badge = apps.get_model("badges", "Badge")
    for badge in Badge.objects.all():
        for column, texts in (("name", badge.names), ("description", badge.descriptions)):
            chosen = (texts or {}).get(settings.LANGUAGE_CODE) or next(
                (text for text in (texts or {}).values() if text), ""
            )
            setattr(badge, column, chosen)
        badge.save(update_fields=["name", "description"])


class Migration(migrations.Migration):

    dependencies = [("badges", "0007_seed_catalog")]

    operations = [
        migrations.AddField(
            model_name="badge",
            name="names",
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.AddField(
            model_name="badge",
            name="descriptions",
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.RunPython(spread, collapse),
        migrations.RemoveField(model_name="badge", name="name"),
        migrations.RemoveField(model_name="badge", name="description"),
        migrations.AlterModelOptions(
            name="badge",
            options={"ordering": ["order", "slug"]},
        ),
    ]
