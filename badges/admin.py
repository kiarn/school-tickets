# SPDX-License-Identifier: GPL-3.0-or-later
"""Two objects, two very different jobs.

The dividing line is D-15's. **The catalogue belongs to the project**: its
strings travel to Crowdin, so they are `msgid`s shipped with the code, not
text somebody types on a Tuesday. **Locally invented badges belong here** --
that is the whole reason `BadgeForm` exists in the application, and the
reason this admin refuses to create anything shared.
"""

from django import forms
from django.contrib import admin
from django.utils.translation import gettext_lazy as _

from accounts.models import AuditLog

from .forms import unique_slug
from .models import Badge, BadgeAward


class BadgeAdminForm(forms.ModelForm):
    class Meta:
        model = Badge
        fields = ["name", "description", "icon", "is_active", "order", "award_count"]
        help_texts = {
            "name": _("Leave empty to keep the wording shipped with the project, "
                      "translated. What you type here is used at this school only."),
            "description": _("Leave empty to keep the shipped wording."),
            "is_active": _("Unticking hides it from the award screen. It keeps the "
                           "badges already given -- deleting it would not."),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Required only where it is the badge's own name. On a catalogue badge
        # it is an override, and empty is its normal state -- that is what
        # makes the customisation reversible.
        shipped = self.instance.pk and self.instance.is_catalog
        if "name" in self.fields:
            self.fields["name"].required = not shipped


@admin.register(Badge)
class BadgeAdmin(admin.ModelAdmin):
    """Where a school makes the catalogue its own.

    It used to be a locked screen. A catalogue badge's `name` **was** the
    `msgid` the .po files were keyed on, so renaming one here did not fail --
    it silently orphaned every translation of it, and the only way out was to
    invent a local badge, which was then never translated at all (D-15).

    Keying the shipped text on the slug instead turned the two text fields
    into overrides, and the lock into an ordinary form: what is typed here
    stays here, and clearing a field brings the translated text back.

    ``is_catalog`` is still not offered. A shared badge means a string in the
    Crowdin catalogues, which is a decision about the project rather than
    about one afternoon in one establishment -- and a row flagged from here
    would surface in the catalogue with no shipped text behind its slug.
    """

    form = BadgeAdminForm
    list_display = ("__str__", "slug", "family", "level", "is_active", "award_count")
    list_filter = ("is_catalog", "is_active", "family")
    list_editable = ("is_active",)
    search_fields = ("slug", "name")

    # award_count is editable on purpose, catalogue badge included: it is a
    # tally, and a tally occasionally needs a human correction (R-17).
    _own = ("name", "description", "icon", "family", "level", "is_active",
            "order", "award_count")
    _read = ("slug", "is_catalog", "created_at")
    # The ladder's structure belongs to the project on the badges it ships: a
    # rung moved to another family here would part company with the catalogue
    # that defines it, and nothing would say so. A badge invented here owns
    # its own structure, so these stay editable on it.
    _shipped_structure = ("family", "level")

    fieldsets = (
        (None, {"fields": _own}),
        (_("Decided elsewhere"), {"fields": _read}),
    )
    add_fieldsets = ((None, {"fields": ("name", "description", "icon", "is_active")}),)

    def get_fieldsets(self, request, obj=None):
        return self.add_fieldsets if obj is None else self.fieldsets

    def get_readonly_fields(self, request, obj=None):
        if obj is not None and obj.is_catalog:
            return self._read + self._shipped_structure
        return self._read

    def save_model(self, request, obj, form, change):
        if not change:
            # What `BadgeForm.save()` does, and for the reasons written
            # there: a badge invented here is never shared and never
            # translated.
            obj.is_catalog = False
            obj.slug = unique_slug(obj.name)
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
    badge recognises a repair (D-10); and it wrote no
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
