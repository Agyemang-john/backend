"""
Pickup points: a shopper collects the order from a partner station instead of
home delivery. Choosing a station makes it the delivery address (an Address
with pickup_point set, at the station's coordinates), so delivery fees and
couriers work as usual; the courier's "delivered" means "at the station".
Each pickup order gets a PickupParcel with a collection code the station
checks before handing it over.
"""
from django.conf import settings
from django.db import models


class PickupPoint(models.Model):
    name = models.CharField(max_length=100)
    code = models.SlugField(max_length=20, unique=True)
    partner = models.CharField(max_length=80, blank=True, help_text="Who runs it, e.g. a shop or courier hub.")
    address = models.CharField(max_length=255)
    town = models.CharField(max_length=60, db_index=True)
    region = models.CharField(max_length=60, blank=True)
    country = models.CharField(max_length=20, default='Ghana')
    latitude = models.FloatField()
    longitude = models.FloatField()
    phone = models.CharField(max_length=20, blank=True)
    opening_hours = models.CharField(max_length=160, blank=True, help_text="e.g. Mon–Sat 8am–7pm")
    hold_days = models.PositiveSmallIntegerField(default=7, help_text="How long a parcel waits before it's returned.")
    is_active = models.BooleanField(default=True, db_index=True)

    def __str__(self):
        return f"{self.name} ({self.town})"


class PickupPointStaff(models.Model):
    """People who may hand parcels over at a station."""
    point = models.ForeignKey(PickupPoint, on_delete=models.CASCADE, related_name='staff')
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='pickup_points')

    class Meta:
        constraints = [models.UniqueConstraint(fields=['point', 'user'], name='uniq_pickup_staff')]


class PickupParcel(models.Model):
    AWAITING, READY, COLLECTED, EXPIRED, CANCELLED = 'awaiting', 'ready', 'collected', 'expired', 'cancelled'
    STATUS_CHOICES = [(AWAITING, 'On its way to the station'), (READY, 'Ready to collect'),
                      (COLLECTED, 'Collected'), (EXPIRED, 'Not collected in time'), (CANCELLED, 'Cancelled')]

    order = models.OneToOneField('order.Order', on_delete=models.CASCADE, related_name='pickup_parcel')
    point = models.ForeignKey(PickupPoint, on_delete=models.PROTECT, related_name='parcels')
    code = models.CharField(max_length=6)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default=AWAITING, db_index=True)
    ready_at = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateTimeField(null=True, blank=True, db_index=True)
    collected_at = models.DateTimeField(null=True, blank=True)
    collected_by_staff = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    failed_attempts = models.PositiveSmallIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [models.Index(fields=['point', 'status'], name='pickup_parcel_point_idx')]

    def __str__(self):
        return f"Parcel for order {self.order_id} at {self.point_id}"
