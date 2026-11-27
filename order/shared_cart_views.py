"""Endpoints for sharing a cart (order/shared_cart.py)."""
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from order import shared_cart


class ShareCartView(APIView):
    """POST {name?, note?, items?: ["productId_variantId|none", ...]} → a link to the requester's cart."""
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'cart_share'

    def post(self, request):
        try:
            share = shared_cart.create_share(
                request, request.data.get('name', ''), request.data.get('note', ''), request.data.get('items') or None)
        except shared_cart.ShareError as exc:
            return Response({'detail': str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response({
            'token': share.token,
            'path': f'/cart/shared/{share.token}',
            'expires_at': share.expires_at,
            'items': len(share.items),
        }, status=status.HTTP_201_CREATED)


class SharedCartView(APIView):
    """GET a shared cart: the items at today's price and stock."""
    permission_classes = [AllowAny]

    def get(self, request, token):
        share = shared_cart.get_share(token)
        if share is None:
            return Response({'detail': 'This shared cart has expired or the link is wrong.'},
                            status=status.HTTP_404_NOT_FOUND)
        # ?view=1 only from the page's browser visit, so link previews and SSR don't count.
        if request.query_params.get('view') == '1':
            shared_cart.record_view(share, request)
        return Response(shared_cart.describe(share, request))


class AddSharedCartView(APIView):
    """POST {items?: [{product_id, variant_id, quantity}]} → into the requester's own cart."""
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'cart_share_add'

    def post(self, request, token):
        share = shared_cart.get_share(token)
        if share is None:
            return Response({'detail': 'This shared cart has expired or the link is wrong.'},
                            status=status.HTTP_404_NOT_FOUND)
        try:
            result = shared_cart.add_to_cart(share, request, request.data.get('items'))
        except shared_cart.ShareError as exc:
            return Response({'detail': str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(result)
