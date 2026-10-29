"""
chat/tests.py
Shopper ↔ seller chat: off-platform filter, API flows, counters, read receipts,
mutes, retention and the websocket.

Run with:  DB_HOST=localhost python manage.py test chat.tests --keepdb
"""

import json
from datetime import timedelta
from unittest import mock
from decimal import Decimal

from asgiref.sync import async_to_sync
from asgiref.testing import ApplicationCommunicator
from django.core.cache import cache
from django.test import SimpleTestCase, TestCase, TransactionTestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from ecommerce.celery import app as celery_app
from notification.models import Notification
from payments.models import SubscriptionPlan, VendorSubscription
from product.models import Category, Main_Category, Product, Sub_Category
from userauths.models import User
from vendor.models import Vendor, VendorMember

from . import moderation, realtime
from .models import ChatViolation, Conversation, Message
from .tasks import purge_old_chats, send_chat_reminders

celery_app.conf.task_always_eager = True


class WebsocketCommunicator(ApplicationCommunicator):
    """Minimal stand-in for channels.testing.WebsocketCommunicator (which needs daphne installed)."""

    def __init__(self, application, path):
        path, _, query = path.partition('?')
        super().__init__(application, {'type': 'websocket', 'path': path, 'query_string': query.encode(),
                                       'headers': [], 'subprotocols': []})

    async def connect(self, timeout=3):
        await self.send_input({'type': 'websocket.connect'})
        response = await self.receive_output(timeout)
        return response['type'] == 'websocket.accept', response.get('code')

    async def send_json_to(self, data):
        await self.send_input({'type': 'websocket.receive', 'text': json.dumps(data)})

    async def receive_json_from(self, timeout=3):
        response = await self.receive_output(timeout)
        return json.loads(response['text'])

    async def disconnect(self, code=1000):
        await self.send_input({'type': 'websocket.disconnect', 'code': code})
        await self.wait(1)

