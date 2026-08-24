# SPDX-License-Identifier: GPL-3.0-or-later
"""Photos taken on the spot: sniffing, metadata, storage.

Three things happen to an upload before it becomes an ``Attachment`` row, and
none of them is optional:

1. **Its type is read from its bytes, never from what the browser claimed.**
   A phone announces whatever it likes in ``Content-Type``; the file name is
   worse still.
2. **Its location metadata is removed** (see ``strip_jpeg_metadata``). A photo
   taken in a classroom carries the GPS coordinates of that classroom, and
   specs/06-rgpd.md asks us to keep what we hold to what we need.
3. **It is written under a name we chose**, never under the one it arrived
   with -- a name from a phone is attacker-controlled input pointed at a file
   system.

Reading is the other half, and it lives in ``tickets.views.attachment``:
nothing here is ever served by the web server directly (D-21).
"""

import io
import logging
import uuid
from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError

from django.conf import settings
from django.core.exceptions import ValidationError
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

log = logging.getLogger(__name__)

#: A phone photo is 2 to 5 MB; 10 leaves room for a burst of detail without
#: turning the estate's storage into the project's next problem (doc 06).
MAX_BYTES = 10 * 1024 * 1024
#: Per post, not per ticket. Six photos of one fault is already generous.
MAX_PER_POST = 6
#: Long edge, in pixels. A bent pin is perfectly visible at 2048, and doc 06
#: names photos as the one volume that grows without bound: a 4000 px phone
#: photo shrinks by roughly a factor of ten on the way in.
MAX_DIMENSION = 2048
#: Re-encoding quality. 85 is the usual floor before artefacts show on the
#: kind of close-up detail this application exists to record.
JPEG_QUALITY = 85

#: Images only. Not a limitation to lift lightly: every other type is a file
#: the application would be handing back out to browsers on request.
_SIGNATURES = (
    (b"\xff\xd8\xff", "image/jpeg", ".jpg"),
    (b"\x89PNG\r\n\x1a\n", "image/png", ".png"),
)

# HEIC is what an iPhone stores. Pillow cannot decode it on its own; if
# ``pillow-heif`` is installed it registers itself here and HEIC starts
# working, with no other change. Without it, a HEIC upload is refused rather
# than stored unscrubbed -- see ``scrub``.
try:  # pragma: no cover - depends on what is installed
    import pillow_heif

    pillow_heif.register_heif_opener()
except ImportError:  # pragma: no cover
    pass


def sniff(data: bytes):
    """Return ``(mime, extension)`` read from the bytes themselves.

    ``None`` when nothing recognisable is there. RIFF/WebP and ISO-BMFF/HEIC
    need a second look further into the header, hence the two special cases:
    iPhones hand out HEIC whenever the browser does not ask for anything else.

    GIF is absent on purpose: nothing that comes out of a camera is a GIF, and
    accepting an animated format means either flattening it or carrying frames
    through the resize below for no benefit.
    """
    for magic, mime, extension in _SIGNATURES:
        if data.startswith(magic):
            return mime, extension
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp", ".webp"
    if data[4:8] == b"ftyp" and data[8:12] in (b"heic", b"heix", b"hevc", b"mif1", b"msf1"):
        return "image/heic", ".heic"
    return None


def orientation_of_jpeg(data: bytes) -> int:
    """Read EXIF tag 0x0112, the only tag worth carrying over.

    Returns 1 (upright) when absent or unreadable. Everything else in the EXIF
    block is dropped, but not this: browsers rotate an image from this tag, so
    dropping it silently would lay every portrait photo on its side.
    """
    index = data.find(b"Exif\x00\x00")
    if index < 0:
        return 1
    tiff = index + 6
    byte_order = data[tiff:tiff + 2]
    if byte_order == b"II":
        endian = "little"
    elif byte_order == b"MM":
        endian = "big"
    else:
        return 1

    def word(at, size):
        return int.from_bytes(data[at:at + size], endian)

    try:
        ifd = tiff + word(tiff + 4, 4)
        for entry in range(word(ifd, 2)):
            at = ifd + 2 + entry * 12
            if word(at, 2) == 0x0112:
                value = word(at + 8, 2)
                return value if 1 <= value <= 8 else 1
    except (IndexError, ValueError):
        return 1
    return 1


