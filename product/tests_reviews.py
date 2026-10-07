"""
product/tests_reviews.py
Product reviews: eligibility, verification, moderation, publication,
statistics, owner edit/delete, staff moderation, media, helpful votes.

Run with:  DB_HOST=localhost python manage.py test product.tests_reviews --keepdb
"""

import shutil
import tempfile
import threading
from datetime import timedelta
from decimal import Decimal
from io import BytesIO

from django.contrib.auth.models import Permission
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone
from PIL import Image
from rest_framework.test import APIClient

from ecommerce.celery import app as celery_app
from order.models import Order, OrderProduct
from product.models import (
    Category, Main_Category, Product, ProductReview, ReviewMedia, ReviewModerationEvent, Sub_Category,
)
from userauths.models import User
from vendor.models import Vendor

celery_app.conf.task_always_eager = True
MEDIA_DIR = tempfile.mkdtemp(prefix='review-media-tests-')

TEST_SETTINGS = {
    'CACHES': {'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}},
    'EMAIL_BACKEND': 'django.core.mail.backends.locmem.EmailBackend',
    'CHANNEL_LAYERS': {'default': {'BACKEND': 'channels.layers.InMemoryChannelLayer'}},
    'MEDIA_ROOT': MEDIA_DIR,
    'STORAGES': {
        'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
        'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'},
    },
    'REVIEW_MODERATION': {'BLOCKED_TERMS': ['idiot'], 'MAX_REVIEWS_PER_HOUR': 5},
}
GOOD_TEXT = 'Comfortable, true to size, and the stitching looks solid.'


def make_user(n, **extra):
    user = User.objects.create_user(f'Rev{n}', 'Iewer', f'rev{n}@example.com', f'+23327000{n:04d}', 'Str0ng!Pass')
    User.objects.filter(pk=user.pk).update(is_active=True, **extra)
    return User.objects.get(pk=user.pk)


def client_for(user=None):
    client = APIClient()
    if user:
        client.force_authenticate(user)
    return client


def jpeg_with_gps():
    img = Image.new('RGB', (3000, 2000), (200, 30, 30))
    exif = Image.Exif()
    exif[0x8825] = {1: 'N', 2: (5.0, 36.0, 0.0)}  # GPSInfo, as phones embed it
    buf = BytesIO()
    img.save(buf, format='JPEG', exif=exif)
    return SimpleUploadedFile('phone.jpg', buf.getvalue(), content_type='image/jpeg')


def tiny_mp4():
    return SimpleUploadedFile('clip.mp4', b'\x00\x00\x00\x18ftypmp42' + b'\x00' * 200, content_type='video/mp4')


class ReviewFixtures:
    def make_world(self):
        cache.clear()
        owner = make_user(1)
        self.vendor = Vendor.objects.create(name='Review Shop', user=owner, email='rshop@example.com',
                                            contact='+233550003333', is_approved=True)
        main = Main_Category.objects.create(title='R Main')
        self.sub = Sub_Category.objects.create(title='R Sub', category=Category.objects.create(title='R Cat', main_category=main))
        self.product = Product.objects.create(title='Review Sneaker', sub_category=self.sub, vendor=self.vendor,
                                              status='published', price=Decimal('300.00'))
        self.buyer = make_user(2)
        self.line = self.buy(self.buyer, delivered=True)
        self.staff = make_user(90, is_staff=True)
        self.staff.user_permissions.add(Permission.objects.get(codename='moderate_productreview'))
        self.staff = User.objects.get(pk=self.staff.pk)

    def buy(self, user, delivered, product=None):
        order = Order.objects.create(user=user, total=Decimal('300.00'), is_ordered=True, status='processing')
        return OrderProduct.objects.create(
            order=order, product=product or self.product, quantity=1, price=Decimal('300.00'),
            amount=Decimal('300.00'), delivered_date=timezone.now() if delivered else None,
            status='delivered' if delivered else 'processing',
        )

    def post_review(self, user=None, **data):
        payload = {'rating': 4, 'review': GOOD_TEXT, **data}
        return client_for(user or self.buyer).post(f'/api/v1/product/{self.product.pk}/reviews/', payload,
                                                   format='json')

    def approved_review(self, user, rating):
        return ProductReview.objects.create(user=user, product=self.product, rating=rating, review=GOOD_TEXT + str(rating),
                                            moderation_status=ProductReview.APPROVED)


