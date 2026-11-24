"""
tools/tests.py
Paid seller tools: plan gating, bulk edit, stock forecast, listing doctor,
profit report, price insights and the auto-repricer.

Run with:  DB_HOST=localhost python manage.py test tools --keepdb
"""
from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone

from notification.models import Notification
from order.models import Order, OrderProduct
from payments.ledger_models import LedgerEntry
from payments.models import SubscriptionPlan
from product.models import CatalogItem, Product, Variants
from tools import forecast, pricing
from tools.models import PriceChange, RepricerRule, ToolUsage
from vendor.models import Vendor
from vendor.tests_operations import OpsTestCase, make_user, vendor_client

ALL_TOOLS = dict(can_bulk_edit=True, can_use_stock_forecast=True, listing_assistant_monthly_limit=2,
                 can_use_profit_report=True, can_use_price_insights=True, can_use_auto_repricer=True,
                 can_use_demand_insights=True, can_use_promotions=True, broadcasts_monthly_limit=3,
                 can_print_labels=True, can_use_chat_tools=True, can_use_store_link=True, can_use_api=True)


class ToolsTestCase(OpsTestCase):

    def setUp(self):
        super().setUp()
        cache.clear()
        SubscriptionPlan.objects.filter(pk=self.plan.pk).update(**ALL_TOOLS)
        self.plan.refresh_from_db()
        self.api = vendor_client(self.owner)

    def sell(self, product, qty, days_ago=1, variant=None):
        line = OrderProduct.objects.create(order=self.order, product=product, variant=variant, quantity=qty,
                                           price=product.price, amount=product.price * qty)
        OrderProduct.objects.filter(pk=line.pk).update(date_created=timezone.now() - timedelta(days=days_ago))
        return line


class GatingTests(ToolsTestCase):

    def test_locked_tool_names_the_plan_that_unlocks_it(self):
        SubscriptionPlan.objects.create(name='Pro Tools', tier='pro', price=Decimal('300'), can_bulk_edit=True)
        SubscriptionPlan.objects.filter(pk=self.plan.pk).update(can_bulk_edit=False)
        cache.clear()
        res = self.api.get('/api/v1/tools/bulk-edit/')
        self.assertEqual(res.status_code, 403)
        self.assertEqual(res.data['upgrade_plan'], 'Pro Tools')

        overview = {t['key']: t for t in self.api.get('/api/v1/tools/').data['tools']}
        self.assertFalse(overview['bulk_edit']['included'])
        self.assertTrue(overview['stock_forecast']['included'])
        self.assertEqual(overview['listing_doctor']['allowance'], 2)

    def test_plan_summary_lists_tools(self):
        res = self.api.get('/api/v1/vendor/plan/')
        self.assertTrue(res.data['tools']['profit_report']['included'])


class BulkEditTests(ToolsTestCase):

    def test_rows_and_saving_changes(self):
        tee = Product.objects.create(title='Tools Tee', sub_category=self.sub, vendor=self.vendor, status='published',
                                     price=Decimal('50'), variant='Size', variation_axes=[{'key': 'size', 'label': 'Size'}])
        small = Variants.objects.create(product=tee, options={'size': 'S'}, price=Decimal('50'), quantity=3)
        rows = self.api.get('/api/v1/tools/bulk-edit/').data['results']
        self.assertIn({'product_id': tee.pk, 'variant_id': small.pk, 'version': 'S'},
                      [{k: r[k] for k in ('product_id', 'variant_id', 'version')} for r in rows])

        other_vendor = Vendor.objects.create(name='Other', user=make_user(9), email='o@example.com',
                                             contact='+233550009999', is_approved=True, status='VERIFIED')
        theirs = Product.objects.create(title='Not Mine', sub_category=self.sub, vendor=other_vendor,
                                        status='published', price=Decimal('9'))
        res = self.api.post('/api/v1/tools/bulk-edit/', {'changes': [
            {'product_id': self.product.pk, 'variant_id': None, 'price': '90.00', 'stock': '12', 'unit_cost': '40'},
            {'product_id': tee.pk, 'variant_id': small.pk, 'price': '55', 'stock': '7'},
            {'product_id': theirs.pk, 'variant_id': None, 'price': '1'},
            {'product_id': self.product.pk, 'variant_id': None, 'price': '-3'},
        ]}, format='json')
        self.assertEqual(res.status_code, 207)
        self.assertEqual(res.data['updated'], 2)
        self.assertEqual([e['index'] for e in res.data['errors']], [2, 3])
        self.product.refresh_from_db(); small.refresh_from_db(); theirs.refresh_from_db()
        self.assertEqual((self.product.price, self.product.total_quantity, self.product.unit_cost),
                         (Decimal('90.00'), 12, Decimal('40.00')))
        self.assertEqual((small.price, small.quantity), (Decimal('55.00'), 7))
        self.assertEqual(theirs.price, Decimal('9'))
        self.assertEqual(PriceChange.objects.filter(vendor=self.vendor, source='bulk_edit').count(), 2)

    def test_action_and_csv_roundtrip(self):
        res = self.api.post('/api/v1/tools/bulk-edit/action/', {'op': 'price_percent', 'value': -10,
                                                               'product_ids': [self.product.pk]}, format='json')
        self.assertEqual(res.status_code, 200, res.content)
        self.product.refresh_from_db()
        self.assertEqual(self.product.price, Decimal('90.00'))

        csv_text = self.api.get('/api/v1/tools/bulk-edit/csv/').content.decode('utf-8-sig')
        self.assertIn('Ops Kettle', csv_text)
        edited = csv_text.replace(',90.00,', ',77.50,')
        upload = SimpleUploadedFile('p.csv', edited.encode('utf-8'), content_type='text/csv')
        res = self.api.post('/api/v1/tools/bulk-edit/csv/', {'file': upload}, format='multipart')
        self.assertEqual(res.status_code, 200, res.content)
        self.product.refresh_from_db()
        self.assertEqual(self.product.price, Decimal('77.50'))


