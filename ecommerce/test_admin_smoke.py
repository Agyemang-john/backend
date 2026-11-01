"""
Admin smoke test: every registered admin page must render for a superuser.

Opens the changelist, the changelist with a search, the add form and the change
form (on a generated object) for every model in admin.site, and reports every
page that errors instead of stopping at the first one.

Run with:  DB_HOST=localhost python manage.py test ecommerce.test_admin_smoke --keepdb
Needs model_bakery (dev only) to generate objects.
"""
import traceback

from django.contrib import admin
from django.core.cache import cache
from django.db import transaction
from django.test import TestCase, override_settings
from django.urls import NoReverseMatch, reverse

from ecommerce.celery import app as celery_app

celery_app.conf.task_always_eager = True


@override_settings(
    ADMIN_2FA_REQUIRED=False,
    CACHES={'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}},
    EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend',
    CHANNEL_LAYERS={'default': {'BACKEND': 'channels.layers.InMemoryChannelLayer'}},
)
class AdminSmokeTest(TestCase):
    def test_every_admin_page_renders(self):
        from decimal import Decimal

        from model_bakery import baker, generators
        from userauths.models import User

        # Random decimals overflow small max_digits fields (order lines, claims, returns).
        generators.add('django.db.models.DecimalField', lambda *a, **k: Decimal('1.00'))
        # Random ints make quantity x price overflow the line total.
        for f in ('IntegerField', 'PositiveIntegerField', 'PositiveSmallIntegerField', 'SmallIntegerField'):
            generators.add(f'django.db.models.{f}', lambda *a, **k: 1)

        cache.set('exchange_rates', {'GHS': 1, 'USD': 1}, None)
        user = User.objects.create_superuser('Admin', 'Smoke', 'admin-smoke@example.com', '+233249999999', 'Str0ng!Passw0rd')
        User.objects.filter(pk=user.pk).update(is_active=True, is_staff=True, is_superuser=True)
        self.client.force_login(User.objects.get(pk=user.pk))

        failures = []

        def hit(label, url):
            try:
                r = self.client.get(url)
                if r.status_code >= 500:
                    failures.append(f"{label} {url} -> {r.status_code}")
            except Exception:  # noqa: BLE001
                failures.append(f"{label} {url}\n{traceback.format_exc(limit=-6)}")

        hit('index', reverse('admin:index'))
        for model, model_admin in sorted(admin.site._registry.items(), key=lambda kv: kv[0]._meta.label):
            meta = model._meta
            base = f"admin:{meta.app_label}_{meta.model_name}"
            obj = None
            try:
                with transaction.atomic():
                    obj = baker.make(model)
            except Exception as exc:  # noqa: BLE001
                failures.append(f"(seed) {meta.label}: could not create object: {type(exc).__name__}: {str(exc)[:160]}")
            try:
                hit(meta.label, reverse(f"{base}_changelist"))
                if model_admin.search_fields:
                    hit(meta.label, reverse(f"{base}_changelist") + '?q=a')
                hit(meta.label, reverse(f"{base}_add"))
                if obj is not None:
                    hit(meta.label, reverse(f"{base}_change", args=[obj.pk]))
            except NoReverseMatch:
                pass

        real = [f for f in failures if not f.startswith('(seed)')]
        print("\n\n".join(failures))
        self.assertEqual(real, [], f"{len(real)} admin page(s) failed")