@override_settings(**TEST_SETTINGS)
class EligibilityAndSubmissionTests(ReviewFixtures, TestCase):

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(MEDIA_DIR, ignore_errors=True)

    def setUp(self):
        self.make_world()

    def test_valid_review_is_verified_and_auto_approved(self):
        res = self.post_review(title='Great pair')
        self.assertEqual(res.status_code, 201, res.data)
        self.assertEqual(res.data['moderation_status'], 'approved')
        self.assertTrue(res.data['is_verified_purchase'])
        review = ProductReview.objects.get(pk=res.data['id'])
        self.assertEqual(review.order_item_id, self.line.pk)
        self.assertTrue(review.status)
        self.assertEqual(ReviewModerationEvent.objects.filter(review=review, to_status='approved').count(), 1)

    def test_unauthenticated_cannot_submit(self):
        res = client_for().post(f'/api/v1/product/{self.product.pk}/reviews/', {'rating': 5, 'review': GOOD_TEXT},
                                format='json')
        self.assertIn(res.status_code, (401, 403))

    def test_not_purchased_or_someone_elses_order(self):
        stranger = make_user(3)
        res = self.post_review(user=stranger)
        self.assertEqual(res.status_code, 403)
        self.assertEqual(res.data['code'], 'not_delivered')
        self.assertEqual(set(res.data), {'detail', 'code'})  # nothing about anyone's orders

    def test_undelivered_order_is_not_eligible(self):
        waiting = make_user(4)
        self.buy(waiting, delivered=False)
        self.assertEqual(self.post_review(user=waiting).status_code, 403)

    def test_duplicate_review_is_refused(self):
        self.assertEqual(self.post_review().status_code, 201)
        res = self.post_review()
        self.assertEqual(res.status_code, 409)
        self.assertEqual(res.data['code'], 'already_reviewed')

    def test_rating_must_be_one_to_five(self):
        self.assertEqual(self.post_review(rating=0).status_code, 400)
        self.assertEqual(self.post_review(rating=6).status_code, 400)

    def test_client_cannot_set_status_or_verification(self):
        res = self.post_review(moderation_status='approved', status=True, is_verified_purchase=False,
                               review='Contact me on whatsapp for a cheaper pair, very good shoes')
        self.assertEqual(res.status_code, 201, res.data)
        review = ProductReview.objects.get(pk=res.data['id'])
        self.assertEqual(review.moderation_status, 'pending')   # flagged, despite the client's claim
        self.assertTrue(review.is_verified_purchase)            # derived from the order, not the client

    def test_suspicious_content_waits_for_moderation_but_negative_reviews_do_not(self):
        res = self.post_review(rating=1, review='Terrible. The sole came off after two days of normal use.')
        self.assertEqual(res.data['moderation_status'], 'approved')  # criticism is fine

        other = make_user(5)
        self.buy(other, delivered=True)
        res = self.post_review(user=other, review='Lovely shoes, email me at seller@example.com for more')
        self.assertEqual(res.data['moderation_status'], 'pending')
        self.assertIn('contains_email', ProductReview.objects.get(pk=res.data['id']).moderation_flags)

    def test_eligibility_endpoint(self):
        url = f'/api/v1/product/{self.product.pk}/reviews/eligibility/'
        self.assertTrue(client_for(self.buyer).get(url).data['can_review'])
        self.assertEqual(client_for().get(url).data['reason'], 'sign_in')
        self.post_review()
        self.assertEqual(client_for(self.buyer).get(url).data['reason'], 'already_reviewed')