TEST_SETTINGS = {
    'CACHES': {'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}},
    'EMAIL_BACKEND': 'django.core.mail.backends.locmem.EmailBackend',
    'CHANNEL_LAYERS': {'default': {'BACKEND': 'channels.layers.InMemoryChannelLayer'}},
}


class ModerationTests(SimpleTestCase):
    BLOCKED = {
        moderation.PHONE: [
            'call 0509651622', '+233 50 965 1622', '050-965-1622', '0 5 0 9 6 5 1 6 2 2',
            'zero five zero nine six five one six two two', '05O965I622', 'my no. is 050.965.16.22',
        ],
        moderation.EMAIL: [
            'john.doe@gmail.com', 'john (at) shop (dot) com', 'johndoe at yahoo dot com', 'send it to my gmail',
        ],
        moderation.LINK: ['https://jiji.com.gh/item/1', 'see www.myshop.com', 'myshop.store has it',
                          'wa.me/233509651622', 'bit.ly/abc'],
        moderation.SOCIAL: ['add me on whatsapp', 'W.h.a.t.s.A.p.p', 'my insta is cool', 'follow @kofi_shop',
                            'telegram me', 'dm me on ig', '1nstagram'],
        moderation.PAYMENT: ['send momo', 'pay me directly and I give discount', 'my account number',
                             "it's cheaper if you pay outside the platform", 'transfer to my bank'],
        moderation.CONTACT_REQUEST: ["what's your number", 'call me', 'give me your contact',
                                     'can we meet up', 'meet me at the mall'],
    }
    ALLOWED = [
        'Is this available in size 42?', 'Price is GHS 1,500 and delivery takes 2 days',
        'My order number is 1249, when will it ship?', 'I ordered on 20/10/2026 10:30',
        'Can I pay cash on delivery?', 'Do you accept bank transfer at checkout?', 'Can I pay with PayPal?',
        'Thank you! I will check https://www.negromart.com/deals', 'Can you meet me halfway on the price?',
        'The messenger will deliver tomorrow', 'We chat here, no problem', 'Do you have the black one?',
        'I want 2 pieces', 'Good vibe right there', 'oh nice, thanks',
    ]

    def test_blocks_off_platform_content(self):
        for category, texts in self.BLOCKED.items():
            for text in texts:
                with self.subTest(text=text):
                    self.assertIn(category, moderation.check(text))

    def test_allows_normal_shopping_talk(self):
        for text in self.ALLOWED:
            with self.subTest(text=text):
                self.assertEqual(moderation.check(text), [])

    def test_number_split_across_messages(self):
        self.assertIn(moderation.PHONE, moderation.check('1622', recent=['050', '965']))
        # Ordinary numbers in sentences don't add up to a phone number.
        self.assertEqual(moderation.check('qty 12', recent=['size 42', 'price 3500']), [])

    def test_explain_lists_categories(self):
        text = moderation.explain([moderation.EMAIL, moderation.PHONE])
        self.assertIn('email addresses or phone numbers', text)


def make_user(n, **extra):
    user = User.objects.create_user(f'Chat{n}', 'Tester', f'chat{n}@example.com', f'+23324000{n:04d}', 'Str0ng!Pass')
    User.objects.filter(pk=user.pk).update(is_active=True, **extra)
    return User.objects.get(pk=user.pk)


def client_for(user):
    client = APIClient()
    client.force_authenticate(user)
    return client


class ChatFixtures:
    def make_world(self):
        cache.clear()
        self.owner = make_user(1)
        self.vendor = Vendor.objects.create(name='Chat Shop', user=self.owner, email='cshop@example.com',
                                            contact='+233550004444', is_approved=True)
        VendorMember.objects.get_or_create(vendor=self.vendor, user=self.owner, defaults={'role': 'owner'})
        # Chat is a Basic-and-up feature.
        self.plan = SubscriptionPlan.objects.create(name='Basic Chat', tier='basic', price=Decimal('50.00'))
        self.subscription = VendorSubscription.objects.create(vendor=self.vendor, plan=self.plan, status='active',
                                                              end_date=timezone.now() + timedelta(days=30))
        self.staff = make_user(2)
        VendorMember.objects.create(vendor=self.vendor, user=self.staff, role='staff')
        main = Main_Category.objects.create(title='C Main')
        sub = Sub_Category.objects.create(title='C Sub', category=Category.objects.create(title='C Cat', main_category=main))
        self.product = Product.objects.create(title='Chat Phone', sub_category=sub, vendor=self.vendor,
                                              status='published', price=Decimal('900.00'))
        self.shopper = make_user(3)

    def start(self, body='Hello, is this still available?', user=None, **extra):
        return client_for(user or self.shopper).post('/api/v1/chat/conversations/', {
            'vendor': self.vendor.slug, 'body': body, 'reason': 'product', 'product_id': self.product.pk, **extra,
        }, format='json')

    def reply(self, conversation_id, body, user=None):
        return client_for(user or self.owner).post(
            f'/api/v1/chat/conversations/{conversation_id}/messages/?as=vendor', {'body': body}, format='json')


@override_settings(**TEST_SETTINGS)
class ChatApiTests(ChatFixtures, TestCase):

    def setUp(self):
        self.make_world()

    def test_shopper_starts_and_store_replies(self):
        res = self.start()
        self.assertEqual(res.status_code, 201, res.data)
        conv_id = res.data['conversation']['id']
        self.assertEqual(res.data['conversation']['counterpart']['name'], 'Chat Shop')
        self.assertEqual(res.data['conversation']['product']['id'], self.product.pk)

        # Store inbox: one unread chat, the shopper shown as "First L." only.
        inbox = client_for(self.staff).get('/api/v1/chat/conversations/?as=vendor').data
        self.assertEqual(len(inbox['results']), 1)
        row = inbox['results'][0]
        self.assertEqual((row['unread'], row['counterpart']['name']), (1, 'Chat3 T.'))
        self.assertNotIn('email', str(row))
        self.assertEqual(client_for(self.owner).get('/api/v1/chat/unread/?as=vendor').data['conversations'], 1)

        res = self.reply(conv_id, 'Yes it is. Which colour would you like?', user=self.staff)
        self.assertEqual(res.status_code, 201, res.data)
        self.assertEqual(res.data['sender_name'], 'Chat2')
        conv = Conversation.objects.get(pk=conv_id)
        self.assertEqual((conv.customer_unread, conv.vendor_unread), (1, 0))  # replying reads the thread

        history = client_for(self.shopper).get(f'/api/v1/chat/conversations/{conv_id}/messages/').data
        self.assertEqual([m['side'] for m in history['results']], ['customer', 'vendor'])

        client_for(self.shopper).post(f'/api/v1/chat/conversations/{conv_id}/read/')
        conv.refresh_from_db()
        self.assertEqual((conv.customer_unread, conv.customer_last_read_id), (0, conv.last_message_id))

    def test_second_contact_reuses_the_conversation(self):
        first = self.start().data['conversation']['id']
        second = self.start('Also, do you deliver to Kumasi?').data['conversation']['id']
        self.assertEqual(first, second)
        self.assertEqual(Message.objects.filter(conversation_id=first).count(), 2)
        self.assertEqual(Conversation.objects.get(pk=first).vendor_unread, 2)

    def test_store_chat_lookup(self):
        data = client_for(self.shopper).get(f'/api/v1/chat/store/{self.vendor.slug}/').data
        self.assertIsNone(data['conversation'])
        self.assertTrue(data['accepting_messages'])
        self.start()
        data = client_for(self.shopper).get(f'/api/v1/chat/store/{self.vendor.slug}/').data
        self.assertEqual(data['conversation']['counterpart']['slug'], self.vendor.slug)
        self.assertTrue(client_for(self.owner).get(f'/api/v1/chat/store/{self.vendor.slug}/').data['own_store'])

    def test_off_platform_message_is_refused_and_logged(self):
        res = self.start('Call me on 0509651622 and pay me directly')
        self.assertEqual(res.status_code, 422)
        self.assertEqual(res.data['code'], 'off_platform')
        self.assertIn('phone', res.data['categories'])
        self.assertFalse(Message.objects.exists())
        self.assertFalse(Conversation.objects.exists())  # no empty chat left behind
        self.assertEqual(ChatViolation.objects.get().user, self.shopper)

    def test_seller_cannot_share_contacts_either(self):
        conv_id = self.start().data['conversation']['id']
        res = self.reply(conv_id, 'Message me on WhatsApp, it is faster')
        self.assertEqual((res.status_code, res.data['code']), (422, 'off_platform'))

    def test_number_split_over_messages_is_refused(self):
        conv_id = self.start().data['conversation']['id']
        self.assertEqual(self.reply(conv_id, '050').status_code, 201)
        self.assertEqual(self.reply(conv_id, '965').status_code, 201)
        self.assertEqual(self.reply(conv_id, '1622').status_code, 422)

    def test_repeat_offender_is_muted(self):
        conv_id = self.start().data['conversation']['id']
        for _ in range(2):
            self.assertEqual(self.reply(conv_id, 'email me at shop@gmail.com').status_code, 422)
        res = self.reply(conv_id, 'whatsapp me')
        self.assertEqual((res.status_code, res.data['code']), (429, 'muted'))
        # Even a clean message waits until the mute ends.
        self.assertEqual(self.reply(conv_id, 'Sorry, it ships tomorrow').status_code, 429)
        ChatViolation.objects.update(created_at=timezone.now() - timedelta(days=2))
        self.assertEqual(self.reply(conv_id, 'Sorry, it ships tomorrow').status_code, 201)

    def test_access_rules(self):
        conv_id = self.start().data['conversation']['id']
        stranger = make_user(4)
        self.assertEqual(client_for(stranger).get(f'/api/v1/chat/conversations/{conv_id}/messages/').status_code, 404)
        self.assertEqual(client_for(stranger).get('/api/v1/chat/conversations/?as=vendor').status_code, 403)
        # A seller can't open a chat with their own store.
        self.assertEqual(self.start(user=self.owner).data['code'], 'own_store')
        # A paused/suspended store can't be contacted.
        Vendor.objects.filter(pk=self.vendor.pk).update(is_suspended=True)
        self.assertEqual(self.start('Still there?').status_code, 409)

    def test_client_id_makes_retries_idempotent(self):
        conv_id = self.start().data['conversation']['id']
        url = f'/api/v1/chat/conversations/{conv_id}/messages/'
        a = client_for(self.shopper).post(url, {'body': 'Thanks!', 'client_id': 'abc-1'}, format='json')
        b = client_for(self.shopper).post(url, {'body': 'Thanks!', 'client_id': 'abc-1'}, format='json')
        self.assertEqual((a.status_code, b.status_code, a.data['id']), (201, 200, b.data['id']))

    def test_offline_store_gets_one_notification_per_burst(self):
        with self.captureOnCommitCallbacks(execute=True):
            self.start()
        with self.captureOnCommitCallbacks(execute=True):
            self.start('One more question')
        notes = Notification.objects.filter(verb='message')
        # owner + staff, once each (cooldown swallows the second message)
        self.assertEqual(sorted(notes.values_list('recipient_id', flat=True)), sorted([self.owner.pk, self.staff.pk]))
        self.assertTrue(notes.first().data['url'].startswith('/messages?c='))

    def test_online_store_is_not_notified(self):
        realtime.mark_online('vendor', self.vendor.pk)
        with self.captureOnCommitCallbacks(execute=True):
            self.start()
        self.assertFalse(Notification.objects.filter(verb='message').exists())

    def test_reading_a_chat_clears_its_bell_notifications(self):
        with self.captureOnCommitCallbacks(execute=True):
            conv_id = self.start().data['conversation']['id']
        self.assertEqual(Notification.objects.filter(verb='message', is_read=False).count(), 2)
        with self.captureOnCommitCallbacks(execute=True):
            client_for(self.staff).post(f'/api/v1/chat/conversations/{conv_id}/read/?as=vendor')
        self.assertFalse(Notification.objects.filter(verb='message', is_read=False).exists())
        conv = Conversation.objects.get(pk=conv_id)
        self.assertEqual(conv.vendor_delivered_id, conv.last_message_id)  # read implies delivered

    def test_bell_counts_are_split_between_shop_and_dashboard(self):
        from notification.utils import send_notification
        send_notification(self.owner, 'message', data={'audience': 'vendor'})
        send_notification(self.owner, 'message', data={'audience': 'customer'})
        send_notification(self.owner, 'vendor_new_order')
        send_notification(self.owner, 'customer_order_shipped')
        send_notification(self.owner, 'announcement')
        count = Notification.objects.unread_count_for
        self.assertEqual((count(self.owner, 'seller'), count(self.owner, 'shopper'), count(self.owner)), (3, 3, 5))

    def test_store_presence_is_public(self):
        url = f'/api/v1/chat/store/{self.vendor.slug}/presence/'
        res = APIClient().get(url)
        self.assertEqual((res.status_code, res.data['online'], res.data['last_seen']), (200, False, None))

        earlier = timezone.now() - timedelta(hours=2)
        Vendor.objects.filter(pk=self.vendor.pk).update(last_seen_at=earlier)
        self.assertEqual(APIClient().get(url).data['last_seen'], earlier.isoformat())

        realtime.mark_online('vendor', self.vendor.pk)
        res = APIClient().get(url)
        self.assertTrue(res.data['online'])
        self.assertGreater(res.data['last_seen'], earlier.isoformat())

    def test_purge_deletes_old_chats(self):
        old_id = self.start().data['conversation']['id']
        Conversation.objects.filter(pk=old_id).update(last_message_at=timezone.now() - timedelta(days=40))
        Message.objects.filter(conversation_id=old_id).update(created_at=timezone.now() - timedelta(days=40))
        other = make_user(5)
        live_id = self.start(user=other).data['conversation']['id']
        Message.objects.create(conversation_id=live_id, sender=other, sender_side='customer', body='old line')
        Message.objects.filter(body='old line').update(created_at=timezone.now() - timedelta(days=40))

        result = purge_old_chats()
        self.assertEqual(result['conversations'], 1)
        self.assertFalse(Conversation.objects.filter(pk=old_id).exists())
        self.assertEqual(list(Message.objects.filter(conversation_id=live_id).values_list('body', flat=True)),
                         ['Hello, is this still available?'])


@override_settings(**TEST_SETTINGS)
class ChatSocketTests(ChatFixtures, TransactionTestCase):

    def setUp(self):
        self.make_world()

    def _connect(self, user, side):
        # The way GET /notification/ws-token/ issues a ticket, signed in on
        # the seller dashboard for the vendor side and the shop otherwise.
        from types import SimpleNamespace
        from ecommerce.asgi import application
        from notification.middleware import issue_ticket
        user.__dict__['_auth_scope'] = 'seller' if side == 'vendor' else 'shopper'
        token = issue_ticket(SimpleNamespace(user=user, auth={}))
        return WebsocketCommunicator(application, f'/ws/chat/?token={token}&as={side}')

    def test_live_message_and_typing(self):
        async def scenario():
            store = self._connect(self.staff, 'vendor')
            connected, _ = await store.connect()
            self.assertTrue(connected)
            shopper = self._connect(self.shopper, 'customer')
            self.assertTrue((await shopper.connect())[0])

            from asgiref.sync import sync_to_async
            res = await sync_to_async(self.start)()
            event = await store.receive_json_from(timeout=3)
            self.assertEqual(event['type'], 'message')
            self.assertEqual(event['conversation']['unread'], 1)
            self.assertEqual(event['message']['body'], 'Hello, is this still available?')
            echo = await shopper.receive_json_from(timeout=3)  # the shopper's other tabs get it too
            self.assertEqual(echo['conversation']['counterpart']['name'], 'Chat Shop')

            await shopper.send_json_to({'action': 'typing', 'conversation_id': res.data['conversation']['id']})
            self.assertEqual((await store.receive_json_from(timeout=3))['type'], 'typing')
            await store.disconnect()
            await shopper.disconnect()
        async_to_sync(scenario)()

    def test_delivery_receipts(self):
        async def scenario():
            from asgiref.sync import sync_to_async
            # Store offline: the first message is only sent (✓).
            res = await sync_to_async(self.start)()
            conv_id, first_id = res.data['conversation']['id'], res.data['message']['id']
            conv = await sync_to_async(Conversation.objects.get)(pk=conv_id)
            self.assertEqual(conv.delivered_id_for('vendor'), 0)

            shopper = self._connect(self.shopper, 'customer')
            self.assertTrue((await shopper.connect())[0])
            # The store opens the dashboard: what came in while away is delivered (✓✓).
            store = self._connect(self.staff, 'vendor')
            self.assertTrue((await store.connect())[0])
            event = await shopper.receive_json_from(timeout=3)
            self.assertEqual((event['type'], event['side'], event['last_delivered_id']), ('delivered', 'vendor', first_id))

            # Store online: a new message is delivered as it arrives.
            res = await sync_to_async(self.start)('Also, do you have it in black?')
            second_id = res.data['message']['id']
            pushed = await store.receive_json_from(timeout=3)
            self.assertEqual((pushed['type'], pushed['unread_chats']), ('message', 1))
            types = {}
            for _ in range(2):  # the echo of our own message, then the receipt
                e = await shopper.receive_json_from(timeout=3)
                types[e['type']] = e
            self.assertEqual(types['delivered']['last_delivered_id'], second_id)
            conv = await sync_to_async(Conversation.objects.get)(pk=conv_id)
            self.assertEqual((conv.vendor_delivered_id, conv.vendor_last_read_id), (second_id, 0))

            # The store reads it: ✓✓ blue for the shopper, badge back to 0 for the store.
            await sync_to_async(client_for(self.staff).post)(f'/api/v1/chat/conversations/{conv_id}/read/?as=vendor')
            read = await shopper.receive_json_from(timeout=3)
            self.assertEqual((read['type'], read['last_read_id']), ('read', second_id))
            own = await store.receive_json_from(timeout=3)
            self.assertEqual((own['type'], own['unread_chats']), ('read', 0))

            await store.disconnect()
            await shopper.disconnect()
            self.assertFalse(realtime.is_online('vendor', self.vendor.pk))
        async_to_sync(scenario)()

    def test_anonymous_and_non_members_are_rejected(self):
        async def scenario():
            from ecommerce.asgi import application
            anon = WebsocketCommunicator(application, '/ws/chat/')
            self.assertFalse((await anon.connect())[0])
            outsider = self._connect(self.shopper, 'vendor')
            self.assertFalse((await outsider.connect())[0])
        async_to_sync(scenario)()


@override_settings(**TEST_SETTINGS)
class ChatPlanTests(ChatFixtures, TestCase):
    """Free stores don't get chat; Basic and above do."""

    def setUp(self):
        self.make_world()
        self.subscription.delete()
        SubscriptionPlan.objects.create(name='Free', tier='free', price=Decimal('0.00'))

    def test_free_store_cannot_be_contacted_or_use_inbox(self):
        self.assertFalse(client_for(self.shopper).get(f'/api/v1/vendor/seller-detail/{self.vendor.slug}/').data['accepts_messages'])
        self.assertFalse(client_for(self.shopper).get(f'/api/v1/chat/store/{self.vendor.slug}/').data['accepting_messages'])
        realtime.mark_online('vendor', self.vendor.pk)
        presence = client_for(self.shopper).get(f'/api/v1/chat/store/{self.vendor.slug}/presence/').data
        self.assertEqual((presence['accepting_messages'], presence['online']), (False, False))
        self.assertEqual(self.start().status_code, 409)
        res = client_for(self.owner).get('/api/v1/chat/conversations/?as=vendor')
        self.assertEqual((res.status_code, res.data['code'], res.data['required_tier']), (403, 'plan_upgrade_required', 'basic'))
        self.assertFalse(client_for(self.owner).get('/api/v1/vendor/plan/').data['can_use_messaging'])

    def test_upgrading_turns_chat_on(self):
        VendorSubscription.objects.create(vendor=self.vendor, plan=SubscriptionPlan.objects.create(
            name='Pro', tier='pro', price=Decimal('99.00')), status='active', end_date=timezone.now() + timedelta(days=30))
        self.assertEqual(self.start().status_code, 201)
        self.assertTrue(client_for(self.owner).get('/api/v1/vendor/plan/').data['can_use_messaging'])


@override_settings(**TEST_SETTINGS)
class ChatReminderTests(ChatFixtures, TestCase):

    def setUp(self):
        self.make_world()
        self.conv_id = self.start().data['conversation']['id']
        Vendor.objects.filter(pk=self.vendor.pk).update(contact='+233550004444')

    def age(self, hours):
        Conversation.objects.filter(pk=self.conv_id).update(last_message_at=timezone.now() - timedelta(hours=hours))

    @mock.patch('chat.tasks.send_chat_sms.delay')
    def test_store_reminded_once_after_24h_without_reply(self, sms):
        self.age(5)
        self.assertEqual(send_chat_reminders()['vendor'], 0)  # too early
        self.age(25)
        self.assertEqual(send_chat_reminders()['vendor'], 1)
        reminders = Notification.objects.filter(verb='message', data__reminder=True)
        self.assertEqual(sorted(reminders.values_list('recipient_id', flat=True)), sorted([self.owner.pk, self.staff.pk]))
        sms.assert_called_once()
        phone, text = sms.call_args.args
        self.assertEqual(phone, '+233550004444')
        self.assertIn('waiting for your reply', text)
        self.assertNotIn('still available', text)  # SMS never quotes the message
        self.assertLessEqual(len(text), 160)
        self.assertEqual(send_chat_reminders()['vendor'], 0)  # once per unanswered message

    @mock.patch('chat.tasks.send_chat_sms.delay')
    def test_shopper_reminded_when_reply_unread(self, sms):
        self.reply(self.conv_id, 'Yes, we have it in black and white.')
        self.age(25)
        self.assertEqual(send_chat_reminders(), {'vendor': 0, 'customer': 1})
        self.assertEqual(sms.call_args.args[0], self.shopper.phone)
        self.assertTrue(Notification.objects.filter(recipient=self.shopper, data__reminder=True).exists())

    @mock.patch('chat.tasks.send_chat_sms.delay')
    def test_no_reminder_when_handled_or_stale(self, sms):
        self.reply(self.conv_id, 'Yes, we have it.')
        client_for(self.shopper).post(f'/api/v1/chat/conversations/{self.conv_id}/read/')
        self.age(25)
        self.assertEqual(send_chat_reminders(), {'vendor': 0, 'customer': 0})  # replied, and the reply was read
        self.start('Hello again?')
        self.age(24 * 5)
        self.assertEqual(send_chat_reminders()['vendor'], 0)  # too old: no backlog floods
        sms.assert_not_called()

    @mock.patch('chat.tasks.send_chat_sms.delay')
    def test_store_that_lost_its_plan_is_not_reminded(self, sms):
        self.subscription.delete()
        self.age(25)
        self.assertEqual(send_chat_reminders()['vendor'], 0)
