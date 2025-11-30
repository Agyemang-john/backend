from rest_framework import serializers
from .models import OpeningHour

DAYS = [
    (1, 'Monday'),
    (2, 'Tuesday'),
    (3, 'Wednesday'),
    (4, 'Thursday'),
    (5, 'Friday'),
    (6, 'Saturday'),
    (7, 'Sunday'),
]

class OpeningHourSerializer(serializers.ModelSerializer):
    day_display = serializers.CharField(source='get_day_display', read_only=True)
    from_hour = serializers.TimeField(format='%I:%M %p', input_formats=['%I:%M %p'], required=False, allow_null=True)
    to_hour = serializers.TimeField(format='%I:%M %p', input_formats=['%I:%M %p'], required=False, allow_null=True)

    class Meta:
        model = OpeningHour
        fields = ['id', 'vendor', 'day', 'day_display', 'from_hour', 'to_hour', 'is_closed']
        read_only_fields = ['id', 'vendor', 'day_display']

    def validate(self, data):
        # Extract data
        vendor = self.context['request'].user.vendor_user
        day = data.get('day')
        from_hour = data.get('from_hour')
        to_hour = data.get('to_hour')
        is_closed = data.get('is_closed', False)
        instance = self.instance  # The instance being updated (if any)

        # Check for existing day for this vendor
        existing_day_query = OpeningHour.objects.filter(vendor=vendor, day=day)
        if instance:  # Exclude the current instance during updates
            existing_day_query = existing_day_query.exclude(pk=instance.pk)
        if existing_day_query.exists():
            raise serializers.ValidationError({
                'day': f"The day {DAYS[day-1][1]} is already set for this vendor. Each day can only be added once."
            })

        # Validate required fields when not closed
        if not is_closed:
            if not from_hour or not to_hour:
                raise serializers.ValidationError({
                    'from_hour': 'This field is required when day is not closed.',
                    'to_hour': 'This field is required when day is not closed.'
                })
            if from_hour >= to_hour:
                raise serializers.ValidationError({
                    'from_hour': 'From Hour must be earlier than To Hour.',
                    'to_hour': 'To Hour must be later than From Hour.'
                })
        else:
            data['from_hour'] = None
            data['to_hour'] = None

        # Check for unique_together constraint
        if not is_closed:  # Only check if not closed, as closed days don't have hours
            query = OpeningHour.objects.filter(
                vendor=vendor,
                day=day,
                from_hour=from_hour,
                to_hour=to_hour
            )
            if instance:  # Exclude the current instance during updates
                query = query.exclude(pk=instance.pk)
            if query.exists():
                raise serializers.ValidationError({
                    'non_field_errors': [
                        f"An opening hour for {DAYS[day-1][1]} from {from_hour.strftime('%I:%M %p')} to {to_hour.strftime('%I:%M %p')} already exists."
                    ]
                })

        return data

    def create(self, validated_data):
        validated_data['vendor'] = self.context['request'].user.vendor_user
        return super().create(validated_data)