"""
order/tests_coupons.py
Coupons end to end: the rules and maths (order/coupons.py), applying a code at
checkout, the total actually charged (pay on delivery and Paystack), who pays
the discount in the seller ledger, refunds, and sellers managing their codes.

Run with:  DB_HOST=localhost python manage.py test order.tests_coupons --keepdb
"""

from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.utils import timezone
from rest_framework.test import APIClient

from address.models import Address
from order.coupons import CouponError, cart_lines, evaluate, forget_offers, quote_cart
from order.models import Cart, CartItem, Order, OrderProduct, Shipment
from payments import ledger
from payments.ledger_models import LedgerEntry
from payments.models import SubscriptionPlan, VendorSubscription
from product.models import Category, ClippedCoupon, Coupon, CouponRedemption, Main_Category, Product, Sub_Category
from product.tests_flash_sales import FlashSaleTestCase, make_user
from userauths.tokens import CustomVendorRefreshToken
from vendor.models import Vendor

COUPON = '/api/v1/order/coupon/'
SUMMARY = '/api/v1/order/summary/'
COD = '/api/v1/payments/place-order-cod/'
VERIFY = '/api/v1/payments/verify-payment/{}/'


class CouponTestCase(FlashSaleTestCase):
    """Two stores: Flash Shop's kettle (100.00) and Other Shop's lamp (50.00)."""

    def setUp(self):
        super().setUp()
        self.other_owner = make_user(3)
        self.other = Vendor.objects.create(
            name='Other Shop', user=self.other_owner, email='other-shop@example.com', contact='+233550004444',
            is_approved=True, status='VERIFIED',
        )
        main = Main_Category.objects.create(title='Lamp Main')
        self.lamp_cat = Category.objects.create(title='Lamps', main_category=main)
        lamp_sub = Sub_Category.objects.create(title='Desk lamps', category=self.lamp_cat)
        self.lamp = Product.objects.create(
            title='Desk Lamp', sub_category=lamp_sub, vendor=self.other, status='published',
            price=Decimal('50.00'), total_quantity=50, brand=self.brand,
        )
        Address.objects.create(user=self.customer, full_name='Ama', region='Greater Accra', town='Accra',
                               address='1 Test St', status=True)
        self.api = APIClient()
        self.api.force_authenticate(self.customer)

    def coupon(self, code='SAVE', **kw):
        defaults = dict(code=code, discount_amount=Decimal('20.00'),
                        valid_from=self.now - timedelta(days=1), valid_to=self.now + timedelta(days=1))
        defaults.update(kw)
        return Coupon.objects.create(**defaults)

    def cart(self, kettles=2, lamps=1):
        cart, _ = Cart.objects.get_or_create(user=self.customer)
        if kettles:
            CartItem.objects.create(cart=cart, product=self.product, quantity=kettles)
        if lamps:
            CartItem.objects.create(cart=cart, product=self.lamp, quantity=lamps)
        return cart


