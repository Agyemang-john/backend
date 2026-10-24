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
from rest_framework.exceptions import MethodNotAllowed, NotFound
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
