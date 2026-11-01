"""Invoice numbers come from a Postgres sequence: unique under concurrency, no table lock."""
from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [('invoices', '0001_initial')]
    operations = [
        migrations.RunSQL(
            "CREATE SEQUENCE IF NOT EXISTS invoice_number_seq START 1001",
            "DROP SEQUENCE IF EXISTS invoice_number_seq",
        ),
    ]