class RulesTests(CouponTestCase):

    def quote(self, coupon, cart, **kw):
        return evaluate(coupon, self.customer, cart_lines(cart), **kw)

    def test_fixed_amount_split_pro_rata_to_the_cent(self):
        cart = self.cart()                      # 200 + 50
        q = self.quote(self.coupon(discount_amount=Decimal('10.00')), cart)
        self.assertEqual(q.discount, Decimal('10.00'))
        self.assertEqual(sum(q.shares.values()), Decimal('10.00'))
        self.assertEqual(sorted(q.shares.values()), [Decimal('2.00'), Decimal('8.00')])

    def test_odd_cents_still_add_up(self):
        cart = self.cart(kettles=1, lamps=2)    # 100 + 100 → 3.33 can't split evenly
        q = self.quote(self.coupon(discount_amount=Decimal('3.33')), cart)
        self.assertEqual(sum(q.shares.values()), Decimal('3.33'))

    def test_fixed_amount_never_exceeds_items(self):
        cart = self.cart(kettles=0, lamps=1)
        q = self.quote(self.coupon(discount_amount=Decimal('80.00')), cart)
        self.assertEqual(q.discount, Decimal('50.00'))

    def test_percentage_with_cap(self):
        cart = self.cart()
        q = self.quote(self.coupon(discount_amount=None, discount_percentage=10), cart)
        self.assertEqual(q.discount, Decimal('25.00'))
        q = self.quote(self.coupon('CAP', discount_amount=None, discount_percentage=10,
                                   max_discount_amount=Decimal('15.00')), cart)
        self.assertEqual(q.discount, Decimal('15.00'))

    def test_store_coupon_only_discounts_that_store(self):
        cart = self.cart()
        q = self.quote(self.coupon(vendor=self.other, discount_amount=None, discount_percentage=50), cart)
        self.assertEqual(q.discount, Decimal('25.00'))       # half the lamp, nothing off the kettles
        self.assertTrue(q.seller_funded)
        lamp_line = cart.cart_items.get(product=self.lamp)
        self.assertEqual(q.shares, {lamp_line.pk: Decimal('25.00')})

    def test_category_restriction(self):
        cart = self.cart()
        coupon = self.coupon(discount_amount=Decimal('100.00'))
        coupon.applicable_categories.add(self.lamp_cat)
        self.assertEqual(self.quote(coupon, cart).discount, Decimal('50.00'))

    def test_refusals(self):
        cart = self.cart(kettles=0, lamps=1)
        cases = [
            (dict(code='OFF', active=False), 'inactive'),
            (dict(code='OLD', valid_to=self.now - timedelta(minutes=1)), 'expired'),
            (dict(code='SOON', valid_from=self.now + timedelta(days=1), valid_to=self.now + timedelta(days=2)),
             'not_started'),
            (dict(code='GONE', max_uses=5, used_count=5), 'used_up'),
            (dict(code='MIN', min_purchase_amount=Decimal('60.00')), 'min_purchase'),
            (dict(code='MINE', vendor=self.vendor), 'not_applicable'),
        ]
        for kw, code in cases:
            with self.subTest(code=code):
                with self.assertRaises(CouponError) as ctx:
                    self.quote(self.coupon(**kw), cart)
                self.assertEqual(ctx.exception.code, code)

    def test_per_shopper_limit(self):
        cart = self.cart()
        coupon = self.coupon(per_user_limit=1)
        order = Order.objects.create(user=self.customer, total=Decimal('1.00'))
        CouponRedemption.objects.create(coupon=coupon, user=self.customer, order=order, amount=Decimal('1.00'))
        with self.assertRaises(CouponError) as ctx:
            self.quote(coupon, cart)
        self.assertEqual(ctx.exception.code, 'already_used')

    def test_lenient_mode_honours_a_coupon_that_ran_out_mid_payment(self):
        cart = self.cart()
        coupon = self.coupon(max_uses=1, used_count=1, valid_to=self.now - timedelta(minutes=1))
        self.assertEqual(self.quote(coupon, cart, strict=False).discount, Decimal('20.00'))


