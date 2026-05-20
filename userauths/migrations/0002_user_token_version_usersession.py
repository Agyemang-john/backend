import uuid
import django.db.models.deletion
import django.utils.timezone
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('userauths', '0001_initial'),
    ]

    operations = [
        migrations.AddField(
            model_name='user',
            name='token_version',
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.CreateModel(
            name='UserSession',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('session_key', models.CharField(db_index=True, max_length=128, unique=True)),
                ('device_type', models.CharField(
                    choices=[('desktop', 'Desktop'), ('mobile', 'Mobile'), ('tablet', 'Tablet'), ('unknown', 'Unknown')],
                    default='unknown',
                    max_length=10,
                )),
                ('device_name', models.CharField(default='Unknown Device', max_length=200)),
                ('browser', models.CharField(blank=True, max_length=100)),
                ('os', models.CharField(blank=True, max_length=100)),
                ('ip_address', models.GenericIPAddressField(blank=True, null=True)),
                ('is_vendor_session', models.BooleanField(default=False)),
                ('last_activity', models.DateTimeField(default=django.utils.timezone.now)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('user', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='sessions',
                    to=settings.AUTH_USER_MODEL,
                )),
            ],
            options={
                'ordering': ['-last_activity'],
            },
        ),
        migrations.AddIndex(
            model_name='usersession',
            index=models.Index(fields=['user', 'is_vendor_session'], name='userauths_u_user_id_idx'),
        ),
    ]