class ForecastTests(ToolsTestCase):

    def test_pace_days_left_and_reorder(self):
        Product.objects.filter(pk=self.product.pk).update(total_quantity=10)
        OrderProduct.objects.filter(pk=self.line.pk).update(date_created=timezone.now() - timedelta(days=40))
        self.order.is_ordered = True
        self.order.save(update_fields=['is_ordered'])
        self.sell(self.product, 14, days_ago=2)      # 14 in the last week
        self.sell(self.product, 14, days_ago=20)     # 28 in four weeks
        data = forecast.forecast(self.vendor, lead_days=7, cover_days=30, now=timezone.now())
        row = next(r for r in data['results'] if r['product_id'] == self.product.pk)
        # 0.6 × 14/7 + 0.4 × 28/28 = 1.6 a day → 10 / 1.6 = 6.2 days
        self.assertEqual(row['per_day'], 1.6)
        self.assertEqual(row['days_left'], 6.2)
        self.assertEqual(row['status'], 'reorder_now')
        self.assertEqual(row['reorder_qty'], 60 - 10 + 0)   # ceil(1.6 × 37) = 60

        self.assertEqual(forecast.alert_vendor(self.vendor), 1)
        self.assertEqual(forecast.alert_vendor(self.vendor), 0, 'once a week per item')
        self.assertTrue(Notification.objects.filter(recipient=self.owner, verb='vendor_stock_running_out').exists())

    def test_endpoint_and_csv(self):
        self.assertEqual(self.api.get('/api/v1/tools/stock/').status_code, 200)
        res = self.api.get('/api/v1/tools/stock/', {'export': 'csv', 'all': '1'})
        self.assertIn('reorder_qty', res.content.decode('utf-8-sig'))


class ListingDoctorTests(ToolsTestCase):

    def fake_client(self, title='Ops Electric Kettle 1.7 L Stainless Steel', fail=None):
        parsed = mock.Mock(title=title, description_paragraphs=['Boils fast.', 'Easy to clean.'],
                           features=['1.7 litres', 'Auto shut-off'])
        client = mock.Mock()
        if fail:
            client.beta.messages.parse.side_effect = fail
        else:
            client.beta.messages.parse.return_value = mock.Mock(stop_reason='end_turn', parsed_output=parsed)
        return client

    def test_scores_point_at_what_to_fix(self):
        data = self.api.get('/api/v1/tools/listing-doctor/').data
        row = next(r for r in data['results'] if r['product_id'] == self.product.pk)
        sections = {i['section'] for i in row['issues']}
        self.assertTrue({'media', 'description'} <= sections)
        self.assertLess(row['score'], 70)
        self.assertEqual(data['allowance'], 2)

    def test_suggest_uses_allowance_and_apply_saves(self):
        url = f'/api/v1/tools/listing-doctor/{self.product.pk}/suggest/'
        with mock.patch('tools.listing_doctor._client', return_value=self.fake_client()):
            res = self.api.post(url, {'notes': 'stainless'}, format='json')
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(res.data['used'], 1)
        self.assertIn('<li>Auto shut-off</li>', res.data['features'])
        self.product.refresh_from_db()
        self.assertEqual(self.product.title, 'Ops Kettle', 'nothing saved until applied')

        res = self.api.post(f'/api/v1/tools/listing-doctor/{self.product.pk}/apply/',
                            {'title': res.data['title'], 'description': res.data['description']}, format='json')
        self.assertEqual(res.status_code, 200, res.content)
        self.product.refresh_from_db()
        self.assertTrue(self.product.title.startswith('Ops Electric Kettle'))
        self.assertIn('<p>Boils fast.</p>', self.product.description)

    def test_failures_give_the_unit_back_and_limit_holds(self):
        import anthropic
        url = f'/api/v1/tools/listing-doctor/{self.product.pk}/suggest/'
        error = anthropic.APIConnectionError(request=mock.Mock())
        with mock.patch('tools.listing_doctor._client', return_value=self.fake_client(fail=error)):
            self.assertEqual(self.api.post(url, {}, format='json').status_code, 503)
        self.assertEqual(ToolUsage.used(self.vendor, 'listing_doctor'), 0)

        with mock.patch('tools.listing_doctor._client', return_value=self.fake_client()):
            self.api.post(url, {}, format='json')
            self.api.post(url, {}, format='json')
            res = self.api.post(url, {}, format='json')
        self.assertEqual(res.status_code, 429)
        self.assertEqual(ToolUsage.used(self.vendor, 'listing_doctor'), 2)

    def test_not_configured_costs_nothing(self):
        with self.settings(ANTHROPIC_API_KEY=''):
            res = self.api.post(f'/api/v1/tools/listing-doctor/{self.product.pk}/suggest/', {}, format='json')
        self.assertEqual(res.status_code, 503)
        self.assertEqual(ToolUsage.used(self.vendor, 'listing_doctor'), 0)


