"""
Emergency: remove someone's authenticator after they lost their phone AND their
recovery codes. Check who they are first. They'll be asked to set it up again.

    python manage.py reset_2fa someone@negromart.com
"""
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from twofactor.services import reset


class Command(BaseCommand):
    help = "Remove a user's authenticator app so they can set it up again"

    def add_arguments(self, parser):
        parser.add_argument('email')

    def handle(self, *args, email, **options):
        user = get_user_model().objects.filter(email__iexact=email).first()
        if user is None:
            raise CommandError(f"No user with email {email}")
        reset(user)
        self.stdout.write(self.style.SUCCESS(f"Two-step verification reset for {user.email}."))
