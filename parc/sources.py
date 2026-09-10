# SPDX-License-Identifier: GPL-3.0-or-later
"""Adapters: whatever the source, the core only ever sees a list of rows.

doc 03 asks for exactly this -- ``lmnapi`` and a hand-uploaded ``devices.csv``
are two producers of the same thing, and the reconciliation code must not be
able to tell them apart. Everything specific to a format stops at this module.
"""

import ipaddress
import json
import re
from dataclasses import dataclass, field


class LmnApiPayloadError(ValueError):
    """The API answered, but not with an estate."""

# --- devices.csv layout ------------------------------------------------------
# Semicolon separated, no header line.
#
# The API names every column, which settles what the file alone could not:
# 6-8 are ``officeKey``, ``windowsKey`` and ``dhcpOptions``; 10, 12, 13, 14 are
# ``lmnReserved*``; 16 is ``options``. None of them carries anything we need,
# so they stay in ``raw`` -- but they are no longer unknown.
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

#: What the API puts in ``status`` for a line that is not a machine: a banner,
#: a blank line, or a host commented out. The named equivalent of the CSV's
#: leading ``#``.
API_STATUS_COMMENT = "comment"


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
    #: The source line as it arrived -- the CSV fields in order, or the API
    #: object. Kept for forensics, read by nothing.
    raw: list[str] | dict = field(default_factory=list)


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


def _admit(estate, seen_macs, *, number, text, row: DeviceRow) -> None:
    """The identity rules, applied identically to both sources.

    doc 03 makes ``devices.csv`` and lmnapi interchangeable, and that only
    holds if a given row is accepted -- or rejected, and for the same stated
    reason -- whichever door it came through. Hence one function called twice,
    rather than two readings that merely happen to agree today.
    """
    if not row.room or not row.hostname or not row.mac:
        estate.skipped.append(SkippedLine(number, "missing_key_field", text))
        return
    if not MAC_RE.match(row.mac):
        # Without a usable MAC the row has no identity at all (doc 03), so
        # there is nothing sensible to file it under.
        estate.skipped.append(SkippedLine(number, "bad_mac", text))
        return
    if row.mac in seen_macs:
        # The estate's external key, duplicated at the source. Keeping the
        # first occurrence is arbitrary; reporting it is not.
        estate.skipped.append(
            SkippedLine(number, f"duplicate_mac_of_{seen_macs[row.mac]}", text)
        )
        return

    seen_macs[row.mac] = row.hostname
    estate.rows.append(row)


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

        _admit(
            estate,
            seen_macs,
            number=number,
            text=text_line,
            row=DeviceRow(
                room=_clean(fields, COL_ROOM),
                hostname=_clean(fields, COL_HOSTNAME),
                mac=_clean(fields, COL_MAC).lower(),
                group=_clean(fields, COL_GROUP),
                ip=_parse_ip(_clean(fields, COL_IP)),
                role=_clean(fields, COL_ROLE),
                pxe=_parse_pxe(_clean(fields, COL_PXE)),
                comment=_clean(fields, COL_COMMENT),
                raw=fields,
            ),
        )

    return estate


def _api_value(entry: dict, key: str) -> str:
    """``null`` and ``""`` and ``---`` all mean absent, and all three occur.

    The API mixes JSON ``null`` with the empty string within a single response
    -- a comment line carries ``null`` everywhere, a banner line carries ``""``
    -- and it passes sophomorix's ``---`` through untouched.
    """
    value = entry.get(key)
    if value is None:
        return ""
    value = str(value).strip()
    return "" if value in EMPTY_MARKERS else value


def rows_from_api(payload) -> ParsedEstate:
    """Adapt a ``GET /v1/devices/list/{school}`` response (lmn 7.4.11).

    A bare JSON list, one object per **line of devices.csv** -- named, but not
    cleaned. Every trap the file holds survives into the API: mixed MAC case,
    ``---`` as an empty marker, duplicate MACs, and the comment lines, still
    there with ``#server`` sitting in the ``room`` field.

    The one thing the API adds is ``status``, and it is worth having: a line
    that is not a machine is marked ``comment``, so a disabled host is named
    as such instead of being recognised by a leading ``#``. Everything else
    goes through :func:`_admit`, exactly as the CSV does.

    ``Not registered`` is *not* filtered. Those rows are real machines with a
    real MAC -- routers, a smart button -- that are simply absent from the
    directory. They can break and need a ticket like any other, and the CSV
    path cannot tell them apart either; dropping them here would make the two
    sources disagree, which doc 03 forbids.
    """
    if not isinstance(payload, list):
        # Better a plain refusal than a silently empty estate: an empty list
        # would read as "every machine has disappeared" and queue a decision
        # for each one.
        raise LmnApiPayloadError(f"expected a list of devices, got {type(payload).__name__}")

    estate = ParsedEstate()
    seen_macs: dict[str, str] = {}

    for number, entry in enumerate(payload, start=1):
        if not isinstance(entry, dict):
            estate.skipped.append(SkippedLine(number, "not_an_object", repr(entry)[:200]))
            continue

        text = json.dumps(entry, ensure_ascii=False, sort_keys=True)

        if _api_value(entry, "status").lower() == API_STATUS_COMMENT:
            estate.skipped.append(SkippedLine(number, "disabled", text))
            continue

        _admit(
            estate,
            seen_macs,
            number=number,
            text=text,
            row=DeviceRow(
                room=_api_value(entry, "room"),
                hostname=_api_value(entry, "hostname"),
                mac=_api_value(entry, "mac").lower(),
                group=_api_value(entry, "group"),
                ip=_parse_ip(_api_value(entry, "ip")),
                role=_api_value(entry, "sophomorixRole"),
                pxe=_parse_pxe(_api_value(entry, "pxeFlag")),
                comment=_api_value(entry, "sophomorixComment"),
                raw=entry,
            ),
        )

    return estate
