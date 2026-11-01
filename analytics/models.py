from django.conf import settings
from django.db import models


class DailyMetric(models.Model):
    """
    One funnel step on one day, overall (experiment='') or for one variant.
    `visitors` is unique visitors (HyperLogLog estimate, ±1%), `events` raw count.
    Written by analytics/collector.flush() from Redis.
    """
    date = models.DateField()
    name = models.CharField(max_length=40)
    experiment = models.CharField(max_length=60, blank=True, default='')
    variant = models.CharField(max_length=40, blank=True, default='')
    visitors = models.PositiveIntegerField(default=0)
    events = models.PositiveIntegerField(default=0)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['date', 'name', 'experiment', 'variant'], name='uniq_daily_metric')]
        indexes = [models.Index(fields=['name', 'date'], name='daily_metric_name_idx')]


class Experiment(models.Model):
    DRAFT, RUNNING, STOPPED = 'draft', 'running', 'stopped'
    STATUS_CHOICES = [(DRAFT, 'Draft'), (RUNNING, 'Running'), (STOPPED, 'Stopped')]

    key = models.SlugField(max_length=60, unique=True, help_text="Used in code: useExperiment('<key>').")
    name = models.CharField(max_length=120)
    description = models.TextField(blank=True)
    # [{"key": "control", "weight": 50}, {"key": "big_button", "weight": 50}]
    variants = models.JSONField(default=list)
    goal = models.CharField(max_length=40, default='purchase', help_text="Funnel step that counts as a conversion.")
    status = models.CharField(max_length=8, choices=STATUS_CHOICES, default=DRAFT, db_index=True)
    started_at = models.DateTimeField(null=True, blank=True)
    ended_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return self.key


class ExperimentExposure(models.Model):
    """A visitor saw a variant (first time). Linked to the user once they sign in, for purchase attribution."""
    experiment = models.ForeignKey(Experiment, on_delete=models.CASCADE, related_name='exposures')
    visitor = models.CharField(max_length=64)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    variant = models.CharField(max_length=40)
    first_seen = models.DateTimeField(auto_now_add=True)
    converted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['experiment', 'visitor'], name='uniq_experiment_visitor')]
        indexes = [
            models.Index(fields=['user', 'converted_at'], name='exposure_user_idx'),
            models.Index(fields=['visitor'], name='exposure_visitor_idx'),
        ]
