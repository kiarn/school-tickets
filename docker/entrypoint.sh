#!/bin/sh
# SPDX-License-Identifier: GPL-3.0-or-later
#
# Roles, not commands. The image is one; what it becomes is decided here.
#
#   web       gunicorn, after migrate and collectstatic
#   worker    manage.py run_worker, after waiting for the schema
#   manage    any management command: enroll, vapid_keys, sync_parc...
#
set -eu

wait_for_database() {
    # Compose can tell us the container is up; only Django can tell us the
    # server answers. Twenty tries, one second apart.
    attempt=0
    until python manage.py shell -c \
        'from django.db import connection; connection.ensure_connection()' \
        >/dev/null 2>&1
    do
        attempt=$((attempt + 1))
        if [ "$attempt" -ge 20 ]; then
            echo "database unreachable after ${attempt} attempts" >&2
            exit 1
        fi
        echo "waiting for the database (${attempt}/20)..."
        sleep 1
    done
}

case "${1:-web}" in
    web)
        wait_for_database
        # The web service owns the schema, and it is the only one that may:
        # two processes migrating the same database at the same time is how a
        # half-applied migration happens.
        python manage.py migrate --no-input
        python manage.py collectstatic --no-input --clear
        exec gunicorn --bind 0.0.0.0:8000 --workers 3 \
             --access-logfile - --error-logfile - \
             school_tickets.wsgi:application
        ;;
    worker)
        wait_for_database
        # Wait for the web service to have migrated rather than migrate too.
        # `migrate --check` exits non-zero while anything is unapplied.
        attempt=0
        until python manage.py migrate --check >/dev/null 2>&1; do
            attempt=$((attempt + 1))
            if [ "$attempt" -ge 60 ]; then
                echo "schema still not migrated after ${attempt} attempts" >&2
                exit 1
            fi
            echo "waiting for the schema (${attempt}/60)..."
            sleep 2
        done
        exec python manage.py run_worker
        ;;
    manage)
        shift
        wait_for_database
        exec python manage.py "$@"
        ;;
    *)
        # Anything else is run verbatim, which keeps `sh` and one-off binaries
        # available without a second entrypoint.
        exec "$@"
        ;;
esac
