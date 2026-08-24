#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Report class names the templates use and the stylesheet does not define.

Written on 2026-08-24, after this exact failure went unnoticed for two days:
``bg-base-100``, ``text-base-content`` and every other daisyUI colour utility
produced no CSS at all, because D-18 does without ``@plugin "daisyui"`` and
nothing had registered those names with Tailwind (fixed in ``assets/app.css``).

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
TEMPLATE_SYNTAX = re.compile(r"[{}|'\"]|^(if|else|elif|endif|not|and|or|==|!=)$|\.\w+$|^\w+\.")
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
            for token in match.group(1).split():
                if "{" in token or "}" in token or TEMPLATE_SYNTAX.search(token):
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
