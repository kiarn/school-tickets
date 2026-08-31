# SPDX-License-Identifier: GPL-3.0-or-later
"""The shipped catalogue, put in place on a fresh install.

A catalogue badge belongs to the project (D-15), so it arrives with the code
rather than being typed in: without this, the first useful screen of the
application is an empty one somebody has to fill in `/admin` before using it.

Only the identity and the structure are written -- the words stay in
`catalog.py`, looked up by slug. Later versions that add a badge do not amend
this migration; they ship their own, or an upgrade runs `manage.py
sync_badges`, which is the same operation and just as idempotent.
"""

from django.db import migrations

from badges.catalog import sync


def seed(apps, schema_editor):
    sync(model=apps.get_model("badges", "Badge"))


def unseed(apps, schema_editor):
    """Only what was never used, and only what nobody changed.

    Deleting a badge cascades to its awards, so going backwards must not take
    recognitions with it -- nor a badge a school has renamed or switched off,
    which is a decision of theirs and not this migration's to undo.
    """
    from badges.catalog import BY_SLUG

    apps.get_model("badges", "Badge").objects.filter(
        slug__in=list(BY_SLUG), is_catalog=True, is_active=True,
        name="", description="", award_count=0, awards__isnull=True,
    ).delete()


class Migration(migrations.Migration):

    dependencies = [("badges", "0006_badge_ladders")]

    operations = [migrations.RunPython(seed, unseed)]
