"""
Seller verification (KYC) on top of the documents stored on Vendor (vendor/kyc.py).

Staff decide each document separately (accepted / rejected with a reason), so
a seller re-uploads only what was wrong. Every step is logged in KycEvent.
"""
from django.conf import settings
from django.db import models


class VerificationProfile(models.Model):
    GHANA_CARD, PASSPORT, VOTER_ID, DRIVERS_LICENCE = 'ghana_card', 'passport', 'voter_id', 'drivers_licence'
    ID_TYPES = [(GHANA_CARD, 'Ghana Card'), (PASSPORT, 'Passport'), (VOTER_ID, "Voter's ID"),
                (DRIVERS_LICENCE, "Driver's licence")]

    PENDING, ACCEPTED, REJECTED, MISSING = 'pending', 'accepted', 'rejected', 'missing'

    vendor = models.OneToOneField('vendor.Vendor', on_delete=models.CASCADE, related_name='verification')
    id_type = models.CharField(max_length=16, choices=ID_TYPES, blank=True, default='')
    id_number = models.CharField(max_length=32, blank=True, default='')
    business_reg_number = models.CharField(max_length=32, blank=True, default='',
                                           help_text="Registrar-General's (ORC) number, for registered businesses.")
    # {"government_issued_id": {"state": "rejected", "reason": "Photo is blurry"}, ...}
    documents = models.JSONField(default=dict, blank=True)
    submitted_at = models.DateTimeField(null=True, blank=True)
    reviewed_at = models.DateTimeField(null=True, blank=True)
    reviewed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    review_note = models.TextField(blank=True, default='')
    # Something changed after approval (new ID, new payout account): staff should look again.
    needs_recheck = models.BooleanField(default=False, db_index=True)

    def __str__(self):
        return f"Verification for {self.vendor}"


class KycEvent(models.Model):
    vendor = models.ForeignKey('vendor.Vendor', on_delete=models.CASCADE, related_name='kyc_events')
    kind = models.CharField(max_length=20)       # submitted | reviewed | approved | rejected | changed
    detail = models.CharField(max_length=500, blank=True, default='')
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ('-created_at',)
        indexes = [models.Index(fields=['vendor', '-created_at'], name='kyc_event_vendor_idx')]
