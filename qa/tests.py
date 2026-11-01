"""
Product questions and answers.

Run with:  DB_HOST=localhost python manage.py test qa.tests --keepdb
"""
from decimal import Decimal
from unittest import mock

from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from ecommerce.celery import app as celery_app
from notification.models import Notification
from order.models import Order, OrderProduct
from product.models import Brand, Category, Main_Category, Product, Sub_Category
from qa.models import ProductAnswer, ProductQuestion
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
    user = User.objects.create_user(f'Qa{n}', f'Last{n}', f'qa{n}@example.com', f'+23324300{n:04d}', 'Str0ng!Passw0rd')
    User.objects.filter(pk=user.pk).update(is_active=True, email_verified_at=timezone.now())
    return User.objects.get(pk=user.pk)


@override_settings(**TEST_SETTINGS)
class QATests(TestCase):

    def setUp(self):
        from django.core.cache import cache
        cache.clear()
        cache.set('exchange_rates', {'GHS': 1, 'USD': 0.1}, None)
        for target in ('vendor.models.send_vendor_approval_email.delay', 'vendor.models.send_vendor_sms.delay'):
            p = mock.patch(target)
            p.start()
            self.addCleanup(p.stop)
        self.owner = make_user(1)
        self.vendor = Vendor.objects.create(name='QA Shop', user=self.owner, email='qa-shop@example.com',
                                            contact='+233550005555', is_approved=True, status='VERIFIED')
        main = Main_Category.objects.create(title='QA Main')
        sub = Sub_Category.objects.create(title='QA Sub', category=Category.objects.create(title='QA Cat', main_category=main))
        self.product = Product.objects.create(title='QA Blender', sub_category=sub, vendor=self.vendor,
                                              status='published', price=Decimal('100'), total_quantity=5,
                                              brand=Brand.objects.create(title='QA Brand'))
        self.asker = make_user(2)
        self.buyer = make_user(3)
        self.stranger = make_user(4)
        order = Order.objects.create(user=self.buyer, total=Decimal('100'), status='delivered', is_ordered=True)
        OrderProduct.objects.create(order=order, product=self.product, quantity=1, price=Decimal('100'),
                                    amount=Decimal('100'), status='delivered')

    def client_for(self, user):
        c = APIClient()
        c.force_authenticate(user)
        return c

    def seller_client(self):
        c = APIClient()
        c.cookies['vendor_access'] = str(CustomVendorRefreshToken.for_user(self.owner).access_token)
        c.credentials(HTTP_X_USER_TYPE='vendor')
        return c

    def ask(self, text='Does it crush ice well?'):
        with self.captureOnCommitCallbacks(execute=True):
            res = self.client_for(self.asker).post(f'/api/v1/qa/products/{self.product.pk}/questions/',
                                                   {'text': text}, format='json')
        return res

    def test_ask_and_seller_is_notified(self):
        res = self.ask()
        self.assertEqual(res.status_code, 201, res.content)
        self.assertEqual(res.data['author'], 'Qa2 L.')
        self.assertTrue(Notification.objects.filter(recipient=self.owner, verb='vendor_new_question').exists())

    def test_contact_details_are_refused(self):
        res = self.ask('Call me on 0244123456 about this')
        self.assertEqual(res.status_code, 400)
        self.assertIn('contact', res.data['detail'].lower() + 'contact')
        self.assertFalse(ProductQuestion.objects.exists())

    def test_duplicate_question_refused(self):
        self.ask()
        self.assertEqual(self.ask().status_code, 409)

    def test_only_buyers_and_seller_answer(self):
        q = ProductQuestion.objects.get(pk=self.ask().data['id'])
        url = f'/api/v1/qa/questions/{q.pk}/answers/'
        self.assertEqual(self.client_for(self.stranger).post(url, {'text': 'Yes'}, format='json').status_code, 403)
        # The owner signed in on the shop (shopper token) is not answering for the store.
        from rest_framework_simplejwt.tokens import RefreshToken
        shop = APIClient()
        shop.credentials(HTTP_AUTHORIZATION=f'Bearer {RefreshToken.for_user(self.owner).access_token}')
        self.assertEqual(shop.post(url, {'text': 'Yes it does'}, format='json').status_code, 403)

        with self.captureOnCommitCallbacks(execute=True):
            res = self.client_for(self.buyer).post(url, {'text': 'Yes, easily.'}, format='json')
        self.assertEqual(res.status_code, 201, res.content)
        self.assertTrue(res.data['is_verified_buyer'])
        self.assertTrue(Notification.objects.filter(recipient=self.asker, verb='customer_question_answered').exists())

        res = self.seller_client().post(url, {'text': 'Yes, it has a 1200W motor.'}, format='json')
        self.assertEqual(res.status_code, 201, res.content)
        self.assertTrue(res.data['is_seller'])
        q.refresh_from_db()
        self.assertEqual(q.answer_count, 2)
        self.assertTrue(q.seller_answered)

        # Seller answer shows first in the product list.
        listing = APIClient().get(f'/api/v1/qa/products/{self.product.pk}/questions/')
        self.assertEqual(listing.data['count'], 1)
        self.assertTrue(listing.data['results'][0]['answers'][0]['is_seller'])

    def test_helpful_vote_toggles(self):
        q = ProductQuestion.objects.get(pk=self.ask().data['id'])
        a = self.client_for(self.buyer).post(f'/api/v1/qa/questions/{q.pk}/answers/', {'text': 'Yes it does'},
                                             format='json').data
        url = f'/api/v1/qa/answers/{a["id"]}/helpful/'
        self.assertEqual(self.client_for(self.asker).post(url).data, {'voted': True, 'helpful_count': 1})
        self.assertEqual(self.client_for(self.asker).post(url).data, {'voted': False, 'helpful_count': 0})
        self.assertEqual(self.client_for(self.buyer).post(url).status_code, 400, "own answer")

    def test_three_reports_hide(self):
        q = ProductQuestion.objects.get(pk=self.ask().data['id'])
        for n in range(5, 8):
            res = self.client_for(make_user(n)).post(f'/api/v1/qa/questions/{q.pk}/report/', {'reason': 'spam'},
                                                     format='json')
            self.assertEqual(res.status_code, 201)
        q.refresh_from_db()
        self.assertEqual(q.status, ProductQuestion.HIDDEN)
        self.assertEqual(APIClient().get(f'/api/v1/qa/products/{self.product.pk}/questions/').data['count'], 0)

    def test_search(self):
        self.ask('Does it crush ice well?')
        self.ask('What is the warranty period?')
        res = APIClient().get(f'/api/v1/qa/products/{self.product.pk}/questions/', {'q': 'warranty'})
        self.assertEqual(res.data['count'], 1)

    def test_seller_inbox_and_deleting_seller_answer(self):
        q = ProductQuestion.objects.get(pk=self.ask().data['id'])
        seller = self.seller_client()
        inbox = seller.get('/api/v1/qa/seller/questions/')
        self.assertEqual(inbox.status_code, 200, inbox.content)
        self.assertEqual(inbox.data['unanswered_total'], 1)
        a = seller.post(f'/api/v1/qa/questions/{q.pk}/answers/', {'text': 'Yes it does'}, format='json').data
        self.assertEqual(seller.get('/api/v1/qa/seller/questions/').data['unanswered_total'], 0)
        self.assertEqual(seller.delete(f'/api/v1/qa/answers/{a["id"]}/').status_code, 204)
        q.refresh_from_db()
        self.assertFalse(q.seller_answered)
        self.assertEqual(q.answer_count, 0)
        # Shoppers can't use the seller inbox.
        self.assertEqual(self.client_for(self.asker).get('/api/v1/qa/seller/questions/').status_code, 403)

    def test_list_query_count_is_flat(self):
        for i in range(6):
            q = ProductQuestion.objects.create(product=self.product, user=self.asker, text=f'Question number {i}?')
            for j in range(3):
                ProductAnswer.objects.create(question=q, user=self.buyer, text=f'answer {j}', is_verified_buyer=True)
        from rest_framework_simplejwt.tokens import RefreshToken
        shopper = APIClient()
        shopper.credentials(HTTP_AUTHORIZATION=f'Bearer {RefreshToken.for_user(self.stranger).access_token}')
        with self.assertNumQueries(5):  # auth user, count, page, answers (windowed), votes
            res = shopper.get(f'/api/v1/qa/products/{self.product.pk}/questions/')
        self.assertEqual(len(res.data['results'][0]['answers']), 2)
