"""
Abandoned-cart reminders.

Run with:  DB_HOST=localhost python manage.py test engagement.tests_cart_reminders --keepdb
"""
from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.core import mail
from django.test import TestCase, override_settings
from django.utils import timezone

from ecommerce.celery import app as celery_app
from engagement.cart_reminders import attribute_orders, sweep
from engagement.models import CartReminder, EmailOptOut
from notification.models import Notification
from order.models import Cart, CartItem, Order
from product.models import Brand, Category, Main_Category, Product, Sub_Category
from userauths.models import User
from vendor.models import Vendor

celery_app.conf.task_always_eager = True

TEST_SETTINGS = {
    'CACHES': {'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}},
    'EMAIL_BACKEND': 'django.core.mail.backends.locmem.EmailBackend',
    'CHANNEL_LAYERS': {'default': {'BACKEND': 'channels.layers.InMemoryChannelLayer'}},
}


def make_user(n):
    user = User.objects.create_user(f'Cart{n}', 'Test', f'cart{n}@example.com', f'+23324400{n:04d}', 'Str0ng!Passw0rd')
    User.objects.filter(pk=user.pk).update(is_active=True, email_verified_at=timezone.now())
    return User.objects.get(pk=user.pk)


@override_settings(**TEST_SETTINGS)
class CartReminderTests(TestCase):

    def setUp(self):
        from django.core.cache import cache
        cache.clear()
        cache.set('exchange_rates', {'GHS': 1, 'USD': 0.1}, None)
        for target in ('vendor.models.send_vendor_approval_email.delay', 'vendor.models.send_vendor_sms.delay'):
            p = mock.patch(target)
            p.start()
            self.addCleanup(p.stop)
        owner = make_user(1)
        vendor = Vendor.objects.create(name='Cart Shop', user=owner, email='cart-shop@example.com',
                                       contact='+233550006666', is_approved=True, status='VERIFIED')
        main = Main_Category.objects.create(title='Cart Main')
        sub = Sub_Category.objects.create(title='Cart Sub', category=Category.objects.create(title='Cart Cat', main_category=main))
        self.product = Product.objects.create(title='Cart Lamp', sub_category=sub, vendor=vendor, status='published',
                                              price=Decimal('60'), total_quantity=5, brand=Brand.objects.create(title='Cart Brand'))
        self.shopper = make_user(2)
        self.cart = Cart.objects.create(user=self.shopper)
        CartItem.objects.create(cart=self.cart, product=self.product, quantity=2)

    def idle(self, delta):
        Cart.objects.filter(pk=self.cart.pk).update(updated_at=timezone.now() - delta)

    def test_line_changes_touch_the_cart(self):
        self.idle(timedelta(hours=5))
        CartItem.objects.filter(cart=self.cart).first().save()
        self.cart.refresh_from_db()
        self.assertLess(timezone.now() - self.cart.updated_at, timedelta(minutes=1))

    def test_not_yet_idle(self):
        self.idle(timedelta(minutes=20))
        self.assertEqual(sweep()['stage1'], 0)

    def test_first_then_second_reminder(self):
        self.idle(timedelta(hours=2))
        counts = sweep()
        self.assertEqual(counts['stage1'], 1)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn('Cart Lamp', mail.outbox[0].subject)
        self.assertIn('GHS 120.00', mail.outbox[0].alternatives[0][0])
        self.assertTrue(Notification.objects.filter(recipient=self.shopper, verb='customer_cart_reminder').exists())
        self.assertEqual(sweep()['stage1'], 0, "once per stage")

        # A day later: second reminder (after the min gap).
        CartReminder.objects.update(sent_at=timezone.now() - timedelta(hours=23))
        self.idle(timedelta(hours=25))
        self.assertEqual(sweep()['stage2'], 1)
        self.assertEqual(len(mail.outbox), 2)
        CartReminder.objects.update(sent_at=timezone.now() - timedelta(days=2))
        self.assertEqual(sweep()['stage1'] + sweep()['stage2'], 0, "never a third")

    def test_changed_cart_starts_again(self):
        self.idle(timedelta(hours=2))
        sweep()
        CartReminder.objects.update(sent_at=timezone.now() - timedelta(days=1))
        CartItem.objects.create(cart=self.cart, product=self.product, quantity=1)
        self.idle(timedelta(hours=2))
        self.assertEqual(sweep()['stage1'], 1)

    def test_skips_if_ordered_since(self):
        self.idle(timedelta(hours=2))
        Order.objects.create(user=self.shopper, total=Decimal('120'), is_ordered=True)
        self.assertEqual(sweep()['stage1'], 0)

    def test_skips_when_nothing_buyable(self):
        Product.objects.filter(pk=self.product.pk).update(total_quantity=0)
        self.idle(timedelta(hours=2))
        self.assertEqual(sweep()['stage1'], 0)

    def test_opt_out_keeps_in_app_only(self):
        EmailOptOut.objects.create(user=self.shopper, category=EmailOptOut.CART)
        self.idle(timedelta(hours=2))
        self.assertEqual(sweep()['stage1'], 1)
        self.assertEqual(len(mail.outbox), 0)
        self.assertFalse(CartReminder.objects.get().emailed)

    def test_guest_carts_ignored(self):
        guest = Cart.objects.create()
        CartItem.objects.create(cart=guest, product=self.product, quantity=1)
        Cart.objects.filter(pk=guest.pk).update(updated_at=timezone.now() - timedelta(hours=2))
        self.idle(timedelta(minutes=5))
        self.assertEqual(sweep()['carts'], 0)

    def test_order_after_reminder_is_attributed(self):
        self.idle(timedelta(hours=2))
        sweep()
        order = Order.objects.create(user=self.shopper, total=Decimal('120'), is_ordered=True)
        self.assertEqual(attribute_orders(), 1)
        self.assertEqual(CartReminder.objects.get().order, order)
        self.assertEqual(attribute_orders(), 0)
