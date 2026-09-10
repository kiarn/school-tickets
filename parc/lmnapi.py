# SPDX-License-Identifier: GPL-3.0-or-later
"""lmnapi client -- the only boundary that talks to the linuxmuster server.

The secret is **never** read from Django settings: it is taken from the
process environment, at call time. That is what makes the web service, which
shares this code, unable to use it -- its systemd unit does not carry the
variable (D-09, D-20).

Authentication through ``X-HOST-Key``: pre-shared secret, an LDAP user bound to
it server side, an IP allow list. See specs/04-permissions.md.
"""

import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request

logger = logging.getLogger(__name__)

ENV_HOST_KEY = "ST_LMNAPI_HOST_KEY"
ENV_BASE_URL = "ST_LMNAPI_BASE_URL"


class LmnApiError(RuntimeError):
    pass


class NotConfigured(LmnApiError):
    """Neither secret nor URL: the worker must say so plainly, not crash obscurely."""


class RateLimited(LmnApiError):
    def __init__(self, retry_after: float = 60.0):
        super().__init__(f"429, retry in {retry_after}s")
        self.retry_after = retry_after


class Forbidden(LmnApiError):
    """Symptom of a host key whose scope or role has changed.

    Must alert the administrator rather than degrade silently: it surfaces as
    ``fetch_status = forbidden`` on the affected rows.
    """


class Client:
    def __init__(self, base_url=None, host_key=None, timeout=30):
        self.base_url = (base_url or os.environ.get(ENV_BASE_URL, "")).rstrip("/")
        self.host_key = host_key or os.environ.get(ENV_HOST_KEY, "")
        self.timeout = timeout

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.host_key)

    def get(self, path: str, params: dict | None = None):
        if not self.configured:
            raise NotConfigured(f"{ENV_BASE_URL} and {ENV_HOST_KEY} must both be set")

        url = f"{self.base_url}{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        request = urllib.request.Request(url, headers={"X-HOST-Key": self.host_key})

        # Every outgoing request is logged: after an incident, knowing what was
        # read beats guessing (specs/04-permissions.md, measure e).
        logger.info("lmnapi GET %s", path)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code == 429:
                raise RateLimited(float(exc.headers.get("Retry-After", 60))) from exc
            if exc.code in (401, 403):
                raise Forbidden(f"{exc.code} on {path}") from exc
            raise LmnApiError(f"{exc.code} on {path}") from exc
        except urllib.error.URLError as exc:
            raise LmnApiError(f"unreachable: {exc.reason}") from exc

    # --- Business entry points ----------------------------------------------
    # Paths taken from the server's own OpenAPI document (lmn 7.4.11), not
    # guessed. What that document does NOT give is the shape of the response
    # bodies: both are declared as a bare object, so Q-03 stays open until one
    # real answer has been read.

    def inventory(self, school: str):
        """Full snapshot of the estate -- rooms and devices.

        The school is a path segment, not a query parameter.
        """
        return self.get(f"/v1/devices/list/{urllib.parse.quote(school)}")

    def linbo_status_all(self):
        """LINBO state for the whole estate, in one collective call.

        Takes no school: the endpoint scopes itself from the role bound to the
        key server side -- a school-administrator only ever sees their own
        hosts. Nothing to pass, and nothing we could widen by passing it.

        Every row must carry the **MAC** next to the hostname: school-tickets
        addresses the API by what lmn can name, but files it under the MAC, the
        only stable identity (doc 03, Q-03). Unconfirmed on a real response.
        """
        return self.get("/v1/linbo/hosts/image-status")