def _minimal_exif(orientation: int) -> bytes:
    """A whole APP1 segment holding one tag and nothing else.

    Big-endian TIFF, one IFD, one entry, no next IFD. 32 bytes of payload.
    """
    tiff = (
        b"MM\x00\x2a\x00\x00\x00\x08"           # header, IFD at offset 8
        b"\x00\x01"                             # one entry
        b"\x01\x12\x00\x03\x00\x00\x00\x01"     # tag 0x0112, SHORT, count 1
        + orientation.to_bytes(2, "big") + b"\x00\x00"
        + b"\x00\x00\x00\x00"                   # no next IFD
    )
    payload = b"Exif\x00\x00" + tiff
    return b"\xff\xe1" + (len(payload) + 2).to_bytes(2, "big") + payload


def strip_jpeg_metadata(data: bytes) -> bytes:
    """Drop EXIF, XMP and comments; keep JFIF, the colour profile, orientation.

    Walks the marker chain rather than searching for byte patterns: a search
    would happily find ``Exif`` inside the compressed image data.

    **On anything unexpected the original is returned unchanged.** Refusing the
    upload instead would leave a pupil standing in front of a broken machine
    with a photo the application will not take, which is the worse failure --
    but it does mean a malformed file keeps its metadata, so the caller logs it.
    """
    if not data.startswith(b"\xff\xd8"):
        return data

    orientation = orientation_of_jpeg(data)
    out = bytearray(b"\xff\xd8")
    if orientation != 1:
        out += _minimal_exif(orientation)

    index, end = 2, len(data)
    while index + 4 <= end:
        if data[index] != 0xFF:
            log.warning("jpeg: lost the marker chain at %d, metadata kept", index)
            return data
        marker = data[index + 1]
        if marker == 0xFF:                                  # fill byte
            index += 1
            continue
        if marker == 0x01 or 0xD0 <= marker <= 0xD9:        # standalone markers
            out += data[index:index + 2]
            index += 2
            continue
        if marker == 0xDA:                                  # start of scan
            out += data[index:]
            return bytes(out)
        length = int.from_bytes(data[index + 2:index + 4], "big")
        if length < 2 or index + 2 + length > end:
            log.warning("jpeg: segment %#x overruns the file, metadata kept", marker)
            return data
        # APP0 is JFIF (pixel density), APP2 the colour profile: both are about
        # the picture. APP1 is EXIF and XMP -- GPS, camera serial, timestamps.
        drop = marker in (0xE1, 0xFE) or 0xE3 <= marker <= 0xEF
        if not drop:
            out += data[index:index + 2 + length]
        index += 2 + length

    log.warning("jpeg: no scan found, metadata kept")
    return data


#: Pillow's name for each type we store, so that a re-encode keeps the format.
_PIL_FORMAT = {"image/jpeg": "JPEG", "image/png": "PNG", "image/webp": "WEBP"}


