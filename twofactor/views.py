from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from vendor.access import IsSellerSignIn

from . import services


class SellerTwoFactorView(APIView):
    """
    GET  → {enabled, recovery_codes_left}
    POST {action: "setup"}            → {secret, qr_svg}
    POST {action: "confirm", code}    → {recovery_codes}  (shown once)
    POST {action: "disable", code}    → {enabled: false}
    Needs the seller sign-in (password + code), so a stolen shopper session can't change it.
    """
    permission_classes = [IsAuthenticated, IsSellerSignIn]

    def get(self, request):
        return Response({'enabled': services.is_enabled(request.user),
                         'recovery_codes_left': services.recovery_left(request.user)})

    def post(self, request):
        action = request.data.get('action')
        try:
            if action == 'setup':
                secret, _uri, svg = services.start_setup(request.user)
                return Response({'secret': secret, 'qr_svg': svg})
            if action == 'confirm':
                codes = services.confirm(request.user, request.data.get('code'))
                return Response({'enabled': True, 'recovery_codes': codes})
            if action == 'disable':
                services.disable(request.user, request.data.get('code'))
                return Response({'enabled': False})
        except services.TwoFactorError as exc:
            return Response({'detail': str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response({'detail': 'Unknown action.'}, status=status.HTTP_400_BAD_REQUEST)
