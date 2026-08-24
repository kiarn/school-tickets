# SPDX-License-Identifier: GPL-3.0-or-later
"""A tombstone's ``cn`` becomes NULL, and the constraint drops its condition.

The condition -- unique among the non-anonymised -- is a partial index, and
MariaDB has none: Django warned and created nothing, so the rule held in
SQLite and was absent in production. Carrying the exception in the data
instead costs one NULL and behaves identically on both engines.
"""

from django.db import migrations, models


def empty_cn_becomes_null(apps, schema_editor):
    """Existing tombstones, before the new constraint can see them.

    Two of them in one school would each read ``(school, "")`` and collide.
    An empty ``cn`` is a tombstone by construction: enrolment requires one,
    through the command and through the admin alike.
    """
    apps.get_model("accounts", "User").objects.filter(cn="").update(cn=None)


def null_cn_becomes_empty(apps, schema_editor):
    apps.get_model("accounts", "User").objects.filter(cn=None).update(cn="")


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0007_alter_auditlog_action"),
        ("auth", "0012_alter_user_first_name_max_length"),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name="user",
            name="unique_cn_per_school_while_named",
        ),
        migrations.AlterField(
            model_name="user",
            name="cn",
            field=models.CharField(blank=True, default=None, max_length=150, null=True),
        ),
        # Between the two on purpose: the column has to accept NULL before the
        # data can hold any, and the data has to be clean before the
        # constraint is asked to police it.
        migrations.RunPython(empty_cn_becomes_null, null_cn_becomes_empty),
        migrations.AddConstraint(
            model_name="user",
            constraint=models.UniqueConstraint(
                fields=("school", "cn"), name="unique_cn_per_school"
            ),
        ),
    ]
