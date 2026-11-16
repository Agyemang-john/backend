# address/serializers.py
"""
Address book input/output.

Accepts the country as an ISO code (`country_code`, preferred) or a name
(`country`, older clients) and stores both. Phones are stored as E.164,
Ghana Post digital addresses as "GA-183-8164". Required fields and labels
follow the country's format (address/formats.py).
"""

from rest_framework import serializers

from address.models import Address
from . import formats


class AddressSerializer(serializers.ModelSerializer):
    full_name = serializers.CharField(max_length=100, error_messages={
        'required': 'Enter the recipient’s full name.',
        'blank': 'Enter the recipient’s full name.',
    })
    mobile = serializers.CharField(max_length=24, error_messages={
        'required': 'Enter a phone number so the rider can reach you.',
        'blank': 'Enter a phone number so the rider can reach you.',
    })
    email = serializers.EmailField(required=False, allow_blank=True, allow_null=True)
    country = serializers.CharField(required=False, allow_blank=True, max_length=60)
    country_code = serializers.CharField(required=False, allow_blank=True, max_length=2)
    region = serializers.CharField(required=False, allow_blank=True, allow_null=True, max_length=80)
    town = serializers.CharField(max_length=80, error_messages={
        'required': 'Enter the city or town.', 'blank': 'Enter the city or town.',
    })
    address = serializers.CharField(max_length=300, error_messages={
        'required': 'Enter the street address or area.', 'blank': 'Enter the street address or area.',
    })
    address_line2 = serializers.CharField(required=False, allow_blank=True, max_length=120)
    landmark = serializers.CharField(required=False, allow_blank=True, max_length=120)
    gps_address = serializers.CharField(required=False, allow_blank=True, allow_null=True, max_length=20)
    delivery_instructions = serializers.CharField(required=False, allow_blank=True, max_length=300)
    latitude = serializers.FloatField(required=False, allow_null=True, min_value=-90, max_value=90)
    longitude = serializers.FloatField(required=False, allow_null=True, min_value=-180, max_value=180)
    location_source = serializers.ChoiceField(
        required=False, allow_blank=True,
        choices=[Address.SOURCE_GPS, Address.SOURCE_PIN, Address.SOURCE_SEARCH])
    status = serializers.BooleanField(required=False, default=False)

    one_line = serializers.CharField(read_only=True)
    street_lines = serializers.ListField(child=serializers.CharField(), read_only=True)
    is_precise = serializers.BooleanField(read_only=True)
    is_pickup = serializers.SerializerMethodField()
    pickup_point_name = serializers.SerializerMethodField()

    class Meta:
        model = Address
        fields = [
            'id', 'user', 'label', 'full_name', 'mobile', 'email',
            'country', 'country_code', 'region', 'town', 'address', 'address_line2', 'landmark',
            'gps_address', 'delivery_instructions', 'latitude', 'longitude', 'location_source',
            'status', 'date_added', 'last_used_at',
            'one_line', 'street_lines', 'is_precise', 'is_pickup', 'pickup_point_name',
        ]
        read_only_fields = ['id', 'user', 'date_added', 'last_used_at']

    def get_is_pickup(self, obj):
        return bool(obj.pickup_point_id)

    def get_pickup_point_name(self, obj):
        return obj.pickup_point.name if obj.pickup_point_id else None

    def to_representation(self, instance):
        data = super().to_representation(instance)
        # Older rows only stored the name.
        if not data.get('country_code') and instance.country:
            data['country_code'] = formats.resolve_country(instance.country)[1] or ''
        return data

    def validate(self, data):
        errors = {}
        partial = self.instance is not None and self.partial

        def current(field):
            if field in data:
                return data[field]
            return getattr(self.instance, field, None) if self.instance is not None else None

        # Country: code wins; a name is accepted from older clients.
        raw_country = data.get('country_code') or data.get('country')
        if raw_country or not partial:
            name, code = formats.resolve_country(raw_country or current('country_code') or current('country'))
            if not code:
                errors['country_code'] = ['Choose a country.']
            else:
                data['country'], data['country_code'] = name, code
        code = data.get('country_code') or current('country_code') or ''
        fmt = formats.format_for(code)

        if 'mobile' in data:
            try:
                data['mobile'] = formats.normalize_phone(data['mobile'], code)
            except formats.InvalidValue as exc:
                errors['mobile'] = [str(exc)]

        if 'gps_address' in data or not partial:
            try:
                data['gps_address'] = formats.normalize_postal(data.get('gps_address') or '', code)
            except formats.InvalidValue as exc:
                errors['gps_address'] = [str(exc)]
            if fmt['postal_required'] and not data.get('gps_address') and 'gps_address' not in errors:
                errors['gps_address'] = [f"Enter the {fmt['postal_label'].lower()}."]

        if 'region' in data or not partial:
            region = ' '.join((data.get('region') or '').split())
            if not region and code == 'GH':
                region = formats.region_from_digital_address(data.get('gps_address')) or ''
            if region and fmt['regions']:
                # Match the official spelling ("greater accra region" → "Greater Accra").
                wanted = region.lower().removesuffix(' region').strip()
                region = next((r for r in fmt['regions'] if r.lower() == wanted), region)
            if not region and fmt['region_required']:
                errors['region'] = [f"Choose the {fmt['region_label'].lower()}."]
            data['region'] = region

        for field in ('full_name', 'town', 'address', 'address_line2', 'landmark', 'delivery_instructions'):
            if field in data and isinstance(data[field], str):
                data[field] = ' '.join(data[field].split())

        has_lat, has_lon = data.get('latitude') is not None, data.get('longitude') is not None
        if has_lat != has_lon:
            errors['latitude'] = ['Send both latitude and longitude, or neither.']
        if not (has_lat and has_lon):
            data.pop('latitude', None)
            data.pop('longitude', None)
            data.pop('location_source', None)

        if errors:
            raise serializers.ValidationError(errors)
        return data
