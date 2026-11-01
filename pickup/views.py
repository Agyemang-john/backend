from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from . import services
from .models import PickupParcel, PickupPoint, PickupPointStaff


def _point_json(p, dist=None):
    return {'id': p.pk, 'name': p.name, 'code': p.code, 'partner': p.partner, 'address': p.address, 'town': p.town,
            'region': p.region, 'latitude': p.latitude, 'longitude': p.longitude, 'phone': p.phone,
            'opening_hours': p.opening_hours, 'hold_days': p.hold_days,
            'distance_km': round(dist, 1) if dist is not None else None}


class PointsView(APIView):
    """?lat=&lon= (nearest first) and/or ?q= (name/town)."""
    permission_classes = [AllowAny]

    def get(self, request):
        try:
            lat = float(request.query_params['lat']) if request.query_params.get('lat') else None
            lon = float(request.query_params['lon']) if request.query_params.get('lon') else None
        except ValueError:
            lat = lon = None
        if lat is None or lon is None:
            lat = lon = None
            # Fall back to the shopper's saved location.
            if request.user.is_authenticated:
                from address.models import Address
                a = Address.objects.filter(user=request.user, status=True).values('latitude', 'longitude').first()
                if a and a['latitude'] is not None:
                    lat, lon = a['latitude'], a['longitude']
        rows = services.nearby(lat, lon, request.query_params.get('q', ''))
        return Response({'results': [_point_json(p, d) for d, p in rows]})


class SelectPointView(APIView):
    """POST {point}: deliver my order to this station (becomes my active delivery address)."""
    permission_classes = [IsAuthenticated]

    def post(self, request):
        from address import delivery
        point = get_object_or_404(PickupPoint, pk=request.data.get('point') or 0, is_active=True)
        try:
            addr = services.select_point(request.user, point)
        except services.PickupError as exc:
            return Response({'detail': str(exc)}, status=exc.status)
        response = Response({'address_id': addr.pk, 'point': _point_json(point)})
        delivery.set_cookie(response, request, delivery.country_code(addr.country))
        return response


class OrderParcelView(APIView):
    """The shopper's pickup parcel for an order (code shown once it's on its way)."""
    permission_classes = [IsAuthenticated]

    def get(self, request, order_id):
        parcel = get_object_or_404(PickupParcel.objects.select_related('point', 'order'), order_id=order_id,
                                   order__user=request.user)
        return Response({
            'status': parcel.status, 'status_label': parcel.get_status_display(),
            'code': parcel.code if parcel.status in (PickupParcel.AWAITING, PickupParcel.READY) else None,
            'ready_at': parcel.ready_at, 'expires_at': parcel.expires_at, 'collected_at': parcel.collected_at,
            'point': _point_json(parcel.point),
        })


class StationParcelsView(APIView):
    """Station staff: parcels at my station(s). ?status=ready|awaiting|all"""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        points = list(PickupPointStaff.objects.filter(user=request.user).values_list('point_id', flat=True))
        if not points:
            return Response({'detail': "You're not set up for a pickup station."}, status=status.HTTP_403_FORBIDDEN)
        qs = PickupParcel.objects.filter(point_id__in=points).select_related('order', 'point').order_by('-ready_at', '-created_at')
        f = request.query_params.get('status', 'ready')
        if f != 'all':
            qs = qs.filter(status=f)
        return Response({'results': [{
            'order_number': p.order.order_number, 'status': p.status, 'point': p.point.name,
            'customer': getattr(p.order.address, 'full_name', '') if p.order.address_id else '',
            'ready_at': p.ready_at, 'expires_at': p.expires_at,
        } for p in qs[:200]]})


class StationCollectView(APIView):
    """Station staff: POST {code, order_number} to hand a parcel over."""
    permission_classes = [IsAuthenticated]

    def post(self, request):
        try:
            parcel = services.collect(request.user, request.data.get('code'), request.data.get('order_number', ''))
        except services.PickupError as exc:
            return Response({'detail': str(exc)}, status=exc.status)
        return Response({'order_number': parcel.order.order_number, 'collected_at': parcel.collected_at})
