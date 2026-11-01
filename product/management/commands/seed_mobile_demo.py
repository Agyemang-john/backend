"""
Demo catalogue for trying the storefront and mobile app against a local API.

    DB_HOST=localhost python manage.py seed_mobile_demo

Creates (or reuses) a store, departments, products with generated images, a
flash sale, home sliders, promo cards and two accounts. Safe to run again:
everything is looked up by name first. Never run it against production.

Accounts it creates (local test data only):
    shopper  demo.shopper@negromart.test / Demo-Shopper-2026!
    seller   demo.seller@negromart.test  / Demo-Seller-2026!
"""

import io
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.core.files.base import ContentFile
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone
from PIL import Image, ImageDraw, ImageFont

from address.models import Address, Country
from core.models import HomeSlider, PromoCard
from product.models import (
    Brand, Category, Color, DeliveryOption, FlashSale, Main_Category, Product, ProductDeliveryOption,
    Size, Sub_Category, Variants,
)
from product.shipping import seed_countries
from userauths.models import User
from vendor.models import Vendor

SHOPPER = ('demo.shopper@negromart.test', 'Demo-Shopper-2026!', 'Ama', 'Mensah', '0240000101')
SELLER = ('demo.seller@negromart.test', 'Demo-Seller-2026!', 'Kofi', 'Boateng', '0240000102')

DEPARTMENTS = {
    'Fashion': {'Clothing': ['Sneakers', 'T-Shirts'], 'Accessories': ['Bags']},
    'Electronics': {'Phones & Audio': ['Headphones', 'Phone Accessories']},
    'Home': {'Kitchen': ['Cookware']},
}

# title, sub-category, brand, price, old price, colour
PRODUCTS = [
    ('Runner Lite Sneakers', 'Sneakers', 'Stride', 320, 399, '#1F2937'),
    ('Court Classic Sneakers', 'Sneakers', 'Stride', 280, None, '#F5F5F5'),
    ('Trail Grip Sneakers', 'Sneakers', 'Peak', 450, 520, '#2E7D32'),
    ('Everyday Cotton Tee', 'T-Shirts', 'Kente Co', 85, None, '#1565C0'),
    ('Heritage Print Tee', 'T-Shirts', 'Kente Co', 120, 150, '#E65100'),
    ('Canvas Tote Bag', 'Bags', 'Carry', 95, None, '#8D6E63'),
    ('Leather Crossbody Bag', 'Bags', 'Carry', 380, 450, '#4E342E'),
    ('Studio Wireless Headphones', 'Headphones', 'Sonic', 650, 799, '#212121'),
    ('Buds Pro Earphones', 'Headphones', 'Sonic', 299, None, '#ECEFF1'),
    ('Fast Charge 20W Adapter', 'Phone Accessories', 'Volt', 75, 99, '#FFFFFF'),
    ('Braided USB-C Cable', 'Phone Accessories', 'Volt', 35, None, '#37474F'),
    ('Non-Stick Frying Pan', 'Cookware', 'Homely', 160, 199, '#424242'),
    ('Stainless Cooking Pot Set', 'Cookware', 'Homely', 540, None, '#B0BEC5'),
]


