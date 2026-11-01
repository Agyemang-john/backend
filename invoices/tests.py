"""
Receipts, seller invoices and the VAT inside tax-inclusive prices.

Run with:  DB_HOST=localhost python manage.py test invoices.tests --keepdb
"""
from decimal import Decimal
from unittest import mock

from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from address.models import Address
from ecommerce.celery import app as celery_app
from invoices.models import Invoice
from invoices.services import issue, issue_all
from order.models import Order, OrderProduct
from product.models import Brand, Category, Main_Category, Product, Sub_Category
from tax.models import SellerTaxRegistration, TaxExemption, TaxRate
from tax.services import split_inclusive
from userauths.models import User
from userauths.tokens import CustomVendorRefreshToken
from vendor.models import Vendor

celery_app.conf.task_always_eager = True

TEST_SETTINGS = {
    'CACHES': {'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}},
    'EMAIL_BACKEND': 'django.core.mail.backends.locmem.EmailBackend',
    'CHANNEL_LAYERS': {'default': {'BACKEND': 'channels.layers.InMemoryChannelLayer'}},
}


def make_user(n):
    user = User.objects.create_user(f'Inv{n}', 'Test', f'inv{n}@example.com', f'+23324600{n:04d}', 'Str0ng!Passw0rd')
    User.objects.filter(pk=user.pk).update(is_active=True, email_verified_at=timezone.now())
    return User.objects.get(pk=user.pk)


def seller_client(owner):
    c = APIClient()
    c.cookies['vendor_access'] = str(CustomVendorRefreshToken.for_user(owner).access_token)
    c.credentials(HTTP_X_USER_TYPE='vendor')
    return c


@override_settings(**TEST_SETTINGS)
class InvoiceTests(TestCase):

    def setUp(self):
        from django.core.cache import cache
        cache.clear()
        cache.set('exchange_rates', {'GHS': 1, 'USD': 0.1}, None)
        for target in ('vendor.models.send_vendor_approval_email.delay', 'vendor.models.send_vendor_sms.delay'):
            p = mock.patch(target)
            p.start()
            self.addCleanup(p.stop)
        self.owner_a, self.owner_b = make_user(1), make_user(2)
        self.shop_a = Vendor.objects.create(name='Inv Shop A', user=self.owner_a, email='inv-a@example.com',
                                            contact='+233550008881', is_approved=True, status='VERIFIED')
        self.shop_b = Vendor.objects.create(name='Inv Shop B', user=self.owner_b, email='inv-b@example.com',
                                            contact='+233550008882', is_approved=True, status='VERIFIED')
        SellerTaxRegistration.objects.create(vendor=self.shop_a, vat_registered=True, tax_id='C0001234567')
        main = Main_Category.objects.create(title='Inv Main')
        self.cat = Category.objects.create(title='Inv Cat', main_category=main)
        self.books = Sub_Category.objects.create(title='Inv Books', category=self.cat)
        self.gadgets = Sub_Category.objects.create(title='Inv Gadgets', category=self.cat)
        brand = Brand.objects.create(title='Inv Brand')
        self.radio = Product.objects.create(title='Inv Radio', sub_category=self.gadgets, vendor=self.shop_a,
                                            status='published', price=Decimal('120'), brand=brand)
        self.book = Product.objects.create(title='Inv Book', sub_category=self.books, vendor=self.shop_a,
                                           status='published', price=Decimal('60'), brand=brand)
        self.mug = Product.objects.create(title='Inv Mug', sub_category=self.gadgets, vendor=self.shop_b,
                                          status='published', price=Decimal('30'), brand=brand)
        self.buyer = make_user(3)
        addr = Address.objects.create(user=self.buyer, full_name='Ama Mensah', country='Ghana', town='Accra',
                                      address='12 Ring Rd', mobile='+233241234567')
        self.order = Order.objects.create(user=self.buyer, address=addr, total=Decimal('225.00'), is_ordered=True,
                                          order_number='ORD-INV-1', payment_method='paystack')
        for product in (self.radio, self.book, self.mug):
            OrderProduct.objects.create(order=self.order, product=product, quantity=1, price=product.price,
                                        amount=product.price)

    def test_split_inclusive(self):
        rates = list(TaxRate.objects.filter(country='GH'))
        self.assertEqual(sum(r.rate for r in rates), Decimal('20.00'), "seeded Ghana rates")
        lt = split_inclusive(Decimal('120.00'), rates)
        self.assertEqual(lt.tax, Decimal('20.00'))
        self.assertEqual(lt.exclusive, Decimal('100.00'))
        self.assertEqual(sum(c['amount'] for c in lt.components), lt.tax)

    def test_receipt_snapshot(self):
        TaxExemption.objects.create(country='GH', sub_category=self.books, reason='Books')
        receipt = issue(self.order)
        d = receipt.data
        self.assertEqual(d['subtotal'], '210.00')
        self.assertEqual(d['charges'], '15.00', "delivery = total - items")
        self.assertEqual(d['tax_total'], '20.00', "radio only: book exempt, shop B not registered")
        notes = {l['title']: l['tax_note'] for l in d['lines']}
        self.assertEqual(notes, {'Inv Radio': '', 'Inv Book': 'VAT exempt', 'Inv Mug': 'Seller not VAT registered'})
        self.assertEqual(d['buyer']['name'], 'Ama Mensah')
        self.assertTrue(receipt.number.startswith('NM'))
        # Frozen: later edits don't change the issued document.
        Product.objects.filter(pk=self.radio.pk).update(title='Renamed')
        self.assertEqual(issue(self.order).data['lines'][0]['title'], 'Inv Radio')

    def test_issue_all_numbers_are_unique(self):
        issue_all(self.order)
        invoices = Invoice.objects.filter(order=self.order)
        self.assertEqual(invoices.count(), 3)
        self.assertEqual(len({i.number for i in invoices}), 3)
        shop_b = invoices.get(vendor=self.shop_b)
        self.assertEqual([l['title'] for l in shop_b.data['lines']], ['Inv Mug'])
        issue_all(self.order)
        self.assertEqual(Invoice.objects.filter(order=self.order).count(), 3, "idempotent")

    def test_buyer_downloads_pdf(self):
        c = APIClient()
        c.force_authenticate(self.buyer)
        res = c.get(f'/api/v1/invoices/orders/{self.order.pk}/receipt/')
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res['Content-Type'], 'application/pdf')
        self.assertTrue(res.content.startswith(b'%PDF'))
        stranger = APIClient()
        stranger.force_authenticate(make_user(4))
        self.assertEqual(stranger.get(f'/api/v1/invoices/orders/{self.order.pk}/receipt/').status_code, 404)

    def test_seller_invoice_only_for_own_orders(self):
        c = seller_client(self.owner_b)
        res = c.get(f'/api/v1/invoices/seller/orders/{self.order.pk}/')
        self.assertEqual(res.status_code, 200, res.content[:200])
        self.assertTrue(res.content.startswith(b'%PDF'))
        other = Order.objects.create(user=self.buyer, total=Decimal('120'), is_ordered=True, order_number='ORD-INV-2')
        OrderProduct.objects.create(order=other, product=self.radio, quantity=1, price=Decimal('120'), amount=Decimal('120'))
        self.assertEqual(c.get(f'/api/v1/invoices/seller/orders/{other.pk}/').status_code, 404)

    def test_seller_registration_api(self):
        c = seller_client(self.owner_b)
        res = c.put('/api/v1/tax/seller/registration/', {'vat_registered': True}, format='json')
        self.assertEqual(res.status_code, 400, "needs a TIN")
        res = c.put('/api/v1/tax/seller/registration/', {'vat_registered': True, 'tax_id': 'p0012345678'}, format='json')
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(res.data['tax_id'], 'P0012345678')
        self.assertEqual(len(res.data['rates']), 3)
