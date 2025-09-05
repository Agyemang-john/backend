
# middleware.py
import requests

class GeoIPMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        ip = request.META.get('REMOTE_ADDR', '')
        try:
            response = requests.get(f"https://ipapi.co/{ip}/json/").json()
            request.country = response.get("country_name")
        except:
            request.country = None
        return self.get_response(request)
