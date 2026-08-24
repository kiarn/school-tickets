# SPDX-License-Identifier: GPL-3.0-or-later
"""Two objects, two very different jobs.

The dividing line is D-15's. **The catalogue belongs to the project**: its
strings travel to Crowdin, so they are `msgid`s shipped with the code, not
text a school types on a Tuesday. **A school's own badges belong here** --
that is the whole reason `BadgeForm` exists in the application, and the
reason this admin refuses to create anything shared.
"""

from django import forms
from django.contrib import admin
from django.utils.translation import gettext_lazy as _

from accounts.models import AuditLog, School

from .forms import unique_slug
from .models import Badge, BadgeAward


class BadgeAdminForm(forms.ModelForm):
    class Meta:
        model = Badge
        fields = ["name", "description", "icon", "category", "award_count"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # The column is `blank=True` because `label` falls back on the slug for
        # robustness, not because a nameless badge is a thing to create: the
        # slug is derived from the name, and an empty one derives nothing.
        #
        # Absent on a catalogue badge, whose every text field is read-only and
        # therefore not on the form at all.
        if "name" in self.fields:
            self.fields["name"].required = True


@admin.register(Badge)
class BadgeAdmin(admin.ModelAdmin):
    """A school's own badges. The catalogue is read here, never written.

    ``is_catalog`` and ``school`` are not offered at all -- the same refusal
    `BadgeForm` already makes, for the same reason, and now with a database
    constraint behind it. Left editable, they let somebody build a badge
    flagged catalogue *and* attached to a school, or one attached to none
    while not being catalogue -- which surfaced in every school's catalogue,
    untranslated, with a slug carrying no school prefix.
    """

    form = BadgeAdminForm
    list_display = ("name", "slug", "school", "is_catalog", "category", "award_count")
    list_filter = ("is_catalog", "school", "category")
    search_fields = ("slug", "name")
    ordering = ("-is_catalog", "category", "name")

    # award_count is editable on purpose, catalogue badge included: it is a
    # tally, and a tally occasionally needs a human correction (R-17).
    _own = ("name", "description", "icon", "category", "award_count")
    _read = ("slug", "school", "is_catalog", "created_at")

    fieldsets = (
        (None, {"fields": _own}),
        (_("Decided elsewhere"), {"fields": _read}),
    )
    add_fieldsets = ((None, {"fields": ("name", "description", "icon", "category")}),)

    def get_fieldsets(self, request, obj=None):
        return self.add_fieldsets if obj is None else self.fieldsets

    def get_readonly_fields(self, request, obj=None):
        if obj is not None and obj.is_catalog:
            # Its `name` and `description` are the `msgid`s the .po files are
            # keyed on. Editing one here does not translate it and does not
            # fail either -- it silently orphans every translation of it.
            # The tally is the exception: it counts what this school did.
            return self._read + ("name", "description", "icon", "category")
        return self._read

    def save_model(self, request, obj, form, change):
        if not change:
            # What `BadgeForm.save()` does, and for the reasons written there:
            # a badge invented by a school is never shared and never
            # translated, and its slug carries the school so that two schools
            # inventing the same name do not collide.
            obj.school = School.objects.default()
            obj.is_catalog = False
            obj.slug = unique_slug(obj.name, obj.school)
        super().save_model(request, obj, form, change)


@admin.register(BadgeAward)
class BadgeAwardAdmin(admin.ModelAdmin):
    """Where an award is taken back, and only that.

    Revoking recognition in front of the person who holds it is an
    administrative correction, not a screen the application invites anyone to
    use -- which is why it has no button on a profile. The docstring said as
    much before the code did: the form here also *awarded*, and awarding from
    here went around every rule `AwardForm` enforces. It set an arbitrary
    `awarded_by` where the application forces the signed-in admin; it reached
    people the application keeps out of the list, reporters included, since a
    badge recognises a repair (D-10); it crossed schools; and it wrote no
    `BADGE_AWARD` line to the audit log, where the application writes one
    every time.

    Awarding has a screen (D-24). This has a delete button.
    """

    list_display = ("badge", "user", "awarded_by", "awarded_at")
    list_filter = ("badge",)
    search_fields = ("user__cn", "user__display_name", "motivation")
    readonly_fields = ("badge", "user", "awarded_by", "awarded_at", "motivation", "ticket")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        """Nothing here is corrected; it is kept or it is taken back.

        Rewriting the sentence somebody was given, months later, is not a
        correction anybody should be able to make quietly.
        """
        return False

    def _log(self, request, awards):
        """Taking a badge back is accounted for, like giving one.

        Written from the two admin hooks rather than from a ``post_delete``
        receiver, and the distinction is the point: ``anonymize()`` deletes
        awards too (R-17), and that is an erasure, not a withdrawal. It has
        its own line -- and adding a per-badge trail pointing at somebody
        exercising their right to be forgotten would be the opposite of what
        the erasure is for.

        Identifiers only, as everywhere in this log: never the name of a third
        party, never the sentence written about them.
        """
        AuditLog.objects.bulk_create([
            AuditLog(
                actor=request.user,
                action=AuditLog.Action.BADGE_REVOKE,
                target_type="badge_award",
                # The award is about to stop existing. Keeping its identifier
                # is what pairs this line with the `badge_award` that opened
                # the story.
                target_id=award.pk,
                payload={"badge": award.badge_id, "user": award.user_id},
            )
            for award in awards
        ])

    def delete_model(self, request, obj):
        self._log(request, [obj])
        super().delete_model(request, obj)

    def delete_queryset(self, request, queryset):
        # The bulk action goes through a collector that never calls
        # ``delete_model``: without this, selecting three rows would erase
        # three recognitions and account for none.
        self._log(request, list(queryset))
        super().delete_queryset(request, queryset)
