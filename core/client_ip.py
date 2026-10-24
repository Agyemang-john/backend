"""
core/client_ip.py
The address a request really came from, for rate limits, lockouts and logs.

Nginx is the only proxy in front of Django (it terminates TLS itself, there is
no CDN). It sets X-Forwarded-For to the address it saw (nginx/default.conf),
so the right-most entry is the one we can trust. Anything to the left of it was
written by the client and is ignored: taking the left-most entry, as the old
helpers did, let anyone dodge throttles by sending a made-up header.

DRF's throttles read the same entry because REST_FRAMEWORK['NUM_PROXIES'] = 1.
"""

import ipaddress


def _valid(value):
    try:
        ipaddress.ip_address(value)
        return True
    except ValueError:
        return False


def client_ip(request) -> str:
    meta = getattr(request, 'META', request)
    xff = meta.get('HTTP_X_FORWARDED_FOR', '')
    if xff:
        last = xff.split(',')[-1].strip()
        if _valid(last):
            return last
    return meta.get('REMOTE_ADDR', '') or ''
