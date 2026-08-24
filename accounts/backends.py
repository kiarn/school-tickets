# SPDX-License-Identifier: GPL-3.0-or-later
"""Local password login, until OIDC lands (D-05, D-26).

Django's own ``ModelBackend`` cannot do this job here: ``USERNAME_FIELD`` is
``oidc_sub``, which is exactly the field that is NULL for everybody who has
never logged in through Keycloak -- that is, everybody, today. So the lookup
has to be by ``cn``, the identifier a person actually knows.

**This backend does not decide who may enter.** Enrolment does (D-22): there is
no sign-up, no account creation, no first-login provisioning. It only checks
that somebody who is already enrolled is who they say they are.
"""

import logging

from django.contrib.auth.backends import BaseBackend
from django.core.cache import cache

from .models import User

log = logging.getLogger(__name__)

#: Failures tolerated for one ``cn`` before it stops answering, and for how
#: long. Counted **per cn and not per IP address**, deliberately: a school sits
#: behind one NAT address, so an IP counter would let one mistyped password
#: lock out the whole building. The cost of the choice is that somebody can
#: keep a known account locked -- an annoyance, not a way in.
MAX_ATTEMPTS = 8
LOCKOUT_SECONDS = 300


def _key(cn: str) -> str:
    return f"login-fail:{cn}"


def locked_out(cn: str) -> bool:
    return cache.get(_key(cn), 0) >= MAX_ATTEMPTS


class LocalPasswordBackend(BaseBackend):
    def authenticate(self, request, cn=None, password=None, username=None, **kwargs):
        # ``username`` is what the Django admin's own login form sends; taking
        # it too means one set of credentials works in both places.
        cn = (cn or username or "").strip()
        if not cn or not password:
            return None
        if locked_out(cn):
            log.warning("login refused: %s is locked out", cn)
            return None

        # A ``cn`` is unique per school, not globally: two schools may each
        # have an "arnaud". Rare enough to walk, and the walk stops on the
        # first password that matches.
        for user in User.objects.filter(cn=cn, anonymized_at__isnull=True):
            if user.check_password(password) and user.is_active:
                cache.delete(_key(cn))
                return user

        # Run the hasher anyway on failure. Without this, an unknown cn answers
        # measurably faster than a known one, and the difference is a way of
        # asking the application who is enrolled.
        User().set_password(password)
        try:
            cache.incr(_key(cn))
        except ValueError:
            cache.set(_key(cn), 1, LOCKOUT_SECONDS)
        return None

    def get_user(self, user_id):
        try:
            return User.objects.get(pk=user_id, is_active=True, anonymized_at__isnull=True)
        except User.DoesNotExist:
            return None
