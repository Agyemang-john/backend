import logging
from decimal import Decimal
from django.conf import settings
from django.db.models import Sum
from cryptography.fernet import Fernet
import requests
from celery import shared_task
from vendor.models import Vendor, VendorPaymentMethod
from payments.models import Payout
from order.models import Order

logger = logging.getLogger('payouts')

class PayoutService:
    def __init__(self):
        self.api_key = settings.HUBTEL_API_KEY
        self.base_url = "https://api.hubtel.com/v2/pos/payouts"

    def get_decrypted_momo_number(self, payment_method):
        """Decrypt the momo_number from the payment method."""
        if not payment_method.momo_number:
            logger.warning(f"No momo_number for vendor {payment_method.vendor.id}")
            return None
        try:
            fernet = Fernet(settings.FERNET_KEY.encode())
            decrypted = fernet.decrypt(payment_method.momo_number.encode()).decode()
            logger.info(f"Decrypted momo_number for vendor {payment_method.vendor.id}")
            return decrypted
        except Exception as e:
            logger.error(f"Decryption failed for vendor {payment_method.vendor.id}: {e}")
            return None

    def create_transfer_recipient(self, momo_number, momo_provider, vendor):
        """Create a Hubtel recipient (phone number and network)."""
        provider_mapping = {
            'MTN': 'mtn-gh',
            'VODAFONE': 'vodafone-gh',
            'AIRTELTIGO': 'airteltigo-gh'
        }
        network = provider_mapping.get(momo_provider)
        if not network:
            logger.error(f"Unsupported provider {momo_provider} for vendor {vendor.id}")
            return None
        return {"phoneNumber": momo_number, "network": network}

    def initiate_transfer(self, recipient, amount, reason, vendor):
        """Initiate a Hubtel transfer to the recipient."""
        payload = {
            "recipient": recipient,
            "amount": float(amount),
            "description": reason,
            "clientReference": f"payout-{vendor.id}"
        }
        headers = {
            "Authorization": f"Basic {self.api_key}",
            "Content-Type": "application/json"
        }
        try:
            response = requests.post(self.base_url, json=payload, headers=headers)
            response_data = response.json()
            if response.status_code == 200 and response_data.get("status") == "success":
                logger.info(f"Transfer initiated: {response_data['transactionId']}")
                return {
                    "status": "success",
                    "transaction_id": response_data["transactionId"],
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

    def process_vendor_payout(self, vendor, orders, amount):
        """Process payout for a vendor based on orders."""
        try:
            payment_method = VendorPaymentMethod.objects.get(vendor=vendor, payment_method='momo', status='verified')
            momo_number = self.get_decrypted_momo_number(payment_method)
            momo_provider = payment_method.momo_provider

            if not momo_number or not momo_provider:
                logger.error(f"Invalid payment details for vendor {vendor.id}")
                return {"status": "error", "message": "Invalid or missing payment details."}

            recipient = self.create_transfer_recipient(momo_number, momo_provider, vendor)
            if not recipient:
                return {"status": "error", "message": "Failed to create transfer recipient."}

            reason = f"Payout for orders {', '.join([str(o.order_number) for o in orders])}"
            result = self.initiate_transfer(recipient, amount, reason, vendor)

            payout = Payout.objects.create(
                vendor=vendor,
                amount=amount,
                status="success" if result["status"] == "success" else "failed",
                transaction_id=result.get("transaction_id"),
                error_message=result.get("message") if result["status"] == "error" else None
            )
            payout.order.set(orders)
            return result
        except VendorPaymentMethod.DoesNotExist:
            logger.error(f"No verified mobile money payment method for vendor {vendor.id}")
            return {"status": "error", "message": "No verified mobile money payment method found."}
        except Exception as e:
            logger.error(f"Payout error for vendor {vendor.id}: {e}")
            return {"status": "error", "message": f"Payout error: {str(e)}"}

@shared_task
def batch_payouts():
    """Celery task to process payouts for all vendors."""
    logger.info("Starting batch payout process")
    vendors = Vendor.objects.filter(payment_methods__payment_method='momo', payment_methods__status='verified').distinct()
    
    for vendor in vendors:
        orders = Order.objects.filter(
            vendors=vendor,
            status='delivered',
            payouts__isnull=True
        )
        if not orders.exists():
            logger.info(f"No eligible orders for vendor {vendor.id}")
            continue

        total_amount = sum(Decimal(str(order.get_vendor_total(vendor))) * Decimal('0.8') for order in orders)
        if total_amount <= 0:
            logger.info(f"No positive amount to pay for vendor {vendor.id}")
            continue

        logger.info(f"Processing payout of {total_amount} GHS for vendor {vendor.id}")
        payout_service = PayoutService()
        result = payout_service.process_vendor_payout(vendor, orders, total_amount)
        
        if result["status"] == "success":
            logger.info(f"Payout successful for vendor {vendor.id}: {result['transaction_id']}")
        else:
            logger.error(f"Payout failed for vendor {vendor.id}: {result['message']}")