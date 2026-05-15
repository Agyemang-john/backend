from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion
import django.utils.timezone


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0007_promocard_text_color_link_color'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='SearchHistory',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('query', models.CharField(max_length=200)),
                ('searched_at', models.DateTimeField(default=django.utils.timezone.now)),
                ('user', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='search_history',
                    to=settings.AUTH_USER_MODEL,
                )),
            ],
            options={
                'ordering': ['-searched_at'],
            },
        ),
        migrations.AddConstraint(
            model_name='searchhistory',
            constraint=models.UniqueConstraint(fields=['user', 'query'], name='unique_user_query'),
        ),
        migrations.AddIndex(
            model_name='searchhistory',
            index=models.Index(fields=['user', 'searched_at'], name='core_search_user_id_idx'),
        ),
    ]
