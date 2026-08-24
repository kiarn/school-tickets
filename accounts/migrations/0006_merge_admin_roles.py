# SPDX-License-Identifier: GPL-3.0-or-later
"""Fold the two administration roles into one, and make them superusers (D-27).

``global_admin`` and ``teacher_admin`` were meant to differ on who may promote
whom. That asymmetry was never written and nothing ever branched on it, so the
two values were the same value under two names.

The second half is the one to read twice: every account that held either role
becomes a **Django superuser**. That is the decision, not a side effect: an
administrator of the school is an administrator of the database. It is also
what makes ``has_perm()`` answer at all: ``is_staff``
followed the role while permissions did not, so an administrator could open
``/admin/`` and find it empty.
"""

from django.db import migrations

OLD = ["global_admin", "teacher_admin"]


def merge(apps, schema_editor):
    User = apps.get_model("accounts", "User")
    User.objects.filter(role__in=OLD).update(role="admin", is_superuser=True)


def split(apps, schema_editor):
    """Reversible into the role that carried the same meaning, not into both."""
    User = apps.get_model("accounts", "User")
    User.objects.filter(role="admin").update(role="global_admin")


class Migration(migrations.Migration):
    dependencies = [("accounts", "0005_alter_user_role")]
    operations = [migrations.RunPython(merge, split)]
