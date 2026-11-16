"""
Models for the address app.

Country / Region / Town / Location are reference tables (Town carries a flat
fee). Address is a shopper's saved delivery address.

Address book rules (address/services.py):
- An address an order already shipped to is never edited in place. Editing it
  archives that row and saves the change as a new row, so every order keeps
  the exact address it went to (invoices, couriers, sellers, disputes).
- Deleting an address with orders archives it instead.
- `archived_at` rows are hidden from the shopper; `Address.objects.active()`.
- `country` holds the country *name* (fees compare names), `country_code` the
  ISO alpha-2 code (delivery cookie, couriers, formats).
"""

from django.db import models
from django.utils import timezone
from django.core.validators import EmailValidator, RegexValidator
from userauths.models import User


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


class AddressQuerySet(models.QuerySet):
    def active(self):
        """What the shopper sees in their address book."""
        return self.filter(archived_at__isnull=True)


class Address(models.Model):
    LABEL_CHOICES = (('home', 'Home'), ('work', 'Work'), ('other', 'Other'))

    # How we know where the address is on the map, best first. Fees and
    # couriers use the coordinates, so the UI nudges shoppers to pin
    # anything weaker than 'search'.
    SOURCE_GPS = 'gps'          # the shopper's device location
    SOURCE_PIN = 'pin'          # dragged on the map
    SOURCE_SEARCH = 'search'    # picked from address search
    SOURCE_GEOCODED = 'geocoded'  # we looked up the typed street/town
    SOURCE_APPROX = 'approx'    # nothing matched; coordinates are a stand-in
    SOURCE_CHOICES = (
        (SOURCE_GPS, 'Device location'), (SOURCE_PIN, 'Map pin'), (SOURCE_SEARCH, 'Address search'),
        (SOURCE_GEOCODED, 'Looked up'), (SOURCE_APPROX, 'Approximate'),
    )
    PRECISE_SOURCES = (SOURCE_GPS, SOURCE_PIN, SOURCE_SEARCH)

    user = models.ForeignKey(User, related_name='address', on_delete=models.CASCADE, null=True)
    label = models.CharField(max_length=10, choices=LABEL_CHOICES, default='home')
    full_name = models.CharField(max_length=100, null=True, blank=True)
    country = models.CharField(max_length=60, null=True, blank=True)
    country_code = models.CharField(max_length=2, blank=True, default='', db_index=True)
    region = models.CharField(max_length=80, null=True, blank=True)
    town = models.CharField(max_length=80, null=True, blank=True)
    # Street line. Named `address` for the many readers that predate line 2.
    address = models.CharField(max_length=300, null=True, blank=True)
    address_line2 = models.CharField(max_length=120, blank=True, default='')
    landmark = models.CharField(max_length=120, blank=True, default='')
    # Ghana Post digital address (GA-183-8164) or a postcode elsewhere.
    gps_address = models.CharField(max_length=20, null=True, blank=True)
    delivery_instructions = models.CharField(max_length=300, blank=True, default='')
    email = models.EmailField(null=True, blank=True, validators=[EmailValidator()])
    mobile = models.CharField(max_length=20, null=True, blank=True, validators=[RegexValidator(r'^\+?\d{8,15}$', message="Invalid phone number.")])
    status = models.BooleanField(default=False)
    latitude = models.FloatField(null=True, blank=True, default=5.5600)
    longitude = models.FloatField(null=True, blank=True, default=-0.2050)
    location_source = models.CharField(max_length=10, choices=SOURCE_CHOICES, blank=True, default='')
    # Set when this address is a pickup station the shopper collects from (pickup/).
    pickup_point = models.ForeignKey('pickup.PickupPoint', on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    archived_at = models.DateTimeField(null=True, blank=True, db_index=True)
    # The newer version this row was replaced by when edited after use.
    replaced_by = models.ForeignKey('self', on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    last_used_at = models.DateTimeField(null=True, blank=True)
    modified_at = models.DateTimeField(auto_now=True)
    date_added = models.DateTimeField(default=timezone.now)

    objects = AddressQuerySet.as_manager()

    class Meta:
        verbose_name_plural = "Addresses"
        ordering = ('-status', '-last_used_at', '-date_added')
        constraints = [
            models.UniqueConstraint(fields=['user'], condition=models.Q(status=True), name='unique_default_address')
        ]

    @property
    def is_precise(self):
        return self.location_source in self.PRECISE_SOURCES

    @property
    def street_lines(self):
        """Street, line 2 and landmark, the way a courier reads them."""
        lines = [self.address or '', self.address_line2 or '']
        if self.landmark:
            lines.append(f"Near {self.landmark}")
        return [l.strip() for l in lines if l and l.strip()]

    @property
    def one_line(self):
        parts = self.street_lines + [self.town or '', self.region or '', self.gps_address or '', self.country or '']
        return ', '.join(p.strip() for p in parts if p and p.strip())

    def __str__(self):
        email = self.user.email if self.user_id else (self.email or 'guest')
        state = 'Archived' if self.archived_at else ('Default' if self.status else 'Secondary')
        return f"{self.full_name} - {email} ({state})"
