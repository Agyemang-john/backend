"""
notification/middleware.py
WebSocket authentication for every socket (notifications, order tracking, chat).

Browsers can't set headers on a WebSocket, so the client first calls
GET /api/v1/notification/ws-token/ (cookie-authenticated) and receives a
ticket: a random string, valid for TICKET_TTL seconds and for one connection.
The socket URL carries the ticket, never a JWT, so an access token never
lands in proxy logs, browser history or JavaScript memory.

The ticket remembers how its owner signed in (shopper or seller sign-in, see
vendor/access.signed_in_as_seller) and which device session issued it, so a
socket can't do more than the HTTP session that asked for it.
"""

import secrets

from channels.db import database_sync_to_async
from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.core.cache import cache
from urllib.parse import parse_qs

User = get_user_model()

TICKET_TTL = 60
_PREFIX = 'ws_ticket:'


def issue_ticket(request):
    """A one-use ticket for the user of this (already authenticated) request."""
    from userauths.authentication import AUTH_SCOPE_ATTR
    user = request.user
    auth = request.auth
    ticket = secrets.token_urlsafe(32)
    cache.set(_PREFIX + ticket, {
        'uid': user.pk,
        'scope': user.__dict__.get(AUTH_SCOPE_ATTR, 'shopper'),
        'tv': user.token_version,
        'sid': auth.get('sid') if auth is not None and hasattr(auth, 'get') else None,
    }, TICKET_TTL)
    return ticket


def _redeem(ticket):
    key = _PREFIX + ticket
    data = cache.get(key)
    # delete() reports whether this call removed it, so two sockets racing
    # with one ticket can't both get in.
    if not data or not cache.delete(key):
        return None
    return data


@database_sync_to_async
def get_user_from_ticket(ticket: str):
    from userauths.authentication import AUTH_SCOPE_ATTR
    from userauths.session_utils import is_session_revoked

    if not ticket or len(ticket) > 100:
        return AnonymousUser()
    data = _redeem(ticket)
    if data is None:
        return AnonymousUser()
    try:
        user = User.objects.get(pk=data['uid'])
    except (User.DoesNotExist, KeyError):
        return AnonymousUser()
    if not user.is_active or user.token_version != data.get('tv'):
        return AnonymousUser()
    if data.get('sid') and is_session_revoked(data['sid']):
        return AnonymousUser()
    user.__dict__[AUTH_SCOPE_ATTR] = 'seller' if data.get('scope') == 'seller' else 'shopper'
    return user


class QueryStringJWTAuthMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        query_string = scope.get("query_string", b"").decode()
        params = parse_qs(query_string)
        ticket = params.get("token", [None])[0]

        scope["user"] = await get_user_from_ticket(ticket) if ticket else AnonymousUser()
        return await self.app(scope, receive, send)
