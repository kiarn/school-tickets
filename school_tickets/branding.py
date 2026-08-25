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

from io import BytesIO
from pathlib import Path

from django.conf import settings
from django.http import FileResponse, Http404, HttpResponse, JsonResponse
from django.urls import reverse
from django.utils.translation import get_language
from PIL import Image

#: What the view will serve, and nothing else. Both render at any size, which
#: is what a header, a favicon and a notification icon each need at once.
TYPES = {".svg": "image/svg+xml", ".png": "image/png"}

#: The bar behind the phone's status bar, and the page under it, as hex.
#: Neither a manifest nor a <meta> parses the oklch() the stylesheet is written
#: in, so these mirror `base-100` and `base-200` of assets/app.css by hand --
#: change them there and here together, there is no third place.
THEME_COLOR = {"light": "#ffffff", "dark": "#262c33"}
BACKGROUND_COLOR = {"light": "#ebebeb", "dark": "#0e1319"}

#: Sizes the manifest declares. 192 and 512 are what a browser asks for before
#: it offers to install anything; 180 is the one iOS reads, and it reads it
#: from <link rel="apple-touch-icon"> rather than from the manifest.
ICON_SIZES = (192, 512)
APPLE_TOUCH_SIZE = 180
#: An allowlist, not a minimum and a maximum: this endpoint resizes an image
#: for anybody who asks, so the set of sizes it will ever compute is closed.
SERVED_SIZES = frozenset(ICON_SIZES) | {APPLE_TOUCH_SIZE}


def logo_path():
    """The configured crest, or None -- the normal state of a fresh install.

    No traversal check, unlike ``tickets.attachments.resolved_path``: this path
    comes from the process environment, set by whoever installed the instance,
    and is never touched by a request. The suffix allowlist is about serving
    the right ``Content-Type``, not about containment.
    """
    if not settings.ST_LOGO:
        return None
    path = Path(settings.ST_LOGO)
    if path.suffix.lower() not in TYPES or not path.is_file():
        return None
    return path


def identity(request):
    """Name and crest on every page, the login page included.

    ``brand_*`` and not ``site_*``: Django's own ``LoginView`` puts a
    ``site_name`` in its context -- the current site's name, which without
    ``django.contrib.sites`` is simply the Host header. A view's context beats
    a context processor, so on the one page that most needs the school's name
    it would silently render the domain instead. A test found it; a reader
    would not have.
    """
    path = logo_path()
    # The status bar colour of an installed application. An account that chose
    # a theme is answered here and once, server-side, exactly as `data-theme`
    # is; an account that follows its device gets the two media-scoped tags
    # instead, and the device decides.
    chosen = getattr(getattr(request, "user", None), "theme", "")
    return {
        "brand_name": settings.ST_SITE_NAME,
        "brand_logo": bool(settings.ST_LOGO),
        # A PNG crest can be resized into the square icon iOS wants; an SVG
        # cannot, not without a rasteriser this project does not carry (D-33).
        "brand_icon": path is not None and path.suffix.lower() == ".png",
        "brand_theme_color": THEME_COLOR.get(chosen, ""),
        "brand_theme_color_light": THEME_COLOR["light"],
        "brand_theme_color_dark": THEME_COLOR["dark"],
    }


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
    path = logo_path()
    if path is None:
        raise Http404
    response = FileResponse(path.open("rb"), content_type=TYPES[path.suffix.lower()])
    # It changes when a school changes its crest, which is to say never.
    response["Cache-Control"] = "public, max-age=86400"
    return response


def icon(request, size):
    """The crest as a square PNG of one declared size.

    A home screen wants sizes, and a school has one file: rather than asking a
    sysadmin for four, the one they configured is fitted -- never cropped, a
    crest is not a decoration to be trimmed -- and centred on a transparent
    square. Only the sizes the manifest and the <head> actually name are
    computed, because this endpoint is public and resizing on demand for an
    arbitrary number would be a way of spending someone else's CPU.

    PNG only. Rasterising an SVG needs a library this project does not carry
    (D-33), so an SVG crest is declared to the manifest as it is, at "any"
    size, and iOS -- which reads no SVG -- simply gets no icon.
    """
    if size not in SERVED_SIZES:
        raise Http404
    path = logo_path()
    if path is None or path.suffix.lower() != ".png":
        raise Http404

    buffer = BytesIO()
    with Image.open(path) as image:
        fitted = image.convert("RGBA")
        fitted.thumbnail((size, size), Image.LANCZOS)
        square = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        square.paste(fitted, ((size - fitted.width) // 2, (size - fitted.height) // 2))
        square.save(buffer, "PNG")

    response = HttpResponse(buffer.getvalue(), content_type="image/png")
    response["Cache-Control"] = "public, max-age=86400"
    return response


def manifest(request):
    """What makes the application installable on a home screen (D-33).

    Served to anybody, like the crest: a browser fetches it on the login page,
    before there is a session, and it holds nothing but what is already on the
    building -- a name, a crest, two colours.

    ``start_url`` is the root, which is the ticket list, which redirects to the
    login page when nobody is signed in. That is the wanted behaviour: the
    installed icon opens on work, not on a start screen.
    """
    path = logo_path()
    icons = []
    if path is not None and path.suffix.lower() == ".svg":
        # One entry, "any": an SVG is every size at once.
        icons = [{"src": reverse("logo"), "sizes": "any", "type": "image/svg+xml"}]
    elif path is not None:
        icons = [
            {
                "src": reverse("icon", args=[size]),
                "sizes": f"{size}x{size}",
                "type": "image/png",
            }
            for size in ICON_SIZES
        ]

    return JsonResponse(
        {
            # Pinned rather than derived from start_url: an id that moves makes
            # a browser treat the next visit as a different application.
            "id": "/",
            "name": settings.ST_SITE_NAME,
            "short_name": settings.ST_SITE_SHORT_NAME,
            "start_url": "/",
            "scope": "/",
            "display": "standalone",
            "lang": get_language(),
            "theme_color": THEME_COLOR["light"],
            "background_color": BACKGROUND_COLOR["light"],
            "icons": icons,
        },
        content_type="application/manifest+json",
    )