class ProfitTests(ToolsTestCase):

    def test_profit_after_fees_refunds_ads_and_cost(self):
        Product.objects.filter(pk=self.product.pk).update(unit_cost=Decimal('60'))
        self.deliver()          # posts SALE 200, COMMISSION -20 for the 2 kettles
        LedgerEntry.objects.create(vendor=self.vendor, entry_type=LedgerEntry.AD_SPEND, amount=Decimal('-15'),
                                   description='Ads', available_at=timezone.now())
        data = self.api.get('/api/v1/tools/profit/').data
        t = data['totals']
        self.assertEqual(Decimal(t['sales']), Decimal('200.00'))
        self.assertEqual(Decimal(t['commission']), Decimal('-20.00'))
        self.assertEqual(Decimal(t['ads']), Decimal('-15.00'))
        self.assertEqual(Decimal(t['cost_of_goods']), Decimal('120.00'))
        self.assertEqual(Decimal(t['profit']), Decimal('45.00'))      # 200 - 20 - 15 - 120
        self.assertEqual(data['units_without_cost'], 0)
        self.assertEqual(Decimal(data['products'][0]['profit']), Decimal('60.00'))   # ads aren't split per product
        self.assertIn('TOTAL', self.api.get('/api/v1/tools/profit/', {'export': 'csv'}).content.decode('utf-8-sig'))


class PricingTests(ToolsTestCase):

    def setUp(self):
        super().setUp()
        self.item = CatalogItem.objects.create(title='Ops Kettle', sub_category=self.sub)
        other_vendor = Vendor.objects.create(name='Rival', user=make_user(8), email='r@example.com',
                                             contact='+233550008888', is_approved=True, status='VERIFIED')
        self.rival = Product.objects.create(title='Ops Kettle', sub_category=self.sub, vendor=other_vendor,
                                            status='published', price=Decimal('95'), total_quantity=4,
                                            catalog_item=self.item)
        Product.objects.filter(pk=self.product.pk).update(catalog_item=self.item, status='published')
        self.product.refresh_from_db()

    def test_insights_show_the_cheapest_other_seller(self):
        row = next(r for r in self.api.get('/api/v1/tools/pricing/').data['results'] if r['product_id'] == self.product.pk)
        self.assertEqual(row['kind'], 'shared')
        self.assertEqual(Decimal(row['lowest_other']), Decimal('95'))
        self.assertEqual(Decimal(row['gap']), Decimal('5.00'))

    def test_repricer_beats_within_limits_and_holds_at_minimum(self):
        res = self.api.post('/api/v1/tools/pricing/rules/', {'product_id': self.product.pk, 'strategy': 'beat',
                                                            'step': '1', 'min_price': '90', 'max_price': '150'},
                            format='json')
        self.assertEqual(res.status_code, 201, res.content)
        self.assertEqual(pricing.run_repricer(), 1)
        self.product.refresh_from_db()
        self.assertEqual(self.product.price, Decimal('94.00'))
        self.assertTrue(PriceChange.objects.filter(product=self.product, source='repricer').exists())

        Product.objects.filter(pk=self.rival.pk).update(price=Decimal('80'))
        RepricerRule.objects.update(last_run_at=None)
        pricing.run_repricer()
        self.product.refresh_from_db()
        self.assertEqual(self.product.price, Decimal('90.00'), 'never below the minimum')
        self.assertTrue(Notification.objects.filter(verb='vendor_repricer_at_minimum').exists())

    def test_repricer_stops_when_the_plan_loses_it(self):
        RepricerRule.objects.create(vendor=self.vendor, product=self.product, min_price=Decimal('50'),
                                    max_price=Decimal('150'))
        SubscriptionPlan.objects.filter(pk=self.plan.pk).update(can_use_auto_repricer=False)
        self.assertEqual(pricing.run_repricer(), 0)
        self.product.refresh_from_db()
        self.assertEqual(self.product.price, Decimal('100.00'))


class DemandTests(ToolsTestCase):

    def test_demand_from_searches_in_my_categories(self):
        from product.models import SearchTermDaily
        from tools import demand
        Product.objects.filter(pk=self.product.pk).update(status='published', is_buybox_winner=True)
        self.product.refresh_from_db()
        self.product.save()                            # refreshes the search vector
        today = timezone.localdate()
        SearchTermDaily.objects.create(term='kettle', date=today, searchers=40)
        from product.models import Sub_Category
        Sub_Category.objects.create(title='Toasters & Grills', category=self.sub.category)
        SearchTermDaily.objects.create(term='steam toaster', date=today, no_result_searchers=12)
        SearchTermDaily.objects.create(term='rare thing', date=today, searchers=1)       # too few shoppers
        self.assertEqual(demand.refresh(), 2)

        data = self.api.get('/api/v1/tools/demand/').data
        opp = {r['term']: r for r in data['opportunities']}
        self.assertEqual(opp['kettle']['live_results'], 1)
        self.assertEqual(opp['kettle']['per_listing'], 40.0)
        self.assertEqual(data['your_share'][0]['yours'], 1)
        nothing = {r['term']: r for r in data['nothing_found']}
        self.assertEqual(nothing['steam toaster']['found_nothing'], 12)
        self.assertEqual(nothing['steam toaster']['category'], 'Toasters & Grills', 'matched by the category name')


