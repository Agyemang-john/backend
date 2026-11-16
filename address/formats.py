"""
address/formats.py

How an address looks in each country, the way Amazon/Walmart's forms adapt
(Google's libaddressinput idea, kept small): what the region and town fields
are called, whether a postcode is needed and its pattern, the dial code, and
the list of regions when there is one to pick from (pycountry subdivisions).

Also the normalisers the serializer uses: phone → E.164, Ghana Post digital
address → "GA-183-8164", country name/code → (name, code).
"""

import re
from functools import lru_cache

import pycountry

try:  # Optional: precise per-country phone validation when installed.
    import phonenumbers
except ImportError:  # pragma: no cover - depends on the environment
    phonenumbers = None


# Dial codes for the markets we sell to and the countries shoppers most often
# ship to. Anywhere else the shopper types the number with its "+code".
DIAL_CODES = {
    # West Africa
    'GH': '233', 'NG': '234', 'CI': '225', 'SN': '221', 'TG': '228', 'BJ': '229', 'BF': '226',
    'ML': '223', 'NE': '227', 'GN': '224', 'SL': '232', 'LR': '231', 'GM': '220', 'GW': '245',
    'CV': '238', 'MR': '222',
    # Central / East / Southern Africa
    'CM': '237', 'GA': '241', 'CG': '242', 'CD': '243', 'TD': '235', 'CF': '236', 'GQ': '240',
    'KE': '254', 'UG': '256', 'TZ': '255', 'RW': '250', 'BI': '257', 'ET': '251', 'SO': '252',
    'DJ': '253', 'ER': '291', 'SS': '211', 'SD': '249', 'ZA': '27', 'ZW': '263', 'ZM': '260',
    'MW': '265', 'MZ': '258', 'AO': '244', 'NA': '264', 'BW': '267', 'LS': '266', 'SZ': '268',
    'MG': '261', 'MU': '230',
    # North Africa / Middle East
    'EG': '20', 'MA': '212', 'DZ': '213', 'TN': '216', 'LY': '218', 'AE': '971', 'SA': '966',
    'QA': '974', 'KW': '965', 'BH': '973', 'OM': '968', 'IL': '972', 'TR': '90', 'LB': '961', 'JO': '962',
    # Europe
    'GB': '44', 'IE': '353', 'FR': '33', 'DE': '49', 'NL': '31', 'BE': '32', 'LU': '352', 'IT': '39',
    'ES': '34', 'PT': '351', 'CH': '41', 'AT': '43', 'SE': '46', 'NO': '47', 'DK': '45', 'FI': '358',
    'PL': '48', 'CZ': '420', 'GR': '30', 'RO': '40', 'HU': '36', 'UA': '380', 'RU': '7',
    # Americas
    'US': '1', 'CA': '1', 'MX': '52', 'BR': '55', 'AR': '54', 'CO': '57', 'CL': '56', 'PE': '51',
    'JM': '1', 'TT': '1', 'BB': '1', 'HT': '509',
    # Asia / Oceania
    'CN': '86', 'IN': '91', 'JP': '81', 'KR': '82', 'SG': '65', 'MY': '60', 'ID': '62', 'PH': '63',
    'TH': '66', 'VN': '84', 'PK': '92', 'BD': '880', 'HK': '852', 'AU': '61', 'NZ': '64',
}

_DEFAULT = {
    'region_label': 'State / Province / Region',
    'town_label': 'City',
    'postal_label': 'Postcode',
    'postal_required': False,
    'postal_pattern': None,
    'postal_example': '',
    'region_required': True,
}