class CheckoutTests(CouponTestCase):

    def test_apply_shows_discount_in_summary_and_remove_clears_it(self):
        self.cart()
        self.coupon()
        res = self.api.post(COUPON, {'code': ' save '}, format='json')   # case and spaces don't matter
        self.assertEqual(res.status_code, 200, res.content)

        summary = self.api.get(SUMMARY).data
        self.assertEqual(Decimal(str(summary['discount_cedis'])), Decimal('20.00'))
        self.assertEqual(Decimal(str(summary['grand_total_cedis'])),
                         Decimal(str(summary['total_price'])) + Decimal(str(summary['delivery_fee'])) - 20)
        self.assertEqual(summary['coupon']['code'], 'SAVE')

        self.assertEqual(self.api.delete(COUPON).status_code, 204)
        summary = self.api.get(SUMMARY).data
        self.assertIsNone(summary['coupon'])
        self.assertEqual(Decimal(str(summary['discount_cedis'])), Decimal('0'))

    def test_bad_code_is_explained(self):
        self.cart()
        res = self.api.post(COUPON, {'code': 'NOPE'}, format='json')
        self.assertEqual((res.status_code, res.data['code']), (400, 'not_found'))
        self.assertIsNone(Cart.objects.get(user=self.customer).coupon_id)

    def test_cod_order_charges_discounted_total_and_records_it(self):
        cart = self.cart()
        coupon = self.coupon(vendor=self.other, discount_amount=Decimal('10.00'))
        self.api.post(COUPON, {'code': 'SAVE'}, format='json')
        before = Cart.objects.get(pk=cart.pk).calculate_grand_total(quote=evaluate(
            coupon, self.customer, cart_lines(cart)))

        res = self.api.post(COD)
        self.assertEqual(res.status_code, 201, res.content)
        order = Order.objects.get(pk=res.data['order_id'])
        self.assertEqual(order.total, before.quantize(Decimal('0.01')))
        self.assertEqual((order.coupon_id, order.discount_total), (coupon.id, Decimal('10.00')))

        lamp = order.order_products.get(product=self.lamp)
        kettle = order.order_products.get(product=self.product)
        self.assertEqual((lamp.discount, lamp.seller_discount), (Decimal('10.00'), Decimal('10.00')))
        self.assertEqual(kettle.discount, Decimal('0.00'))
        self.assertEqual(lamp.paid_amount, Decimal('40.00'))

        coupon.refresh_from_db()
        self.assertEqual(coupon.used_count, 1)
        self.assertTrue(CouponRedemption.objects.filter(coupon=coupon, order=order).exists())
        self.assertIsNone(Cart.objects.get(pk=cart.pk).coupon_id)   # not carried to the next order

        # Invoices show the coupon as its own row and add up to what was paid.
        from invoices.services import build_snapshot
        receipt = build_snapshot(order)
        self.assertEqual((receipt['discount'], receipt['coupon_code']), ('10.00', 'SAVE'))
        self.assertEqual(receipt['total'], f"{order.total:.2f}")
        store = build_snapshot(order, vendor=self.other)
        self.assertEqual((store['subtotal'], store['discount'], store['total']), ('50.00', '10.00', '40.00'))

        # Default one use per shopper: a second order can't reuse it.
        self.cart()
        res = self.api.post(COUPON, {'code': 'SAVE'}, format='json')
        self.assertEqual(res.data['code'], 'already_used')

    def test_cod_refuses_when_coupon_stopped_working(self):
        self.cart()
        coupon = self.coupon()
        self.api.post(COUPON, {'code': 'SAVE'}, format='json')
        Coupon.objects.filter(pk=coupon.pk).update(active=False)
        res = self.api.post(COD)
        self.assertEqual((res.status_code, res.data['code']), (400, 'coupon_invalid'))
        self.assertFalse(Order.objects.exists())

    @mock.patch('payments.views.requests.get')
    @mock.patch('payments.views.create_order_from_payment_task.delay')
    def test_paystack_accepts_discounted_amount_and_passes_shares(self, task, get):
        from payments.tests_order_payment import paystack
        cart = self.cart()
        coupon = self.coupon()
        self.api.post(COUPON, {'code': 'SAVE'}, format='json')
        expected = int(Cart.objects.get(pk=cart.pk).calculate_grand_total() * 100)
        full = int((Cart.objects.get(pk=cart.pk).calculate_grand_total() + 20) * 100)
        self.assertEqual(full - expected, 2000)

        get.return_value = paystack('REF-COUPON', expected)
        res = self.api.get(VERIFY.format('REF-COUPON'))
        self.assertEqual(res.status_code, 200, res.content)
        kwargs = task.call_args.kwargs
        self.assertEqual(kwargs['coupons'], [{'id': coupon.id, 'discount': '20.00'}])
        self.assertEqual(kwargs['code_id'], coupon.id)
        self.assertEqual(sum(Decimal(i['discount']) for i in kwargs['cart_items_data']), Decimal('20.00'))
        self.assertIsNone(Cart.objects.get(pk=cart.pk).coupon_id)

    def test_paid_order_task_records_coupon(self):
        from payments.tasks import create_order_from_payment_task
        coupon = self.coupon()
        address = Address.objects.get(user=self.customer)
        create_order_from_payment_task.apply(kwargs=dict(
            user_id=self.customer.id, payment_data={'amount': 18000}, payment_id=None,
            cart_items_data=[{
                'product_id': self.product.id, 'variant_id': None, 'quantity': 2, 'delivery_option_id': None,
                'amount': '200.00', 'flash_sale_id': None, 'flash_units': 0,
                'discount': '20.00', 'seller_discount': '0',
            }],
            address_id=address.id, ip='127.0.0.1', reference='ref-c', coupon={'id': coupon.id, 'discount': '20.00'},
        ))
        line = OrderProduct.objects.get(order__user=self.customer)
        self.assertEqual((line.amount, line.discount, line.seller_discount),
                         (Decimal('200.00'), Decimal('20.00'), Decimal('0.00')))
        self.assertEqual(line.order.discount_total, Decimal('20.00'))
        coupon.refresh_from_db()
        self.assertEqual(coupon.used_count, 1)


