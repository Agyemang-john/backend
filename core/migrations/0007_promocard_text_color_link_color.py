from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0006_add_promo_card'),
    ]

    operations = [
        migrations.AddField(
            model_name='promocard',
            name='text_color',
            field=models.CharField(
                default='#ffffff', max_length=20,
                help_text="CSS color for title/eyebrow/link text (e.g. #ffffff or #1A1A1A)"
            ),
        ),
        migrations.AddField(
            model_name='promocard',
            name='link_color',
            field=models.CharField(
                default='#ffffff', max_length=20,
                help_text="CSS color for the 'Shop now' link text (e.g. #FFC220 or #0071CE)"
            ),
        ),
    ]
