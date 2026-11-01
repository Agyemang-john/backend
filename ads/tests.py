"""
Sponsored products: targeting, auction pricing, click billing, budgets, ledger billing, attribution.

Run with:  DB_HOST=localhost python manage.py test ads.tests --keepdb
"""
from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from ads import services
from ads.models import AdCampaign, AdCampaignProduct, AdClick, AdDailyStat, AdKeyword
from ecommerce.celery import app as celery_app
from order.models import Order, OrderProduct
from payments.ledger_models import LedgerEntry
from product.models import Brand, Category, Main_Category, Product, Sub_Category
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
    user = User.objects.create_user(f'Ads{n}', 'Test', f'ads{n}@example.com', f'+23324900{n:04d}', 'Str0ng!Passw0rd')
    User.objects.filter(pk=user.pk).update(is_active=True, email_verified_at=timezone.now())
    return User.objects.get(pk=user.pk)


@override_settings(**TEST_SETTINGS)
class AdsTests(TestCase):

    def setUp(self):
        from django.core.cache import cache
        cache.clear()
        cache.set('exchange_rates', {'GHS': 1, 'USD': 0.1}, None)
        for target in ('vendor.models.send_vendor_approval_email.delay', 'vendor.models.send_vendor_sms.delay'):
            p = mock.patch(target)
            p.start()
            self.addCleanup(p.stop)
        main = Main_Category.objects.create(title='Ads Main')
        self.sub = Sub_Category.objects.create(title='Ads Phones', category=Category.objects.create(title='Ads Cat', main_category=main))
        brand = Brand.objects.create(title='Ads Brand')
        self.owners, self.shops, self.products = [], [], []
        for i in range(3):
            owner = make_user(i + 1)
            shop = Vendor.objects.create(name=f'Ads Shop {i}', user=owner, email=f'ads-shop{i}@example.com',
                                         contact=f'+23355000700{i}', is_approved=True, status='VERIFIED')
            # Earnings so the store can pay for ads.
            LedgerEntry.objects.create(vendor=shop, entry_type=LedgerEntry.ADJUSTMENT, amount=Decimal('100'),
                                       description='seed', available_at=timezone.now())
            p = Product.objects.create(title=f'Ads Phone {i} Galaxy', sub_category=self.sub, vendor=shop,
                                       status='published', price=Decimal('500'), total_quantity=5, brand=brand)
            self.owners.append(owner); self.shops.append(shop); self.products.append(p)

    def campaign(self, i, bid, budget='50', keywords=(), auto=True):
        c = AdCampaign.objects.create(vendor=self.shops[i], name=f'C{i}', bid=Decimal(bid), daily_budget=Decimal(budget),
                                      start_date=timezone.localdate(), auto_targeting=auto)
        AdCampaignProduct.objects.create(campaign=c, product=self.products[i])
        for k in keywords:
            AdKeyword.objects.create(campaign=c, phrase=k)
        return c

    def test_second_price_auction(self):
        self.campaign(0, '1.00')
        self.campaign(1, '3.00')
        winners = services.auction(services._candidates('category', sub_category_id=self.sub.pk))
        self.assertEqual([w['product_id'] for w in winners], [self.products[1].pk, self.products[0].pk])
        self.assertEqual(winners[0]['cost'], Decimal('1.01'), "pays just above the runner-up")
        self.assertEqual(winners[1]['cost'], services.RESERVE_CPC)

    def test_keyword_exact_and_phrase(self):
        self.campaign(0, '1.00', keywords=['galaxy'], auto=False)
        c1 = self.campaign(1, '1.00', auto=False)
        AdKeyword.objects.create(campaign=c1, phrase='cheap phone', match=AdKeyword.EXACT)
        found = {p for _, p, _ in services._candidates('search', query='Samsung Galaxy A15')}
        self.assertEqual(found, {self.products[0].pk})
        self.assertEqual({p for _, p, _ in services._candidates('search', query='cheap phone')}, {self.products[1].pk})
        self.assertEqual(services._candidates('search', query='a cheap phone deal'), [])

    def test_listing_carries_sponsored_and_click_bills(self):
        self.campaign(0, '2.00')
        res = APIClient().get(f'/api/v1/product/category/{self.sub.slug}/')
        self.assertEqual(len(res.data['sponsored']), 1)
        token = res.data['sponsored'][0]['ad_token']
        shopper = APIClient()
        shopper.force_authenticate(make_user(9))
        self.assertEqual(shopper.post('/api/v1/ads/click/', {'token': token}, format='json').status_code, 200)
        self.assertEqual(shopper.post('/api/v1/ads/click/', {'token': token}, format='json').status_code, 200)
        self.assertEqual(AdClick.objects.count(), 1, "same visitor billed once")
        stat = AdDailyStat.objects.get()
        self.assertEqual((stat.impressions, stat.clicks, stat.spend), (1, 1, services.RESERVE_CPC))
        self.assertEqual(APIClient().post('/api/v1/ads/click/', {'token': 'forged'}, format='json').status_code, 400)

    def test_budget_stops_serving(self):
        c = self.campaign(0, '2.00', budget='5')
        AdDailyStat.objects.create(campaign=c, product=self.products[0], date=timezone.localdate(), spend=Decimal('4.00'))
        self.assertEqual(services._candidates('category', sub_category_id=self.sub.pk), [])

    def test_unfunded_store_not_served(self):
        LedgerEntry.objects.filter(vendor=self.shops[0]).delete()
        self.campaign(0, '2.00')
        services.refresh_funded_vendors()
        self.assertEqual(services._candidates('category', sub_category_id=self.sub.pk), [])

    def test_nightly_billing_and_attribution(self):
        c = self.campaign(0, '2.00')
        buyer = make_user(8)
        yesterday = timezone.now() - timedelta(days=1)
        click = AdClick.objects.create(campaign=c, product=self.products[0], user=buyer, visitor='u:1',
                                       cost=Decimal('2.00'), placement='search')
        AdClick.objects.filter(pk=click.pk).update(created_at=yesterday)
        AdDailyStat.objects.create(campaign=c, product=self.products[0], date=timezone.localdate(yesterday),
                                   clicks=1, spend=Decimal('2.00'))
        order = Order.objects.create(user=buyer, total=Decimal('500'), is_ordered=True, order_number='ORD-ADS-1')
        OrderProduct.objects.create(order=order, product=self.products[0], quantity=1, price=Decimal('500'), amount=Decimal('500'))

        self.assertEqual(services.bill_spend(), 1)
        entry = LedgerEntry.objects.get(entry_type=LedgerEntry.AD_SPEND)
        self.assertEqual((entry.vendor, entry.amount), (self.shops[0], Decimal('-2.00')))
        self.assertEqual(services.bill_spend(), 0, "idempotent")
        self.assertEqual(services.attribute_orders(), 1)
        self.assertEqual(AdDailyStat.objects.get().sales, Decimal('500.00'))

    def test_seller_campaign_api(self):
        api = APIClient()
        api.cookies['vendor_access'] = str(CustomVendorRefreshToken.for_user(self.owners[0]).access_token)
        api.credentials(HTTP_X_USER_TYPE='vendor')
        bad = api.post('/api/v1/ads/seller/campaigns/', {'name': 'X', 'bid': '1', 'daily_budget': '10',
                                                          'products': [self.products[1].pk]}, format='json')
        self.assertEqual(bad.status_code, 400, "someone else's product")
        res = api.post('/api/v1/ads/seller/campaigns/', {
            'name': 'Launch', 'bid': '1.50', 'daily_budget': '20', 'products': [self.products[0].pk],
            'keywords': [{'phrase': '  Galaxy  Phone '}]}, format='json')
        self.assertEqual(res.status_code, 201, res.content)
        self.assertEqual(res.data['keywords'][0]['phrase'], 'galaxy phone')
        cid = res.data['id']
        res = api.patch(f'/api/v1/ads/seller/campaigns/{cid}/', {'status': 'paused'}, format='json')
        self.assertEqual(res.data['status'], 'paused')
        self.assertEqual(api.get(f'/api/v1/ads/seller/campaigns/{cid}/').status_code, 200)


