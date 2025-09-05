# apps/newsletters/serializers.py
from rest_framework import serializers
from .models import Subscriber

class SubscriberSerializer(serializers.ModelSerializer):
    class Meta:
        model = Subscriber
        fields = ["email", "first_name", "last_name", "locale", "country"]

class ConfirmSerializer(serializers.Serializer):
    token = serializers.CharField()
