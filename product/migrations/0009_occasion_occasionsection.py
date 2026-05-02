from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('product', '0008_add_collection_model'),
    ]

    operations = [
        migrations.CreateModel(
            name='Occasion',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('title', models.CharField(max_length=200)),
                ('slug', models.SlugField(unique=True)),
                ('subtitle', models.CharField(blank=True, max_length=300,
                                              help_text="Bottom tag-line, e.g. 'Get it all right here'")),
                ('icon', models.CharField(blank=True, max_length=10,
                                          help_text='Optional emoji shown beside the title, e.g. 🌸')),
                ('accent_color', models.CharField(default='#0071CE', max_length=7,
                                                   help_text='Hex color used for hover/accent elements')),
                ('is_active', models.BooleanField(default=True)),
                ('start_date', models.DateField(blank=True, null=True,
                                                help_text='Auto-show from this date (leave blank = always active)')),
                ('end_date', models.DateField(blank=True, null=True,
                                              help_text='Auto-hide after this date (leave blank = no expiry)')),
                ('position', models.PositiveIntegerField(default=0,
                                                          help_text='Lower = shown first on homepage')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
            ],
            options={
                'verbose_name': 'Occasion',
                'verbose_name_plural': 'Occasions',
                'ordering': ['position'],
            },
        ),
        migrations.CreateModel(
            name='OccasionSection',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('title', models.CharField(max_length=200,
                                           help_text="Section heading, e.g. 'Everything Mom wants'")),
                ('position', models.PositiveIntegerField(default=0)),
                ('occasion', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE,
                                               related_name='sections', to='product.occasion')),
                ('collection', models.ForeignKey(
                    blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                    related_name='occasion_sections', to='product.collection',
                    help_text="Products shown in this card and destination of the 'View all' link"
                )),
            ],
            options={
                'verbose_name': 'Occasion Section',
                'verbose_name_plural': 'Occasion Sections',
                'ordering': ['position'],
            },
        ),
    ]
