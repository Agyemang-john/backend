"""
Starter holiday calendar. Every row is disabled: switch one on in the admin
(Holiday calendar) and the curator opens its occasion when it comes into
season. Eid has no dates yet — they follow the moon, so add each year's date.
"""

from django.db import migrations

EVENTS = [
    # slug, title, icon, rule fields, lead, after, countries, keywords
    ('valentines', "Valentine's Day gifts", '💝', dict(rule='fixed', month=2, day=14), 14, 0, [],
     'valentine, gift, love, perfume, jewelry, necklace, chocolate, flower, couple, watch'),
    ('ghana-independence', 'Independence Day', '🇬🇭', dict(rule='fixed', month=3, day=6), 7, 0, ['GH'],
     'ghana, kente, flag, african print, traditional, smock'),
    ('easter', 'Easter', '🐣', dict(rule='easter'), 14, 1, [],
     'easter, family, dress, kids, outfit, chocolate, egg, church'),
    ('mothers-day', "Mother's Day gifts", '🌸', dict(rule='nth_weekday', month=5, weekday=6, nth=2), 14, 0, [],
     'mom, mother, women, gift, perfume, handbag, jewelry, skincare, dress'),
    ('fathers-day', "Father's Day gifts", '👔', dict(rule='nth_weekday', month=6, weekday=6, nth=3), 14, 0, [],
     'dad, father, men, gift, watch, grooming, shirt, wallet, cologne'),
    ('back-to-school', 'School Essentials', '🎒', dict(rule='fixed', month=9, day=1), 30, 14, [],
     'school, backpack, bag, notebook, stationery, uniform, shoe, lunch, laptop, dorm, towel'),
    ('black-friday', 'Black Friday', '🏷️', dict(rule='nth_weekday', month=11, weekday=3, nth=4, offset_days=1), 7, 3, [],
     ''),
    ('farmers-day', "Farmers' Day", '🌾', dict(rule='nth_weekday', month=12, weekday=4, nth=1), 7, 0, ['GH'],
     'farm, garden, tool, outdoor, boot, seed, food, grocery'),
    ('christmas', 'Christmas gifts', '🎄', dict(rule='fixed', month=12, day=25), 30, 1, [],
     'christmas, gift, toy, decoration, party, dress, family, festive, light'),
    ('eid-al-fitr', 'Eid al-Fitr', '🌙', dict(rule='dates', dates=[]), 10, 2, [],
     'eid, kaftan, abaya, dress, perfume, gift, family, fragrance'),
]


def seed(apps, schema_editor):
    SeasonalEvent = apps.get_model('product', 'SeasonalEvent')
    Country = apps.get_model('address', 'Country')
    for position, (slug, title, icon, rule, lead, after, countries, keywords) in enumerate(EVENTS):
        event, created = SeasonalEvent.objects.get_or_create(slug=slug, defaults=dict(
            title=title, icon=icon, lead_days=lead, days_after=after, keywords=keywords,
            is_enabled=False, position=position, **rule,
        ))
        if created and countries:
            event.countries.set(Country.objects.filter(code__in=countries))


def unseed(apps, schema_editor):
    apps.get_model('product', 'SeasonalEvent').objects.filter(slug__in=[e[0] for e in EVENTS]).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('product', '0024_holiday_calendar_countries'),
        ('address', '0001_initial'),
    ]

    operations = [migrations.RunPython(seed, unseed)]
