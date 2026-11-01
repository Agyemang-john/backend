from django.conf import settings
from django.db import models


class OrderRisk(models.Model):
    """
    The fraud score of one order (risk/rules.py). HIGH orders are held: the
    seller can't book a shipment until staff approve. Reasons are stored so
    staff see why, and so the rules can be tuned against outcomes.
    """
    LOW, REVIEW, HIGH = 'low', 'review', 'high'
    LEVEL_CHOICES = [(LOW, 'Low'), (REVIEW, 'Review'), (HIGH, 'High')]

    CLEAR, HELD, APPROVED, CANCELLED = 'clear', 'held', 'approved', 'cancelled'
    STATUS_CHOICES = [(CLEAR, 'Clear'), (HELD, 'Held for review'), (APPROVED, 'Approved by staff'),
                      (CANCELLED, 'Cancelled as fraud')]

    order = models.OneToOneField('order.Order', on_delete=models.CASCADE, related_name='risk')
    score = models.PositiveSmallIntegerField()
    level = models.CharField(max_length=6, choices=LEVEL_CHOICES)
    reasons = models.JSONField(default=list)       # [{"code", "label", "points"}]
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default=CLEAR, db_index=True)
    reviewed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    reviewed_at = models.DateTimeField(null=True, blank=True)
    note = models.CharField(max_length=255, blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [models.Index(fields=['level', 'status', '-created_at'], name='risk_queue_idx')]

    def __str__(self):
        return f"{self.order_id}: {self.score} ({self.level})"