def _image(label, color, size=(800, 800)):
    """A flat colour image with the label on it, as a JPEG file."""
    img = Image.new('RGB', size, color)
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype('arial.ttf', size[0] // 14)
    except OSError:
        font = ImageFont.load_default()
    light = sum(int(color[i:i + 2], 16) for i in (1, 3, 5)) > 382
    words, lines = label.split(), ['']
    for word in words:
        lines[-1] = (lines[-1] + ' ' + word).strip()
        if len(lines[-1]) > 14:
            lines.append('')
    text = '\n'.join(l for l in lines if l)
    draw.multiline_text((size[0] / 2, size[1] / 2), text, fill='#111111' if light else '#FFFFFF', font=font, anchor='mm', align='center')
    buf = io.BytesIO()
    img.save(buf, 'JPEG', quality=85)
    return ContentFile(buf.getvalue(), name=f"{label.lower().replace(' ', '-')}.jpg")


def _user(email, password, first, last, phone):
    user = User.objects.filter(email=email).first()
    if not user:
        user = User.objects.create_user(first_name=first, last_name=last, email=email, phone=phone, password=password)
    user.is_active = True
    user.email_verified_at = user.email_verified_at or timezone.now()
    user.save()
    return user


class Command(BaseCommand):
    help = 'Seed a small demo catalogue and test accounts for local development.'

    def handle(self, *args, **options):
        if not settings.DEBUG:
            raise CommandError('Refusing to seed demo data with DEBUG off.')

        seed_countries()
        ghana = Country.objects.filter(name='Ghana').first()

        shopper = _user(*SHOPPER)
        seller = _user(*SELLER)

        vendor = Vendor.objects.filter(user=seller).first()
        if not vendor:
            vendor = Vendor.objects.create(
                user=seller, name='Accra Demo Store', email=SELLER[0], contact=SELLER[4],
                status='VERIFIED', is_approved=True, is_subscribed=True, shipping_from_country=ghana,
            )

        standard, _ = DeliveryOption.objects.get_or_create(
            name='Standard Delivery', defaults={'description': 'Delivered to your door', 'min_days': 2, 'max_days': 5, 'cost': Decimal('15.00')},
        )
        express, _ = DeliveryOption.objects.get_or_create(
            name='Express Delivery', defaults={'description': 'Next-day in Accra and Kumasi', 'min_days': 1, 'max_days': 2, 'cost': Decimal('35.00')},
        )

        subs = {}
        for main_title, categories in DEPARTMENTS.items():
            main, _ = Main_Category.objects.get_or_create(title=main_title)
            for cat_title, sub_titles in categories.items():
                cat = Category.objects.filter(title=cat_title).first()
                if not cat:
                    cat = Category(title=cat_title, main_category=main)
                    cat.image.save(f'{cat_title}.jpg', _image(cat_title, '#ECEFF1'), save=False)
                    cat.main_image.save(f'{cat_title}-main.jpg', _image(cat_title, '#CFD8DC'), save=False)
                    cat.save()
                for sub_title in sub_titles:
                    sub = Sub_Category.objects.filter(title=sub_title).first()
                    if not sub:
                        sub = Sub_Category(title=sub_title, category=cat)
                        sub.image.save(f'{sub_title}.jpg', _image(sub_title, '#E0E0E0'), save=False)
                        sub.save()
                    subs[sub_title] = sub

        made = 0
        for title, sub_title, brand_title, price, old, color in PRODUCTS:
            brand, _ = Brand.objects.get_or_create(title=brand_title)
            if Product.objects.filter(title=title).exists():
                continue
            product = Product(
                title=title, sub_category=subs[sub_title], vendor=vendor, brand=brand, status='published',
                price=Decimal(price), old_price=Decimal(old or price), total_quantity=40,
                description=f'<p>{title} from {brand_title}. Demo product for local testing.</p>',
                features='<ul><li>Quality materials</li><li>Free returns within 30 days</li></ul>',
                specifications='<p>Weight: 1 kg</p>', delivery_returns='<p>Delivered in 2–5 days. Free 30-day returns.</p>',
                return_period_days=30,
            )
            product.image.save(f'{title}.jpg', _image(title, color), save=False)
            product.save()
            if ghana:
                product.available_in_regions.add(ghana)
            ProductDeliveryOption.objects.create(product=product, delivery_option=standard, default=True)
            ProductDeliveryOption.objects.create(product=product, delivery_option=express, default=False)
            made += 1

        # One product with sizes and colours, to try the variant pickers.
        if not Product.objects.filter(title='Club Hoodie').exists():
            hoodie = Product(
                title='Club Hoodie', sub_category=subs['T-Shirts'], vendor=vendor, brand=Brand.objects.get(title='Kente Co'),
                status='published', variant='Size-Color', price=Decimal('210'), old_price=Decimal('260'), total_quantity=60,
                description='<p>Soft fleece hoodie. Demo product with size and colour options.</p>', return_period_days=30,
            )
            hoodie.image.save('club-hoodie.jpg', _image('Club Hoodie', '#263238'), save=False)
            hoodie.save()
            if ghana:
                hoodie.available_in_regions.add(ghana)
            ProductDeliveryOption.objects.create(product=hoodie, delivery_option=standard, default=True)
            for color_name, code in (('Black', '#212121'), ('Red', '#C62828')):
                c, _ = Color.objects.get_or_create(name=color_name, defaults={'code': code})
                for size_name in ('S', 'M', 'L'):
                    s, _ = Size.objects.get_or_create(name=size_name)
                    v = Variants(product=hoodie, color=c, size=s, quantity=10, price=Decimal('210'), title=f'{color_name} / {size_name}')
                    v.image.save(f'hoodie-{color_name}.jpg', _image(f'Hoodie {color_name}', code), save=False)
                    v.save()
            made += 1

        now = timezone.now()
        for title, sale in (('Studio Wireless Headphones', 499), ('Trail Grip Sneakers', 360)):
            product = Product.objects.get(title=title)
            if not FlashSale.objects.filter(product=product, end_time__gt=now).exists():
                FlashSale.objects.create(
                    product=product, sale_price=Decimal(sale), original_price=product.price,
                    start_time=now - timedelta(hours=1), end_time=now + timedelta(hours=20),
                    max_quantity=25, created_by=vendor,
                )

        if not HomeSlider.objects.exists():
            for i, (title, sub, color) in enumerate((('New season sneakers', 'sneakers', '#37474F'), ('Sound that moves you', 'headphones', '#0D47A1'))):
                slide = HomeSlider(title=title, subtitle='Demo collection', link_url=f'/category/{sub}', cta_label='Shop now', order=i)
                slide.image_mobile.save(f'slide-{i}.jpg', _image(' ', color, (900, 500)), save=False)
                slide.image_desktop.save(f'slide-{i}-d.jpg', _image(' ', color, (1600, 500)), save=False)
                slide.save()

        if not PromoCard.objects.exists():
            for i, (title, link, color, tall) in enumerate((
                ('Bags for every day', '/category/bags', '#6D4C41', False),
                ('Kitchen upgrades', '/category/cookware', '#455A64', False),
                ('Flash deals on audio', '/flash-sales', '#1A1A2E', True),
            )):
                card = PromoCard(title=title, link_url=link, card_color='dark' if tall else 'gray', is_tall=tall, position=i,
                                 badge_text='Hot' if tall else '', eyebrow='Demo')
                card.image.save(f'promo-{i}.jpg', _image(' ', color, (600, 800 if tall else 400)), save=False)
                card.save()

        if not Address.objects.filter(user=shopper).exists():
            Address.objects.create(
                user=shopper, full_name='Ama Mensah', country='Ghana', region='Greater Accra', town='Accra',
                address='12 Oxford Street, Osu', mobile='+233240000101', email=SHOPPER[0], status=True,
            )

        self.stdout.write(self.style.SUCCESS(
            f'Demo data ready: {made} new products, store "{vendor.name}". '
            f'Shopper: {SHOPPER[0]} (password in this command\'s docstring).'
        ))
