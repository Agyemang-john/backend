"""
userauths/djoser_views.py
The parts of djoser's /api/users/ API the apps use, and nothing else.

djoser exposes a full user CRUD by default. Registration has its own view
(RegisterView: captcha + throttle), activation too (ActivateEmailView), and
profile edits go through customer/ where email changes need the password. So:

  kept    GET users/me/, resend_activation, reset_password,
          reset_password_confirm, set_password, set_email
  closed  users/ (create, list), users/<id>/, me PUT/PATCH/DELETE, activation,
          reset_email, reset_email_confirm

Endpoints that send email or check a password are rate limited per IP or
per account, so they can't be used to spam inboxes or guess passwords.
"""

from djoser.views import UserViewSet
from rest_framework import status
from rest_framework.decorators import action
from rest_framework.exceptions import MethodNotAllowed, NotFound
from rest_framework.response import Response
from rest_framework.throttling import AnonRateThrottle, UserRateThrottle


class EmailActionThrottle(AnonRateThrottle):
    scope = 'email_action'
    rate = '5/hour'


class PasswordCheckThrottle(UserRateThrottle):
    scope = 'password_check'
    rate = '5/min'


ALLOWED_ACTIONS = {
    'me', 'resend_activation', 'reset_password', 'reset_password_confirm',
    'set_password', 'set_username',
}
EMAIL_ACTIONS = {'resend_activation', 'reset_password'}
PASSWORD_ACTIONS = {'set_password', 'set_username', 'reset_password_confirm'}


class RestrictedUserViewSet(UserViewSet):

    def initial(self, request, *args, **kwargs):
        if self.action not in ALLOWED_ACTIONS:
            raise NotFound()
        if self.action == 'me' and request.method != 'GET':
            raise MethodNotAllowed(request.method)
        super().initial(request, *args, **kwargs)

    def get_throttles(self):
        throttles = super().get_throttles()
        if self.action in EMAIL_ACTIONS:
            throttles.append(EmailActionThrottle())
        elif self.action in PASSWORD_ACTIONS:
            throttles.append(PasswordCheckThrottle())
        return throttles

    @action(["post"], detail=False)
    def reset_password(self, request, *args, **kwargs):
        """
        djoser's, plus accounts created with Google: they have no password,
        which djoser skips, so "Forgot password?" is how they add one (the
        emailed link proves the inbox is theirs). Always 204, account or not.
        """
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = serializer.get_user()
        if user is None:
            user = _google_only_user(serializer.data.get(getattr(serializer, 'email_field', 'email'), ''))
        if user is not None:
            from .google_auth import send_password_link
            send_password_link(request, user)
        return Response(status=status.HTTP_204_NO_CONTENT)

    @action(["post"], detail=False)
    def reset_password_confirm(self, request, *args, **kwargs):
        check = self.get_serializer(data=request.data)
        user = check.user if check.is_valid() else None
        first_password = user is not None and not user.has_usable_password()
        response = super().reset_password_confirm(request, *args, **kwargs)
        if first_password and response.status_code < 300:
            from .google_auth import notify_method_change
            user.refresh_from_db()
            if user.auth_provider == 'google':
                user.auth_provider = 'email'
                user.save(update_fields=['auth_provider'])
            notify_method_change(user, 'password_added', request)
        return response


def _google_only_user(email):
    from django.contrib.auth import get_user_model
    from .google_auth import google_link
    user = get_user_model().objects.filter(email__iexact=email or '', is_active=True).first() if email else None
    if user is not None and not user.has_usable_password() and google_link(user) is not None:
        return user
    return None
