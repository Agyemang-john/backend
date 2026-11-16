"""
Address book API and the address search behind the form.

    GET/POST         addresses/                 the shopper's book / add one
    GET/PUT/PATCH/DEL addresses/<id>/           one address (edits of used addresses make a new row)
    GET/PUT          addresses/default/         the default / make one the default
    GET              search/?q=&country=&lat=&lon=&lang=&session=   type-ahead suggestions
    GET              place/?ref=&session=       the full place behind a suggestion
    GET              reverse/?lat=&lon=         the address at a point (device location, map pin)
    GET              formats/<CC>/              labels, regions, postcode rule, dial code

Business rules live in address/services.py; providers in address/geo.py.
"""

import logging

from django.utils.cache import patch_cache_control
from rest_framework import status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import SimpleRateThrottle
from rest_framework.views import APIView

from address.models import Address
from . import delivery, formats, geo, services
from .serializers import AddressSerializer

logger = logging.getLogger(__name__)


def _book(user):
    return services.book(user).select_related('pickup_point')


def _lang(request):
    lang = (request.query_params.get('lang') or request.COOKIES.get('language') or 'en')[:2].lower()
    return lang if lang in ('en', 'fr') else 'en'


class AddressListCreateView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        return Response(AddressSerializer(_book(request.user), many=True).data)

    def post(self, request):
        serializer = AddressSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            address, created = services.create(request.user, dict(serializer.validated_data))
        except services.AddressLimitReached:
            return Response(
                {'code': 'address_limit', 'detail': f'You can save up to {services.MAX_ADDRESSES} addresses. '
                                                    'Delete one you no longer use to add another.'},
                status=status.HTTP_400_BAD_REQUEST)
        address.refresh_from_db()
        data = {**AddressSerializer(address).data, 'merged': not created}
        response = Response(data, status=status.HTTP_201_CREATED if created else status.HTTP_200_OK)
        delivery.sync_cookie(response, request, request.user)
        return response


class AddressDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def _get(self, request, id):
        return _book(request.user).filter(pk=id).first()

    def get(self, request, id):
        address = self._get(request, id)
        if address is None:
            return Response({'detail': 'Address not found.'}, status=status.HTTP_404_NOT_FOUND)
        return Response(AddressSerializer(address).data)

    def put(self, request, id, partial=False):
        address = self._get(request, id)
        if address is None:
            return Response({'detail': 'Address not found.'}, status=status.HTTP_404_NOT_FOUND)
        serializer = AddressSerializer(address, data=request.data, partial=partial)
        serializer.is_valid(raise_exception=True)
        saved = services.update(request.user, address, dict(serializer.validated_data))
        saved.refresh_from_db()
        # `id` changes when the old address had orders: the client swaps it in.
        response = Response({**AddressSerializer(saved).data, 'replaced_id': address.pk if saved.pk != address.pk else None})
        delivery.sync_cookie(response, request, request.user)
        return response

    def patch(self, request, id):
        return self.put(request, id, partial=True)

    def delete(self, request, id):
        address = self._get(request, id)
        if address is None:
            return Response({'detail': 'Address not found.'}, status=status.HTTP_404_NOT_FOUND)
        default = services.remove(request.user, address)
        response = Response({'deleted': id, 'default_id': default.pk if default else None})
        delivery.sync_cookie(response, request, request.user)
        return response


class MakeDefaultAddressView(APIView):
    permission_classes = [IsAuthenticated]

    def put(self, request):
        address_id = request.data.get('id')
        if not address_id:
            return Response({'error': 'Address ID is required'}, status=status.HTTP_400_BAD_REQUEST)
        address = _book(request.user).filter(pk=address_id).first()
        if address is None:
            return Response({'error': 'Address not found'}, status=status.HTTP_404_NOT_FOUND)
        services.set_default(request.user, address)
        response = Response({'success': True, 'message': 'Address set as default',
                             'address': AddressSerializer(address).data})
        delivery.set_cookie(response, request, address.country_code or delivery.country_code(address.country))
        return response

    def get(self, request):
        default = _book(request.user).filter(status=True).first()
        if default is None:
            return Response({'message': 'No default address found'}, status=status.HTTP_404_NOT_FOUND)
        return Response(AddressSerializer(default).data)


# ── Address search ───────────────────────────────────────────────────────────

class GeoThrottle(SimpleRateThrottle):
    """Type-ahead fires per keystroke; this caps runaway clients, not shoppers."""
    scope = 'address_geo'
    rate = '120/min'

    def get_cache_key(self, request, view):
        ident = request.user.pk if request.user and request.user.is_authenticated else self.get_ident(request)
        return self.cache_format % {'scope': self.scope, 'ident': ident}


def _unavailable():
    return Response({'code': 'geo_unavailable', 'detail': 'Address search is unavailable right now. '
                                                         'Type the address instead.'},
                    status=status.HTTP_503_SERVICE_UNAVAILABLE)


def _point(params):
    try:
        lat, lon = float(params.get('lat')), float(params.get('lon'))
    except (TypeError, ValueError):
        return None
    return (lat, lon) if -90 <= lat <= 90 and -180 <= lon <= 180 else None


class GeoSearchView(APIView):
    permission_classes = [IsAuthenticated]
    throttle_classes = [GeoThrottle]

    def get(self, request):
        p = request.query_params
        try:
            results = geo.search(p.get('q', ''), country_code=p.get('country'), near=_point(p),
                                 lang=_lang(request), session=(p.get('session') or '')[:64] or None)
        except geo.GeoUnavailable:
            return _unavailable()
        return Response({'results': results, 'provider': geo.provider_name()})


class GeoPlaceView(APIView):
    permission_classes = [IsAuthenticated]
    throttle_classes = [GeoThrottle]

    def get(self, request):
        p = request.query_params
        try:
            place = geo.resolve(p.get('ref', ''), lang=_lang(request), session=(p.get('session') or '')[:64] or None)
        except geo.PlaceNotFound:
            return Response({'code': 'place_not_found', 'detail': 'That place has expired. Search again.'},
                            status=status.HTTP_404_NOT_FOUND)
        except geo.GeoUnavailable:
            return _unavailable()
        return Response(place)


class GeoReverseView(APIView):
    # Guests too: "use my location" picks their delivery country.
    permission_classes = [AllowAny]
    throttle_classes = [GeoThrottle]

    def get(self, request):
        point = _point(request.query_params)
        if point is None:
            return Response({'detail': 'lat and lon are required.'}, status=status.HTTP_400_BAD_REQUEST)
        try:
            place = geo.reverse(point[0], point[1], lang=_lang(request))
        except geo.PlaceNotFound:
            return Response({'code': 'place_not_found', 'detail': 'We couldn’t find an address here.'},
                            status=status.HTTP_404_NOT_FOUND)
        except geo.GeoUnavailable:
            return _unavailable()
        return Response(place)


class AddressFormatView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []

    def get(self, request, code):
        _, iso = formats.resolve_country(code)
        if not iso:
            return Response({'detail': 'Unknown country.'}, status=status.HTTP_404_NOT_FOUND)
        response = Response(formats.format_for(iso))
        patch_cache_control(response, public=True, max_age=60 * 60 * 24)
        return response
