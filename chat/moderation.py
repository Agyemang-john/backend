"""
chat/moderation.py
Keeps deals on Negromart: refuses chat messages that share contact details or
try to move the conversation or the payment somewhere else.

    check(text)                 → sorted list of categories found ([] = allowed)
    check(text, recent=[...])   → also catches a phone number typed in pieces
                                  across the sender's last few messages
    explain(categories)         → the sentence shown to the sender

Detection works on several views of the same text so the usual tricks fail:
  - spelled numbers ("zero five oh nine...") and look-alike letters between
    digits (O→0, l/I→1) are turned into digits before looking for phone numbers;
  - "name (at) gmail (dot) com" style emails;
  - a "squashed" copy (lower-case, leetspeak undone, everything but letters
    removed) to catch "w.h.a.t.s.a.p.p", "T e l e g r a m", "1nstagram".

The lists are deliberately conservative: short words that are common in
normal chat ("line", "wa", "ig" on their own) are only matched in phrases.
Tune with settings.CHAT_BLOCKED_TERMS (extra words/phrases, any category).
"""

import re
import unicodedata

from django.conf import settings

PHONE = 'phone'
EMAIL = 'email'
LINK = 'link'
SOCIAL = 'social'
PAYMENT = 'payment'
CONTACT_REQUEST = 'contact_request'

LABELS = {
    PHONE: 'phone numbers',
    EMAIL: 'email addresses',
    LINK: 'links to other websites',
    SOCIAL: 'social media or messaging apps',
    PAYMENT: 'payment outside Negromart',
    CONTACT_REQUEST: 'requests to talk outside Negromart',
}

# A phone number: 9+ digits (Ghana local numbers are 10, international 12+).
PHONE_MIN_DIGITS = 9
ALLOWED_DOMAINS = ('negromart.com',)

_NUMBER_WORDS = {
    'zero': '0', 'oh': '0', 'nought': '0', 'one': '1', 'two': '2', 'three': '3', 'four': '4',
    'five': '5', 'six': '6', 'seven': '7', 'eight': '8', 'nine': '9',
}
_NUMBER_WORD_RE = re.compile(r'\b(' + '|'.join(_NUMBER_WORDS) + r')\b')
_DOUBLE_RE = re.compile(r'\b(double|triple)\s*(\d)')
# Digits separated by spaces, dashes, dots, brackets, underscores, stars or commas.
# "/" and ":" are left out so dates and times don't add up to a "phone number".
_DIGIT_RUN_RE = re.compile(r'\+?\d(?:[\s\-.()_*,]{0,3}\d)+')

_EMAIL_RE = re.compile(
    r'[a-z0-9._%+\-]+\s*(?:@|\(\s*at\s*\)|\[\s*at\s*\]|\{\s*at\s*\}|\s+at\s+)\s*'
    r'[a-z0-9\-]+\s*(?:\.|\(\s*dot\s*\)|\[\s*dot\s*\]|\{\s*dot\s*\}|\s+dot\s+)\s*[a-z]{2,6}\b'
)
_EMAIL_PROVIDERS_RE = re.compile(
    r'\b(gmail|g\s*mail|yahoo|ymail|hotmail|outlook|icloud|proton\s*mail|aol|live\.com|zoho\s*mail)\b'
)

_TLDS = ('com|net|org|gh|io|me|co|ly|app|link|shop|store|biz|info|xyz|online|site|ng|uk|us|to|gg|page|'
         'bio|tv|cc|top|club|africa')
_URL_RE = re.compile(r'(https?://|www\.)\S+')
_DOMAIN_RE = re.compile(r'\b((?:[a-z0-9\-]+\.)+(?:' + _TLDS + r'))\b(?:/\S*)?')
_SPELLED_DOMAIN_RE = re.compile(r'\b[a-z0-9\-]+\s*(?:\(\s*dot\s*\)|\[\s*dot\s*\]|\s+dot\s+)\s*(?:' + _TLDS + r')\b')

