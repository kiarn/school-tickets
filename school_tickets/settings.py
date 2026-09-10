# SPDX-License-Identifier: GPL-3.0-or-later
"""Project settings. See specs/01-decisions.md."""

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = os.environ.get("ST_SECRET_KEY", "dev-only-not-for-production")
DEBUG = os.environ.get("ST_DEBUG", "0") == "1"
ALLOWED_HOSTS = [h for h in os.environ.get("ST_ALLOWED_HOSTS", "").split(",") if h]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    # The project package itself: no model, no migration, only the startup
    # checks of school_tickets/checks.py.
    "school_tickets.apps.SchoolTicketsConfig",
    "accounts",
    "parc",
    "tickets",
    "badges",
    "notifications",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.locale.LocaleMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    # After the user is known, and deliberately so: see the module docstring.
    "accounts.middleware.UserLanguageMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "school_tickets.urls"
WSGI_APPLICATION = "school_tickets.wsgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                # LANGUAGE_CODE on <html lang="...">: a screen reader and a
                # browser translator both read it, and German is the default.
                "django.template.context_processors.i18n",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                # The unread badge has to be right on every page or it is
                # worse than absent (specs/09-notifications.md).
                "notifications.context_processors.unread",
                # Name and crest, needed before login as much as after (D-30).
                "school_tickets.branding.identity",
                # Whether the menu offers the estate. No query -- the role is
                # already on the request.
                "parc.context_processors.estate_access",
            ],
        },
    },
]

# --- Database (D-03) ---------------------------------------------------------
# MariaDB in utf8mb4: comments carry emoji, and utf8mb3 truncates them.
# The SQLite fallback exists for development and tests only.
if os.environ.get("ST_DB", "mariadb") == "sqlite":
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": BASE_DIR / "dev.sqlite3",
        }
    }
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.mysql",
            "NAME": os.environ.get("ST_DB_NAME", "school_tickets"),
            "USER": os.environ.get("ST_DB_USER", "school_tickets"),
            "PASSWORD": os.environ.get("ST_DB_PASSWORD", ""),
            "HOST": os.environ.get("ST_DB_HOST", "localhost"),
            "PORT": os.environ.get("ST_DB_PORT", "3306"),
            "OPTIONS": {"charset": "utf8mb4"},
            "TEST": {"CHARSET": "utf8mb4", "COLLATION": "utf8mb4_unicode_ci"},
        }
    }

# The worker (D-20) calls close_old_connections() on every iteration; leaving
# CONN_MAX_AGE at 0 means each turn starts on a fresh connection, which avoids
# "server has gone away" after wait_timeout.
CONN_MAX_AGE = 0

AUTH_USER_MODEL = "accounts.User"

# --- Authentication (D-26) ---------------------------------------------------
# The local password backend is the stopgap until OIDC (D-05). It looks accounts
# up by `cn`, because USERNAME_FIELD is `oidc_sub` and that is NULL for anybody
# who has never been through Keycloak. ModelBackend stays behind it: it is what
# still answers `has_perm()` for the Django admin.
AUTHENTICATION_BACKENDS = [
    "accounts.backends.LocalPasswordBackend",
    "django.contrib.auth.backends.ModelBackend",
]
LOGIN_URL = "accounts:login"
LOGIN_REDIRECT_URL = "tickets:list"
LOGOUT_REDIRECT_URL = "accounts:login"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# --- i18n (D-15) -------------------------------------------------------------
# Source strings (msgid) are English; de/fr/en catalogues come from Crowdin.
LANGUAGE_CODE = "de"
LANGUAGES = [("de", "Deutsch"), ("fr", "Français"), ("en", "English")]
# Tests assert the source strings, not a translation. See school_tickets/runner.py.
TEST_RUNNER = "school_tickets.runner.EnglishTestRunner"
LOCALE_PATHS = [BASE_DIR / "locale"]
USE_I18N = True
TIME_ZONE = os.environ.get("ST_TIME_ZONE", "Europe/Berlin")
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "static"] if (BASE_DIR / "static").exists() else []

# --- Attachments (D-21) ------------------------------------------------------
# MEDIA_URL is deliberately NOT served by the web server: every attachment goes
# through tickets.views.attachment, which re-checks the parent ticket's
# visibility. See specs/08-visibilite.md.
MEDIA_ROOT = Path(os.environ.get("ST_MEDIA_ROOT", BASE_DIR / "media"))
ST_X_ACCEL_PREFIX = os.environ.get("ST_X_ACCEL_PREFIX", "")

# --- Which school to ask linuxmuster about (D-49) ----------------------------
# **Remote, not local.** This application no longer has a school of its own --
# one instance serves one establishment, so the entity was removed from the
# schema. linuxmuster, on the other hand, is multi-school by design: `lmnapi`
# wants to be told which one, and "default-school" is what a single-school
# server calls its own. This is that parameter and nothing else.
ST_DEFAULT_SCHOOL_SLUG = os.environ.get("ST_DEFAULT_SCHOOL", "default-school")

