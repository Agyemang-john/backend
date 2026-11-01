from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.throttling import AnonRateThrottle
from rest_framework.views import APIView

from . import collector

MAX_BATCH = 25


def _visitor(request):
    return collector.clean_visitor(request.headers.get('X-Visitor-ID') or request.COOKIES.get('visitor_id', ''))


class CollectThrottle(AnonRateThrottle):
    rate = '120/min'


class CollectView(APIView):
    """POST {events: [{name}]} from the storefront. Unknown names are dropped; bots are ignored."""
    permission_classes = [AllowAny]
    throttle_classes = [CollectThrottle]

    def post(self, request):
        from product.utils import detect_bot
        visitor = _visitor(request)
        events = request.data.get('events') if isinstance(request.data, dict) else None
        if not visitor or not isinstance(events, list) or detect_bot(request):
            return Response(status=status.HTTP_204_NO_CONTENT)
        collector.link_user(visitor, request.user)
        variants = collector.visitor_experiments(visitor)
        for e in events[:MAX_BATCH]:
            name = e.get('name') if isinstance(e, dict) else None
            if name != 'purchase':          # purchases are recorded by the server
                collector.record(name, visitor, variants)
        return Response(status=status.HTTP_204_NO_CONTENT)


class ExperimentsView(APIView):
    """GET {experiment_key: variant} for this visitor (records the exposure)."""
    permission_classes = [AllowAny]

    def get(self, request):
        visitor = _visitor(request)
        if not visitor:
            return Response({})
        collector.link_user(visitor, request.user)
        return Response(collector.assignments(visitor, request.user))