@override_settings(**TEST_SETTINGS)
class PublicationAndStatisticsTests(ReviewFixtures, TestCase):

    def setUp(self):
        self.make_world()

    def test_only_approved_reviews_are_listed_and_counted(self):
        self.approved_review(make_user(10), 5)
        self.approved_review(make_user(11), 4)
        for n, status in ((12, 'pending'), (13, 'rejected'), (14, 'hidden')):
            ProductReview.objects.create(user=make_user(n), product=self.product, rating=1, review=GOOD_TEXT,
                                         moderation_status=status)
        url = f'/api/v1/product/{self.product.pk}/reviews/'
        self.assertEqual(client_for().get(url).data['count'], 2)
        summary = client_for().get(f'{url}summary/').data
        self.assertEqual(summary['count'], 2)
        self.assertEqual(summary['distribution']['1'], 0)
        self.assertEqual(summary['average'], 4.5)
        self.product.refresh_from_db()
        self.assertEqual((self.product.review_count, self.product.avg_rating), (2, 4.5))

    def test_statistics_follow_moderation(self):
        review = ProductReview.objects.create(user=make_user(10), product=self.product, rating=2, review=GOOD_TEXT,
                                              moderation_status='pending')
        url = f'/api/v1/product/{self.product.pk}/reviews/summary/'
        self.assertEqual(client_for().get(url).data['count'], 0)
        staff = client_for(self.staff)
        self.assertEqual(staff.post(f'/api/v1/product/reviews/{review.pk}/moderate/', {'action': 'approve'},
                                    format='json').status_code, 200)
        self.assertEqual(client_for().get(url).data['count'], 1)
        staff.post(f'/api/v1/product/reviews/{review.pk}/moderate/', {'action': 'hide', 'reason': 'Off-topic'},
                   format='json')
        self.assertEqual(client_for().get(url).data['count'], 0)
        self.product.refresh_from_db()
        self.assertEqual(self.product.review_count, 0)

    def test_filters_sorting_pagination(self):
        for n, rating in enumerate([5, 4, 1, 5], start=20):
            self.approved_review(make_user(n), rating)
        url = f'/api/v1/product/{self.product.pk}/reviews/'
        public = client_for()
        self.assertEqual(public.get(url, {'rating': 5}).data['count'], 2)
        self.assertEqual(public.get(url, {'sort': 'rating_low'}).data['results'][0]['rating'], 1)
        self.assertEqual(public.get(url, {'sort': 'rating_high'}).data['results'][0]['rating'], 5)
        page = public.get(url, {'page_size': 3}).data
        self.assertEqual((len(page['results']), page['count']), (3, 4))
        self.assertNotIn('moderation_reason', page['results'][0])


@override_settings(**TEST_SETTINGS)
class OwnershipAndModerationTests(ReviewFixtures, TestCase):

    def setUp(self):
        self.make_world()
        self.review_id = self.post_review().data['id']

    def test_owner_edits_and_content_is_rechecked(self):
        res = client_for(self.buyer).patch(f'/api/v1/product/reviews/{self.review_id}/',
                                           {'review': 'Still great, buy at www.cheapshoes.com'}, format='json')
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data['moderation_status'], 'pending')

    def test_others_cannot_edit_or_delete(self):
        intruder = client_for(make_user(30))
        url = f'/api/v1/product/reviews/{self.review_id}/'
        self.assertEqual(intruder.patch(url, {'rating': 1}, format='json').status_code, 404)
        self.assertEqual(intruder.delete(url).status_code, 404)
        self.assertEqual(client_for(self.buyer).delete(url).status_code, 204)

    def test_my_reviews_include_status_but_not_internal_notes(self):
        data = client_for(self.buyer).get('/api/v1/product/reviews/mine/').data['results'][0]
        self.assertEqual(data['moderation_status'], 'approved')
        self.assertNotIn('moderation_reason', data)

    def test_only_staff_with_permission_can_moderate(self):
        url = f'/api/v1/product/reviews/{self.review_id}/moderate/'
        body = {'action': 'hide', 'reason': 'Spam'}
        self.assertEqual(client_for(self.buyer).post(url, body, format='json').status_code, 403)
        plain_staff = make_user(31, is_staff=True)
        self.assertEqual(client_for(plain_staff).post(url, body, format='json').status_code, 403)
        self.assertEqual(client_for(self.staff).get('/api/v1/product/reviews/moderation/').status_code, 200)

    def test_staff_transitions_reason_and_history(self):
        staff = client_for(self.staff)
        url = f'/api/v1/product/reviews/{self.review_id}/moderate/'
        self.assertEqual(staff.post(url, {'action': 'hide'}, format='json').status_code, 400)  # reason needed
        res = staff.post(url, {'action': 'hide', 'reason': 'Contains a personal attack'}, format='json')
        self.assertEqual(res.data['moderation_status'], 'hidden')
        self.assertEqual(res.data['moderated_by_email'], self.staff.email)
        self.assertEqual(staff.post(url, {'action': 'reject', 'reason': 'x'}, format='json').status_code, 409)
        self.assertEqual(staff.post(url, {'action': 'approve'}, format='json').data['moderation_status'], 'approved')
        history = staff.get(f'/api/v1/product/reviews/{self.review_id}/history/').data
        self.assertEqual([h['to_status'] for h in history][:2], ['approved', 'hidden'])

        pending = ProductReview.objects.create(user=make_user(32), product=self.product, rating=3,
                                               review=GOOD_TEXT, moderation_status='pending')
        res = staff.post(f'/api/v1/product/reviews/{pending.pk}/moderate/',
                         {'action': 'reject', 'reason': 'Promotional'}, format='json')
        self.assertEqual(res.data['moderation_status'], 'rejected')


