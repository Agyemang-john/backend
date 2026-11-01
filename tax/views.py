from rest_framework import serializers
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from vendor.access import Capability, get_current_vendor, require_capability

from .models import SellerTaxRegistration
from .services import rates_for


class RegistrationSerializer(serializers.ModelSerializer):
    class Meta:
        model = SellerTaxRegistration
        fields = ['vat_registered', 'tax_id', 'registered_name', 'verified']
        read_only_fields = ['verified']

    def validate(self, attrs):
        registered = attrs.get('vat_registered', getattr(self.instance, 'vat_registered', False))
        tax_id = (attrs.get('tax_id', getattr(self.instance, 'tax_id', '')) or '').strip()
        if registered and len(tax_id) < 6:
            raise serializers.ValidationError({'tax_id': 'Enter your TIN / VAT number to charge VAT.'})
        attrs['tax_id'] = tax_id.upper()
        return attrs


class SellerTaxRegistrationView(APIView):
    """GET/PUT the store's VAT registration (finance team only for changes)."""
    permission_classes = [IsAuthenticated, require_capability(Capability.MANAGE_FINANCE, read=Capability.VIEW_FINANCE)]

    def _payload(self, vendor, reg):
        data = RegistrationSerializer(reg).data if reg else {
            'vat_registered': False, 'tax_id': '', 'registered_name': '', 'verified': False}
        country = str(vendor.country or 'GH')
        data['rates'] = [{'name': r.name, 'rate': r.rate} for r in rates_for(country)]
        return data

    def get(self, request):
        vendor = get_current_vendor(request.user)
        reg = SellerTaxRegistration.objects.filter(vendor=vendor).first()
        return Response(self._payload(vendor, reg))

    def put(self, request):
        vendor = get_current_vendor(request.user)
        reg = SellerTaxRegistration.objects.filter(vendor=vendor).first()
        ser = RegistrationSerializer(reg, data=request.data, partial=True)
        ser.is_valid(raise_exception=True)
        changed_id = reg is not None and ser.validated_data.get('tax_id', reg.tax_id) != reg.tax_id
        reg = ser.save(vendor=vendor)
        if changed_id and reg.verified:
            reg.verified = False   # a new number needs checking again
            reg.save(update_fields=['verified'])
        return Response(self._payload(vendor, reg))
