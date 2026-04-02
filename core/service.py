"""
core/service.py
Business logic helpers for the core app.

get_exchange_rates():
    Fetches live exchange rates from the ExchangeRate API, caches for 24 hours.
    Falls back to the CurrencyRate model if the API is unavailable, cached for 1 hour.
    Last resort: hardcoded USD rate so the site never breaks on currency conversion.
"""

import logging
import requests
from django.core.cache import cache
from django.conf import settings
from .models import CurrencyRate

logger = logging.getLogger(__name__)


def get_exchange_rates():
    """
    Return a dict of {currency_code: rate_relative_to_GHS}.
    Results are cached; falls back to DB then hardcoded rates on failure.
    """
    cache_key = 'exchange_rates'
    rates = cache.get(cache_key)
    if not rates:
        try:
            response = requests.get(
                f'https://v6.exchangerate-api.com/v6/{settings.EXCHANGE_RATE_API_KEY}/latest/GHS'
            )
            response.raise_for_status()
            rates = response.json()['conversion_rates']
            cache.set(cache_key, rates, timeout=86400)  # 24 hours
        except Exception as e:
            logger.error(f"Failed to fetch exchange rates from API: {e}")
            # Fallback: read rates from the CurrencyRate table
            rates = {rate.currency: rate.rate for rate in CurrencyRate.objects.all()}
            if not rates.get('USD'):
                rates['USD'] = 0.094  # Hardcoded last-resort fallback
            cache.set(cache_key, rates, timeout=3600)  # 1 hour
    return rates