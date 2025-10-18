from django.db import models
from django.utils import timezone
from django.core.validators import EmailValidator, RegexValidator
from userauths.models import User
from .geocode import geocode_address  # import your helper


class Country(models.Model):
    name = models.CharField(max_length=40, unique=True)
    code = models.CharField(max_length=3, unique=True, blank=True, null=True, default='GH')  # ISO code like 'GH', 'US', etc.

    class Meta:
        verbose_name_plural = "Countries"

    def __str__(self):
        return f"{self.name} ({self.code})"


class Region(models.Model):
    country = models.ForeignKey(Country, on_delete=models.CASCADE)
    name = models.CharField(max_length=40)

    class Meta:
        verbose_name_plural = "Regions"

    def __str__(self):
        return self.name


class Town(models.Model):
    fee = models.FloatField(default=0, null=True, blank=True)
    region = models.ForeignKey(Region, on_delete=models.CASCADE)
    name = models.CharField(max_length=40)

    class Meta:
        verbose_name_plural = "Towns"

    def __str__(self):
        return self.name

class Location(models.Model):
    user = models.ForeignKey(User, on_delete=models.SET_NULL, null=True)
    country = models.ForeignKey(Country, on_delete=models.CASCADE)
    region = models.ForeignKey(Region, on_delete=models.CASCADE)
    town = models.ForeignKey(Town, on_delete=models.CASCADE)
    session_id = models.CharField(max_length=100, null=True, blank=True)
    
    def __str__(self):
        return self.country.name

    class Meta:
        verbose_name_plural = "Location"

class Address(models.Model):
    user = models.ForeignKey(User, related_name='address', on_delete=models.CASCADE, null=True)
    full_name = models.CharField(max_length=50, null=True, blank=True)
    country = models.CharField(max_length=20, null=True, blank=True)
    region = models.CharField(max_length=30, null=True,blank=True)
    town = models.CharField(max_length=30, null=True, blank=True)
    address = models.CharField(max_length=300, null=True, blank=True)
    gps_address = models.CharField(max_length=20, null=True, blank=True)
    email = models.EmailField(null=True, blank=True, validators=[EmailValidator()])
    mobile = models.CharField(max_length=15, null=True, blank=True, validators=[RegexValidator(r'^\+?\d{10,15}$', message="Invalid phone number.")])
    status = models.BooleanField(default=False)
    latitude = models.FloatField(null=True, blank=True, default=5.5600)
    longitude = models.FloatField(null=True, blank=True, default=-0.2050)
    modified_at = models.DateTimeField(auto_now=True)
    date_added = models.DateTimeField(default=timezone.now)

    def __str__(self):
        return f"{self.full_name} - {self.user.email} ({self.status})"

    class Meta:
        verbose_name_plural = "Addresses"
        ordering = ('-status',)
        constraints = [
            models.UniqueConstraint(fields=['user'], condition=models.Q(status=True), name='unique_default_address')
        ]

    def save(self, *args, **kwargs):
        # Auto-geocode if lat/lon not provided
        if not self.latitude or not self.longitude:
            parts = [self.town, self.region, self.country]
            query = ", ".join([p for p in parts if p])
            lat, lon = geocode_address(query)
            if lat and lon:
                self.latitude = lat
                self.longitude = lon
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.full_name} - {self.user.email} ({'Default' if self.status else 'Secondary'})"

