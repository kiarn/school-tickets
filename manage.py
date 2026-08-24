#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
import os
import sys


def main():
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "school_tickets.settings")
    from django.core.management import execute_from_command_line

    execute_from_command_line(sys.argv)


if __name__ == "__main__":
    main()
