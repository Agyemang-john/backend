import logging
import requests
from decimal import Decimal
from django.conf import settings
from django.db.models import Sum
from django.core.validators import RegexValidator
from celery import shared_task
from vendor.models import Vendor, VendorPaymentMethod
from payments.models import Payout
from order.models import Order
from userauths.models import User
import re
import difflib
from django.core.exceptions import ValidationError

logger = logging.getLogger('payouts')

class PayoutService:
    def __init__(self):
        self.api_key = settings.PAYSTACK_SECRET_KEY
        self.base_url = "https://api.paystack.co"
        self.ghana_banks_cache = None
        self.stopwords = {'BANK', 'GHANA', 'LIMITED', 'LTD', 'PLC', 'AND', 'LOANS', 'SAVINGS', 'AFRICA', 'AGRICULTURAL', 'DEVELOPMENT'}

    def get_ghana_banks(self):
        """Fetch list of Ghanaian banks from Paystack API."""
        if self.ghana_banks_cache:
            return self.ghana_banks_cache
        url = f"{self.base_url}/bank?country=ghana"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json"
        }
        try:
            response = requests.get(url, headers=headers)
            response_data = response.json()
            if response.status_code == 200 and response_data.get("status"):
                filtered_banks = [
                    bank for bank in response_data['data']
                    if bank.get('currency') == 'GHS' and bank.get('type') == 'ghipss' and bank.get('active')
                ]
                self.ghana_banks_cache = {
                    self.normalize_bank_name(bank['name']): bank['code']
                    for bank in filtered_banks
                }
                logger.info(f"Fetched {len(self.ghana_banks_cache)} GHS banks from Paystack API")
                return self.ghana_banks_cache
            else:
                logger.error(f"Failed to fetch banks: {response_data.get('message')}")
                return {}
        except Exception as e:
            logger.error(f"Error fetching banks: {e}")
            return {}

    def normalize_bank_name(self, name):
        """Normalize bank name: upper, remove stopwords, strip."""
        words = [word.upper() for word in name.split() if word.upper() not in self.stopwords]
        return ' '.join(words)

    def extract_keywords(self, user_input):
        """Extract key words/abbreviations from user input (e.g., 'ADB Bank' -> ['ADB'])."""
        words = [word.upper().strip() for word in re.split(r'\W+', user_input) if len(word) > 1]
        abbrevs = [w for w in words if len(w) <= 3]
        return abbrevs[0] if abbrevs else words[0] if words else user_input.upper()

    def get_bank_code(self, bank_name, country='ghana'):
        """Get bank code with fuzzy matching to handle misspellings/abbreviations."""
        if country.lower() != 'ghana':
            logger.error(f"Only Ghana supported for now: {country}")
            return None, 0.0

        ghana_banks = self.get_ghana_banks()
        if not ghana_banks:
            return None, 0.0

        user_name_upper = bank_name.upper()
        normalized_user = self.normalize_bank_name(bank_name)
        key_abbrev = self.extract_keywords(bank_name)

        # Exact match (original or normalized)
        if user_name_upper in ghana_banks:
            return ghana_banks[user_name_upper], 1.0
        for norm_key in ghana_banks:
            if normalized_user == norm_key:
                return ghana_banks[norm_key], 1.0

        # Fuzzy match on full name
        matches = difflib.get_close_matches(user_name_upper, ghana_banks.keys(), n=1, cutoff=0.75)
        if matches:
            score = difflib.SequenceMatcher(None, user_name_upper, matches[0]).ratio()
            logger.info(f"Fuzzy match for '{bank_name}': '{matches[0]}' (score: {score:.2f})")
            return ghana_banks[matches[0]], score

        # Abbreviation/keyword fallback
        for key, code in ghana_banks.items():
            if key_abbrev in key:
                score = difflib.SequenceMatcher(None, key_abbrev, key).ratio()
                logger.info(f"Abbrev match for '{bank_name}': '{key}' (score: {score:.2f})")
                return code, score

        logger.warning(f"No match found for bank name: '{bank_name}'")
        return None, 0.0

    def create_momo_recipient(self, momo_number, momo_provider, vendor):
        """Create a Paystack transfer recipient for Mobile Money (Ghana)."""
        provider_mapping = {
            'MTN': 'mtn',
            'VODAFONE': 'vodafone',
            'AIRTELTIGO': 'airteltigo'
        }
        mobile_network = provider_mapping.get(momo_provider)
        if not mobile_network:
            logger.error(f"Unsupported provider {momo_provider} for vendor {vendor.id}")
            return None

        payload = {
            "type": "mobile_money",
            "name": str(vendor),
            "account_number": momo_number,
            "bank_code": mobile_network,
            "currency": "GHS"
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json"
        }
        try:
            response = requests.post(f"{self.base_url}/transferrecipient", json=payload, headers=headers)
            response_data = response.json()
            if response.status_code == 201 and response_data.get("status"):
                logger.info(f"Created MoMo recipient for vendor {vendor.id}: {response_data['data']['recipient_code']}")
                return response_data["data"]["recipient_code"]
            else:
                logger.error(f"Failed to create MoMo recipient for vendor {vendor.id}: {response_data.get('message')}")
                return None
        except Exception as e:
            logger.error(f"Error creating MoMo recipient for vendor {vendor.id}: {e}")
            return None

    def create_bank_recipient(self, account_number, bank_name, vendor):
        """Create a Paystack transfer recipient for a bank account (Ghana)."""
        bank_code, match_score = self.get_bank_code(bank_name)
        if not bank_code:
            logger.error(f"No valid bank code found for '{bank_name}' (match score too low) for vendor {vendor.id}")
            return None

        payload = {
            "type": "nuban",
            "name": str(vendor),
            "account_number": account_number,
            "bank_code": bank_code,
            "currency": "GHS"
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json"
        }
        try:
            response = requests.post(f"{self.base_url}/transferrecipient", json=payload, headers=headers)
            response_data = response.json()
            if response.status_code == 201 and response_data.get("status"):
                logger.info(f"Created bank recipient for vendor {vendor.id} (bank: {bank_name}, code: {bank_code}, match: {match_score:.2f}): {response_data['data']['recipient_code']}")
                return response_data["data"]["recipient_code"]
            else:
                logger.error(f"Failed to create bank recipient for vendor {vendor.id}: {response_data.get('message')}")
                return None
        except Exception as e:
            logger.error(f"Error creating bank recipient for vendor {vendor.id}: {e}")
            return None

    def initiate_transfer(self, recipient_code, amount, reason):
        """Initiate a Paystack transfer to the recipient."""
        payload = {
            "source": "balance",
            "amount": int(amount * 100),  # Paystack uses kobo (cents)
            "recipient": recipient_code,
            "reason": reason
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json"
        }
        try:
            response = requests.post(f"{self.base_url}/transfer", json=payload, headers=headers)
            response_data = response.json()
            if response.status_code == 200 and response_data.get("status"):
                logger.info(f"Transfer initiated: {response_data['data']['reference']}")
                return {
                    "status": "success",
                    "transaction_id": response_data["data"]["reference"],
                    "message": "Transfer initiated successfully."
                }
            else:
                logger.error(f"Transfer failed: {response_data.get('message')}")
                return {
                    "status": "error",
                    "message": response_data.get("message", "Transfer failed.")
                }
        except Exception as e:
            logger.error(f"Transfer error: {e}")
            return {
                "status": "error",
                "message": f"Transfer error: {str(e)}"
            }

    def process_vendor_payout(self, vendor, orders, amount, product_total, delivery_fee):
        """Process payout for a vendor based on orders, storing product total and delivery fee."""
        try:
            payment_method = VendorPaymentMethod.objects.filter(
                vendor=vendor, 
                status='verified'
            ).first()
            if not payment_method:
                logger.error(f"No verified payment method for vendor {vendor.id}")
                return {"status": "error", "message": "No verified payment method found."}

            payment_type = payment_method.payment_method

            if payment_type == 'momo':
                if not payment_method.momo_number or not payment_method.momo_provider:
                    logger.error(f"Invalid MoMo details for vendor {vendor.id}")
                    return {"status": "error", "message": "Invalid MoMo details."}
                recipient_code = self.create_momo_recipient(
                    payment_method.momo_number, 
                    payment_method.momo_provider, 
                    vendor
                )
            elif payment_type == 'bank':
                if not payment_method.bank_account_number or not payment_method.bank_name:
                    logger.error(f"Invalid bank details for vendor {vendor.id}")
                    return {"status": "error", "message": "Invalid bank details."}
                recipient_code = self.create_bank_recipient(
                    payment_method.bank_account_number,
                    payment_method.bank_name, 
                    vendor
                )
            else:
                logger.error(f"Unsupported payment method {payment_type} for vendor {vendor.id}")
                return {"status": "error", "message": f"Unsupported payment method: {payment_type}"}

            if not recipient_code:
                return {"status": "error", "message": "Failed to create transfer recipient."}

            reason = f"Payout for orders {', '.join([str(o.order_number) for o in orders])}"
            result = self.initiate_transfer(recipient_code, amount, reason)

            payout = Payout.objects.create(
                vendor=vendor,
                amount=amount,
                product_total=product_total,
                delivery_fee=delivery_fee,
                status="success" if result["status"] == "success" else "failed",
                transaction_id=result.get("transaction_id"),
                error_message=result.get("message") if result["status"] == "error" else None
            )
            payout.order.set(orders)
            return result
        except Exception as e:
            logger.error(f"Payout error for vendor {vendor.id}: {e}")
            return {"status": "error", "message": f"Payout error: {str(e)}"}

