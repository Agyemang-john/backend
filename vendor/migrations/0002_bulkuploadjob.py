import uuid
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("vendor", "0001_initial"),
    ]

    operations = [
        migrations.CreateModel(
            name="BulkUploadJob",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("vendor", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="bulk_upload_jobs", to="vendor.vendor")),
                ("status", models.CharField(
                    choices=[
                        ("queued",     "Queued"),
                        ("processing", "Processing"),
                        ("done",       "Done"),
                        ("failed",     "Failed"),
                    ],
                    default="queued",
                    max_length=20,
                )),
                ("total_rows",          models.PositiveIntegerField(default=0)),
                ("success_count",       models.PositiveIntegerField(default=0)),
                ("failed_count",        models.PositiveIntegerField(default=0)),
                ("created_product_ids", models.JSONField(default=list)),
                ("errors",              models.JSONField(default=list)),
                ("error_message",       models.TextField(blank=True)),
                ("created_at",          models.DateTimeField(auto_now_add=True)),
                ("updated_at",          models.DateTimeField(auto_now=True)),
            ],
            options={
                "ordering": ["-created_at"],
            },
        ),
    ]