def scrub(data: bytes, mime: str):
    """Strip the metadata, cap the size. Returns ``(bytes, mime, width, height)``.

    Pillow decides here (R-18, answered on 2026-08-24), and it buys two things
    at once: metadata removal on every format it can read, and the downscale
    doc 06 asks for.

    Two details are deliberate.

    **A JPEG that needs no resizing is not re-encoded.** Re-compressing an
    already-compressed photo costs detail for nothing, so that path keeps the
    hand-written marker walk below, which is lossless.

    **Rotation is baked into the pixels** before anything else. Stripping EXIF
    removes the orientation tag browsers rotate by; ``exif_transpose`` turns
    the tag into actual pixels first, so the photo stays upright with no
    metadata left to carry.

    A file Pillow cannot decode is **refused**, not stored as it arrived: the
    whole reason for taking the dependency was to be able to promise that what
    we keep has been scrubbed. In practice that means HEIC on an installation
    without ``pillow-heif``.
    """
    try:
        image = Image.open(io.BytesIO(data))
        image.load()
    except (UnidentifiedImageError, OSError, ValueError):
        raise ValidationError(
            _("This image could not be read (%(mime)s). Take it again as a JPEG."),
            params={"mime": mime},
        )

    image = ImageOps.exif_transpose(image)
    width, height = image.size

    if max(width, height) <= MAX_DIMENSION and mime == "image/jpeg":
        return strip_jpeg_metadata(data), mime, width, height

    if max(width, height) > MAX_DIMENSION:
        image.thumbnail((MAX_DIMENSION, MAX_DIMENSION), Image.LANCZOS)
        width, height = image.size

    # Anything Pillow reads but we do not store as such -- HEIC above all --
    # comes out as JPEG. Pillow writes no EXIF unless asked to, which is the
    # stripping itself.
    out_format = _PIL_FORMAT.get(mime, "JPEG")
    out_mime = mime if out_format != "JPEG" else "image/jpeg"
    if out_format == "JPEG" and image.mode not in ("RGB", "L"):
        image = image.convert("RGB")

    buffer = io.BytesIO()
    if out_format == "JPEG":
        image.save(buffer, "JPEG", quality=JPEG_QUALITY, optimize=True)
    else:
        image.save(buffer, out_format)
    return buffer.getvalue(), out_mime, width, height


def store(upload, *, ticket, user, comment=None):
    """Validate one upload and write it out. Returns an unsaved ``Attachment``.

    Unsaved on purpose: the caller decides the transaction the row belongs to.
    """
    from .models import Attachment

    if upload.size > MAX_BYTES:
        raise ValidationError(
            _("%(name)s is too large (maximum %(limit)s MB)."),
            params={"name": upload.name, "limit": MAX_BYTES // (1024 * 1024)},
        )

    data = upload.read()
    kind = sniff(data)
    if kind is None:
        raise ValidationError(
            _("%(name)s is not an image."), params={"name": upload.name}
        )
    mime, extension = kind
    data, mime, width, height = scrub(data, mime)
    # HEIC comes back as a JPEG, so the extension follows the type we stored,
    # never the one that arrived.
    extension = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}[mime]

    now = timezone.localtime()
    relative = Path(
        "attachments", ticket.school.slug, f"{now:%Y}", f"{now:%m}",
        f"{uuid.uuid4().hex}{extension}",
    )
    absolute = Path(settings.MEDIA_ROOT) / relative
    absolute.parent.mkdir(parents=True, exist_ok=True)
    absolute.write_bytes(data)

    return Attachment(
        ticket=ticket,
        comment=comment,
        uploaded_by=user,
        # Kept for display and for the download name only. Never joined to a
        # path: the name on disk is the uuid above.
        filename=Path(upload.name or "photo").name[:255],
        mime=mime,
        size_bytes=len(data),
        storage_path=str(relative),
        width=width,
        height=height,
    )


def store_all(uploads, *, ticket, user, comment=None):
    """Write a whole batch, refusing the batch if it is too long."""
    uploads = list(uploads)
    if len(uploads) > MAX_PER_POST:
        raise ValidationError(
            _("At most %(limit)s photos at a time."), params={"limit": MAX_PER_POST}
        )
    from .models import Attachment

    return Attachment.objects.bulk_create(
        [store(upload, ticket=ticket, user=user, comment=comment) for upload in uploads]
    )


def delete_file(attachment) -> None:
    """Remove the bytes from disk. Docs 06 asks that this be easy to do."""
    path = Path(settings.MEDIA_ROOT) / attachment.storage_path
    try:
        path.unlink(missing_ok=True)
    except OSError:
        log.exception("could not remove %s", path)