@override_settings(**TEST_SETTINGS)
class NewStoreEndToEndTests(TestCase):
    """A store with no earnings yet, exactly as in production: it can only advertise with a saved card."""

    def setUp(self):
        AdsTests.setUp(self)
        LedgerEntry.objects.all().delete()          # brand-new stores: nothing earned yet
        from django.core.cache import cache
        cache.delete(services.FUNDED_KEY)
        self.api = APIClient()
        self.api.cookies['vendor_access'] = str(CustomVendorRefreshToken.for_user(self.owners[0]).access_token)
        self.api.credentials(HTTP_X_USER_TYPE='vendor')

    def create_campaign(self):
        res = self.api.post('/api/v1/ads/seller/campaigns/', {
            'name': 'Launch', 'bid': '1.00', 'daily_budget': '20', 'products': [self.products[0].pk]}, format='json')
        self.assertEqual(res.status_code, 201, res.content)
        from django.core.cache import cache
        cache.delete(services.FUNDED_KEY)

    def search(self):
        return APIClient().get('/api/v1/product/search/', {'q': 'galaxy'}).data

    def test_no_card_no_earnings_is_explained(self):
        self.create_campaign()
        self.assertEqual(self.search()['sponsored'], [])
        page = self.api.get('/api/v1/ads/seller/campaigns/').data
        self.assertEqual((page['funded'], page['has_card']), (False, False))

    def test_saved_card_store_is_served_first_and_card_pays(self):
        from payments.models import PaystackAuthorization, PaystackCustomer
        cust = PaystackCustomer.objects.create(vendor=self.shops[0], customer_code='CUS_x', email='ads-shop0@example.com') \
            if 'vendor' in [f.name for f in PaystackCustomer._meta.fields] else None
        PaystackAuthorization.objects.create(vendor=self.shops[0], paystack_customer=cust, authorization_code='AUTH_x',
                                             card_type='visa', last4='4081', exp_month='12', exp_year='2030',
                                             is_reusable=True, is_default=True)
        self.create_campaign()

        data = self.search()
        self.assertEqual(len(data['sponsored']), 1, data.get('sponsored'))
        ad = data['sponsored'][0]
        self.assertTrue(ad['sponsored'])
        self.assertEqual(ad['product']['id'], self.products[0].pk)

        shopper = APIClient()
        shopper.force_authenticate(make_user(7))
        self.assertEqual(shopper.post('/api/v1/ads/click/', {'token': ad['ad_token']}, format='json').status_code, 200)
        self.assertEqual(AdDailyStat.objects.get().clicks, 1)
        # A day's worth of clicks, billed the next night.
        AdDailyStat.objects.update(date=timezone.localdate() - timedelta(days=1), spend=Decimal('5.00'))

        ok = mock.Mock()
        ok.json.return_value = {'data': {'status': 'success', 'id': 99}}
        with mock.patch('requests.post', return_value=ok) as post:
            self.assertEqual(services.bill_spend(), 1)
        self.assertEqual(post.call_args.kwargs['json']['amount'], 500)      # GHS 5.00 in pesewas
        from ads.billing import balance
        self.assertEqual(balance(self.shops[0].pk), Decimal('0.00'), "card covered the spend")

    def test_declined_card_pauses_ads(self):
        from payments.models import PaystackAuthorization, PaystackCustomer
        cust = PaystackCustomer.objects.create(vendor=self.shops[0], customer_code='CUS_y', email='ads-shop0@example.com') \
            if 'vendor' in [f.name for f in PaystackCustomer._meta.fields] else None
        PaystackAuthorization.objects.create(vendor=self.shops[0], paystack_customer=cust, authorization_code='AUTH_y',
                                             card_type='visa', last4='0000', exp_month='12', exp_year='2030', is_reusable=True)
        c = AdCampaign.objects.create(vendor=self.shops[0], name='C', bid=Decimal('1'), daily_budget=Decimal('20'),
                                      start_date=timezone.localdate())
        AdDailyStat.objects.create(campaign=c, product=self.products[0], date=timezone.localdate() - timedelta(days=1),
                                   clicks=3, spend=Decimal('3.00'))
        bad = mock.Mock()
        bad.json.return_value = {'data': {'status': 'failed', 'gateway_response': 'Insufficient funds'}}
        with mock.patch('requests.post', return_value=bad):
            services.bill_spend()
        self.assertNotIn(self.shops[0].pk, services.refresh_funded_vendors())
