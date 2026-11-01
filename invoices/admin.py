from django.contrib import admin
from django.http import HttpResponse
from django.urls import path, reverse
from django.utils.html import format_html

from .models import Invoice
from .pdf import render


@admin.register(Invoice)
class InvoiceAdmin(admin.ModelAdmin):
    list_display = ('number', 'kind', 'order', 'vendor', 'total', 'tax_total', 'issued_at', 'pdf_link')
    list_filter = ('kind',)
    search_fields = ('number', 'order__order_number', 'vendor__name')
    raw_id_fields = ('order', 'vendor')
    readonly_fields = ('number', 'kind', 'order', 'vendor', 'issued_at', 'currency', 'total', 'tax_total', 'data')
    date_hierarchy = 'issued_at'

    def has_add_permission(self, request):
        return False   # issued automatically; numbers must not be hand-made

    def has_delete_permission(self, request, obj=None):
        return False   # issued documents are kept

    def get_urls(self):
        return [path('<int:pk>/pdf/', self.admin_site.admin_view(self.pdf_view), name='invoices_invoice_pdf')] + super().get_urls()

    def pdf_view(self, request, pk):
        invoice = Invoice.objects.get(pk=pk)
        resp = HttpResponse(render(invoice), content_type='application/pdf')
        resp['Content-Disposition'] = f'inline; filename="{invoice.number}.pdf"'
        return resp

    @admin.display(description='PDF')
    def pdf_link(self, obj):
        return format_html('<a href="{}" target="_blank">Open</a>', reverse('admin:invoices_invoice_pdf', args=[obj.pk]))
