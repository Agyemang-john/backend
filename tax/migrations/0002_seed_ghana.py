"""
Ghana VAT components as of 2026 (VAT Act 2025, effective 1 Jan 2026):
VAT 15% + NHIL 2.5% + GETFund levy 2.5% = 20% of the tax-exclusive value;
the COVID-19 levy was abolished. Editable in the admin — confirm with your accountant.
"""
from django.db import migrations

RATES = [('vat', 'VAT (15%)', '15.00', 10), ('nhil', 'NHIL (2.5%)', '2.50', 20), ('getfund', 'GETFund levy (2.5%)', '2.50', 30)]


def seed(apps, schema_editor):
    TaxRate = apps.get_model('tax', 'TaxRate')
    for code, name, rate, pos in RATES:
        TaxRate.objects.get_or_create(country='GH', code=code, effective_from=None,
                                      defaults={'name': name, 'rate': rate, 'position': pos})


class Migration(migrations.Migration):
    dependencies = [('tax', '0001_initial')]
    operations = [migrations.RunPython(seed, migrations.RunPython.noop)]
