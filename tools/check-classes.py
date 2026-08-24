#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Report class names the templates use and the stylesheet does not define.

The failure mode is what makes the check worth having: **an unknown class is
not an error**. Tailwind drops it, the browser ignores it, the page renders
without it, and nothing anywhere says a word. The same silence covers a class
renamed by a daisyUI upgrade -- ``card-compact`` became ``card-sm`` in v5 --
and a class name built by string concatenation in a template, which Tailwind
cannot see and therefore never generates.

    ./tools/tailwindcss -i assets/app.css -o static/app.css --minify
    python3 tools/check-classes.py

Exits non-zero when something is missing. False positives are possible on
exotic selectors; they are read, not silenced.
"""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CSS = ROOT / "static" / "app.css"
TEMPLATES = ROOT / "templates"

#: Django template syntax lands inside class="..." and is not a class name.
#: Tags go first, taking their conditions with them -- the literal text they
#: wrap is a class name and stays.
TEMPLATE_TAG = re.compile(r"{%.*?%}", re.S)
#: A variable, on the other hand, leaves a hole in the middle of a token. Such
#: a name is invisible to Tailwind too, so it is skipped rather than reported.
TEMPLATE_VAR = re.compile(r"{{.*?}}", re.S)
HOLE = "\x00"
#: Characters Tailwind escapes with a backslash in the generated selector.
ESCAPED = set(":./[]()%,#!")


def selector_pattern(token: str) -> str:
    body = "".join(
        (r"\\?" + re.escape(char)) if char in ESCAPED else re.escape(char) for char in token
    )
    return r"\." + body + r"(?![\w-])"


def main() -> int:
    if not CSS.is_file():
        print(f"{CSS} is missing -- compile the stylesheet first.", file=sys.stderr)
        return 2
    css = CSS.read_text()

    used = {}
    for template in sorted(TEMPLATES.rglob("*.html")):
        for match in re.finditer(r'class="([^"]*)"', template.read_text(), re.S):
            attribute = TEMPLATE_VAR.sub(HOLE, TEMPLATE_TAG.sub(" ", match.group(1)))
            for token in attribute.split():
                if HOLE in token:
                    continue
                used.setdefault(token, set()).add(template.relative_to(ROOT))

    missing = {
        token: where
        for token, where in used.items()
        if not re.search(selector_pattern(token), css)
    }
    for token in sorted(missing):
        print(f"no CSS for {token!r}: {', '.join(str(p) for p in sorted(missing[token]))}")
    print(f"{len(used)} classes used, {len(missing)} without a rule.")
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main())