_SHORT_LINKS_RE = re.compile(r'\b(wa\.me|t\.me|bit\.ly|tinyurl|linktr\.ee|chat\.whatsapp)\b')
_HANDLE_RE = re.compile(r'(?<![\w.@])@[a-z0-9_.]{3,30}\b')
# Matched in the squashed text (letters only), so spacing/punctuation tricks fail.
_SQUASHED_SOCIAL = (
    'whatsapp', 'whatsap', 'watsapp', 'watsap', 'wattsapp', 'whatapp', 'telegram', 'instagram', 'snapchat',
    'facebook', 'tiktok', 'twitter', 'linkedin', 'signalapp',
)
_SOCIAL_WORDS_RE = re.compile(
    r'\b(insta|imo|wechat|viber|botim|threads app|signal app)\b'
    r'|\b(?:on|via|my|your|add me on|dm me on|chat (?:me )?on|message me on|text me on|follow me on)\s+'
    r'(?:ig|fb|tg|wa|snap|sc|x|tt)\b'
)

# Paying the seller personally. Cash on delivery, bank transfer and PayPal are
# Negromart checkout options (order/models.py), so those words alone are fine.
_PAYMENT_RE = re.compile(
    r'\b(momo|mo\s*mo|mobile\s*money|mtn\s*money|vodafone\s*cash|telecel\s*cash|airtel\s*tigo\s*money|'
    r'at\s*money|western\s*union|moneygram|cash\s*app|zelle|venmo|crypto|usdt|bitcoin|btc|'
    r'account\s*(?:number|no\.?|#)|acc\s*(?:number|no\.?|#)|my\s+(?:bank|paypal|wallet)|'
    r'pay\s+(?:me\s+)?(?:directly|direct|outside|personally)|pay\s+me\b|send\s+(?:the\s+)?money\s+(?:to\s+me|direct)|'
    r'transfer\s+(?:it\s+|the\s+money\s+)?to\s+(?:me|my)|'
    r'outside\s+(?:of\s+)?(?:negromart|the\s+(?:site|platform|app|website))|'
    r'off\s+(?:the\s+)?(?:platform|site|app|website)|'
    r'(?:cheaper|discount|less)\s+(?:if|when)\s+(?:you\s+)?(?:pay|buy|order)\s+(?:me\s+)?(?:direct|outside|from\s+me))\b'
)

_CONTACT_REQUEST_RE = re.compile(
    r'\b(call\s+me|text\s+me|sms\s+me|ring\s+me|flash\s+me|beep\s+me|whatsapp\s+me|email\s+me|mail\s+me|'
    r'(?:my|your|ur)\s+(?:phone\s+|mobile\s+|cell\s+|whatsapp\s+|personal\s+)?(?:number|no\.?|digits|line|contact|'
    r'email|mail|handle|socials?)|phone\s+number|contact\s+(?:number|details|info)|'
    r'reach\s+me\s+(?:on|at|via)|add\s+me\s+(?:on|up)|meet\s+(?:up|in\s+person)|meetup|'
    r'meet\s+(?:me|you)\s+(?:at|in|by|near|outside)|'
    r'come\s+to\s+my\s+(?:shop|house|place|office))\b'
)

_LEET = str.maketrans({'0': 'o', '1': 'i', '3': 'e', '4': 'a', '5': 's', '7': 't', '@': 'a', '$': 's', '!': 'i',
                       '|': 'l'})


def _normalise(text):
    text = unicodedata.normalize('NFKC', text or '')
    # Drop zero-width characters people insert to dodge filters.
    text = re.sub(r'[​-‏⁠﻿]', '', text)
    return text.lower()


