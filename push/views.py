import hashlib

from django.conf import settings
from rest_framework import serializers, status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import PushSubscription
from .services import fcm_enabled, webpush_enabled

MAX_PER_USER = 20


class PushConfigView(APIView):
    permission_classes = [AllowAny]

    def get(self, request):
        return Response({'webpush': webpush_enabled(), 'fcm': fcm_enabled(),
                         'vapid_public_key': getattr(settings, 'WEBPUSH_VAPID_PUBLIC_KEY', '') if webpush_enabled() else ''})


class SubscribeSerializer(serializers.Serializer):
    app = serializers.ChoiceField(choices=PushSubscription.APP_CHOICES)
    endpoint = serializers.URLField(required=False, max_length=2000)
    keys = serializers.DictField(child=serializers.CharField(max_length=200), required=False)
    fcm_token = serializers.CharField(required=False, max_length=4096)

    def validate(self, attrs):
        if attrs.get('fcm_token'):
            return attrs
        keys = attrs.get('keys') or {}
        if not attrs.get('endpoint') or not keys.get('p256dh') or not keys.get('auth'):
            raise serializers.ValidationError('Send a browser push subscription or an FCM token.')
        if not attrs['endpoint'].startswith('https://'):
            raise serializers.ValidationError('Push endpoint must be https.')
        return attrs


def _hash(endpoint):
    return hashlib.sha256(endpoint.encode()).hexdigest()


class SubscriptionView(APIView):
    """POST a browser PushSubscription (or FCM token) for an app · DELETE {endpoint|fcm_token} to stop."""
    permission_classes = [IsAuthenticated]

    def post(self, request):
        ser = SubscribeSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        d = ser.validated_data
        if d['app'] == PushSubscription.SELLER:
            from vendor.access import is_vendor
            if not is_vendor(request.user):
                return Response({'detail': 'Seller notifications need a seller sign-in.'}, status=status.HTTP_403_FORBIDDEN)
        endpoint = d.get('fcm_token') or d['endpoint']
        sub = PushSubscription.objects.filter(endpoint_hash=_hash(endpoint)).first() or PushSubscription(endpoint=endpoint)
        sub.user, sub.app = request.user, d['app']
        sub.kind = PushSubscription.FCM if d.get('fcm_token') else PushSubscription.WEBPUSH
        keys = d.get('keys') or {}
        sub.p256dh, sub.auth = keys.get('p256dh', ''), keys.get('auth', '')
        sub.user_agent = request.headers.get('User-Agent', '')[:200]
        sub.failures = 0
        sub.save()
        # Keep each user's device list bounded.
        stale = list(PushSubscription.objects.filter(user=request.user).order_by('-created_at')
                     .values_list('pk', flat=True)[MAX_PER_USER:])
        PushSubscription.objects.filter(pk__in=stale).delete()
        return Response({'ok': True}, status=status.HTTP_201_CREATED)

    def delete(self, request):
        endpoint = request.data.get('endpoint') or request.data.get('fcm_token') or ''
        PushSubscription.objects.filter(user=request.user, endpoint_hash=_hash(endpoint)).delete()
        return Response(status=status.HTTP_204_NO_CONTENT)
