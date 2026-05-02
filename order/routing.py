from django.urls import re_path
from . import consumers

websocket_urlpatterns = [
    re_path(r"^ws/order/(?P<order_id>\d+)/tracking/$", consumers.OrderTrackingConsumer.as_asgi()),
]