# --- Instance identity (D-30) -------------------------------------------------
# One instance serves one school, so its name and crest are configuration and
# not data. See school_tickets/branding.py.
ST_SITE_NAME = os.environ.get("ST_SITE_NAME", "school-tickets")
# Absolute path to a square PNG. Empty means the name stands alone, which is
# what a fresh install looks like. school_tickets/checks.py says at startup
# when the file is there but not usable.
ST_LOGO = os.environ.get("ST_LOGO", "")
# What fits under an icon on a home screen. Falls back to the full name, which
# the phone then elides itself -- a truncation we would do worse.
ST_SITE_SHORT_NAME = os.environ.get("ST_SITE_SHORT_NAME", "") or ST_SITE_NAME

# --- Worker (D-20) -----------------------------------------------------------
# Cadences in seconds, and every one of them configurable on purpose: a school
# that re-images every night and one that re-images once a term do not want the
# same numbers. ``parc/jobs.py`` says what each job does with its cadence and
# why it runs when it does. ``TICK`` is the loop's own pulse, not a cadence:
# it only decides how promptly a job that has come due is noticed.
ST_WORKER_TICK = int(os.environ.get("ST_WORKER_TICK", "5"))
ST_WORKER_REFRESH_EVERY = int(os.environ.get("ST_WORKER_REFRESH_EVERY", "30"))
ST_WORKER_LINBO_EVERY = int(os.environ.get("ST_WORKER_LINBO_EVERY", "3600"))
ST_WORKER_INVENTORY_EVERY = int(os.environ.get("ST_WORKER_INVENTORY_EVERY", "86400"))
ST_OPENING_HOURS = (7, 18)          # local hours, start inclusive, end exclusive
ST_OPENING_WEEKDAYS = (0, 1, 2, 3, 4)

# --- Notifications (specs/09-notifications.md) --------------------------------
# A DIFFERENT window from the one above, and the difference is the point:
# ST_OPENING_HOURS says when workstations are switched on, this says when a
# teenager may decently be disturbed -- the application is read outside school
# hours, hence 20:00 rather than 18:00. Nothing here is urgent: what falls
# outside the window waits for the morning, and stays readable in the
# application meanwhile.
ST_NOTIFY_HOURS = (7, 20)
ST_NOTIFY_WEEKDAYS = tuple(
    int(day) for day in os.environ.get("ST_NOTIFY_WEEKDAYS", "0,1,2,3,4").split(",") if day
)
ST_WORKER_NOTIFY_EVERY = int(os.environ.get("ST_WORKER_NOTIFY_EVERY", "60"))

# Web Push (D-14). The PRIVATE key belongs to the worker's environment file and
# to nothing else -- the web service must not be able to send anything, exactly
# as it cannot call lmnapi (D-09). The public key is public: the browser needs
# it to subscribe, so the web service does read that one.
ST_VAPID_PRIVATE_KEY = os.environ.get("ST_VAPID_PRIVATE_KEY", "")
ST_VAPID_PUBLIC_KEY = os.environ.get("ST_VAPID_PUBLIC_KEY", "")
ST_VAPID_SUBJECT = os.environ.get("ST_VAPID_SUBJECT", "mailto:admin@example.org")

# Volume guard (specs/03-parc-synchronisation.md): a sync that would remove more
# than this fraction of the estate is refused without applying anything.
ST_SYNC_MAX_REMOVAL_RATIO = float(os.environ.get("ST_SYNC_MAX_REMOVAL_RATIO", "0.20"))
# MAC overlap ratio above which a room rename is proposed.
ST_ROOM_RENAME_THRESHOLD = float(os.environ.get("ST_ROOM_RENAME_THRESHOLD", "0.60"))

# --- What counts as "behind" for a LINBO sync --------------------------------
# Configurable and never hard-coded (doc 05): a school that re-images every
# night and one that re-images once a term do not mean the same thing by it.
# Fourteen days is a default, not a rule -- long enough that an ordinary week
# of holidays does not colour the whole estate.
ST_LINBO_STALE_AFTER = int(os.environ.get("ST_LINBO_STALE_AFTER", 14 * 86400))
# Past this, we stop presenting the value as current and say instead how long
# the information has been unavailable. The sweep runs hourly, so six hours
# means several passes have been missed -- the worker is down, or lmnapi is.
# The distinction matters: a stopped worker looks exactly like a broken estate
# unless the screen says which it is.
ST_LINBO_OBSERVATION_MAX_AGE = int(os.environ.get("ST_LINBO_OBSERVATION_MAX_AGE", 6 * 3600))

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"console": {"class": "logging.StreamHandler"}},
    "root": {"handlers": ["console"], "level": os.environ.get("ST_LOG_LEVEL", "INFO")},
}
