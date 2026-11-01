"""
Two-factor for the Django admin (staff). Every staff session must pass an
authenticator code before any admin page loads; staff without an authenticator
are sent to set one up first. Enforced by AdminTwoFactorMiddleware when
settings.ADMIN_2FA_REQUIRED is on (default: on whenever DEBUG is off).
"""
from django.conf import settings
from django.contrib.auth.decorators import user_passes_test
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme

from . import services

SESSION_KEY = 'admin_2fa_ok'
ADMIN_PREFIX = '/dashboard/negromart/'


def _next(request):
    nxt = request.GET.get('next') or request.POST.get('next') or ADMIN_PREFIX
    return nxt if url_has_allowed_host_and_scheme(nxt, allowed_hosts={request.get_host()}) else ADMIN_PREFIX


class AdminTwoFactorMiddleware:
    EXEMPT = ('login/', 'logout/', '2fa/', 'jsi18n/')

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        path = request.path
        if (getattr(settings, 'ADMIN_2FA_REQUIRED', not settings.DEBUG) and path.startswith(ADMIN_PREFIX)
                and not any(path.startswith(ADMIN_PREFIX + e) for e in self.EXEMPT)):
            user = getattr(request, 'user', None)
            if user is not None and user.is_authenticated and user.is_staff and not request.session.get(SESSION_KEY):
                target = 'twofactor-admin-verify' if services.is_enabled(user) else 'twofactor-admin-setup'
                return redirect(f"{reverse(target)}?next={path}")
        return self.get_response(request)


staff_required = user_passes_test(lambda u: u.is_authenticated and u.is_staff, login_url=ADMIN_PREFIX + 'login/')


@staff_required
def verify_view(request):
    if not services.is_enabled(request.user):
        return redirect(reverse('twofactor-admin-setup'))
    error = ''
    if request.method == 'POST':
        try:
            ok = services.verify(request.user, request.POST.get('code', ''))
        except services.TwoFactorError as exc:
            ok, error = False, str(exc)
        if ok:
            request.session.cycle_key()            # new session id after the second factor
            request.session[SESSION_KEY] = True
            return redirect(_next(request))
        error = error or "That code didn't match."
    return render(request, 'twofactor/admin_verify.html', {'error': error, 'next': _next(request)})


@staff_required
def setup_view(request):
    if services.is_enabled(request.user):
        return redirect(reverse('twofactor-admin-verify'))
    error, codes = '', None
    if request.method == 'POST':
        try:
            codes = services.confirm(request.user, request.POST.get('code', ''))
            request.session.cycle_key()
            request.session[SESSION_KEY] = True
        except services.TwoFactorError as exc:
            error = str(exc)
    if codes:
        return render(request, 'twofactor/admin_codes.html', {'codes': codes, 'next': _next(request)})
    if request.method == 'POST' and error:
        # Keep the same secret so the app entry already added still works.
        from .models import TOTPDevice
        device = TOTPDevice.objects.get(user=request.user)
        import pyotp
        secret = services._secret(device)
        uri = pyotp.TOTP(secret).provisioning_uri(name=request.user.email, issuer_name=services.ISSUER)
        qr = services.qr_svg(uri)
    else:
        secret, uri, qr = services.start_setup(request.user)
    return render(request, 'twofactor/admin_setup.html', {'secret': secret, 'qr': qr, 'error': error, 'next': _next(request)})


@staff_required
def recovery_view(request):
    """Staff: get a fresh set of recovery codes (old ones stop working)."""
    if not services.is_enabled(request.user):
        return redirect(reverse('twofactor-admin-setup'))
    error = ''
    if request.method == 'POST':
        try:
            codes = services.regenerate_recovery(request.user, request.POST.get('code', ''))
            return render(request, 'twofactor/admin_codes.html', {'codes': codes, 'next': ADMIN_PREFIX})
        except services.TwoFactorError as exc:
            error = str(exc)
    return render(request, 'twofactor/admin_recovery.html',
                  {'error': error, 'left': services.recovery_left(request.user)})
