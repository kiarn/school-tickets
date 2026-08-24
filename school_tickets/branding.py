# SPDX-License-Identifier: GPL-3.0-or-later
"""The name and the crest of the one school this instance serves.

D-30 settles what this is: **instance configuration**, in the line of the
other ``ST_*`` variables, and not a column on ``School``. One instance per
establishment means there is nothing to look up and nothing to disambiguate --
including on the login page, which used to be the awkward case precisely
because it renders before anybody is known.

The logo is a path rather than a static file on purpose. A crest belongs to
the school, not to the package: asking a sysadmin to point at a file they
already have beats asking them to drop it into a directory ``collectstatic``
owns and then rebuild.
"""

from pathlib import Path

from django.conf import settings
from django.http import FileResponse, Http404

#: What the view will serve, and nothing else. Both render at any size, which
#: is what a header, a favicon and a notification icon each need at once.
TYPES = {".svg": "image/svg+xml", ".png": "image/png"}


def identity(request):
    """Name and crest on every page, the login page included.

    ``brand_*`` and not ``site_*``: Django's own ``LoginView`` puts a
    ``site_name`` in its context -- the current site's name, which without
    ``django.contrib.sites`` is simply the Host header. A view's context beats
    a context processor, so on the one page that most needs the school's name
    it would silently render the domain instead. A test found it; a reader
    would not have.
    """
    return {"brand_name": settings.ST_SITE_NAME, "brand_logo": bool(settings.ST_LOGO)}


def logo(request):
    """Served to anybody, deliberately.

    It is drawn on the login page and used as the favicon, both of which
    happen before there is a session. Nothing about a crest is confidential --
    it is on the building.

    No traversal check here, unlike ``tickets.attachments.resolved_path``: this
    path comes from the process environment, set by whoever installed the
    instance, and is never touched by a request. The suffix allowlist is about
    serving the right ``Content-Type``, not about containment.
    """
    if not settings.ST_LOGO:
        raise Http404
    path = Path(settings.ST_LOGO)
    content_type = TYPES.get(path.suffix.lower())
    if content_type is None or not path.is_file():
        raise Http404
    response = FileResponse(path.open("rb"), content_type=content_type)
    # It changes when a school changes its crest, which is to say never.
    response["Cache-Control"] = "public, max-age=86400"
    return response