class LedgerTests(CouponTestCase):

    def setUp(self):
        super().setUp()
        plan = SubscriptionPlan.objects.create(name='Ledger Plan', tier='basic', price=Decimal('50.00'),
                                               commission_rate=Decimal('10.00'), payout_delay_days=0)
        VendorSubscription.objects.create(vendor=self.vendor, plan=plan, status='active',
                                          end_date=timezone.now() + timedelta(days=30))

    def delivered_line(self, coupon, discount, seller_discount):
        order = Order.objects.create(user=self.customer, total=Decimal('100.00') - discount, is_ordered=True,
                                     coupon=coupon, discount_total=discount)
        line = OrderProduct.objects.create(order=order, product=self.product, quantity=1, price=Decimal('100.00'),
                                           amount=Decimal('100.00'), discount=discount,
                                           seller_discount=seller_discount)
        shipment = Shipment.objects.create(order=order, vendor=self.vendor, status='delivered',
                                           delivered_at=timezone.now())
        shipment.items.set([line])
        ledger.post_shipment_earnings(shipment.pk)
        return line

    def entries(self, line):
        return {e.entry_type: e.amount for e in LedgerEntry.objects.filter(order_product=line)}

    def test_negromart_coupon_leaves_seller_earnings_untouched(self):
        line = self.delivered_line(self.coupon(), Decimal('20.00'), Decimal('0'))
        self.assertEqual(self.entries(line), {
            LedgerEntry.SALE: Decimal('100.00'), LedgerEntry.COMMISSION: Decimal('-10.00'),
        })
        # Full refund of the 80 the shopper paid: the seller hands back all 100
        # they earned (Negromart recovers its 20).
        self.assertEqual(ledger.seller_refund_debit(line, Decimal('80.00')), (Decimal('100.00'), Decimal('100.00')))

    def test_store_coupon_comes_off_seller_earnings_and_commission(self):
        line = self.delivered_line(self.coupon(vendor=self.vendor), Decimal('20.00'), Decimal('20.00'))
        self.assertEqual(self.entries(line), {
            LedgerEntry.SALE: Decimal('100.00'), LedgerEntry.SELLER_DISCOUNT: Decimal('-20.00'),
            LedgerEntry.COMMISSION: Decimal('-8.00'),
        })
        self.assertEqual(ledger.seller_refund_debit(line, Decimal('80.00')), (Decimal('80.00'), Decimal('80.00')))

    def test_shopper_refund_is_what_they_paid(self):
        from order.returns import _unit_price
        line = self.delivered_line(self.coupon(), Decimal('20.00'), Decimal('0'))
        self.assertEqual(_unit_price(line), Decimal('80.00'))