# Only what differs from _DEFAULT.
_FORMATS = {
    'GH': {'region_label': 'Region', 'town_label': 'City / Town', 'postal_label': 'Ghana Post digital address',
           'postal_pattern': r'^[A-Z]{2}-\d{3,4}-\d{3,4}$', 'postal_example': 'GA-183-8164'},
    'NG': {'region_label': 'State', 'town_label': 'City / LGA', 'postal_example': '100001'},
    'KE': {'region_label': 'County', 'town_label': 'Town', 'postal_example': '00100'},
    'ZA': {'region_label': 'Province', 'town_label': 'City', 'postal_required': True,
           'postal_pattern': r'^\d{4}$', 'postal_example': '2196'},
    'CI': {'region_label': 'District', 'town_label': 'Ville'},
    'SN': {'region_label': 'Région', 'town_label': 'Ville', 'postal_example': '10200'},
    'TG': {'region_label': 'Région', 'town_label': 'Ville', 'region_required': False},
    'BJ': {'region_label': 'Département', 'town_label': 'Ville', 'region_required': False},
    'US': {'region_label': 'State', 'postal_label': 'ZIP code', 'postal_required': True,
           'postal_pattern': r'^\d{5}(-\d{4})?$', 'postal_example': '10001'},
    'CA': {'region_label': 'Province', 'postal_label': 'Postal code', 'postal_required': True,
           'postal_pattern': r'^[A-Z]\d[A-Z] ?\d[A-Z]\d$', 'postal_example': 'K1A 0B1'},
    'GB': {'region_label': 'County', 'town_label': 'Town / City', 'postal_required': True, 'region_required': False,
           'postal_pattern': r'^[A-Z]{1,2}\d[A-Z\d]? ?\d[A-Z]{2}$', 'postal_example': 'SW1A 1AA'},
    'IE': {'region_label': 'County', 'postal_label': 'Eircode', 'postal_example': 'D02 X285'},
    'FR': {'postal_label': 'Code postal', 'town_label': 'Ville', 'postal_required': True, 'region_required': False,
           'postal_pattern': r'^\d{5}$', 'postal_example': '75008'},
    'DE': {'postal_label': 'PLZ', 'postal_required': True, 'region_required': False,
           'postal_pattern': r'^\d{5}$', 'postal_example': '10115'},
    'NL': {'postal_required': True, 'region_required': False,
           'postal_pattern': r'^\d{4} ?[A-Z]{2}$', 'postal_example': '1011 AB'},
    'BE': {'postal_required': True, 'region_required': False, 'postal_pattern': r'^\d{4}$', 'postal_example': '1000'},
    'IT': {'region_label': 'Province', 'postal_label': 'CAP', 'postal_required': True,
           'postal_pattern': r'^\d{5}$', 'postal_example': '00144'},
    'ES': {'region_label': 'Province', 'postal_required': True, 'postal_pattern': r'^\d{5}$', 'postal_example': '28013'},
    'AE': {'region_label': 'Emirate', 'town_label': 'Area'},
    'IN': {'postal_label': 'PIN code', 'postal_required': True, 'postal_pattern': r'^\d{6}$', 'postal_example': '110001'},
    'AU': {'postal_required': True, 'postal_pattern': r'^\d{4}$', 'postal_example': '2000', 'town_label': 'Suburb'},
    'CN': {'region_label': 'Province', 'postal_example': '100000'},
}

# Ghana Post digital addresses start with the region letter.
GH_DIGITAL_REGION = {
    'A': 'Ashanti', 'B': 'Bono', 'C': 'Central', 'E': 'Eastern', 'G': 'Greater Accra',
    'N': 'Northern', 'U': 'Upper East', 'X': 'Upper West', 'V': 'Volta', 'W': 'Western',
}

# Short or common names pycountry won't resolve on its own.
_NAME_ALIASES = {
    'uk': 'GB', 'england': 'GB', 'scotland': 'GB', 'wales': 'GB', 'usa': 'US', 'u.s.a.': 'US',
    'united states of america': 'US', 'america': 'US', 'ivory coast': 'CI', "cote d'ivoire": 'CI',
    'côte d’ivoire': 'CI', 'drc': 'CD', 'congo-kinshasa': 'CD', 'congo-brazzaville': 'CG',
    'south korea': 'KR', 'north korea': 'KP', 'russia': 'RU', 'vietnam': 'VN', 'iran': 'IR',
    'syria': 'SY', 'tanzania': 'TZ', 'bolivia': 'BO', 'venezuela': 'VE', 'laos': 'LA', 'moldova': 'MD',
    'turkey': 'TR', 'czech republic': 'CZ', 'swaziland': 'SZ', 'cape verde': 'CV',
}