class PromotionTests(ToolsTestCase):

    def setUp(self):
        super().setUp()
        self.order.is_ordered = True
        self.order.save(update_fields=['is_ordered'])
        Product.objects.filter(pk=self.product.pk).update(status='published')
        self.product.refresh_from_db()

    def put(self, kind, settings_, active=True):
        return self.api.put(f'/api/v1/tools/promotions/{kind}/', {'is_active': active, 'settings': settings_}, format='json')

    def test_win_back_sends_a_personal_coupon_once(self):
        from django.core import mail
        from order.coupons import CouponError, check_live
        from tools import promotions
        from tools.models import PromotionSend
        OrderProduct.objects.filter(pk=self.line.pk).update(date_created=timezone.now() - timedelta(days=70))
        res = self.put('win_back', {'days': 60, 'percent': 15, 'valid_days': 14})
        self.assertEqual(res.status_code, 200, res.content)

        self.assertEqual(promotions.run_all(), 1)
        send = PromotionSend.objects.get(user=self.customer)
        coupon = send.coupon
        self.assertEqual((coupon.vendor_id, coupon.discount_percentage, coupon.max_uses), (self.vendor.pk, 15.0, 1))
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn(coupon.code, mail.outbox[0].alternatives[0][0])
        self.assertTrue(Notification.objects.filter(recipient=self.customer, verb='customer_store_offer').exists())

        check_live(coupon, self.customer)                     # theirs: fine
        with self.assertRaises(CouponError) as err:
            check_live(coupon, self.owner)                    # anyone else: refused
        self.assertEqual(err.exception.code, 'not_yours')

        promotions.run_all()
        self.assertEqual(PromotionSend.objects.filter(user=self.customer).count(), 1, 'cooldown holds')

    def test_opted_out_shoppers_get_no_email(self):
        from django.core import mail
        from engagement.models import EmailOptOut
        from tools import promotions
        OrderProduct.objects.filter(pk=self.line.pk).update(date_created=timezone.now() - timedelta(days=70))
        EmailOptOut.objects.create(user=self.customer, category=EmailOptOut.STORE_OFFERS)
        self.put('win_back', {'days': 60, 'percent': 10, 'valid_days': 14})
        promotions.run_all()
        self.assertEqual(len(mail.outbox), 0)
        self.assertTrue(Notification.objects.filter(recipient=self.customer, verb='customer_store_offer').exists())

    def test_thank_you_after_first_delivery(self):
        from tools import promotions
        from tools.models import PromotionSend
        OrderProduct.objects.filter(pk=self.line.pk).update(status='delivered',
                                                            delivered_date=timezone.now() - timedelta(days=4))
        self.put('thank_you', {'after_days': 3, 'percent': 10, 'valid_days': 30})
        promotions.run_all()
        self.assertTrue(PromotionSend.objects.filter(user=self.customer, rule__kind='thank_you').exists())

    def test_weekly_sale_schedules_flash_sales_and_cancels(self):
        from product.models import FlashSale
        res = self.put('weekly_sale', {'weekdays': list(range(7)), 'start': '23:00', 'hours': 1, 'percent': 20,
                                       'product_ids': [self.product.pk]})
        self.assertEqual(res.status_code, 200, res.content)
        sale = FlashSale.objects.get(product=self.product)
        self.assertEqual(sale.sale_price, Decimal('80.00'))
        self.assertIn('1 sale scheduled', res.data['last_result'])

        self.put('weekly_sale', {'weekdays': list(range(7)), 'start': '23:00', 'hours': 1, 'percent': 20,
                                 'product_ids': [self.product.pk]}, active=False)
        self.assertFalse(FlashSale.objects.filter(product=self.product, start_time__gt=timezone.now()).exists())

    def test_bad_settings_and_plan(self):
        self.assertEqual(self.put('weekly_sale', {'weekdays': [1], 'product_ids': []}).status_code, 400)
        self.assertEqual(self.put('win_back', {'percent': 95}).status_code, 400)
        from tools import promotions
        OrderProduct.objects.filter(pk=self.line.pk).update(date_created=timezone.now() - timedelta(days=70))
        self.put('win_back', {'days': 60, 'percent': 10, 'valid_days': 14})
        SubscriptionPlan.objects.filter(pk=self.plan.pk).update(can_use_promotions=False)
        self.assertEqual(promotions.run_all(), 0)


