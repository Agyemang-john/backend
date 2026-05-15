from django.db import migrations, models
import django.db.models.deletion
import django.utils.timezone
from django.conf import settings


class Migration(migrations.Migration):

    dependencies = [
        ('product', '0010_product_cached_rating'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='ProductViewLog',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('visitor_key', models.CharField(max_length=100)),
                ('is_bot', models.BooleanField(default=False)),
                ('is_returning', models.BooleanField(default=False)),
                ('device_type', models.CharField(
                    choices=[('mobile', 'Mobile'), ('tablet', 'Tablet'), ('desktop', 'Desktop'), ('unknown', 'Unknown')],
                    default='unknown',
                    max_length=10,
                )),
                ('date', models.DateField()),
                ('viewed_at', models.DateTimeField(auto_now_add=True)),
                ('product', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='view_logs',
                    to='product.product',
                )),
                ('user', models.ForeignKey(
                    blank=True,
                    null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    to=settings.AUTH_USER_MODEL,
                )),
            ],
            options={
                'ordering': ['-viewed_at'],
                'indexes': [
                    models.Index(fields=['product', 'date'], name='prod_view_log_prod_date_idx'),
                    models.Index(fields=['product', 'is_bot', 'date'], name='prod_view_log_bot_date_idx'),
                    models.Index(fields=['visitor_key', 'product'], name='prod_view_log_visitor_idx'),
                ],
            },
        ),
        migrations.CreateModel(
            name='ProductDailyStats',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('date', models.DateField()),
                ('total_views', models.PositiveIntegerField(default=0)),
                ('unique_views', models.PositiveIntegerField(default=0)),
                ('returning_views', models.PositiveIntegerField(default=0)),
                ('bot_views', models.PositiveIntegerField(default=0)),
                ('product', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='daily_stats',
                    to='product.product',
                )),
            ],
            options={
                'indexes': [
                    models.Index(fields=['product', 'date'], name='prod_daily_stats_prod_date_idx'),
                ],
            },
        ),
        migrations.AddConstraint(
            model_name='productdailystats',
            constraint=models.UniqueConstraint(fields=['product', 'date'], name='unique_product_daily_stats'),
        ),
        migrations.CreateModel(
            name='RecentlyViewedProduct',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('viewed_at', models.DateTimeField(auto_now=True)),
                ('product', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    to='product.product',
                )),
                ('user', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='recently_viewed',
                    to=settings.AUTH_USER_MODEL,
                )),
            ],
            options={
                'ordering': ['-viewed_at'],
                'indexes': [
                    models.Index(fields=['user', 'viewed_at'], name='recently_viewed_user_idx'),
                ],
            },
        ),
        migrations.AddConstraint(
            model_name='recentlyviewedproduct',
            constraint=models.UniqueConstraint(fields=['user', 'product'], name='unique_user_recently_viewed'),
        ),
    ]
