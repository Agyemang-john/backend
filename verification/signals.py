"""A verified store that changes where payouts go is re-checked by staff (account-takeover guard)."""
from django.db.models.signals import post_save
from django.dispatch import receiver

from vendor.models import VendorPaymentMethod


@receiver(post_save, sender=VendorPaymentMethod, dispatch_uid='kyc_payout_changed')
def payout_method_saved(sender, instance, created, **kwargs):
    if kwargs.get('raw'):
        return
    vendor = instance.vendor
    if not vendor.is_approved:
        return
    from .models import VerificationProfile
    from .services import log
    VerificationProfile.objects.update_or_create(vendor=vendor, defaults={'needs_recheck': True})
    log(vendor, 'changed', f"Payout method {'added' if created else 'changed'}: {instance.payment_method}")
