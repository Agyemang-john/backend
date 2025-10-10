import os
import requests
from dotenv import load_dotenv

load_dotenv()

API_KEY = os.getenv("ARKESEL_API_KEY")

class APIKeyMissingError(Exception):
    pass

if not API_KEY:
    raise APIKeyMissingError(
        "All methods require an API key. See "
        "https://sms.arkesel.com/user/sms-api/info "
        "for how to retrieve an API key from Arkesel."
    )


class ArkeselSMS:
    BASE_URL = "https://sms.arkesel.com/api/v2"

    def __init__(self, api_key: str = API_KEY):
        self.api_key = api_key
        self.headers = {
            "api-key": self.api_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def send_sms(self, sender: str, message: str, recipients: list[str]):
        url = f"{self.BASE_URL}/sms/send"
        payload = {"sender": sender, "message": message, "recipients": recipients}
        response = requests.post(url, headers=self.headers, json=payload)
        return response.json()

    def scheduled_sms(self, sender: str, message: str, recipients: list[str], scheduled_date: str):
        url = f"{self.BASE_URL}/sms/send"
        payload = {
            "sender": sender,
            "message": message,
            "recipients": recipients,
            "scheduled_date": scheduled_date,
        }
        response = requests.post(url, headers=self.headers, json=payload)
        return response.json()

    def webhook_sms(self, sender: str, message: str, recipients: list[str], callback_url: str):
        url = f"{self.BASE_URL}/sms/send"
        payload = {
            "sender": sender,
            "message": message,
            "recipients": recipients,
            "callback_url": callback_url,
        }
        response = requests.post(url, headers=self.headers, json=payload)
        return response.json()

    def sandbox_sms(self, sender: str, message: str, recipients: list[str], sandbox: bool = True):
        url = f"{self.BASE_URL}/sms/send"
        payload = {"sender": sender, "message": message, "recipients": recipients, "sandbox": sandbox}
        response = requests.post(url, headers=self.headers, json=payload)
        return response.json()

    def voice_sms(self, voice_file: str, recipients: list[str]):
        url = f"{self.BASE_URL}/sms/voice/send"
        payload = {"voice_file": voice_file, "recipients": recipients}
        response = requests.post(url, headers=self.headers, json=payload)
        return response.json()

    def send_group_sms(self, sender: str, group_name: str, message: str):
        url = f"{self.BASE_URL}/sms/send/contact-group"
        payload = {"sender": sender, "group_name": group_name, "message": message}
        response = requests.post(url, headers=self.headers, json=payload)
        return response.json()


class SmsInfo:
    BASE_URL = "https://sms.arkesel.com/api/v2"

    def __init__(self, api_key: str = API_KEY):
        self.api_key = api_key
        self.headers = {
            "api-key": self.api_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def sms_balance(self):
        url = f"{self.BASE_URL}/clients/balance-details"
        response = requests.get(url, headers=self.headers)
        return response.json()

    def sms_details(self, message_id: str):
        url = f"{self.BASE_URL}/sms/{message_id}"
        response = requests.get(url, headers=self.headers)
        return response.json()
