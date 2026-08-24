# syntax=docker/dockerfile:1
# SPDX-License-Identifier: GPL-3.0-or-later
#
# One image, two processes -- the same split as the two systemd units in
# debian/ (D-09, D-20). What separates them is the command and the environment
# file, never the code: the worker holds the lmnapi host key and the VAPID
# private key, the web service holds neither.
#
# Build from the repository root:
#   docker build -f docker/Dockerfile -t school-tickets:dev .

FROM python:3.12-slim-bookworm AS builder

# mysqlclient is a C extension: it needs a compiler and the MariaDB client
# headers here, and only the shared library at run time.
RUN apt-get update \
 && apt-get install --no-install-recommends -y \
      build-essential pkg-config default-libmysqlclient-dev \
 && rm -rf /var/lib/apt/lists/*

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

WORKDIR /src
COPY pyproject.toml README.md ./
COPY school_tickets ./school_tickets
COPY accounts ./accounts
COPY parc ./parc
COPY tickets ./tickets
COPY badges ./badges
COPY notifications ./notifications

# gunicorn is not a project dependency: it is a deployment choice, exactly as
# in debian/school-tickets-web.service. It belongs to the image, not to
# pyproject.toml.
#
# The project is installed for its dependencies, then removed again: the code
# runs from /app, the way it runs from /usr/share/school-tickets in the .deb.
# Leaving a second copy in site-packages would make BASE_DIR -- and therefore
# the templates directory -- depend on which one Python imported first.
RUN pip install --no-cache-dir . gunicorn==23.0.0 \
 && pip uninstall -y school_tickets


FROM python:3.12-slim-bookworm

RUN apt-get update \
 && apt-get install --no-install-recommends -y libmariadb3 \
 && rm -rf /var/lib/apt/lists/*

# A fixed uid, because the media volume outlives the container and its files
# have to stay readable after a rebuild.
RUN useradd --system --uid 10001 --create-home --home-dir /app --shell /usr/sbin/nologin app

COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DJANGO_SETTINGS_MODULE=school_tickets.settings

WORKDIR /app
COPY --chown=app:app . /app

# Two mount points, and the README says why losing either one hurts:
#   /var/lib/school-tickets/media  the photos, kept for ever (R-11)
#   /app/staticfiles               collectstatic output, read by nginx
# STATIC_ROOT is not configurable through the environment, hence the path.
RUN mkdir -p /var/lib/school-tickets/media /app/staticfiles \
 && chown -R app:app /var/lib/school-tickets /app/staticfiles

USER app
EXPOSE 8000
ENTRYPOINT ["/app/docker/entrypoint.sh"]
CMD ["web"]
