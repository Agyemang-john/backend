from django.core.management.base import BaseCommand
from django.core.mail import send_mail
from django.utils import timezone
from datetime import timedelta
from userauths.models import *
from userauths.utils import *

class Command(BaseCommand):
    help = 'Send birthday wishes to users'

    def handle(self, *args, **kwargs):
        today = timezone.now().date()
        upcoming_birthdays = Profile.objects.filter(
            date_of_birth__month=today.month,
            date_of_birth__day=today.day
        )

        for user in upcoming_birthdays:
            message = f"""Dear Valued Customer, We hope this message finds you well! As a cherished member of our community, we wanted to wish you a very Happy Birthday! Thank you for your continued support and loyalty"""
            send_sms(user.user.phone, message)

        self.stdout.write(self.style.SUCCESS('Birthday wishes sent successfully'))
