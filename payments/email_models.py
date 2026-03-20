# subscriptions/email_models.py
#
# Add these models to subscriptions/models.py or keep as a separate file
# and import them in models.py:
#   from .email_models import EmailTemplate, SubscriptionEmailConfig
#
# After adding, run:
#   python manage.py makemigrations subscriptions
#   python manage.py migrate

from django.db import models
from django.core.validators import MinValueValidator


class EmailTemplate(models.Model):
    """
    HTML email templates editable from the Django admin.
    Each template type maps to one email task.
    The body field supports Django template syntax — {{ vendor_name }}, etc.
    """
    TYPE_CHOICES = [
        ('confirmation',    'Subscription Confirmation'),
        ('renewal_success', 'Renewal Success'),
        ('expiring_soon',   'Expiring Soon Warning'),
        ('expired',         'Subscription Expired'),
        ('payment_failed',  'Payment Method Required'),
        ('cancellation',    'Subscription Cancelled'),
    ]

    type        = models.CharField(max_length=30, choices=TYPE_CHOICES, unique=True)
    subject     = models.CharField(max_length=200, help_text='Email subject line. Supports {{ plan_name }}, {{ vendor_name }}.')
    html_file   = models.CharField(
        max_length=200,
        help_text='Path relative to your templates/ folder, e.g. emails/subscription_confirmation.html',
        blank=True,
    )
    # Fallback plain-text body if HTML file not found
    text_body   = models.TextField(
        help_text='Plain-text fallback. Supports Django template variables: {{ vendor_name }}, {{ plan_name }}, {{ end_date }}, {{ days_left }}, {{ frontend_url }}',
        blank=True,
    )
    is_active   = models.BooleanField(default=True, help_text='Uncheck to disable this email type entirely.')
    created_at  = models.DateTimeField(auto_now_add=True)
    updated_at  = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name        = 'Email Template'
        verbose_name_plural = 'Email Templates'
        ordering            = ['type']

    def __str__(self):
        return f"{self.get_type_display()} — {'Active' if self.is_active else 'Disabled'}"


class SubscriptionEmailConfig(models.Model):
    """
    Singleton model — admin edits ONE row that controls all email scheduling.
    Days values are read by Celery tasks at runtime, so changes take effect
    immediately without restarting workers.
    """
    # Warning thresholds
    expiry_warning_days = models.PositiveIntegerField(
        default=7,
        validators=[MinValueValidator(1)],
        help_text='Send the "expiring soon" email this many days before the subscription ends.',
    )
    second_warning_days = models.PositiveIntegerField(
        default=3,
        validators=[MinValueValidator(1)],
        help_text='Send a second warning this many days before expiry. Set to 0 to disable.',
    )
    renewal_advance_days = models.PositiveIntegerField(
        default=1,
        validators=[MinValueValidator(1)],
        help_text='Attempt auto-renewal this many days before the subscription end date.',
    )

    # Retry settings
    renewal_max_retries = models.PositiveIntegerField(
        default=3,
        help_text='How many times to retry a failed renewal charge (once per day each retry).',
    )

    # Email sender overrides
    from_email   = models.EmailField(default='noreply@negromart.com', help_text='Sender address for all subscription emails.')
    from_name    = models.CharField(max_length=100, default='Negromart', help_text='Sender display name.')
    reply_to     = models.EmailField(blank=True, help_text='Reply-to address (optional).')
    support_url  = models.URLField(default='https://seller.negromart.com/support/', help_text='Support link shown in emails.')
    frontend_url = models.URLField(default='https://seller.negromart.com', help_text='Base URL for links in emails.')

    # Celery Beat schedule (read by admin — the admin registers periodic tasks dynamically)
    run_renewals_hour     = models.PositiveIntegerField(default=8,  help_text='Hour (0–23) to run the daily renewal task (UTC).')
    run_renewals_minute   = models.PositiveIntegerField(default=0,  help_text='Minute (0–59) for the renewal task.')
    run_expiry_check_hour = models.PositiveIntegerField(default=9,  help_text='Hour to run the expiry warning check (UTC).')
    run_expiry_check_minute = models.PositiveIntegerField(default=0)
    run_expire_old_hour   = models.PositiveIntegerField(default=0,  help_text='Hour to run the subscription expiration cleanup (UTC).')
    run_expire_old_minute = models.PositiveIntegerField(default=30)

    class Meta:
        verbose_name        = 'Email & Schedule Configuration'
        verbose_name_plural = 'Email & Schedule Configuration'

    def __str__(self):
        return 'Global Email & Schedule Config'

    def save(self, *args, **kwargs):
        """Enforce singleton — only one row allowed."""
        self.pk = 1
        super().save(*args, **kwargs)
        self._update_periodic_tasks()

    @classmethod
    def get(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj

    def _update_periodic_tasks(self):
        """
        When config is saved, update the django-celery-beat PeriodicTask entries
        so schedule changes in admin take effect immediately.
        """
        try:
            from django_celery_beat.models import PeriodicTask, CrontabSchedule
            import json

            tasks = [
                {
                    'name':   'subscriptions.process_renewals',
                    'task':   'subscriptions.process_renewals',
                    'hour':   self.run_renewals_hour,
                    'minute': self.run_renewals_minute,
                },
                {
                    'name':   'subscriptions.warn_expiring_soon',
                    'task':   'subscriptions.warn_expiring_soon',
                    'hour':   self.run_expiry_check_hour,
                    'minute': self.run_expiry_check_minute,
                },
                {
                    'name':   'subscriptions.expire_old_subscriptions',
                    'task':   'subscriptions.expire_old_subscriptions',
                    'hour':   self.run_expire_old_hour,
                    'minute': self.run_expire_old_minute,
                },
            ]

            for t in tasks:
                cron, _ = CrontabSchedule.objects.get_or_create(
                    hour=str(t['hour']),
                    minute=str(t['minute']),
                    day_of_week='*', day_of_month='*', month_of_year='*',
                )
                PeriodicTask.objects.update_or_create(
                    name=t['name'],
                    defaults={
                        'crontab':  cron,
                        'task':     t['task'],
                        'enabled':  True,
                        'args':     json.dumps([]),
                    },
                )
        except ImportError:
            # django-celery-beat not installed — skip
            pass
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning(f'Could not update PeriodicTask: {e}')