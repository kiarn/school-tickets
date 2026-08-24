# SPDX-License-Identifier: GPL-3.0-or-later
"""Adapters: whatever the source, the core only ever sees a list of rows.

doc 03 asks for exactly this -- ``lmnapi`` and a hand-uploaded ``devices.csv``
are two producers of the same thing, and the reconciliation code must not be
able to tell them apart. Everything specific to a format stops at this module.
"""

import ipaddress
import re
from dataclasses import dataclass, field

# --- devices.csv layout ------------------------------------------------------
# Semicolon separated, no header line.
#
# Columns 1-5, 9 and 11 are read from a live server's file and their meaning is
# established. Columns 6-8 and 10 are NOT: on the sample, 6 holds a subnet mask
# and 7, 8, 10 hold "1", "" or "---". They are carried in ``raw`` and nothing
# is built on them until the shape of the API response confirms what they are.
# Columns 12-14 and 16 were empty on every single row, so their content is
# simply unknown.
COL_ROOM = 0
COL_HOSTNAME = 1
COL_GROUP = 2
COL_MAC = 3
COL_IP = 4
COL_ROLE = 8
COL_PXE = 10
COL_COMMENT = 14

#: Below this, the line cannot carry a PXE flag and is not a device line.
MIN_FIELDS = COL_PXE + 1

#: sophomorix writes this where a value is absent, next to plain emptiness.
#: Both mean the same thing to us; treating "---" as a value puts three dashes
#: on screen where a blank belongs.
EMPTY_MARKERS = frozenset({"", "-", "---"})

MAC_RE = re.compile(r"^([0-9a-f]{2}:){5}[0-9a-f]{2}$")


@dataclass(frozen=True)
class DeviceRow:
    """One machine, as the core wants it -- source-agnostic."""

    room: str
    hostname: str
    mac: str
    group: str = ""
    ip: str | None = None
    role: str = ""
    pxe: int | None = None
    comment: str = ""
    raw: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class SkippedLine:
    number: int
    reason: str
    text: str


@dataclass
class ParsedEstate:
    rows: list[DeviceRow] = field(default_factory=list)
    skipped: list[SkippedLine] = field(default_factory=list)

    def reasons(self) -> dict:
        counts: dict = {}
        for line in self.skipped:
            counts[line.reason] = counts.get(line.reason, 0) + 1
        return counts


def _clean(fields: list[str], index: int) -> str:
    if index >= len(fields):
        return ""
    value = fields[index].strip()
    return "" if value in EMPTY_MARKERS else value


def _parse_ip(value: str) -> str | None:
    """A bad address costs the address, never the machine.

    A device whose IP is malformed is still a device that can break and still
    needs a ticket. Dropping the row would hide it from the estate entirely.
    """
    if not value:
        return None
    try:
        return str(ipaddress.ip_address(value))
    except ValueError:
        return None


def _parse_pxe(value: str) -> int | None:
    if not value.isdigit():
        return None
    return int(value)


def parse_devices_csv(text: str) -> ParsedEstate:
    """Read a sophomorix ``devices.csv``.

    Three traps, all of them met on real data:

    1. **A leading ``#`` disables a machine, it is not only a comment.** A live
       file carried ``#server;dockerhost;...;10.0.0.4`` -- a host commented out
       to clear a collision with another row holding the same address. Both
       that and a free-text banner line must leave the estate. Splitting
       naively would create a room literally named ``#server``.
    2. **MAC case is not consistent**, even within one file. Normalised here so
       that the core never has to think about it again.
    3. **``---`` is an empty marker**, and sits in columns that otherwise hold
       numbers.

    Malformed lines are collected rather than raised: one bad row must not cost
    the other three hundred, and the caller reports what it skipped.
    """
    estate = ParsedEstate()
    seen_macs: dict[str, str] = {}

    for number, line in enumerate(text.splitlines(), start=1):
        text_line = line.rstrip()

        if not text_line.strip():
            continue
        if text_line.lstrip().startswith("#"):
            # Covers both meanings; for us they coincide -- not in the estate.
            estate.skipped.append(SkippedLine(number, "disabled", text_line))
            continue

        fields = text_line.split(";")
        if len(fields) < MIN_FIELDS:
            estate.skipped.append(SkippedLine(number, "too_few_fields", text_line))
            continue

        room = _clean(fields, COL_ROOM)
        hostname = _clean(fields, COL_HOSTNAME)
        mac = _clean(fields, COL_MAC).lower()

        if not room or not hostname or not mac:
            estate.skipped.append(SkippedLine(number, "missing_key_field", text_line))
            continue
        if not MAC_RE.match(mac):
            # Without a usable MAC the row has no identity at all (doc 03), so
            # there is nothing sensible to file it under.
            estate.skipped.append(SkippedLine(number, "bad_mac", text_line))
            continue
        if mac in seen_macs:
            # The estate's external key, duplicated at the source. Keeping the
            # first occurrence is arbitrary; reporting it is not.
            estate.skipped.append(
                SkippedLine(number, f"duplicate_mac_of_{seen_macs[mac]}", text_line)
            )
            continue
        seen_macs[mac] = hostname

        estate.rows.append(
            DeviceRow(
                room=room,
                hostname=hostname,
                mac=mac,
                group=_clean(fields, COL_GROUP),
                ip=_parse_ip(_clean(fields, COL_IP)),
                role=_clean(fields, COL_ROLE),
                pxe=_parse_pxe(_clean(fields, COL_PXE)),
                comment=_clean(fields, COL_COMMENT),
                raw=fields,
            )
        )

    return estate


def rows_from_api(payload) -> list[DeviceRow]:
    """Adapt an lmnapi ``/v1/devices`` response.

    **Not written yet, and deliberately not guessed.** ``devices.csv`` gives
    the columns and their meaning, but not the JSON field names, not the
    envelope (a bare list? ``{"devices": [...]}``? keyed by room?), and not
    whether the API re-exports the raw columns or a named object.

    Writing this against an assumption would produce code that looks finished
    and reconciles nothing. It needs one real response; see Q-03.
    """
    raise NotImplementedError(
        "the shape of GET /v1/devices has not been read yet -- see specs/07, Q-03"
    )