class SellerCouponTests(CouponTestCase):
    URL = '/api/v1/vendor/coupons/'

    def setUp(self):
        super().setUp()
        plan = SubscriptionPlan.objects.create(name='Pro Coupons', tier='pro', price=Decimal('99.00'),
                                               can_offer_discounts=True)
        VendorSubscription.objects.create(vendor=self.vendor, plan=plan, status='active',
                                          end_date=timezone.now() + timedelta(days=30))
        self.seller = APIClient()
        self.seller.cookies['vendor_access'] = str(CustomVendorRefreshToken.for_user(self.owner).access_token)
        self.seller.credentials(HTTP_X_USER_TYPE='vendor')

    def payload(self, **kw):
        data = dict(code='kettle-10', discount_type='percent', value='10',
                    valid_from=(self.now + timedelta(minutes=1)).isoformat(),
                    valid_to=(self.now + timedelta(days=7)).isoformat())
        data.update(kw)
        return data

    def test_create_list_and_delete(self):
        res = self.seller.post(self.URL, self.payload(), format='json')
        self.assertEqual(res.status_code, 201, res.content)
        self.assertEqual((res.data['code'], res.data['status']), ('KETTLE-10', 'scheduled'))
        coupon = Coupon.objects.get(pk=res.data['id'])
        self.assertEqual((coupon.vendor, coupon.discount_percentage, coupon.discount_amount),
                         (self.vendor, 10.0, None))
        self.assertEqual([c['id'] for c in self.seller.get(self.URL).data['results']], [coupon.id])
        self.assertEqual(self.seller.delete(f'{self.URL}{coupon.id}/').status_code, 204)

    def test_guardrails(self):
        post = lambda **kw: self.seller.post(self.URL, self.payload(**kw), format='json')
        self.coupon('TAKEN')
        for kw, field in [
            (dict(code='a!'), 'code'),
            (dict(code='taken'), 'code'),
            (dict(value='95'), 'value'),
            (dict(discount_type='amount', value='0.50'), 'value'),
            (dict(valid_to=(self.now + timedelta(days=120)).isoformat()), 'valid_to'),
            (dict(products=[self.lamp.id]), 'products'),     # another store's product
        ]:
            with self.subTest(field=field, kw=kw):
                res = post(**kw)
                self.assertEqual(res.status_code, 400, res.content)
                self.assertIn(field, res.data)

    def test_used_coupon_can_only_be_switched_off(self):
        coupon = self.coupon('USED', vendor=self.vendor)
        order = Order.objects.create(user=self.customer, total=Decimal('1.00'))
        CouponRedemption.objects.create(coupon=coupon, user=self.customer, order=order, amount=Decimal('5.00'))
        url = f'{self.URL}{coupon.id}/'
        self.assertEqual(self.seller.patch(url, {'value': '50', 'discount_type': 'percent'}, format='json').status_code, 400)
        self.assertEqual(self.seller.patch(url, {'active': False}, format='json').status_code, 200)
        self.assertEqual(self.seller.delete(url).status_code, 400)
        listing = self.seller.get(self.URL).data['results'][0]
        self.assertEqual((listing['orders'], listing['total_discount']), (1, '5.00'))

    def test_other_stores_coupons_are_invisible(self):
        theirs = self.coupon('THEIRS', vendor=self.other)
        self.assertEqual(self.seller.get(self.URL).data['results'], [])
        self.assertEqual(self.seller.patch(f'{self.URL}{theirs.id}/', {'active': False}, format='json').status_code, 404)


