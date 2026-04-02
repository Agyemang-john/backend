"""
payments/paystack.py
Lightweight wrapper around the Paystack REST API.

Usage:
    paystack = Paystack()
    ok, data = paystack.verify_payment("REF_123")
"""

from django.conf import settings
import requests


class Paystack:
    PAYSTACK_SK = settings.PAYSTACK_PRIVATE_KEY
    base_url = "https://api.paystack.co/"

    def verify_payment(self, ref, *args, **kwargs):
        """
        Verify a Paystack transaction by reference.
        Returns (status: bool, data_or_message: dict|str).
        """
        path = f'transaction/verify/{ref}'
        headers = {
            "Authorization": f"Bearer {self.PAYSTACK_SK}",
            "Content-Type": "application/json",
        }
        url = self.base_url + path
        response = requests.get(url, headers=headers)

        response_data = response.json()
        if response.status_code == 200:
            return response_data['status'], response_data['data']

        return response_data['status'], response_data['message']
