# notifications/routing.py
from django.urls import re_path
from . import consumers
import order.routing
import vendor.routing

websocket_urlpatterns = [
    re_path(r"^ws/notifications/count/$", consumers.NotificationCountConsumer.as_asgi()),
    re_path(r"^ws/notifications/$", consumers.NotificationConsumer.as_asgi()),
    *order.routing.websocket_urlpatterns,
    *vendor.routing.websocket_urlpatterns,
]