class BroadcastTests(ToolsTestCase):

    def setUp(self):
        super().setUp()
        self.order.is_ordered = True
        self.order.save(update_fields=['is_ordered'])
        self.fan = make_user(7)
        self.vendor.followers.add(self.fan)

    def post(self, **data):
        from celery import current_app
        body = {'audience': 'everyone', 'subject': 'New arrivals', 'message': 'Fresh kettles just landed this week.', **data}
        eager = current_app.conf.task_always_eager
        current_app.conf.task_always_eager = True
        try:
            with self.captureOnCommitCallbacks(execute=True):
                return self.api.post('/api/v1/tools/broadcasts/', body, format='json')
        finally:
            current_app.conf.task_always_eager = eager

    def test_sends_to_followers_and_buyers_in_app_and_email(self):
        from django.core import mail
        from engagement.models import EmailOptOut
        from tools.models import Broadcast
        EmailOptOut.objects.create(user=self.fan, category=EmailOptOut.STORE_OFFERS)
        sizes = self.api.get('/api/v1/tools/broadcasts/').data['audience_sizes']
        self.assertEqual({k: v['people'] for k, v in sizes.items()}, {'followers': 1, 'buyers': 1, 'everyone': 2})

        res = self.post(product_ids=[self.product.pk])
        self.assertEqual(res.status_code, 201, res.content)
        b = Broadcast.objects.get(pk=res.data['id'])
        self.assertEqual((b.status, b.recipients, b.emailed), ('sent', 2, 1))
        self.assertEqual(Notification.objects.filter(verb='customer_store_news').count(), 2)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, [self.customer.email])
        self.assertIn('Ops Kettle', mail.outbox[0].alternatives[0][0])

        again = self.post()
        self.assertEqual(again.status_code, 400)
        self.assertIn('One message a day', again.data['error'])

    def test_moderation_allowance_and_audience(self):
        bad = self.post(message='Call me on 0244 123 456 for a better price')
        self.assertEqual(bad.status_code, 400)
        self.assertIn('phone', bad.data['error'])
        self.assertEqual(ToolUsage.used(self.vendor, 'broadcasts'), 0)

        SubscriptionPlan.objects.filter(pk=self.plan.pk).update(broadcasts_monthly_limit=1)
        ToolUsage.objects.create(vendor=self.vendor, tool='broadcasts', month=timezone.localdate().replace(day=1), count=1)
        res = self.post()
        self.assertEqual(res.status_code, 400)
        self.assertIn('all 1 messages', res.data['error'])

    def verify_phone(self, user, phone):
        type(user).objects.filter(pk=user.pk).update(phone=phone, phone_verified_at=timezone.now())

    def test_sms_goes_to_verified_ghana_numbers_and_unused_texts_come_back(self):
        from unittest import mock

        from tools.models import Broadcast
        SubscriptionPlan.objects.filter(pk=self.plan.pk).update(broadcast_sms_monthly_limit=10)
        self.verify_phone(self.fan, '024 412 3456')
        self.verify_phone(self.customer, '+44 7700 900123')          # not Ghanaian: no SMS
        sizes = self.api.get('/api/v1/tools/broadcasts/').data['audience_sizes']
        self.assertEqual(sizes['everyone'], {'people': 2, 'sms': 1})

        client = mock.MagicMock()
        client.send_sms.return_value = {'status': 'success'}
        with mock.patch('userauths.tasks._get_sms_client', return_value=client):
            res = self.post(with_sms=True)
        self.assertEqual(res.status_code, 201, res.content)
        b = Broadcast.objects.get(pk=res.data['id'])
        self.assertEqual((b.status, b.sms_reserved, b.sms_sent, b.error), ('sent', 1, 1, ''))
        kwargs = client.send_sms.call_args.kwargs
        self.assertEqual(kwargs['recipients'], ['233244123456'])
        self.assertTrue(kwargs['message'].startswith(f'{self.vendor.name}: New arrivals.'))
        self.assertIn('Stop:', kwargs['message'])
        self.assertLessEqual(len(kwargs['message']), 306)
        self.assertEqual(ToolUsage.used(self.vendor, 'broadcast_sms'), 1)

    def test_sms_failure_keeps_the_message_and_returns_the_texts(self):
        from unittest import mock

        from tools.models import Broadcast
        SubscriptionPlan.objects.filter(pk=self.plan.pk).update(broadcast_sms_monthly_limit=10)
        self.verify_phone(self.fan, '0244123456')
        client = mock.MagicMock()
        client.send_sms.return_value = {'status': 'error', 'message': 'Insufficient balance'}
        with mock.patch('userauths.tasks._get_sms_client', return_value=client):
            res = self.post(with_sms=True)
        b = Broadcast.objects.get(pk=res.data['id'])
        self.assertEqual((b.status, b.recipients, b.sms_sent), ('sent', 2, 0))
        self.assertIn('Insufficient balance', b.error)
        self.assertEqual(ToolUsage.used(self.vendor, 'broadcast_sms'), 0)

    def test_sms_needs_plan_and_enough_texts(self):
        self.verify_phone(self.fan, '0244123456')
        res = self.post(with_sms=True)
        self.assertIn('does not include SMS', res.data['error'])
        SubscriptionPlan.objects.filter(pk=self.plan.pk).update(broadcast_sms_monthly_limit=1)
        ToolUsage.objects.create(vendor=self.vendor, tool='broadcast_sms', month=timezone.localdate().replace(day=1), count=1)
        res = self.post(with_sms=True)
        self.assertIn('you have 0 left', res.data['error'])
        self.assertEqual(ToolUsage.used(self.vendor, 'broadcasts'), 0, 'refused: no message used either')

    def test_preview_goes_to_the_seller_only_and_costs_nothing(self):
        from django.core import mail
        res = self.api.post('/api/v1/tools/broadcasts/', {'preview': True, 'subject': 'New arrivals',
                                                          'message': 'Fresh kettles just landed this week.'}, format='json')
        self.assertEqual(res.status_code, 200, res.content)
        self.assertTrue(res.data['emailed'])
        self.assertEqual([m.to for m in mail.outbox], [[self.owner.email]])
        self.assertTrue(mail.outbox[0].subject.startswith('[Preview]'))
        self.assertIn('Stop:', res.data['sms_text'])
        self.assertEqual(ToolUsage.used(self.vendor, 'broadcasts'), 0)
        self.assertIsNone(broadcasts_module().next_allowed(self.vendor), 'a preview does not start the 24h wait')

    def test_empty_audience_explains_why_and_team_is_left_out(self):
        self.vendor.followers.clear()
        self.vendor.followers.add(self.owner)                       # the seller following their own store
        Order.objects.filter(pk=self.order.pk).update(is_ordered=False)
        res = self.post(audience='followers')
        self.assertEqual(res.status_code, 400)
        self.assertIn('Your own team never gets your messages', res.data['error'])

    def test_stuck_messages_are_sent_or_closed(self):
        from datetime import timedelta as td
        from unittest import mock

        from tools.models import Broadcast
        queued = Broadcast.objects.create(vendor=self.vendor, audience='everyone', subject='Hello there',
                                          message='Fresh kettles just landed.')
        half = Broadcast.objects.create(vendor=self.vendor, audience='everyone', subject='Hello again',
                                        message='Fresh kettles just landed.', status='sending', sms_reserved=3)
        Broadcast.objects.filter(pk__in=[queued.pk, half.pk]).update(created_at=timezone.now() - td(hours=2))
        ToolUsage.objects.create(vendor=self.vendor, tool='broadcasts', month=timezone.localdate().replace(day=1), count=2)
        ToolUsage.objects.create(vendor=self.vendor, tool='broadcast_sms', month=timezone.localdate().replace(day=1), count=3)
        with mock.patch('tools.tasks.send_broadcast.delay') as send:
            result = broadcasts_module().requeue_stuck()
        send.assert_called_once_with(queued.pk)
        self.assertEqual(result, {'requeued': 1, 'stopped': 1})
        half.refresh_from_db()
        self.assertEqual(half.status, 'failed')
        self.assertEqual((ToolUsage.used(self.vendor, 'broadcasts'), ToolUsage.used(self.vendor, 'broadcast_sms')), (1, 0))


