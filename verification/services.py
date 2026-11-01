"""KYC rules: which documents a store needs, number checks, submissions, staff decisions."""
import re
from difflib import SequenceMatcher

from django.db import transaction
from django.utils import timezone

from .models import KycEvent, VerificationProfile

DOC_LABELS = {
    'government_issued_id': 'Government-issued ID',
    'student_id': 'Student ID',
    'license': 'Business registration certificate / licence',
    'proof_of_address': 'Proof of address',
}
GHANA_CARD_RE = re.compile(r'^GHA-\d{9}-\d$')
PASSPORT_RE = re.compile(r'^[A-Z][A-Z0-9]{6,8}$')
BUSINESS_REG_RE = re.compile(r'^[A-Z]{2,3}\d{6,12}$')    # e.g. CS123456789 / BN123456789


class KycError(Exception):
    pass


def required_documents(vendor):
    if vendor.vendor_type == 'student':
        return ['student_id', 'proof_of_address']
    docs = ['government_issued_id', 'proof_of_address']
    if vendor.business_type not in ('sole_proprietor', 'other', None, ''):
        docs.append('license')
    return docs


def profile_for(vendor):
    profile, _ = VerificationProfile.objects.get_or_create(vendor=vendor)
    return profile


def clean_id_number(id_type, raw):
    value = re.sub(r'\s+', '', (raw or '').upper())
    if not value:
        return ''
    if id_type == VerificationProfile.GHANA_CARD:
        if re.fullmatch(r'GHA\d{10}', value):
            value = f"GHA-{value[3:12]}-{value[12]}"
        if not GHANA_CARD_RE.match(value):
            raise KycError("Enter your Ghana Card number like GHA-123456789-0.")
    elif id_type == VerificationProfile.PASSPORT and not PASSPORT_RE.match(value):
        raise KycError("Enter the passport number as printed (letters and digits).")
    elif len(value) < 5:
        raise KycError("Enter the full ID number.")
    return value


def clean_business_reg(raw):
    value = re.sub(r'[\s-]+', '', (raw or '').upper())
    if value and not BUSINESS_REG_RE.match(value):
        raise KycError("Enter the registration number as on your certificate, e.g. CS123456789.")
    return value


def _doc_state(vendor, profile, field):
    if not getattr(vendor, field):
        return {'state': VerificationProfile.MISSING, 'reason': ''}
    return profile.documents.get(field) or {'state': VerificationProfile.PENDING, 'reason': ''}


def overview(vendor):
    profile = profile_for(vendor)
    docs = [{'field': f, 'label': DOC_LABELS[f], **_doc_state(vendor, profile, f)} for f in required_documents(vendor)]
    return {
        'status': vendor.status,
        'is_approved': vendor.is_approved,
        'id_type': profile.id_type,
        'id_number_masked': (profile.id_number[:4] + '•' * max(0, len(profile.id_number) - 6) + profile.id_number[-2:])
        if profile.id_number else '',
        'business_reg_number': profile.business_reg_number,
        'documents': docs,
        'review_note': profile.review_note if vendor.status == 'REJECTED' else '',
        'submitted_at': profile.submitted_at,
        'needs_business_reg': 'license' in required_documents(vendor),
    }


def log(vendor, kind, detail='', actor=None):
    KycEvent.objects.create(vendor=vendor, kind=kind, detail=detail[:500], actor=actor)


@transaction.atomic
def submit(vendor, user, *, id_type=None, id_number=None, business_reg_number=None, files=None):
    """Seller (re)submits details and/or documents. Changed documents go back to 'pending'."""
    from vendor.kyc import clean_kyc_upload

    profile = VerificationProfile.objects.select_for_update().get_or_create(vendor=vendor)[0]
    changed = []
    if id_type is not None:
        if id_type not in dict(VerificationProfile.ID_TYPES):
            raise KycError("Choose an ID type.")
        profile.id_type = id_type
    if id_number is not None:
        new = clean_id_number(profile.id_type, id_number)
        if new != profile.id_number:
            profile.id_number = new
            changed.append('ID number')
    if business_reg_number is not None:
        new = clean_business_reg(business_reg_number)
        if new != profile.business_reg_number:
            profile.business_reg_number = new
            changed.append('business registration number')
    for field, upload in (files or {}).items():
        if field not in DOC_LABELS:
            continue
        clean_kyc_upload(upload)
        setattr(vendor, field, upload)
        profile.documents[field] = {'state': VerificationProfile.PENDING, 'reason': ''}
        changed.append(DOC_LABELS[field])
    if not changed and id_type is None:
        raise KycError("Nothing to submit.")
    vendor.save()
    profile.submitted_at = timezone.now()
    if vendor.is_approved:
        profile.needs_recheck = True      # approved store changed its identity details
    profile.save()
    if vendor.status == 'REJECTED':
        type(vendor).objects.filter(pk=vendor.pk).update(status='PENDING')
        vendor.status = 'PENDING'
    log(vendor, 'changed' if vendor.is_approved else 'submitted', ', '.join(changed), user)
    return profile