@shared_task
def batch_payouts():
    """Celery task to process payouts for all vendors with verified payment methods."""
    logger.info("Starting batch payout process")
    vendors = Vendor.objects.filter(
        payment_methods__status='verified'
    ).distinct()
    
    for vendor in vendors:
        orders = Order.objects.filter(
            vendors=vendor,
            status='delivered',
            payouts__isnull=True
        )
        if not orders.exists():
            logger.info(f"No eligible orders for vendor {vendor.id}")
            continue

        product_total = sum(Decimal(str(order.get_vendor_total(vendor))) for order in orders)
        delivery_fee = sum(Decimal(str(order.calculate_vendor_delivery_fee(vendor))) for order in orders)
        total_amount = (product_total + delivery_fee) * Decimal('0.8')

        if total_amount <= 0:
            logger.info(f"No positive amount to pay for vendor {vendor.id}")
            continue

        logger.info(f"Processing payout of {total_amount} GHS for vendor {vendor.id} (Products: {product_total}, Delivery: {delivery_fee})")
        payout_service = PayoutService()
        result = payout_service.process_vendor_payout(vendor, orders, total_amount, product_total, delivery_fee)
        
        if result["status"] == "success":
            logger.info(f"Payout successful for vendor {vendor.id}: {result['transaction_id']}")
        else:
            logger.error(f"Payout failed for vendor {vendor.id}: {result['message']}")
            from django.core.mail import send_mail
            send_mail(
                subject="Payout Failure",
                message=f"Payout failed for vendor {vendor.id}: {result['message']}",
                from_email="no-reply@negromart.com",
                recipient_list=["admin@negromart.com"]
            )