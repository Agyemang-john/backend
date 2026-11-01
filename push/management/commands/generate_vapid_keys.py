"""
Print a new VAPID key pair for web push. Set them in the environment once and
don't rotate casually: existing browser subscriptions are tied to the public key.
"""
import base64

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from django.core.management.base import BaseCommand


def _b64(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b'=').decode()


class Command(BaseCommand):
    help = "Generate WEBPUSH_VAPID_PUBLIC_KEY / WEBPUSH_VAPID_PRIVATE_KEY values"

    def handle(self, *args, **options):
        key = ec.generate_private_key(ec.SECP256R1())
        private = key.private_numbers().private_value.to_bytes(32, 'big')
        public = key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
        self.stdout.write(f"WEBPUSH_VAPID_PUBLIC_KEY={_b64(public)}")
        self.stdout.write(f"WEBPUSH_VAPID_PRIVATE_KEY={_b64(private)}")