@transaction.atomic
def review(vendor, staff_user, decisions, note=''):
    """
    decisions: {field: ('accepted'|'rejected', reason)}. Approves the store when every
    required document is accepted; rejects (with reasons to the seller) when any is rejected.
    """
    profile = VerificationProfile.objects.select_for_update().get_or_create(vendor=vendor)[0]
    for field, (state, reason) in decisions.items():
        if field not in DOC_LABELS or state not in (VerificationProfile.ACCEPTED, VerificationProfile.REJECTED):
            continue
        if state == VerificationProfile.REJECTED and not (reason or '').strip():
            raise KycError(f"Give a reason for rejecting the {DOC_LABELS[field].lower()}.")
        profile.documents[field] = {'state': state, 'reason': (reason or '').strip()[:300]}
    profile.reviewed_at, profile.reviewed_by, profile.review_note = timezone.now(), staff_user, note[:2000]
    profile.needs_recheck = False
    profile.save()

    required = required_documents(vendor)
    states = {f: _doc_state(vendor, profile, f)['state'] for f in required}
    rejected = [f for f, s in states.items() if s == VerificationProfile.REJECTED]
    missing = [f for f, s in states.items() if s == VerificationProfile.MISSING]
    if rejected or missing:
        if vendor.status in ('PENDING', 'REJECTED'):
            vendor.status, vendor.is_approved = 'REJECTED', False
            vendor.save()      # Vendor.save sends the not-approved email/SMS when is_approved changes
        reasons = '; '.join(f"{DOC_LABELS[f]}: {profile.documents[f]['reason']}" for f in rejected)
        if missing:
            reasons = '; '.join(filter(None, [reasons, 'Missing: ' + ', '.join(DOC_LABELS[f] for f in missing)]))
        log(vendor, 'rejected', reasons, staff_user)
        _notify(vendor, f"Please update your verification documents. {reasons}")
        return 'rejected'
    if all(s == VerificationProfile.ACCEPTED for s in states.values()):
        if not vendor.is_approved:
            today = timezone.localdate()
            vendor.status, vendor.is_approved, vendor.is_suspended = 'VERIFIED', True, False
            if not vendor.subscription_end_date:
                from datetime import timedelta
                vendor.subscription_start_date, vendor.subscription_end_date = today, today + timedelta(days=365)
                vendor.is_subscribed = True
            vendor.save()      # sends the approval email/SMS
        log(vendor, 'approved', note, staff_user)
        _notify(vendor, "Your store is verified. You can start selling.")
        return 'approved'
    log(vendor, 'reviewed', note, staff_user)
    return 'pending'


def payout_name_match(vendor):
    """0-100 similarity between the bank account name and the store / owner names (None if no bank account)."""
    method = vendor.payment_methods.exclude(bank_account_name__isnull=True).exclude(bank_account_name='').first()
    if method is None:
        return None
    account = _norm(method.bank_account_name)
    names = [_norm(vendor.name), _norm(f"{vendor.user.first_name} {vendor.user.last_name}" if vendor.user else '')]
    return max(int(100 * SequenceMatcher(None, account, n).ratio()) for n in names if n) if any(names) else None


def _norm(s):
    s = re.sub(r'\b(ltd|limited|enterprise|enterprises|ventures|company|co)\b', '', (s or '').lower())
    return ' '.join(sorted(re.sub(r'[^a-z ]', ' ', s).split()))


def _notify(vendor, message):
    from notification.utils import send_notification
    if vendor.user_id:
        send_notification(vendor.user, 'verification_update', target=vendor,
                          data={'message': message, 'url': '/verification'})