def broadcasts_module():
    from tools import broadcasts
    return broadcasts


class LabelTests(ToolsTestCase):

    def setUp(self):
        super().setUp()
        from address.models import Address
        self.order.address = Address.objects.create(user=self.customer, full_name='Ama Mensah', address='12 Ring Road',
                                                    town='Accra', region='Greater Accra', country='Ghana',
                                                    mobile='+233244000111')
        self.order.save(update_fields=['address'])
        other = Vendor.objects.create(name='Other Shop', user=make_user(6), email='os@example.com',
                                      contact='+233550006666', is_approved=True, status='VERIFIED')
        theirs = Product.objects.create(title='Not Our Toaster', sub_category=self.sub, vendor=other,
                                        status='published', price=Decimal('40'))
        OrderProduct.objects.create(order=self.order, product=theirs, quantity=1, price=Decimal('40'), amount=Decimal('40'))

    def test_orders_to_ship_and_documents(self):
        from tools import labels
        rows = self.api.get('/api/v1/tools/labels/orders/').data['results']
        self.assertEqual([(r['id'], r['units'], r['name']) for r in rows], [(self.order.pk, 2, 'Ama Mensah')])

        orders = labels.orders_for(self.vendor, [self.order.pk])
        self.assertEqual([line.product.title for line in orders[0][1]], ['Ops Kettle'], "only this store's items")
        self.assertEqual(labels.pick_rows(orders), [('Ops Kettle', '', '', 2, [labels.number(self.order)])])

        for document, size in (('labels', '4x6'), ('labels', 'a4'), ('packing_slips', ''), ('pick_list', '')):
            with self.subTest(document=document, size=size):
                res = self.api.post('/api/v1/tools/labels/', {'order_ids': [self.order.pk], 'document': document,
                                                              'size': size}, format='json')
                self.assertEqual(res.status_code, 200, getattr(res, 'data', None))
                self.assertEqual(res['Content-Type'], 'application/pdf')
                self.assertTrue(res.content.startswith(b'%PDF'))

    def test_refuses_other_stores_orders(self):
        other_order = Order.objects.create(user=self.customer, total=Decimal('5'), is_ordered=True, status='processing')
        res = self.api.post('/api/v1/tools/labels/', {'order_ids': [other_order.pk]}, format='json')
        self.assertEqual(res.status_code, 404)


class ChatToolTests(ToolsTestCase):

    def start(self, body='Hello, is this in stock?'):
        from chat import services
        with self.captureOnCommitCallbacks(execute=True):
            conversation, _ = services.start_conversation(self.customer, self.vendor, body)
        return conversation

    def test_away_message_once_and_unread_kept(self):
        from chat.models import Message
        res = self.api.put('/api/v1/tools/chat/away/', {'mode': 'always', 'message': 'We are on holiday until Monday.'},
                           format='json')
        self.assertEqual(res.status_code, 200, res.content)
        self.assertTrue(res.data['away_now'])

        conversation = self.start()
        auto = Message.objects.get(conversation=conversation, is_automatic=True)
        self.assertEqual(auto.body, 'We are on holiday until Monday.')
        conversation.refresh_from_db()
        self.assertEqual(conversation.vendor_unread, 1, "the store still has to read the shopper's message")

        from chat import services
        with self.captureOnCommitCallbacks(execute=True):
            services.send_message(conversation, self.customer, 'customer', 'Anyone there?')
        self.assertEqual(Message.objects.filter(conversation=conversation, is_automatic=True).count(), 1)

    def test_no_auto_reply_without_hours_or_plan(self):
        from chat.models import Message
        self.api.put('/api/v1/tools/chat/away/', {'mode': 'outside_hours', 'message': 'We reply from 8am to 6pm.'},
                     format='json')
        conversation = self.start()
        self.assertFalse(Message.objects.filter(conversation=conversation, is_automatic=True).exists(),
                         'no opening hours set: never "outside" them')
        self.api.put('/api/v1/tools/chat/away/', {'mode': 'always', 'message': 'Closed this week, back soon.'}, format='json')
        SubscriptionPlan.objects.filter(pk=self.plan.pk).update(can_use_chat_tools=False)
        from tools.chat_tools import auto_reply
        self.assertIsNone(auto_reply(conversation.pk))

    def test_saved_replies_and_stats(self):
        from chat import services
        from chat.models import Message
        bad = self.api.post('/api/v1/tools/chat/replies/', {'title': 'Call', 'body': 'Call 0244123456'}, format='json')
        self.assertEqual(bad.status_code, 400)
        res = self.api.post('/api/v1/tools/chat/replies/', {'title': 'Delivery', 'body': 'We deliver in 2-3 days.'},
                            format='json')
        self.assertEqual(res.status_code, 201, res.content)

        conversation = self.start()
        with self.captureOnCommitCallbacks(execute=True):
            reply, _ = services.send_message(conversation, self.owner, 'vendor', 'Yes, in stock!')
        first = Message.objects.filter(conversation=conversation).order_by('id').first()
        Message.objects.filter(pk=first.pk).update(created_at=reply.created_at - timedelta(minutes=30))
        stats = self.api.get('/api/v1/tools/chat/away/').data['stats']
        self.assertEqual((stats['answered'], stats['median_minutes'], stats['within_hour_percent']), (1, 30, 100))
        self.assertEqual(stats['waiting_now'], 0)