class ClipCouponTests(CouponTestCase):
    """Amazon-style clip coupons: badge on the product, clip it, applies by itself at checkout."""
    OFFERS = '/api/v1/order/coupons/offers/'
    CLIP = '/api/v1/order/coupons/{}/clip/'

    def setUp(self):
        super().setUp()
        forget_offers()

    def clip_coupon(self, code='CLIP-KETTLE', **kw):
        kw.setdefault('vendor', self.vendor)
        kw.setdefault('clippable', True)
        kw.setdefault('discount_amount', None)
        kw.setdefault('discount_percentage', 10)
        return self.coupon(code, **kw)

    def test_badge_shows_on_covered_products_only(self):
        coupon = self.clip_coupon()
        self.coupon('PRIVATE', vendor=self.other)            # private code: no badge
        res = self.client.get(self.OFFERS, {'products': f'{self.product.id},{self.lamp.id}'})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(list(res.data['offers']), [str(self.product.id)])
        self.assertEqual(res.data['offers'][str(self.product.id)]['id'], coupon.id)
        self.assertEqual(res.data['clipped'], [])

        from product.cards import card_extras
        cards = card_extras([self.product, self.lamp])
        self.assertEqual(cards[self.product.id]['coupon']['percent'], 10)
        self.assertIsNone(cards[self.lamp.id]['coupon'])

    def test_badge_hidden_once_budget_spent_or_ended(self):
        self.clip_coupon(budget=Decimal('5.00'), discount_spent=Decimal('5.00'))
        self.clip_coupon('CLIP-OLD', valid_to=self.now - timedelta(minutes=1))
        self.assertEqual(self.client.get(self.OFFERS, {'products': self.product.id}).data['offers'], {})

    def test_clipped_coupon_applies_without_a_code_and_is_recorded(self):
        self.cart()                                          # 2 kettles (200) + lamp (50)
        coupon = self.clip_coupon()
        summary = self.api.get(SUMMARY).data
        self.assertEqual(Decimal(str(summary['discount_cedis'])), Decimal('0'))   # not clipped yet
        self.assertEqual([o['id'] for o in summary['coupons_available']], [coupon.id])

        self.assertEqual(self.api.post(self.CLIP.format(coupon.id)).status_code, 201)
        self.assertEqual(self.api.get(self.OFFERS, {'products': self.product.id}).data['clipped'], [coupon.id])
        summary = self.api.get(SUMMARY).data
        self.assertEqual(Decimal(str(summary['discount_cedis'])), Decimal('20.00'))
        self.assertEqual([c['code'] for c in summary['clipped_coupons']], ['CLIP-KETTLE'])
        self.assertEqual(summary['coupons_available'], [])

        res = self.api.post(COD)
        self.assertEqual(res.status_code, 201, res.content)
        order = Order.objects.get(pk=res.data['order_id'])
        kettle = order.order_products.get(product=self.product)
        self.assertEqual((kettle.discount, kettle.seller_discount), (Decimal('20.00'), Decimal('20.00')))
        self.assertEqual((order.coupon_id, order.discount_total), (None, Decimal('20.00')))
        coupon.refresh_from_db()
        self.assertEqual((coupon.used_count, coupon.discount_spent), (1, Decimal('20.00')))

    def test_clip_and_a_typed_code_stack(self):
        cart = self.cart()
        clip = self.clip_coupon()                            # 10% of kettles = 20
        ClippedCoupon.objects.create(user=self.customer, coupon=clip)
        self.coupon('ALL10', discount_amount=None, discount_percentage=10)   # Negromart: 10% of what's left
        self.assertEqual(self.api.post(COUPON, {'code': 'ALL10'}, format='json').status_code, 200)

        deal = quote_cart(Cart.objects.get(pk=cart.pk))
        self.assertEqual([q.coupon.code for q in deal.clips], ['CLIP-KETTLE'])
        self.assertEqual(deal.code.discount, Decimal('23.00'))           # 10% of (180 + 50)
        self.assertEqual(deal.discount, Decimal('43.00'))

        res = self.api.post(COD)
        self.assertEqual(res.status_code, 201, res.content)
        order = Order.objects.get(pk=res.data['order_id'])
        kettle = order.order_products.get(product=self.product)
        self.assertEqual(kettle.seller_discount, Decimal('20.00'))       # only the store's own coupon
        self.assertEqual(kettle.discount, Decimal('38.00'))              # 20 + 18
        self.assertEqual(order.discount_total, Decimal('43.00'))
        self.assertEqual(order.coupon.code, 'ALL10')
        self.assertEqual(order.coupon_redemptions.count(), 2)

    def test_each_item_gets_only_its_best_clipped_coupon(self):
        cart = self.cart(lamps=0)
        small = self.clip_coupon('CLIP-SMALL', discount_percentage=5)
        big = self.clip_coupon('CLIP-BIG', discount_percentage=15)
        for c in (small, big):
            ClippedCoupon.objects.create(user=self.customer, coupon=c)
        deal = quote_cart(Cart.objects.get(pk=cart.pk))
        self.assertEqual([q.coupon.code for q in deal.clips], ['CLIP-BIG'])
        self.assertEqual(deal.discount, Decimal('30.00'))

    def test_budget_caps_the_discount_and_then_stops(self):
        cart = self.cart(lamps=0)
        coupon = self.clip_coupon(budget=Decimal('12.00'))   # 10% would be 20
        ClippedCoupon.objects.create(user=self.customer, coupon=coupon)
        self.assertEqual(quote_cart(Cart.objects.get(pk=cart.pk)).discount, Decimal('12.00'))
        Coupon.objects.filter(pk=coupon.pk).update(discount_spent=Decimal('12.00'))
        self.assertEqual(quote_cart(Cart.objects.get(pk=cart.pk)).discount, Decimal('0'))

    def test_clip_refusals(self):
        private = self.coupon('PRIVATE', vendor=self.vendor)
        self.assertEqual(self.api.post(self.CLIP.format(private.id)).data['code'], 'not_found')
        ended = self.clip_coupon(valid_to=self.now - timedelta(minutes=1))
        self.assertEqual(self.api.post(self.CLIP.format(ended.id)).data['code'], 'expired')
        self.assertIn(APIClient().post(self.CLIP.format(ended.id)).status_code, (401, 403))
        live = self.clip_coupon('CLIP-LIVE')
        self.api.post(self.CLIP.format(live.id))
        self.assertEqual(self.api.delete(self.CLIP.format(live.id)).status_code, 204)
        self.assertFalse(ClippedCoupon.objects.filter(user=self.customer).exists())

    def test_seller_creates_clip_coupon_without_a_code(self):
        plan = SubscriptionPlan.objects.create(name='Pro Clip', tier='pro', price=Decimal('99.00'),
                                               can_offer_discounts=True)
        VendorSubscription.objects.create(vendor=self.vendor, plan=plan, status='active',
                                          end_date=timezone.now() + timedelta(days=30))
        seller = APIClient()
        seller.cookies['vendor_access'] = str(CustomVendorRefreshToken.for_user(self.owner).access_token)
        seller.credentials(HTTP_X_USER_TYPE='vendor')
        dates = dict(valid_from=(self.now + timedelta(minutes=1)).isoformat(),
                     valid_to=(self.now + timedelta(days=7)).isoformat())
        res = seller.post('/api/v1/vendor/coupons/', dict(
            clippable=True, discount_type='percent', value='10', budget='300', **dates), format='json')
        self.assertEqual(res.status_code, 201, res.content)
        self.assertTrue(res.data['code'].startswith('CLIP-'))
        self.assertEqual((res.data['clippable'], res.data['budget'], res.data['clips']), (True, '300.00', 0))
        # A private code still needs a code.
        res = seller.post('/api/v1/vendor/coupons/', dict(
            clippable=False, discount_type='percent', value='10', **dates), format='json')
        self.assertEqual(res.status_code, 400)
        self.assertIn('code', res.data)
