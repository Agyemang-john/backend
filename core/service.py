import requests
from django.core.cache import cache
from django.conf import settings
from .models import CurrencyRate

def get_exchange_rates():
    cache_key = 'exchange_rates'
    rates = cache.get(cache_key)
    if not rates:
        try:
            response = requests.get(
                f'https://v6.exchangerate-api.com/v6/{settings.EXCHANGE_RATE_API_KEY}/latest/GHS'
            )
            response.raise_for_status()  # Raise exception for bad status codes
            rates = response.json()['conversion_rates']
            cache.set(cache_key, rates, timeout=86400)  # Cache for 24 hours
        except Exception as e:
            print(f"Error fetching rates: {e}")
            # Fallback to CurrencyRate model
            rates = {rate.currency: rate.rate for rate in CurrencyRate.objects.all()}
            if not rates.get('USD'):
                rates['USD'] = 0.094  # Hard-coded fallback if model is empty
            cache.set(cache_key, rates, timeout=3600)  # Cache fallback for 1 hour
    return rates