class StoreLinkTests(ToolsTestCase):

    def setUp(self):
        super().setUp()
        Product.objects.filter(pk=self.product.pk).update(status='published')

    def test_handle_rules_and_resolve(self):
        from rest_framework.test import APIClient
        from tools.models import StoreLink
        for bad, words in (('a', '3 to 30'), ('-bad-', '3 to 30'), ('admin', 'reserved'), ('my-negromart', 'reserved')):
            with self.subTest(handle=bad):
                res = self.api.put('/api/v1/tools/store/', {'handle': bad}, format='json')
                self.assertEqual(res.status_code, 400)
                self.assertIn(words, res.data['error'])
        res = self.api.put('/api/v1/tools/store/', {'handle': 'Ops-Shop'}, format='json')
        self.assertEqual(res.status_code, 200, res.content)
        self.assertTrue(res.data['short_url'].endswith('/s/ops-shop'))

        shopper = APIClient(HTTP_USER_AGENT='Mozilla/5.0 (Windows NT 10.0) Chrome/120', HTTP_X_VISITOR_ID='v-1')
        self.assertEqual(shopper.get('/api/v1/tools/s/ops-shop/').data['slug'], self.vendor.slug)
        shopper.get('/api/v1/tools/s/ops-shop/')                       # same visitor: counted once
        APIClient(HTTP_USER_AGENT='Googlebot/2.1').get('/api/v1/tools/s/ops-shop/')
        self.assertEqual(StoreLink.objects.get(vendor=self.vendor).visits, 1)
        self.assertEqual(APIClient().get('/api/v1/tools/s/nobody-here/').status_code, 404)

    def test_branding_on_the_public_store_page(self):
        bad = self.api.put('/api/v1/tools/store/', {'branding': {'announcement': 'WhatsApp us on 0244123456'}}, format='json')
        self.assertEqual(bad.status_code, 400)
        res = self.api.put('/api/v1/tools/store/', {'branding': {
            'accent_color': '#1E88E5', 'tagline': 'Kitchen things that last', 'announcement': 'Free delivery this week',
            'featured_product_ids': [self.product.pk]}}, format='json')
        self.assertEqual(res.status_code, 200, res.content)

        from rest_framework.test import APIClient
        page = APIClient().get(f'/api/v1/vendor/seller-detail/{self.vendor.slug}/').data
        self.assertEqual(page['branding']['tagline'], 'Kitchen things that last')
        self.assertEqual(page['branding']['featured'][0]['product']['id'], self.product.pk)

        SubscriptionPlan.objects.filter(pk=self.plan.pk).update(can_use_store_link=False)
        from payments.entitlements import forget_plan
        forget_plan(self.vendor)
        page = APIClient().get(f'/api/v1/vendor/seller-detail/{self.vendor.slug}/').data
        self.assertIsNone(page['branding'], 'plan lapsed: plain store page')