@lru_cache(maxsize=512)
def resolve_country(value):
    """(name, CODE) for a country name or ISO code, or (None, None)."""
    if not value:
        return None, None
    raw = str(value).strip()
    key = raw.lower()
    code = _NAME_ALIASES.get(key)
    country = None
    try:
        if code:
            country = pycountry.countries.get(alpha_2=code)
        elif len(raw) == 2:
            country = pycountry.countries.get(alpha_2=raw.upper())
        elif len(raw) == 3 and raw.isalpha() and raw.isupper():
            country = pycountry.countries.get(alpha_3=raw)
        if country is None:
            country = pycountry.countries.lookup(raw)
    except LookupError:
        return None, None
    if country is None:
        return None, None
    # pycountry's `name`, the same string the Country table is seeded with:
    # delivery fees compare the two to tell local from international.
    return country.name, country.alpha_2


@lru_cache(maxsize=256)
def regions_for(code):
    """Top-level subdivisions to pick from, or [] when free text works better."""
    if not code:
        return []
    subs = pycountry.subdivisions.get(country_code=code) or []
    top = sorted({s.name for s in subs if not getattr(s, 'parent_code', None)})
    # Long lists (France's departments, …) are worse than typing.
    return top if 0 < len(top) <= 80 else []


def format_for(code):
    code = (code or '').upper()
    fmt = {**_DEFAULT, **_FORMATS.get(code, {})}
    name, _ = resolve_country(code)
    return {
        'country_code': code,
        'country': name or code,
        'dial_code': DIAL_CODES.get(code, ''),
        'regions': regions_for(code),
        **fmt,
    }


# ── Normalisers ────────────────────────────────────────────────────────────────

class InvalidValue(ValueError):
    pass


def normalize_phone(raw, country_code=None):
    """E.164 ("+233241234567"), or raise InvalidValue. '' stays ''."""
    if not raw:
        return ''
    text = str(raw).strip()
    if phonenumbers is not None:
        try:
            num = phonenumbers.parse(text, (country_code or '').upper() or None)
        except phonenumbers.NumberParseException:
            raise InvalidValue('Enter a valid phone number.')
        if not phonenumbers.is_valid_number(num):
            raise InvalidValue('This phone number doesn’t look right for the country.')
        return phonenumbers.format_number(num, phonenumbers.PhoneNumberFormat.E164)

    digits = re.sub(r'[\s\-().]', '', text)
    if digits.startswith('00'):
        digits = '+' + digits[2:]
    if not digits.startswith('+'):
        dial = DIAL_CODES.get((country_code or '').upper())
        if not dial:
            if re.fullmatch(r'\d{10,15}', digits):
                return digits  # Can't tell the country; keep what was typed.
            raise InvalidValue('Add your country code, e.g. +44 7700 900123.')
        local = digits[1:] if digits.startswith('0') else digits
        # Typed the dial code without "+" ("233241234567").
        if local.startswith(dial) and len(local) - len(dial) >= 7:
            local = local[len(dial):]
        digits = f'+{dial}{local}'
    if not re.fullmatch(r'\+\d{8,15}', digits):
        raise InvalidValue('Enter a valid phone number.')
    return digits


def normalize_postal(raw, country_code):
    """Upper-cased, tidied postcode / digital address, or raise InvalidValue."""
    if not raw:
        return ''
    code = (country_code or '').upper()
    text = re.sub(r'\s+', ' ', str(raw).strip().upper())
    if code == 'GH':
        compact = re.sub(r'[^A-Z0-9]', '', text)
        m = re.fullmatch(r'([A-Z]{2})(\d{3,4})(\d{4})', compact)
        if not m:
            raise InvalidValue('Ghana Post digital addresses look like GA-183-8164.')
        return '-'.join(m.groups())
    pattern = _FORMATS.get(code, {}).get('postal_pattern')
    if pattern and not re.fullmatch(pattern, text):
        example = _FORMATS[code].get('postal_example')
        raise InvalidValue(f'Enter a valid postcode, e.g. {example}.' if example else 'Enter a valid postcode.')
    return text


def region_from_digital_address(gps):
    """'Greater Accra' for 'GA-183-8164' (Ghana only), else None."""
    if not gps:
        return None
    return GH_DIGITAL_REGION.get(gps.strip().upper()[:1])
