"""
product/tests_review_reminders.py
"How was your purchase?" reminders: timing, eligibility, batching,
idempotency, follow-up, opt-out, channels, failures and conversion tracking.

Run with:  DB_HOST=localhost python manage.py test product.tests_review_reminders --keepdb
"""

from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.core import mail
from django.test import TestCase, override_settings
from django.utils import timezone

from notification.models import Notification
from order.models import ReturnRequest
from product import review_reminders as reminders
from product.models import Product, ProductReview, ReviewReminder, ReviewReminderOptOut
from product.tests_reviews import GOOD_TEXT, TEST_SETTINGS, ReviewFixtures, client_for

REMINDER_SETTINGS = {
    **TEST_SETTINGS,
    'SITE_URL': 'https://www.negromart.test',
    'REVIEW_REMINDERS': {'FIRST_AFTER_DAYS': 7, 'FOLLOW_UP_AFTER_DAYS': 21, 'MAX_AGE_DAYS': 45,
                         'MIN_DAYS_BETWEEN': 4, 'MAX_ITEMS_PER_MESSAGE': 2, 'SMS_ENABLED': False},
}


def days_ago(n):
    return timezone.now() - timedelta(days=n)


@override_settings(**REMINDER_SETTINGS)
class ReviewReminderTests(ReviewFixtures, TestCase):

    def setUp(self):
        self.make_world()
        self.line.delivered_date = days_ago(8)
        self.line.save(update_fields=['delivered_date'])

    def run_reminders(self):
        with self.captureOnCommitCallbacks(execute=True):
            return reminders.queue_due_reminders()

    def delivered(self, days, product=None, user=None):
        line = self.buy(user or self.buyer, delivered=True, product=product)
        line.delivered_date = days_ago(days)
        line.save(update_fields=['delivered_date'])
        return line

    def another_product(self, title):
        return Product.objects.create(title=title, sub_category=self.sub, vendor=self.vendor,
                                      status='published', price=Decimal('120.00'))

    # ── timing and eligibility ──

    def test_due_purchase_gets_one_email_with_rating_links(self):
        self.assertEqual(self.run_reminders(), 1)
        self.assertEqual(len(mail.outbox), 1)
        msg = mail.outbox[0]
        self.assertEqual(msg.to, [self.buyer.email])
        self.assertIn('Review Sneaker', msg.subject)
        html = msg.alternatives[0][0]
        base = f'https://www.negromart.test/{self.product.sku}/{self.product.slug}?review=1'
        for stars in range(1, 6):
            self.assertIn(f'{base}&amp;rating={stars}&amp;utm_source=review_reminder', html)
        self.assertIn('/reviews/reminders/unsubscribe/', msg.extra_headers['List-Unsubscribe'])
        self.assertIn('Rate it:', msg.body)  # plain-text part is the hand-written one

        reminder = ReviewReminder.objects.get()
        self.assertEqual((reminder.status, reminder.stage, reminder.order_item_id),
                         (ReviewReminder.SENT, ReviewReminder.FIRST, self.line.pk))
        self.assertEqual(reminder.channels, ['email', 'in_app'])
        note = Notification.objects.get(recipient=self.buyer, verb='customer_review_reminder')
        self.assertEqual(note.data['url'], f'/{self.product.sku}/{self.product.slug}?review=1')

    def test_too_soon_after_delivery(self):
        self.line.delivered_date = days_ago(3)
        self.line.save(update_fields=['delivered_date'])
        self.assertEqual(self.run_reminders(), 0)
        self.assertEqual(len(mail.outbox), 0)

    def test_old_purchases_are_never_dug_up(self):
        self.line.delivered_date = days_ago(60)
        self.line.save(update_fields=['delivered_date'])
        self.assertEqual(self.run_reminders(), 0)

    def test_not_delivered_is_not_reminded(self):
        self.line.delivered_date = None
        self.line.status = 'shipped'
        self.line.save(update_fields=['delivered_date', 'status'])
        self.assertEqual(self.run_reminders(), 0)

    def test_already_reviewed_any_status_is_not_reminded(self):
        ProductReview.objects.create(user=self.buyer, product=self.product, rating=2, review=GOOD_TEXT,
                                     moderation_status=ProductReview.PENDING)
        self.assertEqual(self.run_reminders(), 0)

    def test_cancelled_or_returned_lines_are_not_reminded(self):
        self.line.status = 'canceled'
        self.line.save(update_fields=['status'])
        self.assertEqual(self.run_reminders(), 0)

        self.line.status = 'delivered'
        self.line.save(update_fields=['status'])
        ReturnRequest.objects.create(order_product=self.line, order=self.line.order, vendor=self.vendor,
                                     customer=self.buyer, status=ReturnRequest.STATUS_REFUNDED,
                                     refund_amount=Decimal('300.00'))
        self.assertEqual(self.run_reminders(), 0)

    def test_unpublished_product_and_inactive_account_are_skipped(self):
        Product.objects.filter(pk=self.product.pk).update(status='disabled')
        self.assertEqual(self.run_reminders(), 0)
        Product.objects.filter(pk=self.product.pk).update(status='published')
        type(self.buyer).objects.filter(pk=self.buyer.pk).update(is_active=False)
        self.assertEqual(self.run_reminders(), 0)

    # ── idempotency, batching, pacing ──

    def test_rerun_never_sends_twice(self):
        self.run_reminders()
        self.run_reminders()
        with self.captureOnCommitCallbacks(execute=True):
            reminders.queue_due_reminders(now=timezone.now() + timedelta(days=5))  # past the rest period
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(ReviewReminder.objects.count(), 1)

    def test_several_products_share_one_message_up_to_the_cap(self):
        for title in ('Canvas Tote', 'Leather Belt'):
            self.delivered(9, product=self.another_product(title))
        self.run_reminders()
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(ReviewReminder.objects.filter(status=ReviewReminder.SENT).count(), 2)  # cap is 2
        self.assertIn('recent purchases', mail.outbox[0].subject)

        # The third product goes out after the rest period, not the next day.
        with self.captureOnCommitCallbacks(execute=True):
            reminders.queue_due_reminders(now=timezone.now() + timedelta(days=1))
        self.assertEqual(len(mail.outbox), 1)
        with self.captureOnCommitCallbacks(execute=True):
            reminders.queue_due_reminders(now=timezone.now() + timedelta(days=5))
        self.assertEqual(len(mail.outbox), 2)
        self.assertEqual(ReviewReminder.objects.count(), 3)

    def test_bought_twice_is_one_reminder_for_the_latest_delivery(self):
        latest = self.delivered(7.5)
        self.run_reminders()
        self.assertEqual(ReviewReminder.objects.get().order_item_id, latest.pk)

    def test_customers_are_reminded_independently(self):
        from product.tests_reviews import make_user
        other = make_user(3)
        self.delivered(10, user=other)
        self.assertEqual(self.run_reminders(), 2)
        self.assertEqual(sorted(m.to[0] for m in mail.outbox), sorted([self.buyer.email, other.email]))

    # ── follow-up ──

    def test_one_follow_up_then_silence(self):
        self.line.delivered_date = days_ago(22)
        self.line.save(update_fields=['delivered_date'])
        first = ReviewReminder.objects.create(user=self.buyer, product=self.product, order_item=self.line,
                                              status=ReviewReminder.SENT, sent_at=days_ago(15))
        ReviewReminder.objects.filter(pk=first.pk).update(created_at=days_ago(15))

        self.run_reminders()
        self.assertEqual(len(mail.outbox), 1)
        self.assertTrue(ReviewReminder.objects.filter(stage=ReviewReminder.FOLLOW_UP,
                                                      status=ReviewReminder.SENT).exists())
        self.assertIn('Still have a minute', mail.outbox[0].subject)

        with self.captureOnCommitCallbacks(execute=True):
            reminders.queue_due_reminders(now=timezone.now() + timedelta(days=10))
        self.assertEqual(len(mail.outbox), 1)

    def test_follow_up_disabled(self):
        self.line.delivered_date = days_ago(22)
        self.line.save(update_fields=['delivered_date'])
        ReviewReminder.objects.create(user=self.buyer, product=self.product, status=ReviewReminder.SENT)
        ReviewReminder.objects.update(created_at=days_ago(15))
        with override_settings(REVIEW_REMINDERS={**REMINDER_SETTINGS['REVIEW_REMINDERS'],
                                                 'FOLLOW_UP_AFTER_DAYS': 0}):
            self.assertEqual(self.run_reminders(), 0)

    # ── opt-out ──

    def test_email_link_opts_out_without_sign_in(self):
        token = reminders.opt_out_token(self.buyer)
        res = client_for().post('/api/v1/product/reviews/reminders/unsubscribe/', {'token': token}, format='json')
        self.assertEqual(res.status_code, 200)
        self.assertTrue(ReviewReminderOptOut.objects.filter(user=self.buyer).exists())
        self.assertEqual(self.run_reminders(), 0)

    def test_tampered_token_is_rejected(self):
        token = reminders.opt_out_token(self.buyer)[:-2] + 'xx'
        res = client_for().post('/api/v1/product/reviews/reminders/unsubscribe/', {'token': token}, format='json')
        self.assertEqual(res.status_code, 400)
        self.assertFalse(ReviewReminderOptOut.objects.exists())

    def test_customer_can_switch_reminders_off_and_on(self):
        client = client_for(self.buyer)
        url = '/api/v1/product/reviews/reminders/'
        self.assertEqual(client.get(url).data, {'opted_out': False})
        self.assertEqual(client.put(url, {'opted_out': True}, format='json').data, {'opted_out': True})
        self.assertEqual(self.run_reminders(), 0)
        self.assertEqual(client.put(url, {'opted_out': 'yes'}, format='json').status_code, 400)
        client.put(url, {'opted_out': False}, format='json')
        self.assertEqual(self.run_reminders(), 1)
        self.assertEqual(client_for().get(url).status_code, 401)

    # ── sending ──

    def test_rechecked_at_send_time(self):
        reminders.queue_due_reminders()   # on_commit not run: still queued
        ids = list(ReviewReminder.objects.values_list('pk', flat=True))
        ProductReview.objects.create(user=self.buyer, product=self.product, rating=5, review=GOOD_TEXT)
        self.assertEqual(reminders.deliver(ids), [])
        self.assertEqual(ReviewReminder.objects.get().status, ReviewReminder.SKIPPED)
        self.assertEqual(len(mail.outbox), 0)

    def test_email_failure_is_recorded_not_resent(self):
        with mock.patch('django.core.mail.EmailMultiAlternatives.send', side_effect=OSError('smtp down')):
            self.run_reminders()
        reminder = ReviewReminder.objects.get()
        self.assertEqual(reminder.status, ReviewReminder.FAILED)
        self.assertIn('smtp down', reminder.error)
        self.assertFalse(Notification.objects.filter(verb='customer_review_reminder').exists())

    def test_sms_only_when_enabled_verified_and_first_reminder(self):
        type(self.buyer).objects.filter(pk=self.buyer.pk).update(phone_verified_at=timezone.now())
        conf = {**REMINDER_SETTINGS['REVIEW_REMINDERS'], 'SMS_ENABLED': True}
        with override_settings(REVIEW_REMINDERS=conf), \
                mock.patch('userauths.arkesel_client.ArkeselSMS.send_sms',
                           return_value={'status': 'success'}) as send_sms:
            self.run_reminders()
        send_sms.assert_called_once()
        self.assertIn('?review=1', send_sms.call_args.kwargs['message'])
        self.assertIn('utm_medium=sms', send_sms.call_args.kwargs['message'])
        self.assertEqual(ReviewReminder.objects.get().channels, ['email', 'sms', 'in_app'])

    def test_sms_failure_does_not_block_email(self):
        type(self.buyer).objects.filter(pk=self.buyer.pk).update(phone_verified_at=timezone.now())
        conf = {**REMINDER_SETTINGS['REVIEW_REMINDERS'], 'SMS_ENABLED': True}
        with override_settings(REVIEW_REMINDERS=conf), \
                mock.patch('userauths.arkesel_client.ArkeselSMS.send_sms', side_effect=RuntimeError('down')):
            self.run_reminders()
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(ReviewReminder.objects.get().channels, ['email', 'in_app'])

    def test_disabled_switch(self):
        with override_settings(REVIEW_REMINDERS={**REMINDER_SETTINGS['REVIEW_REMINDERS'], 'ENABLED': False}):
            self.assertEqual(self.run_reminders(), 0)

    # ── conversion and the dashboard list ──

    def test_review_after_reminder_counts_as_converted(self):
        self.run_reminders()
        res = self.post_review()
        self.assertEqual(res.status_code, 201, res.data)
        self.assertIsNotNone(ReviewReminder.objects.get().reviewed_at)

    def test_awaiting_review_list(self):
        tote = self.another_product('Canvas Tote')
        self.delivered(1, product=tote)
        self.buy(self.buyer, delivered=False, product=self.another_product('Not Yet Delivered'))
        res = client_for(self.buyer).get('/api/v1/product/reviews/awaiting/')
        self.assertEqual(res.status_code, 200)
        self.assertEqual([r['product_id'] for r in res.data['results']], [tote.pk, self.product.pk])

        self.post_review()
        res = client_for(self.buyer).get('/api/v1/product/reviews/awaiting/')
        self.assertEqual([r['product_id'] for r in res.data['results']], [tote.pk])
        self.assertEqual(client_for().get('/api/v1/product/reviews/awaiting/').status_code, 401)
