"""
Price-drop and back-in-stock alerts.

Run with:  DB_HOST=localhost python manage.py test engagement.tests_alerts --keepdb
"""
from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.core import mail
from django.db.models import F
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from ecommerce.celery import app as celery_app
from engagement.alerts import sweep
from engagement.emails import opt_out_token
from engagement.models import EmailOptOut, ProductAlert
from notification.models import Notification
from product.models import Brand, Category, FlashSale, Main_Category, Product, Sub_Category, Variants, Wishlist
from userauths.models import User
from vendor.models import Vendor

celery_app.conf.task_always_eager = True

TEST_SETTINGS = {
    'CACHES': {'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}},
    'EMAIL_BACKEND': 'django.core.mail.backends.locmem.EmailBackend',
    'CHANNEL_LAYERS': {'default': {'BACKEND': 'channels.layers.InMemoryChannelLayer'}},
}
PASSWORD = 'Str0ng!Passw0rd'


def make_user(n):
    user = User.objects.create_user(f'Alert{n}', 'Test', f'alert{n}@example.com', f'+23324200{n:04d}', PASSWORD)
    User.objects.filter(pk=user.pk).update(is_active=True, email_verified_at=timezone.now())
    return User.objects.get(pk=user.pk)


@override_settings(**TEST_SETTINGS)
class AlertTestCase(TestCase):

    def setUp(self):
        from django.core.cache import cache
        cache.clear()
        cache.set('exchange_rates', {'GHS': 1, 'USD': 0.1}, None)
        for target in ('vendor.models.send_vendor_approval_email.delay', 'vendor.models.send_vendor_sms.delay'):
            p = mock.patch(target)
            p.start()
            self.addCleanup(p.stop)
        owner = make_user(1)
        self.vendor = Vendor.objects.create(name='Alert Shop', user=owner, email='alert-shop@example.com',
                                            contact='+233550004444', is_approved=True, status='VERIFIED')
        main = Main_Category.objects.create(title='Alert Main')
        cat = Category.objects.create(title='Alert Cat', main_category=main)
        sub = Sub_Category.objects.create(title='Alert Sub', category=cat)
        self.product = Product.objects.create(
            title='Alert Kettle', sub_category=sub, vendor=self.vendor, status='published',
            price=Decimal('100.00'), total_quantity=10, brand=Brand.objects.create(title='Alert Brand'),
        )
        self.shopper = make_user(2)
        self.client = APIClient()
        self.client.force_authenticate(self.shopper)

    def watch(self, kind='price', **extra):
        res = self.client.post('/api/v1/engagement/alerts/', {'product': self.product.pk, 'kind': kind, **extra},
                               format='json')
        self.assertEqual(res.status_code, 201, res.content)
        return ProductAlert.objects.get(pk=res.data['id'])

    def set_price(self, price):
        Product.objects.filter(pk=self.product.pk).update(price=Decimal(price))


class PriceDropTests(AlertTestCase):

    def test_drop_of_five_percent_notifies_once(self):
        self.watch()
        self.set_price('94.00')
        counts = sweep()
        self.assertEqual(counts['price'], 1)
        self.assertEqual(Notification.objects.filter(recipient=self.shopper, verb='customer_price_drop').count(), 1)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn('Price drop', mail.outbox[0].subject)
        self.assertIn('List-Unsubscribe', mail.outbox[0].extra_headers)
        # Nothing new next time.
        self.assertEqual(sweep()['price'], 0)

    def test_small_drop_is_ignored(self):
        self.watch()
        self.set_price('97.00')
        self.assertEqual(sweep()['price'], 0)

    def test_new_low_after_cooldown_notifies_again(self):
        alert = self.watch()
        self.set_price('90.00')
        sweep()
        ProductAlert.objects.filter(pk=alert.pk).update(last_sent_at=timezone.now() - timedelta(days=3))
        self.set_price('80.00')
        self.assertEqual(sweep()['price'], 1)

    def test_bounce_back_to_old_low_is_not_news(self):
        alert = self.watch()
        self.set_price('90.00')
        sweep()
        ProductAlert.objects.filter(pk=alert.pk).update(last_sent_at=timezone.now() - timedelta(days=3))
        self.set_price('110.00')
        sweep()
        self.set_price('90.00')
        self.assertEqual(sweep()['price'], 0)

    def test_target_price(self):
        self.watch(target_price='80.00')
        self.set_price('85.00')
        self.assertEqual(sweep()['price'], 0, "above target: wait")
        self.set_price('79.00')
        self.assertEqual(sweep()['price'], 1)

    def test_flash_sale_counts_as_a_drop(self):
        self.watch()
        now = timezone.now()
        FlashSale.objects.create(product=self.product, sale_price=Decimal('70.00'), original_price=Decimal('100.00'),
                                 start_time=now - timedelta(minutes=1), end_time=now + timedelta(hours=2))
        self.assertEqual(sweep()['price'], 1)

    def test_opt_out_stops_email_not_in_app(self):
        self.watch()
        EmailOptOut.objects.create(user=self.shopper, category=EmailOptOut.ALERTS)
        self.set_price('50.00')
        self.assertEqual(sweep()['price'], 1)
        self.assertEqual(len(mail.outbox), 0)
        self.assertTrue(Notification.objects.filter(recipient=self.shopper, verb='customer_price_drop').exists())

    def test_one_email_per_shopper_per_sweep(self):
        other = Product.objects.create(title='Alert Toaster', sub_category=self.product.sub_category,
                                       vendor=self.vendor, status='published', price=Decimal('50.00'),
                                       total_quantity=3, brand=self.product.brand)
        self.watch()
        self.client.post('/api/v1/engagement/alerts/', {'product': other.pk, 'kind': 'price'}, format='json')
        self.set_price('50.00')
        Product.objects.filter(pk=other.pk).update(price=Decimal('25.00'))
        self.assertEqual(sweep()['price'], 2)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn('watching', mail.outbox[0].subject)


class BackInStockTests(AlertTestCase):

    def test_fires_once_when_restocked(self):
        Product.objects.filter(pk=self.product.pk).update(total_quantity=0)
        alert = self.watch('stock')
        self.assertFalse(alert.last_seen_in_stock)
        self.assertEqual(sweep()['stock'], 0)
        # Restocked through an F() update (no signals), like returns do.
        Product.objects.filter(pk=self.product.pk).update(total_quantity=F('total_quantity') + 4)
        self.assertEqual(sweep()['stock'], 1)
        alert.refresh_from_db()
        self.assertFalse(alert.is_active)
        self.assertEqual(sweep()['stock'], 0)

    def test_variant_alert_watches_only_that_variant(self):
        Product.objects.filter(pk=self.product.pk).update(variant='Size')
        red = Variants.objects.create(product=self.product, title='Red', price=Decimal('100'), quantity=0)
        blue = Variants.objects.create(product=self.product, title='Blue', price=Decimal('100'), quantity=0)
        self.watch('stock', variant=red.pk)
        Variants.objects.filter(pk=blue.pk).update(quantity=5)
        self.assertEqual(sweep()['stock'], 0)
        Variants.objects.filter(pk=red.pk).update(quantity=1)
        self.assertEqual(sweep()['stock'], 1)

    def test_paused_shop_is_not_in_stock(self):
        Product.objects.filter(pk=self.product.pk).update(total_quantity=0)
        self.watch('stock')
        Vendor.objects.filter(pk=self.vendor.pk).update(shop_paused=True)
        Product.objects.filter(pk=self.product.pk).update(total_quantity=5)
        self.assertEqual(sweep()['stock'], 0)
        Vendor.objects.filter(pk=self.vendor.pk).update(shop_paused=False)
        self.assertEqual(sweep()['stock'], 1)


class WishlistAndApiTests(AlertTestCase):

    def test_wishlist_watches_price_and_unwishlist_stops(self):
        with self.captureOnCommitCallbacks(execute=True):
            item = Wishlist.objects.create(user=self.shopper, product=self.product)
        alert = ProductAlert.objects.get(user=self.shopper, product=self.product)
        self.assertEqual(alert.source, ProductAlert.WISHLIST)
        self.assertEqual(alert.reference_price, Decimal('100.00'))
        item.delete()
        self.assertFalse(ProductAlert.objects.filter(pk=alert.pk).exists())

    def test_manual_alert_survives_unwishlist(self):
        self.watch()
        with self.captureOnCommitCallbacks(execute=True):
            item = Wishlist.objects.create(user=self.shopper, product=self.product)
        item.delete()
        self.assertTrue(ProductAlert.objects.filter(user=self.shopper, product=self.product).exists())

    def test_list_and_delete(self):
        alert = self.watch()
        res = self.client.get('/api/v1/engagement/alerts/', {'product': self.product.pk})
        self.assertEqual(res.data['results'][0]['current_price'], Decimal('100.00'))
        other = APIClient()
        other.force_authenticate(make_user(3))
        self.assertEqual(other.delete(f'/api/v1/engagement/alerts/{alert.pk}/').status_code, 404)
        self.assertEqual(self.client.delete(f'/api/v1/engagement/alerts/{alert.pk}/').status_code, 204)

    def test_rejects_variant_of_other_product(self):
        other = Product.objects.create(title='Alert Other', sub_category=self.product.sub_category,
                                       vendor=self.vendor, status='published', price=Decimal('5'),
                                       brand=self.product.brand)
        v = Variants.objects.create(product=other, title='X', price=Decimal('5'), quantity=1)
        res = self.client.post('/api/v1/engagement/alerts/', {'product': self.product.pk, 'kind': 'stock',
                                                              'variant': v.pk}, format='json')
        self.assertEqual(res.status_code, 400)

    def test_unsubscribe_link(self):
        token = opt_out_token(self.shopper.pk, EmailOptOut.ALERTS)
        anon = APIClient()
        self.assertEqual(anon.get(f'/api/v1/engagement/unsubscribe/{token}/').status_code, 200)
        self.assertEqual(anon.post(f'/api/v1/engagement/unsubscribe/{token}/').status_code, 200)
        self.assertTrue(EmailOptOut.objects.filter(user=self.shopper, category='alerts').exists())
        self.assertEqual(anon.post('/api/v1/engagement/unsubscribe/forged/').status_code, 400)

    def test_email_preferences(self):
        res = self.client.put('/api/v1/engagement/email-preferences/', {'alerts': False}, format='json')
        self.assertEqual(res.data, {'alerts': False, 'cart': True})
        res = self.client.put('/api/v1/engagement/email-preferences/', {'alerts': True}, format='json')
        self.assertEqual(res.data['alerts'], True)
