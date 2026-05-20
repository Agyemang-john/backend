import os
import logging
import requests
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

_API_KEY = os.getenv("ARKESEL_API_KEY")

if not _API_KEY:
    logger.warning(
        "ARKESEL_API_KEY is not set. SMS tasks will be disabled until the key is configured."
    )


class ArkeselSMS:
    BASE_URL = "https://sms.arkesel.com/api/v2"

    def __init__(self, api_key: str = None):
        self.api_key = api_key or _API_KEY

    def _headers(self):
        if not self.api_key:
            raise RuntimeError(
                "ARKESEL_API_KEY is not configured. Set it in your environment variables."
            )
        return {
            "api-key": self.api_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def send_sms(self, sender: str, message: str, recipients: list):
        url = f"{self.BASE_URL}/sms/send"
        payload = {"sender": sender, "message": message, "recipients": recipients}
        response = requests.post(url, headers=self._headers(), json=payload)
        return response.json()

    def scheduled_sms(self, sender: str, message: str, recipients: list, scheduled_date: str):
        url = f"{self.BASE_URL}/sms/send"
        payload = {
            "sender": sender,
            "message": message,
            "recipients": recipients,
            "scheduled_date": scheduled_date,
        }
        response = requests.post(url, headers=self._headers(), json=payload)
        return response.json()

    def webhook_sms(self, sender: str, message: str, recipients: list, callback_url: str):
        url = f"{self.BASE_URL}/sms/send"
        payload = {
            "sender": sender,
            "message": message,
            "recipients": recipients,
            "callback_url": callback_url,
        }
        response = requests.post(url, headers=self._headers(), json=payload)
        return response.json()

    def sandbox_sms(self, sender: str, message: str, recipients: list, sandbox: bool = True):
        url = f"{self.BASE_URL}/sms/send"
        payload = {"sender": sender, "message": message, "recipients": recipients, "sandbox": sandbox}
        response = requests.post(url, headers=self._headers(), json=payload)
        return response.json()

    def voice_sms(self, voice_file: str, recipients: list):
        url = f"{self.BASE_URL}/sms/voice/send"
        payload = {"voice_file": voice_file, "recipients": recipients}
        response = requests.post(url, headers=self._headers(), json=payload)
        return response.json()

    def send_group_sms(self, sender: str, group_name: str, message: str):
        url = f"{self.BASE_URL}/sms/send/contact-group"
        payload = {"sender": sender, "group_name": group_name, "message": message}
        response = requests.post(url, headers=self._headers(), json=payload)
        return response.json()


class SmsInfo:
    BASE_URL = "https://sms.arkesel.com/api/v2"

    def __init__(self, api_key: str = None):
        self.api_key = api_key or _API_KEY

    def _headers(self):
        if not self.api_key:
            raise RuntimeError(
                "ARKESEL_API_KEY is not configured. Set it in your environment variables."
            )
        return {
            "api-key": self.api_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def sms_balance(self):
        url = f"{self.BASE_URL}/clients/balance-details"
        response = requests.get(url, headers=self._headers())
        return response.json()

    def sms_details(self, message_id: str):
        url = f"{self.BASE_URL}/sms/{message_id}"
        response = requests.get(url, headers=self._headers())
        return response.json()
