# SPDX-License-Identifier: GPL-3.0-or-later
"""What the crest has to be, said once at startup rather than never.

``ST_LOGO`` is refused silently by ``branding.logo_path`` when it is not a
square PNG -- the header simply draws no crest, the manifest declares no icon,
and nothing anywhere explains why. That silence is the whole reason this file
exists: the requirement is checked once, at startup and on ``manage.py check``,
and the answer names the file and what is wrong with it.

Warnings, never errors: a wrong crest must not stop a school's ticket system
from starting on a Monday morning.
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
