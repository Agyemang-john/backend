# ecommerce/asgi.py — FINAL VERSION
import os
from django.core.asgi import get_asgi_application

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'ecommerce.settings')
django_asgi_app = get_asgi_application()

from channels.routing import ProtocolTypeRouter, URLRouter
from notification.middleware import QueryStringJWTAuthMiddleware
import notification.routing

application = ProtocolTypeRouter({
    "http": django_asgi_app,
    "websocket": QueryStringJWTAuthMiddleware(
        URLRouter(
            notification.routing.websocket_urlpatterns
        )
    ),
})