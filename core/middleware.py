class CurrencyMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        currency = request.headers.get('X-Currency', 'GHS')
        request.currency = currency
        return self.get_response(request)
