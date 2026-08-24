# SPDX-License-Identifier: GPL-3.0-or-later
"""Generate the VAPID key pair a Web Push deployment needs (D-14).

    manage.py vapid_keys

Prints the two settings and stops. It writes nothing: which file each key goes
into is the whole point of the separation, and a command that guessed would
undo it.

**The private key belongs to the worker's environment file and to nothing
else.** The web service must not be able to send a notification, exactly as it
cannot call lmnapi (D-09). The public key is public -- browsers receive it in
order to subscribe -- so the web service is the one that reads it.

Regenerating the pair invalidates every existing subscription: browsers are
bound to the key they subscribed with. They re-subscribe on their next visit to
the notifications page, and the old rows disappear on the first 410.
"""

import base64

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Generate a VAPID key pair for Web Push (D-14, specs/09-notifications.md)."

    def handle(self, *args, **options):
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import ec

        private = ec.generate_private_key(ec.SECP256R1())
        raw_private = private.private_numbers().private_value.to_bytes(32, "big")
        raw_public = private.public_key().public_bytes(
            serialization.Encoding.X962,
            serialization.PublicFormat.UncompressedPoint,
        )

        def b64(data: bytes) -> str:
            return base64.urlsafe_b64encode(data).decode().rstrip("=")

        self.stdout.write(self.style.SUCCESS("VAPID key pair generated.\n"))
        self.stdout.write("Worker environment file ONLY:")
        self.stdout.write(f"  ST_VAPID_PRIVATE_KEY={b64(raw_private)}")
        self.stdout.write("  ST_VAPID_SUBJECT=mailto:you@your-school.example\n")
        self.stdout.write("Both environment files (this one is public):")
        self.stdout.write(f"  ST_VAPID_PUBLIC_KEY={b64(raw_public)}\n")
        self.stdout.write(
            "The private key must not appear in the web service's environment: "
            "that separation is what keeps the network-facing process unable to "
            "send anything (D-09)."
        )
