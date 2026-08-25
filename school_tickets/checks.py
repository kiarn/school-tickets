# SPDX-License-Identifier: GPL-3.0-or-later
"""What an installation has to look like, said once at startup rather than never.

Both checks here guard the same failure shape: something is configured, the
application quietly does without it, and **nothing anywhere explains why**. A
crest that is not a square PNG is refused by ``branding.logo_path`` -- the
header simply draws no crest and the manifest declares no icon. A catalogue
that was never compiled leaves every page in English on a German-speaking
school's server, and gettext says nothing about it either.

Warnings, never errors: neither a wrong crest nor a missing catalogue must stop
a school's ticket system from starting on a Monday morning.
"""

from pathlib import Path

from django.conf import settings
from django.core.checks import Warning, register
from PIL import Image, UnidentifiedImageError

from . import branding


@register()
def crest_is_a_square_png(app_configs, **kwargs):
    """A crest that will not be served is a crest whose owner must hear about it."""
    if not settings.ST_LOGO:
        # The normal state of a fresh install (D-30), not a misconfiguration.
        return []

    path = Path(settings.ST_LOGO)
    if path.suffix.lower() != branding.SUFFIX:
        return [Warning(
            f"ST_LOGO is not a PNG: {path}",
            hint="Only PNG is served (D-33): iOS reads no SVG, and the home "
                 "screen icons are resized from this one file.",
            id="school_tickets.W001",
        )]
    if not path.is_file():
        return [Warning(
            f"ST_LOGO points at no file: {path}",
            hint="Inside the container, if this instance runs in one: the file "
                 "has to be bind-mounted before the path resolves.",
            id="school_tickets.W002",
        )]
    try:
        with Image.open(path) as image:
            width, height = image.size
    except (OSError, UnidentifiedImageError):
        return [Warning(
            f"ST_LOGO is not a readable image: {path}",
            hint="A .png suffix is not a PNG file.",
            id="school_tickets.W003",
        )]
    if width != height:
        return [Warning(
            f"ST_LOGO is {width}x{height}, not square: {path}",
            hint="Home screen icons are square. This one is fitted and centred "
                 "on transparency rather than cropped, so it will install with "
                 "empty bands. A square crest of 512 px or more avoids them.",
            id="school_tickets.W004",
        )]
    if width < max(branding.ICON_SIZES):
        return [Warning(
            f"ST_LOGO is {width} px, smaller than the {max(branding.ICON_SIZES)} px "
            f"icon a home screen asks for: {path}",
            hint="It is scaled up, and it will look it.",
            id="school_tickets.W005",
        )]
    return []


@register()
def catalogues_are_compiled(app_configs, **kwargs):
    """A translation nobody compiled is a translation nobody sees.

    ``.mo`` files are build artefacts and stay out of the repository (D-34):
    they derive from the ``.po`` by one deterministic command, and a binary in
    git is a second source of truth that can silently drift from the first.
    The cost of that choice is exactly this failure -- deploy without running
    ``compilemessages`` and the application comes out in English, with no error
    and no log line -- so the cost is paid here instead.

    English is skipped: its ``msgstr`` are empty on purpose, the msgid being
    the English already (D-23).
    """
    missing = []
    for code, _name in settings.LANGUAGES:
        if code == "en":
            continue
        for root in settings.LOCALE_PATHS:
            source = Path(root) / code / "LC_MESSAGES" / "django.po"
            if source.is_file() and not source.with_suffix(".mo").is_file():
                missing.append(code)
    if not missing:
        return []
    return [Warning(
        "Translations are not compiled: %s" % ", ".join(sorted(set(missing))),
        hint="Run `manage.py compilemessages -i .venv`. Without it every page "
             "renders in English, whatever LANGUAGE_CODE says.",
        id="school_tickets.W006",
    )]
