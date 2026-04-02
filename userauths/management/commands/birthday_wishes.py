"""
Management command to send birthday wishes via SMS to users whose
date_of_birth matches today. Uses the ArkeselSMS client.

Usage:
    python manage.py birthday_wishes
    (best run daily via cron or Celery Beat)
"""

import logging
from django.core.management.base import BaseCommand
from django.utils import timezone
from userauths.models import Profile
from userauths.arkesel_client import ArkeselSMS

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = 'Send birthday wishes to users whose birthday is today'

    def handle(self, *args, **kwargs):
        today = timezone.now().date()
        upcoming_birthdays = Profile.objects.filter(
            date_of_birth__month=today.month,
            date_of_birth__day=today.day
        )

        sms_client = ArkeselSMS()
        sent_count = 0

        for profile in upcoming_birthdays:
            phone = profile.user.phone
            if not phone:
                continue

            message = (
                "Dear Valued Customer, We hope this message finds you well! "
                "As a cherished member of our community, we wanted to wish you "
                "a very Happy Birthday! Thank you for your continued support and loyalty."
            )

            try:
                sms_client.send_sms(
                    sender="YourBrand",
                    message=message,
                    recipients=[phone]
                )
                sent_count += 1
            except Exception as e:
                logger.error(f"Failed to send birthday SMS to {phone}: {e}")

        self.stdout.write(self.style.SUCCESS(f'Birthday wishes sent to {sent_count} users'))