def _digitise(text):
    """Spelled and disguised digits → digits ("zero five O 9" → "0 5 0 9")."""
    text = _NUMBER_WORD_RE.sub(lambda m: _NUMBER_WORDS[m.group(1)], text)
    text = _DOUBLE_RE.sub(lambda m: m.group(2) * (2 if m.group(1) == 'double' else 3), text)
    for _ in range(2):  # twice, so "0OO5" (two letters in a row) is caught
        text = re.sub(r'(?<=\d)[o](?=[\d\s\-.]*\d)', '0', text)
        text = re.sub(r'(?<=\d)[li](?=[\d\s\-.]*\d)', '1', text)
    return text



def is_mostly_digits(text):
    """A message that is basically a number ("0509", "96 51", "five oh nine")."""
    chars = [ch for ch in _digitise(_normalise(text)) if not ch.isspace() and ch not in '-.()+_*,']
    digits = sum(ch.isdigit() for ch in chars)
    return digits >= 2 and digits / len(chars) >= 0.6


def _has_phone(text):
    for match in _DIGIT_RUN_RE.finditer(_digitise(text)):
        if sum(ch.isdigit() for ch in match.group()) >= PHONE_MIN_DIGITS:
            return True
    return False


def _has_link(text):
    if _URL_RE.search(text) or _SHORT_LINKS_RE.search(text) or _SPELLED_DOMAIN_RE.search(text):
        return not _only_allowed_domains(text)
    for match in _DOMAIN_RE.finditer(text):
        if not match.group(1).endswith(ALLOWED_DOMAINS):
            return True
    return False


def _only_allowed_domains(text):
    hosts = re.findall(r'(?:https?://)?(?:www\.)?((?:[a-z0-9\-]+\.)+[a-z]{2,})', text)
    return bool(hosts) and all(h.endswith(ALLOWED_DOMAINS) for h in hosts) \
        and not _SHORT_LINKS_RE.search(text) and not _SPELLED_DOMAIN_RE.search(text)


def check(text, recent=()):
    """
    Categories of off-platform content in `text` ([] when the message is fine).

    `recent` is the sender's previous few messages in this conversation (oldest
    first); a phone number typed in pieces across them counts too.
    """
    norm = _normalise(text)
    squashed = re.sub(r'[^a-z]', '', norm.translate(_LEET))
    found = set()

    if _EMAIL_RE.search(norm) or _EMAIL_PROVIDERS_RE.search(norm):
        found.add(EMAIL)
    if _has_phone(norm):
        found.add(PHONE)
    elif recent and is_mostly_digits(text):
        # "050" … "965" … "1622": the trailing run of number-only messages.
        pieces = [norm]
        for previous in reversed(recent):
            if not is_mostly_digits(previous):
                break
            pieces.append(_normalise(previous))
        digits = sum(ch.isdigit() for piece in pieces for ch in _digitise(piece))
        if len(pieces) > 1 and digits >= PHONE_MIN_DIGITS:
            found.add(PHONE)
    if _has_link(norm):
        found.add(LINK)
    if (_HANDLE_RE.search(norm) or _SOCIAL_WORDS_RE.search(norm)
            or any(word in squashed for word in _SQUASHED_SOCIAL)):
        found.add(SOCIAL)
    if _PAYMENT_RE.search(norm):
        found.add(PAYMENT)
    if _CONTACT_REQUEST_RE.search(norm):
        found.add(CONTACT_REQUEST)

    for term in getattr(settings, 'CHAT_BLOCKED_TERMS', ()):
        if re.search(r'\b' + re.escape(term.lower()) + r'\b', norm):
            found.add(CONTACT_REQUEST)
            break
    return sorted(found)


def explain(categories):
    parts = [LABELS[c] for c in categories if c in LABELS]
    listed = ', '.join(parts[:-1]) + (' or ' if len(parts) > 1 else '') + parts[-1] if parts else 'contact details'
    return (f"Message not sent: it looks like it contains {listed}. To keep you protected, chats, "
            "payments and deliveries must stay on Negromart.")