@override_settings(**TEST_SETTINGS)
class MediaAndHelpfulTests(ReviewFixtures, TestCase):

    def setUp(self):
        self.make_world()

    def upload(self, file, user=None):
        return client_for(user or self.buyer).post('/api/v1/product/reviews/media/', {'file': file}, format='multipart')

    def test_photo_is_resized_and_location_data_removed(self):
        res = self.upload(jpeg_with_gps())
        self.assertEqual(res.status_code, 201, res.data)
        media = ReviewMedia.objects.get(pk=res.data['id'])
        self.assertEqual(max(media.width, media.height), 1600)
        with Image.open(media.file.path) as stored:
            self.assertNotIn(0x8825, stored.getexif())

    def test_malicious_or_invalid_uploads_are_rejected(self):
        exe = SimpleUploadedFile('virus.jpg', b'MZ\x90\x00not an image', content_type='image/jpeg')
        html = SimpleUploadedFile('x.mp4', b'<script>alert(1)</script>', content_type='video/mp4')
        self.assertEqual(self.upload(exe).status_code, 400)
        self.assertEqual(self.upload(html).status_code, 400)
        self.assertEqual(self.upload(tiny_mp4()).status_code, 201)

    def test_only_own_uploads_attach(self):
        photo = self.upload(jpeg_with_gps()).data['id']
        stranger_upload = self.upload(jpeg_with_gps(), user=make_user(40)).data['id']
        res = self.post_review(media_ids=[photo, stranger_upload])
        self.assertEqual([m['id'] for m in res.data['media']], [photo])

    def test_helpful_votes_once_per_customer_and_not_on_own_review(self):
        review_id = self.post_review().data['id']
        voter = client_for(make_user(41))
        url = f'/api/v1/product/reviews/{review_id}/helpful/'
        self.assertEqual(voter.post(url).data['helpful_count'], 1)
        self.assertEqual(voter.post(url).data['helpful_count'], 1)
        self.assertEqual(client_for(self.buyer).post(url).status_code, 400)
        self.assertEqual(voter.delete(url).data['helpful_count'], 0)

    def test_orphan_uploads_cleaned_up(self):
        orphan_id = self.upload(tiny_mp4()).data['id']
        ReviewMedia.objects.filter(pk=orphan_id).update(created_at=timezone.now() - timedelta(days=2))
        from product.tasks import cleanup_orphan_review_media
        self.assertEqual(cleanup_orphan_review_media(), 1)


@override_settings(**TEST_SETTINGS)
class ConcurrencyTests(ReviewFixtures, TransactionTestCase):
    """Two simultaneous submissions must not create two reviews."""

    def setUp(self):
        self.make_world()

    def test_concurrent_submissions_create_one_review(self):
        results = []

        def submit():
            try:
                results.append(self.post_review().status_code)
            finally:
                connection.close()

        threads = [threading.Thread(target=submit) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(ProductReview.objects.filter(user=self.buyer, product=self.product).count(), 1)
        self.assertEqual(results.count(201), 1)
        self.assertTrue(all(code in (201, 409) for code in results), results)