class SellerApiTests(ToolsTestCase):

    def setUp(self):
        super().setUp()
        Product.objects.filter(pk=self.product.pk).update(seller_sku='KET-1', total_quantity=4)
        OrderProduct.objects.create(order=self.order, product=self.product, quantity=1, price=self.product.price,
                                    amount=self.product.price)
        Order.objects.filter(pk=self.order.pk).update(is_ordered=True)
        self.order.vendors.add(self.vendor)

    def key(self, can_write=True):
        from rest_framework.test import APIClient
        res = self.api.post('/api/v1/tools/api-setup/', {'name': 'Warehouse sheet', 'can_write': can_write}, format='json')
        self.assertEqual(res.status_code, 201, res.content)
        self.assertTrue(res.data['key'].startswith('nm_'))
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION='Bearer ' + res.data['key'])
        return client, res.data

    def test_key_reads_products_and_orders_and_syncs_stock(self):
        from payments.entitlements import forget_plan
        client, made = self.key()
        rows = client.get('/api/v1/tools/api/products/').data['results']
        self.assertIn(('KET-1', 4), [(r['seller_sku'], r['stock']) for r in rows])
        order = client.get('/api/v1/tools/api/orders/').data['results'][0]
        self.assertEqual(order['id'], self.order.pk)
        self.assertEqual({line['seller_sku'] for line in order['lines']}, {'KET-1'})

        res = client.post('/api/v1/tools/api/stock/', {'items': [
            {'seller_sku': 'KET-1', 'stock': 9, 'price': '61.00'}, {'seller_sku': 'NOPE', 'stock': 1}, {'stock': 2},
        ]}, format='json')
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(res.data['updated'], 1)
        self.assertEqual([e['index'] for e in res.data['errors']], [1, 2])
        self.product.refresh_from_db()
        self.assertEqual((self.product.total_quantity, self.product.price), (9, Decimal('61.00')))
        self.assertTrue(PriceChange.objects.filter(product=self.product, source=PriceChange.API).exists())

        # The key works nowhere else, and stops with the plan or when revoked.
        self.assertIn(client.get('/api/v1/tools/').status_code, (401, 403))
        SubscriptionPlan.objects.filter(pk=self.plan.pk).update(can_use_api=False)
        forget_plan(self.vendor)
        self.assertEqual(client.get('/api/v1/tools/api/products/').status_code, 403)
        SubscriptionPlan.objects.filter(pk=self.plan.pk).update(can_use_api=True)
        forget_plan(self.vendor)
        self.api.delete('/api/v1/tools/api-setup/keys/%d/' % made['id'])
        self.assertEqual(client.get('/api/v1/tools/api/products/').status_code, 401)

    def test_read_only_key_cannot_write(self):
        client, _ = self.key(can_write=False)
        self.assertEqual(client.get('/api/v1/tools/api/products/').status_code, 200)
        res = client.post('/api/v1/tools/api/stock/', {'items': [{'seller_sku': 'KET-1', 'stock': 1}]}, format='json')
        self.assertEqual(res.status_code, 403)

    def test_webhooks_signed_retried_and_private_addresses_refused(self):
        import json
        from unittest import mock

        from tools import webhooks
        from tools.models import Webhook, WebhookDelivery

        bad = self.api.post('/api/v1/tools/api-setup/webhooks/',
                            {'url': 'http://example.com/hook', 'events': ['order.created']}, format='json')
        self.assertEqual(bad.status_code, 400)
        with mock.patch('tools.webhooks.socket.getaddrinfo', return_value=[(2, 1, 6, '', ('10.0.0.5', 443))]):
            private = self.api.post('/api/v1/tools/api-setup/webhooks/',
                                    {'url': 'https://intranet.example/hook', 'events': ['order.created']}, format='json')
        self.assertEqual(private.status_code, 400)
        self.assertIn('private', private.data['error'])

        public = [(2, 1, 6, '', ('93.184.216.34', 443))]
        with mock.patch('tools.webhooks.socket.getaddrinfo', return_value=public):
            res = self.api.post('/api/v1/tools/api-setup/webhooks/',
                                {'url': 'https://shop.example/hook', 'events': ['order.created', 'order.updated']},
                                format='json')
        self.assertEqual(res.status_code, 201, res.content)
        hook = Webhook.objects.get(pk=res.data['id'])

        sent = []

        def fake_post(url, data, **kwargs):
            sent.append((data, kwargs['headers']))
            response = mock.MagicMock(status_code=500 if len(sent) == 1 else 200)
            response.__enter__.return_value = response
            return response

        with mock.patch('tools.webhooks.socket.getaddrinfo', return_value=public), \
                mock.patch('tools.webhooks.requests.post', side_effect=fake_post), \
                mock.patch('tools.tasks.deliver_webhook.delay') as queued, \
                mock.patch('tools.tasks.deliver_webhook.apply_async') as retry:
            with self.captureOnCommitCallbacks(execute=True):
                webhooks.order_event_later(self.order.pk, 'order.created')
            delivery = WebhookDelivery.objects.get(webhook=hook)
            queued.assert_called_once_with(delivery.pk)
            from tools.tasks import deliver_webhook
            deliver_webhook.run(delivery.pk)                    # first try: answered 500
            delivery.refresh_from_db()
            self.assertEqual((delivery.status, delivery.attempts, delivery.response_code), ('pending', 1, 500))
            self.assertEqual(retry.call_args.kwargs['countdown'], webhooks.RETRY_DELAYS[0])
            deliver_webhook.run(delivery.pk)                    # the retry

        delivery.refresh_from_db()
        self.assertEqual(delivery.status, 'sent')
        body, headers = sent[-1]
        self.assertEqual(headers['X-Negromart-Event'], 'order.created')
        t = headers['X-Negromart-Signature'].split(',')[0][2:]
        self.assertEqual(headers['X-Negromart-Signature'], webhooks.sign(hook.secret, body, t))
        self.assertEqual({line['seller_sku'] for line in json.loads(body)['data']['lines']}, {'KET-1'})

    def test_status_change_queues_order_updated(self):
        from unittest import mock

        from tools.models import Webhook, WebhookDelivery
        hook = Webhook.objects.create(vendor=self.vendor, url='https://shop.example/hook', secret='whsec_x',
                                      events=['order.updated'])
        order = Order.objects.get(pk=self.order.pk)
        order.status = 'shipped'
        with mock.patch('tools.tasks.deliver_webhook.delay'), self.captureOnCommitCallbacks(execute=True):
            order.save()
        self.assertEqual(WebhookDelivery.objects.filter(webhook=hook, event='order.updated').count(), 1)
        with mock.patch('tools.tasks.deliver_webhook.delay'), self.captureOnCommitCallbacks(execute=True):
            order.save()                                        # nothing changed: no new event
        self.assertEqual(WebhookDelivery.objects.filter(webhook=hook).count(), 1)


class EmptyStoreTests(ToolsTestCase):
    """A brand-new store (no products, orders, followers or settings) opens every tool page without errors."""

    PAGES = ['', 'products/', 'products/?all=1', 'bulk-edit/', 'bulk-edit/csv/', 'stock/', 'demand/', 'broadcasts/',
             'store/', 'api-setup/', 'chat/replies/', 'chat/away/', 'labels/orders/', 'promotions/',
             'listing-doctor/', 'profit/', 'pricing/', 'pricing/rules/', 'pricing/changes/']

    def test_every_tool_page_loads_for_a_new_store(self):
        from payments.models import VendorSubscription
        owner = make_user(30)
        store = Vendor.objects.create(name='Brand New Shop', user=owner, email='new-shop@example.com',
                                      contact='+233550003030', is_approved=True, status='VERIFIED')
        VendorSubscription.objects.create(vendor=store, plan=self.plan, status='active',
                                          end_date=timezone.now() + timedelta(days=30))
        api = vendor_client(owner)
        for page in self.PAGES:
            with self.subTest(page=page):
                res = api.get(f'/api/v1/tools/{page}')
                self.assertEqual(res.status_code, 200, getattr(res, 'data', res.content[:300]))
        res = api.post('/api/v1/tools/broadcasts/', {'audience': 'everyone', 'subject': 'Hello there',
                                                     'message': 'Our very first message.'}, format='json')
        self.assertEqual(res.status_code, 400)
        self.assertIn('Nobody in this audience yet', res.data['error'])
