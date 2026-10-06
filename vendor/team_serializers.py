"""
vendor/team_serializers.py
Read/write shapes for the store-team endpoints in team_views.py.
"""

from rest_framework import serializers

from .access import capabilities_for_role
from .models import VendorInvitation, VendorMember


class VendorMemberSerializer(serializers.ModelSerializer):
    first_name = serializers.CharField(source='user.first_name', read_only=True)
    last_name = serializers.CharField(source='user.last_name', read_only=True)
    email = serializers.EmailField(source='user.email', read_only=True)
    capabilities = serializers.SerializerMethodField()
    is_you = serializers.SerializerMethodField()

    class Meta:
        model = VendorMember
        fields = [
            'id', 'first_name', 'last_name', 'email', 'role', 'capabilities',
            'is_you', 'created_at',
        ]

    def get_capabilities(self, obj):
        return sorted(capabilities_for_role(obj.role))

    def get_is_you(self, obj):
        request = self.context.get('request')
        return bool(request and obj.user_id == request.user.pk)


class VendorInvitationSerializer(serializers.ModelSerializer):
    invited_by_name = serializers.SerializerMethodField()
    status = serializers.SerializerMethodField()

    class Meta:
        model = VendorInvitation
        fields = ['id', 'email', 'role', 'invited_by_name', 'status', 'created_at', 'expires_at']

    def get_invited_by_name(self, obj):
        user = obj.invited_by
        return f"{user.first_name} {user.last_name}".strip() if user else None

    def get_status(self, obj):
        return 'pending' if obj.is_pending else 'expired'


class CreateInvitationSerializer(serializers.Serializer):
    email = serializers.EmailField(max_length=128)
    role = serializers.ChoiceField(choices=[VendorMember.ROLE_ADMIN, VendorMember.ROLE_STAFF])

    def validate_email(self, value):
        return value.strip().lower()


class UpdateMemberSerializer(serializers.Serializer):
    role = serializers.ChoiceField(choices=[VendorMember.ROLE_ADMIN, VendorMember.ROLE_STAFF])


class AcceptInvitationSerializer(serializers.Serializer):
    token = serializers.CharField(max_length=128)
