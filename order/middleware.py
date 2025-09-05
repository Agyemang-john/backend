import uuid
from django.utils.deprecation import MiddlewareMixin
from django.conf import settings

 
class CartIDMiddleware(MiddlewareMixin):
    """
    Middleware to ensure every user (guest or authenticated) has a `cart_id` cookie.
    This prevents errors when handling carts.
    """

    def process_response(self, request, response):
        if "cart_id" not in request.COOKIES:
            cart_id = str(uuid.uuid4())
            response.set_cookie(
                key="cart_id",
                value=cart_id,
                max_age=365 * 24 * 60 * 60,  # 1 year
                path="/",
                secure=not settings.DEBUG,  # Secure in production
                httponly=True,
                samesite="None",
            )
        return response