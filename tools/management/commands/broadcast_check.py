"""
Why didn't a shopper get a store message?

    python manage.py broadcast_check                       # the last 3 messages and who they went to
    python manage.py broadcast_check --user shopper@x.com  # also: is this shopper in each audience, and why not
"""
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand

from tools.broadcasts import _groups, _pick
from tools.models import Broadcast


class Command(BaseCommand):
    help = 'Show who recent "Messages to followers" went to, and why a shopper was left out.'

    def add_arguments(self, parser):
        parser.add_argument('--user', help='Email of the shopper who expected the message')
        parser.add_argument('--last', type=int, default=3)

    def handle(self, *args, **opts):
        from engagement.models import EmailOptOut
        from notification.models import Notification
        from vendor.models import VendorMember

        User = get_user_model()
        shopper = None
        if opts.get('user'):
            shopper = User.objects.filter(email__iexact=opts['user']).first()
            if not shopper:
                self.stdout.write(self.style.ERROR(f"No account with email {opts['user']}"))
                return
            self.stdout.write(f'Shopper #{shopper.pk} {shopper.get_full_name()} active={shopper.is_active}')

        for b in Broadcast.objects.select_related('vendor').order_by('-created_at')[:opts['last']]:
            store = b.vendor
            followers, buyers = _groups(store)
            people = _pick((followers, buyers), b.audience)
            got = Notification.objects.filter(verb='customer_store_news', created_at__gte=b.created_at,
                                              data__store=store.name, recipient_id__in=people).count()
            self.stdout.write(self.style.MIGRATE_HEADING(
                f'\n#{b.pk} "{b.subject}" from {store.name} · {b.status} · audience={b.audience} · {b.created_at:%d %b %H:%M}'))
            self.stdout.write(f'  recorded: reached {b.recipients}, emailed {b.emailed}, sms {b.sms_sent}, error={b.error!r}')
            self.stdout.write(f'  audience now: {len(followers)} followers, {len(buyers)} buyers → {len(people)} people; '
                              f'in-app notifications found: {got}')
            if not shopper:
                continue
            team = shopper.pk == store.user_id or VendorMember.objects.filter(vendor=store, user=shopper).exists()
            follows = store.followers.filter(pk=shopper.pk).exists()
            checks = [
                ('follows this store', follows),
                ('bought from it in the last 12 months', shopper.pk in buyers),
                ('NOT on the store team (team never receives)', not team),
                (f'in audience "{b.audience}"', shopper.pk in people),
                ('got the in-app notification', Notification.objects.filter(
                    recipient=shopper, verb='customer_store_news', created_at__gte=b.created_at).exists()),
                ('email allowed (store offers not turned off)', not EmailOptOut.objects.filter(
                    user=shopper, category=EmailOptOut.STORE_OFFERS).exists()),
            ]
            for label, ok in checks:
                self.stdout.write(f"  {'✔' if ok else '✘'} {label}")
