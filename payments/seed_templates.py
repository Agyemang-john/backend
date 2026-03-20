# Run this in the Django shell:
# python manage.py shell
# then paste the whole block at once

from payments.email_models import EmailTemplate

templates = [
    ('confirmation',    'Your {{ plan_name }} Plan is Active — Negromart',              'subscriptions/subscription_confirmation.html'),
    ('renewal_success', 'Subscription Renewed — Negromart',                             'subscriptions/renewal_success.html'),
    ('expiring_soon',   'Your subscription expires in {{ days_left }} days — Negromart','subscriptions/expiring_soon.html'),
    ('expired',         'Your subscription has expired — Negromart',                    'subscriptions/subscription_expired.html'),
    ('payment_failed',  'Action required: Update your payment method — Negromart',      'subscriptions/payment_failed.html'),
    ('cancellation',    'Subscription cancelled — Negromart',                           'subscriptions/subscription_cancelled.html'),
]

for type_, subject, html_file in templates:
    obj, created = EmailTemplate.objects.get_or_create(
        type=type_,
        defaults={'subject': subject, 'html_file': html_file, 'is_active': True}
    )
    status = 'created' if created else 'already exists'
    print(f'  [{status}] {type_}')

print("\nDone — templates seeded.")