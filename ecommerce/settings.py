import os
from pathlib import Path
from decouple import config
import dj_database_url
# Build paths inside the project like this: BASE_DIR / 'subdir'.
BASE_DIR = Path(__file__).resolve().parent.parent

from corsheaders.defaults import default_headers
from datetime import timedelta
from celery.schedules import crontab
# from urllib.parse import urlparse, parse_qsl

# Quick-start development settings - unsuitable for production
# See https://docs.djangoproject.com/en/5.2/howto/deployment/checklist/

# SECURITY WARNING: keep the secret key used in production secret!
SECRET_KEY = config('SECRET_KEY')

# SECURITY WARNING: don't run with debug turned on in production!
DEBUG = config("DEBUG", default=False, cast=bool)

ALLOWED_HOSTS = config(
    "ALLOWED_HOSTS",
    default="127.0.0.1,localhost"
).split(",")
DEVELOPMENT_MODE = config("DEVELOPMENT_MODE")
ENV = config("DJANGO_ENV", "development")  # "development" or "production"


# Application definition

INSTALLED_APPS = [
    'jazzmin',
    'channels',
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',

    'storages',
    'corsheaders',
    'django_countries',
    'rest_framework',
    'rest_framework_simplejwt.token_blacklist',
    'django_filters',
    "djoser",
    'social_django',
    'core',
    'userauths',
    'product',
    'vendor',
    'order',
    'address',
    'customer',
    'payments',
    'newsletter',
    'django_ckeditor_5',
    'django_celery_beat',
    'notification',
    'recommendation',
]

MIDDLEWARE = [
    'corsheaders.middleware.CorsMiddleware',
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
    'vendor.middleware.VendorActivityMiddleware',
]

ROOT_URLCONF = 'ecommerce.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [os.path.join(BASE_DIR, "templates")],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'ecommerce.wsgi.application'
ASGI_APPLICATION = "ecommerce.asgi.application"

# Database
# https://docs.djangoproject.com/en/5.2/ref/settings/#databases
FERNET_KEY = config("FERNET_KEY")

if DEBUG:
    DATABASES = {
        'default': {
            'ENGINE': 'django.db.backends.postgresql',
            'NAME': config('DB_NAME'),
            'USER': config('DB_USER'),
            'PASSWORD': config('DB_PASSWORD'),
            'HOST': config('DB_HOST', default='localhost'),
            'PORT': config('DB_PORT'),
        }
    }
else:
    DATABASES = {
        'default': dj_database_url.parse(
            config('DATABASE_POOL_URL'),
            conn_max_age=600,
            ssl_require=True,
            conn_health_checks=True,
        )
    }

# Password validation
# https://docs.djangoproject.com/en/5.2/ref/settings/#auth-password-validators

AUTH_PASSWORD_VALIDATORS = [
    {
        'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator',
    },
]


# Internationalization
# https://docs.djangoproject.com/en/5.2/topics/i18n/

LANGUAGE_CODE = 'en-us'

TIME_ZONE = 'UTC'

USE_I18N = True

USE_TZ = True


# Static files (CSS, JavaScript, Images)
# https://docs.djangoproject.com/en/5.2/howto/static-files/

# Media and Static settings
STATIC_URL = '/static/'

# Tell Django where to look for static files during development
STATICFILES_DIRS = [
    os.path.join(BASE_DIR, "static"),
]

# Directory where static files will be collected for production
STATIC_ROOT = os.path.join(BASE_DIR, "staticfiles")

# Media (user uploads, not static assets)
MEDIA_URL = '/media/'
MEDIA_ROOT = os.path.join(BASE_DIR, 'media')

if ENV == 'production':
    AWS_ACCESS_KEY_ID = config("AWS_ACCESS_KEY_ID")
    AWS_SECRET_ACCESS_KEY = config("AWS_SECRET_ACCESS_KEY")
    AWS_STORAGE_BUCKET_NAME = "negromart-spaces"
    AWS_S3_REGION_NAME = "nyc3"
    AWS_S3_ENDPOINT_URL = f"https://{AWS_S3_REGION_NAME}.digitaloceanspaces.com"
    AWS_S3_CUSTOM_DOMAIN = f"{AWS_STORAGE_BUCKET_NAME}.{AWS_S3_REGION_NAME}.cdn.digitaloceanspaces.com"
    AWS_DEFAULT_ACL = "public-read"
    AWS_QUERYSTRING_AUTH = False
    AWS_S3_FILE_OVERWRITE = False
    AWS_S3_OBJECT_PARAMETERS = {
        'CacheControl': 'max-age=86400'
    }
    # Static and media files in Spaces
    STORAGES = {
        "default": {
            "BACKEND": "storages.backends.s3boto3.S3Boto3Storage",
            "OPTIONS": {
                "location": "media",  # Media files in media/ directory
            },
        },
        "staticfiles": {
            "BACKEND": "storages.backends.s3boto3.S3Boto3Storage",
            "OPTIONS": {
                "location": "static",  # Static files in static/ directory
            },
        },
    }
    STATIC_URL = f"https://{AWS_S3_CUSTOM_DOMAIN}/static/"
else:
    STORAGES = {
        "default": {
            "BACKEND": "django.core.files.storage.FileSystemStorage",
        },
        "staticfiles": {
            "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage",
        },
    }


# Default primary key field type
# https://docs.djangoproject.com/en/5.2/ref/settings/#default-auto-field

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

CKEDITOR_BASEPATH = 'uploads/'


# Authentication Backends
AUTHENTICATION_BACKENDS = [
    'userauths.backends.EmailOrPhoneBackend',
    'django.contrib.auth.backends.ModelBackend',
]

AUTH_USER_MODEL = 'userauths.User'

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "userauths.authentication.CustomJWTAuthentication",
    ],

    "DEFAULT_THROTTLE_CLASSES": [
        "rest_framework.throttling.AnonRateThrottle",
        "rest_framework.throttling.UserRateThrottle",
    ],

    "DEFAULT_THROTTLE_RATES": {
        "anon": "4000/day",
        "user": "100000/day",
        "login": "5/min",
        "anon_login": "10/min",
        "register": "5/hour",
        "auth_refresh": "30/min",
        "auth_verify": "100/min",
        "vendor_heartbeat": "30/hour",
    },
}


# Cloudflare Turnstile CAPTCHA
TURNSTILE_SECRET_KEY = config('TURNSTILE_SECRET_KEY', default='')
TURNSTILE_SITE_KEY = config('TURNSTILE_SITE_KEY', default='')

#Paystack configuration
PAYSTACK_SECRET_KEY = config('PAYSTACK_SECRET_KEY')
PAYSTACK_PUBLIC_KEY = config('PAYSTACK_PUBLIC_KEY')

# DJOSER CONFIGURATION
SITE_NAME = "Negromart"
DOMAIN = config('DOMAIN')
FRONTEND_LOGIN_URL = config("FRONTEND_LOGIN_URL")

# Emailing settings
SITE_URL = config('FRONTEND_BASE_URL')   # set correctly in each environment

EMAIL_TIMEOUT = 30  # seconds
EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"
EMAIL_HOST = "smtp.sendgrid.net"
EMAIL_PORT = 2525   #2525
EMAIL_USE_TLS = True
EMAIL_HOST_USER = "apikey"   # keep this literal
EMAIL_HOST_PASSWORD = config('SENDGRID_API_KEY')
DEFAULT_FROM_EMAIL = "Negromart <no-reply@negromart.com>"


DJOSER = {
    'TOKEN_SERIALIZER': 'userauths.serializers.CustomTokenObtainPairSerializer',
    'PASSWORD_RESET_CONFIRM_URL': 'auth/password-reset/{uid}/{token}',
    'SEND_ACTIVATION_EMAIL': True,
    'ACTIVATION_URL': 'auth/activation/{uid}/{token}',
    'USER_CREATE_PASSWORD_RETYPE': False,
    'PASSWORD_RESET_CONFIRM_RETYPE': True,
    'TOKEN_MODEL': None,
    # 'SERIALIZERS': {
    #     'activation': 'djoser.serializers.ActivationSerializer',
    #     'resend_activation': 'djoser.serializers.SendEmailResetSerializer',
    # },
}

REDIS_URL = config("REDIS_URL")                          # Required in all envs
REDIS_RESULT_URL = config("REDIS_RESULT_URL", default=REDIS_URL.replace("/0", "/1"))

# Cache (Redis)
CACHES = {
    "default": {
        "BACKEND": "django_redis.cache.RedisCache",
        "LOCATION": REDIS_URL,
        "OPTIONS": {
            "CLIENT_CLASS": "django_redis.client.DefaultClient",
            "MAX_CONNECTIONS": 30,
            "IGNORE_EXCEPTIONS": True,
            "CONNECTION_POOL_KWARGS": {"retry_on_timeout": True},
        },
    }
}

# SESSION CONFIGURATION
SESSION_COOKIE_AGE = 60 * 60 * 24 * 60 # 60 days in seconds
SESSION_SAVE_EVERY_REQUEST = True
SESSION_EXPIRE_AT_BROWSER_CLOSE = False
SESSION_COOKIE_SAMESITE = 'Lax' if DEBUG else 'None'
SESSION_COOKIE_SECURE = False if DEBUG else True
SESSION_COOKIE_NAME = "sessionid"
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_DOMAIN = ".negromart.com" if not DEBUG else None

# Sessions in Redis (fast + shared between workers)
SESSION_ENGINE = "django.contrib.sessions.backends.cache"
SESSION_CACHE_ALIAS = "default"

# Celery
CELERY_BROKER_URL = REDIS_URL
CELERY_RESULT_BACKEND = REDIS_RESULT_URL

REDIS_CHANNELS_URL = config("REDIS_CHANNELS_URL", default=REDIS_URL.replace("/0", "/2"))
CHANNEL_LAYERS = {
    "default": {
        "BACKEND": "channels_redis.core.RedisChannelLayer",
        "CONFIG": {
            "hosts": [REDIS_CHANNELS_URL],
            "capacity": 1500,        # handles 1000+ concurrent users easily
            "expiry": 10,
        },
        "OPTIONS": {
            "require_valid_group_name": True,
            "require_valid_channel_name": True,
        },
    },
}

# settings.py
RECENTLY_VIEWED_MAX = 10        # how many IDs to keep per user/session
VIEW_DEDUP_TTL      = 86400     # seconds before same user can re-count a view (24h)
RECENT_LIST_TTL     = 2592000   # seconds before the recent list expires (30 days)
RETURN_WINDOW_TTL   = 2592000   # seconds for returning visitor detection window (30 days)

# Vendor inactivity settings
# Auto-close a shop after this many days without any API activity or login.
VENDOR_INACTIVITY_DAYS = int(config("VENDOR_INACTIVITY_DAYS", default=30))
# Send warnings this many days BEFORE the auto-close threshold.
# [7, 3] means: warn at day 23 (7 days left) and day 27 (3 days left).
VENDOR_INACTIVITY_WARN_DAYS = [7, 3]

CELERY_ACCEPT_CONTENT = ["json"]
CELERY_TASK_SERIALIZER = "json"
CELERY_TIMEZONE = "UTC"
CELERY_ENABLE_UTC = True
CELERY_BEAT_SCHEDULER = "django_celery_beat.schedulers:DatabaseScheduler"

CELERY_BEAT_SCHEDULE = {
    # Purge expired / used OTP records every 30 minutes
    "cleanup-expired-otps": {
        "task": "userauths.tasks.cleanup_expired_otps",
        "schedule": 1800,  # 30 minutes
    },
    # Flush Redis view-count buffers → DB every 3 minutes
    "flush-view-counts": {
        "task": "product.tasks.flush_view_counts",
        "schedule": 180,
    },
    # Recalculate trending scores every 4 hours (multi-signal bulk scoring)
    "update-trending-scores": {
        "task": "product.tasks.update_trending_scores",
        "schedule": 14400,  # 4 hours
        "options": {"expires": 13000},  # drop the task if the next one fires before this runs
    },
    # Recalculate category engagement hourly
    "update-category-engagement": {
        "task": "product.tasks.update_category_engagement_scores",
        "schedule": 3600,
    },
    # Recalculate brand engagement hourly
    "update-brand-engagement": {
        "task": "product.tasks.update_brand_engagement_scores",
        "schedule": 3600,
    },
    # Expire flash sales every 60 seconds
    "expire-flash-sales": {
        "task": "product.tasks.expire_flash_sales",
        "schedule": 60,
    },
    # Aggregate yesterday's view logs into daily stats (runs at midnight UTC)
    "aggregate-daily-view-stats": {
        "task": "product.tasks.aggregate_daily_stats",
        "schedule": crontab(hour=0, minute=5),
    },
    # Flush Redis vendor view-count buffers → DB every 3 minutes
    "flush-vendor-view-counts": {
        "task": "vendor.tasks.flush_vendor_view_counts",
        "schedule": 180,
    },
    # Flush Redis brand view-count buffers → DB every 3 minutes
    "flush-brand-view-counts": {
        "task": "product.tasks.flush_brand_view_counts",
        "schedule": 180,
    },
    # Flush Redis subcategory view-count buffers → DB every 3 minutes
    "flush-subcategory-view-counts": {
        "task": "product.tasks.flush_subcategory_view_counts",
        "schedule": 180,
    },
    # Flush Redis category view-count buffers → DB every 3 minutes
    "flush-category-view-counts": {
        "task": "product.tasks.flush_category_view_counts",
        "schedule": 180,
    },
    # Aggregate yesterday's vendor view logs into daily stats
    "aggregate-vendor-daily-view-stats": {
        "task": "vendor.tasks.aggregate_vendor_daily_stats",
        "schedule": crontab(hour=0, minute=10),
    },

    # Recalculate subcategory engagement hourly (same cadence as category + brand)
    "update-subcategory-engagement": {
        "task": "product.tasks.update_subcategory_engagement_scores",
        "schedule": 3600,  # 1 hour
    },

    # Re-index all published products into Elasticsearch every 4 hours.
    # Keeps search results fresh without hammering ES on every product save.
    # Drop the task if the previous run is still going (expires < schedule).
    # "index-products": {
    #     "task": "product.tasks.index_products_task",
    #     "schedule": 14400,  # 4 hours
    #     "options": {"expires": 13000},
    # },

    # ── Recommender ───────────────────────────────────────────────────────────
    # Full retrain: ALS collaborative factors, TF-IDF/SVD content embeddings,
    # blended item-item neighbours, and the per-shopper "Recommended for you"
    # rails. Nightly at 02:30 UTC — taste shifts over days, and off-peak keeps
    # the CPU spike away from shoppers. Skips itself if a run is still going.
    "train-recommender": {
        "task": "recommendation.tasks.train_recommender",
        "schedule": crontab(hour=2, minute=30),
        "options": {"expires": 7200},
    },
    # Rescore "Today's Deals" hourly: snapshots today's prices (which is what
    # makes the anti-inflation check possible) then reranks every deal.
    "score-deals": {
        "task": "recommendation.tasks.score_deals",
        "schedule": 3600,
        "options": {"expires": 3000},
    },
    # Log click-through and add-to-cart rate per rail, weekly.
    "recommendation-surface-report": {
        "task": "recommendation.tasks.report_surface_performance",
        "schedule": crontab(day_of_week="monday", hour=6, minute=0),
    },
    # Drop recommendation events older than 90 days and price history over a year.
    "prune-recommendation-events": {
        "task": "recommendation.tasks.prune_recommendation_events",
        "schedule": crontab(day_of_week="sunday", hour=3, minute=30),
    },

    # Delete media files that are no longer referenced by any DB record.
    # Runs once a week (Sunday at 02:00 UTC) to keep storage costs down.
    "cleanup-orphaned-files": {
        "task": "core.tasks.cleanup_orphaned_files_task",
        "schedule": crontab(day_of_week="sunday", hour=2, minute=0),
    },

    # Pay out all vendors with verified MoMo accounts for delivered orders.
    # Runs every 2 days at 03:00 UTC. Only orders not yet paid out are included.
    # "batch-payouts": {
    #     "task": "payments.tasks.batch_payouts",
    #     "schedule": 172800,  # 2 days in seconds (172800 = 2 × 24 × 3600)
    # },

    # ── Vendor subscription lifecycle tasks ───────────────────────────────────
    # These three tasks run on a crontab schedule defined in the
    # SubscriptionEmailConfig singleton (admin → Payments → Email & Schedule Config).
    # When an admin saves that config, _update_periodic_tasks() overwrites the
    # django-celery-beat PeriodicTask rows in the DB — so the new times take
    # effect immediately without touching settings.py or restarting workers.
    #
    # The times below are the DEFAULTS (UTC) — they must match the defaults
    # in SubscriptionEmailConfig so the initial DB entry is correct on first run.
    # The key names must be IDENTICAL to the 'name' field used in
    # _update_periodic_tasks() so update_or_create() finds the right row.

    # Queues a charge_vendor_for_renewal task for every active auto-renewing
    # subscription whose end_date falls renewal_advance_days from now (default 1 day).
    # Runs daily at 08:00 UTC (matches SubscriptionEmailConfig.run_renewals_hour default).
    "subscriptions.process_renewals": {
        "task": "subscriptions.process_renewals",
        "schedule": crontab(hour=8, minute=0),
    },

    # Sends expiry warning emails + SMS to vendors whose subscription is
    # ending in expiry_warning_days (default 7) or second_warning_days (default 3).
    # auto_renew=OFF → urgent "please renew" message.
    # auto_renew=ON  → informational "you'll be charged soon" heads-up.
    # Runs daily at 09:00 UTC (matches SubscriptionEmailConfig.run_expiry_check_hour default).
    "subscriptions.warn_expiring_soon": {
        "task": "subscriptions.warn_expiring_soon",
        "schedule": crontab(hour=9, minute=0),
    },

    # Safety net: finds any active subscription whose end_date has already
    # passed (charge failed and retries exhausted), marks it expired, downgrades
    # the vendor to Free, and fires the expired email + SMS.
    # Runs daily at 00:30 UTC (matches SubscriptionEmailConfig.run_expire_old_hour default).
    "subscriptions.expire_old_subscriptions": {
        "task": "subscriptions.expire_old_subscriptions",
        "schedule": crontab(hour=0, minute=30),
    },

    # ── Vendor activity / inactivity tasks ───────────────────────────────────
    # Flush Redis `vendor:last_seen:{id}` keys → Vendor.last_seen_at every 5 min.
    "flush-vendor-last-seen": {
        "task": "vendor.tasks.flush_vendor_last_seen",
        "schedule": 300,
    },
    # Check for inactive vendors daily at 01:00 UTC; warn, then auto-close.
    "check-inactive-vendors": {
        "task": "vendor.tasks.check_inactive_vendors",
        "schedule": crontab(hour=1, minute=0),
    },
}

#SIMPLE JWT CONFIGURATION
AUTH_COOKIE = 'access'
AUTH_ACCESS_MAX_AGE = timedelta(hours=1).total_seconds()
AUTH_REFRESH_MAX_AGE = timedelta(days=30).total_seconds()
AUTH_COOKIE_SECURE = False if DEBUG else True 
AUTH_COOKIE_HTTP_ONLY = True
AUTH_COOKIE_PATH = '/'
AUTH_COOKIE_SAMESITE = "Lax" if DEBUG else "None"
AUTH_COOKIE_DOMAIN = None
if not DEBUG:
    AUTH_COOKIE_DOMAIN = ".negromart.com"

# VENDOR SIMPLE JWT CONFIGURATION
VENDOR_ACCESS_AUTH_COOKIE = 'vendor_access'
VENDOR_REFRESH_AUTH_COOKIE = 'vendor_refresh'
VENDOR_AUTH_ACCESS_MAX_AGE = timedelta(hours=1).total_seconds()
VENDOR_AUTH_REFRESH_MAX_AGE = timedelta(days=12).total_seconds()
VENDOR_AUTH_COOKIE_SECURE = False if DEBUG else True 
VENDOR_AUTH_COOKIE_HTTP_ONLY = True
VENDOR_AUTH_COOKIE_PATH = '/'
VENDOR_AUTH_COOKIE_SAMESITE = "Lax" if DEBUG else "None"
VENDOR_AUTH_COOKIE_DOMAIN = None
if not DEBUG:
    VENDOR_AUTH_COOKIE_DOMAIN = ".negromart.com"


from datetime import timedelta

SIMPLE_JWT = {
    'ACCESS_TOKEN_LIFETIME': timedelta(hours=1),
    'REFRESH_TOKEN_LIFETIME': timedelta(days=7),
    'ROTATE_REFRESH_TOKENS': True,
    'BLACKLIST_AFTER_ROTATION': True,
    'AUTH_HEADER_TYPES': ('Bearer',),
    'JTI_CLAIM': 'jti',
    'ALGORITHM': 'HS256',
}

# GeoIP

# CORS
CORS_ALLOW_METHODS = [
    'GET',
    'POST',
    'PUT',
    'PATCH',
    'DELETE',
    'OPTIONS',
]
CORS_ALLOW_CREDENTIALS = True

CSRF_TRUSTED_ORIGINS = [
    "https://negromart.com",
    "https://www.negromart.com",
    "https://seller.negromart.com",
    "https://corporate.negromart.com",
    "https://api.negromart.com",
]

if DEBUG:
    CSRF_TRUSTED_ORIGINS += [
        "http://localhost:3000",
        "http://127.0.0.1:3000",
    ]


CORS_ALLOWED_ORIGINS = [
    "https://negromart.com",
    "https://www.negromart.com",
    "https://seller.negromart.com",
    "https://corporate.negromart.com",
    "https://negromart-space.sfo3.cdn.digitaloceanspaces.com",
    "https://negromart-space.sfo3.digitaloceanspaces.com",
]

if DEBUG:
    # Development-only origins — never active in production
    CORS_ALLOWED_ORIGINS += [
        "http://localhost:3000",
        "http://127.0.0.1:3000",
        "http://172.22.176.1:8000",
        "http://localhost:8082",
        "exp://10.142.141.54:8082",
        "https://frontend-sigma-khaki-70.vercel.app",  # remove once a stable staging URL exists
    ]


CORS_ALLOW_HEADERS = list(default_headers) + [
    "X-Visitor-ID",
    "X-Guest-Cart",
    "X-Currency",
    "X-Device",
    "X-Search-History",
    "X-Recently-Viewed",
    "X-Recently-Viewed-Vendors",
    "X-Recent-Views",
    "X-SSR-Refresh",
    "X-User-Type",
    "X-Client-IP",
    "cache-control",
    'accept',
    'accept-encoding',
    'authorization',
    'content-type',
    'dnt',
    'origin',
    'user-agent',
    'x-csrftoken',
    'x-requested-with',
]

EXCHANGE_RATE_API_KEY = config('EXCHANGE_RATE_API_KEY')

DHL_API_KEY = config('DHL_API_KEY')
DHL_ACCOUNT_NUMBER = config('DHL_ACCOUNT_NUMBER')
DHL_API_SECRET = config('DHL_API_SECRET')
DHL_API_URL = config('DHL_API_URL')
# HyperVerge Configuration
HYPERVERGE_APP_ID = 'your_app_id_here'  # From HyperVerge dashboard
HYPERVERGE_APP_KEY = 'your_app_key_here'  # From dashboard (keep secret!)
HYPERVERGE_BASE_URL = 'https://global-api.hyperverge.co/v2/'  # Confirm in dashboard; use /ind-docs/ for India-specific if needed
HYPERVERGE_WORKFLOW_ID = 'kyc_full'  # Optional; e.g., for full ID + liveness workflow
# CKEDITOR CONFIGURATION

ELASTICSEARCH_URL = config("ELASTICSEARCH_URL", default="http://elasticsearch:9200")
# ELASTICSEARCH_USER = config("ELASTICSEARCH_USER", default="")
# ELASTICSEARCH_PASSWORD = config("ELASTICSEARCH_PASSWORD", default="")

LOCATIONIQ_API_KEY = config('LOCATIONIQ_API_KEY')


ARKESEL_API_KEY = config('ARKESEL_API_KEY')
ARKESEL_SENDER = 'Negromart'  # Your sender ID

customColorPalette = [
    {
        'color': 'hsl(4, 90%, 58%)',
        'label': 'Red'
    },
    {
        'color': 'hsl(340, 82%, 52%)',
        'label': 'Pink'
    },
    {
        'color': 'hsl(291, 64%, 42%)',
        'label': 'Purple'
    },
    {
        'color': 'hsl(262, 52%, 47%)',
        'label': 'Deep Purple'
    },
    {
        'color': 'hsl(231, 48%, 48%)',
        'label': 'Indigo'
    },
    {
        'color': 'hsl(207, 90%, 54%)',
        'label': 'Blue'
    },
]

CKEDITOR_5_CONFIGS = {
    'default': {
        'toolbar': {
            'items': [
                'heading', '|',
                'bold', 'italic', 'underline', 'strikethrough', 'highlight', '|',
                'link', 'bulletedList', 'numberedList', 'todoList', '|',
                'outdent', 'indent', '|',
                'blockQuote', '|',
                'insertTable', 'imageUpload', 'mediaEmbed', 'codeBlock', '|',
                'fontFamily', 'fontSize', 'fontColor', 'fontBackgroundColor', '|',
                'removeFormat', 'sourceEditing'
            ],
            'shouldNotGroupWhenFull': True
        },

    },
    'extends': {
        'blockToolbar': [
            'paragraph', 'heading1', 'heading2', 'heading3',
            '|',
            'bulletedList', 'numberedList',
            '|',
            'blockQuote',
        ],
        'toolbar': {
            'items': ['heading', '|', 'outdent', 'indent', '|', 'bold', 'italic', 'link', 'underline', 'strikethrough',
                      'code','subscript', 'superscript', 'highlight', '|', 'codeBlock', 'sourceEditing', 'insertImage',
                    'bulletedList', 'numberedList', 'todoList', '|',  'blockQuote', 'imageUpload', '|',
                    'fontSize', 'fontFamily', 'fontColor', 'fontBackgroundColor', 'mediaEmbed', 'removeFormat',
                    'insertTable',
                    ],
            'shouldNotGroupWhenFull': 'true'
        },
        'image': {
            'toolbar': ['imageTextAlternative', '|', 'imageStyle:alignLeft',
                        'imageStyle:alignRight', 'imageStyle:alignCenter', 'imageStyle:side',  '|'],
            'styles': [
                'full',
                'side',
                'alignLeft',
                'alignRight',
                'alignCenter',
            ]

        },
        'table': {
            'contentToolbar': [ 'tableColumn', 'tableRow', 'mergeTableCells',
            'tableProperties', 'tableCellProperties' ],
            'tableProperties': {
                'borderColors': customColorPalette,
                'backgroundColors': customColorPalette
            },
            'tableCellProperties': {
                'borderColors': customColorPalette,
                'backgroundColors': customColorPalette
            }
        },
        'heading' : {
            'options': [
                { 'model': 'paragraph', 'title': 'Paragraph', 'class': 'ck-heading_paragraph' },
                { 'model': 'heading1', 'view': 'h1', 'title': 'Heading 1', 'class': 'ck-heading_heading1' },
                { 'model': 'heading2', 'view': 'h2', 'title': 'Heading 2', 'class': 'ck-heading_heading2' },
                { 'model': 'heading3', 'view': 'h3', 'title': 'Heading 3', 'class': 'ck-heading_heading3' }
            ]
        }
    },
    'list': {
        'properties': {
            'styles': 'true',
            'startIndex': 'true',
            'reversed': 'true',
        }
    },
    'fontFamily': {
        'options': [
            'default',
            'Arial, Helvetica, sans-serif',
            'Times New Roman, Times, serif',
            'Courier New, Courier, monospace'
        ]
    },
    'fontSize': {
        'options': [9, 11, 13, 15, 17, 19, 21, 24, 28, 32, 36],
        'supportAllValues': True
    },
    'link': {
        'decorators': {
            'addTargetToExternalLinks': {
                'mode': 'automatic',
                'callback': lambda url: url.startswith('http'),
                'attributes': {'target': '_blank', 'rel': 'noopener noreferrer'}
            }
        }
    },
    'mediaEmbed': {'previewsInData': True}
}
Ckeditor5_filetype_whitelist = [
    "image/jpeg", 
    "image/png", 
    "image/jpg", 
    "image/gif", 
    "image/bmp", 
    "image/webp", 
    "video/mp4", 
    "video/webm", 
    "video/ogg", 
    "audio/mpeg", 
    "audio/ogg", 
    "audio/wav", 
    "audio/webm",
    "application/pdf",
    "text/plain",
    "application/msword",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.ms-excel",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
]


# django-ipware: header precedence for extracting the client IP.
# CF-Connecting-IP is checked first (Cloudflare), then standard proxy headers.
IPWARE_META_PRECEDENCE_ORDER = (
    'HTTP_CF_CONNECTING_IP',    # Cloudflare (most reliable if using CF)
    'HTTP_X_FORWARDED_FOR',     # Standard proxy header (Nginx, LBs)
    'HTTP_X_REAL_IP',           # Nginx's X-Real-IP
    'REMOTE_ADDR',              # Direct connection (last resort)
)

# Trust Docker-internal IPs as proxies so ipware reads X-Forwarded-For.
# In Docker, Nginx talks to Django via the bridge network (172.x.x.x),
# NOT 127.0.0.1 — so we must trust the entire private range.
IPWARE_TRUSTED_PROXY_LIST = [
    '127.0.0.1',
    '10.0.0.0/8',        # Docker overlay / DigitalOcean internal
    '172.16.0.0/12',     # Docker bridge networks (172.16–31.x.x)
    '192.168.0.0/16',    # Other private ranges
]

# Number of proxies between the client and Django.
# 0 = best-effort (ipware picks the leftmost public IP from X-Forwarded-For).
# Set to 1 ONLY if you have exactly one proxy (Nginx) AND no upstream LB/CDN.
# 0 is safer because it works whether or not there's a CDN in front.
IPWARE_TRUSTED_PROXY_COUNT = 0

SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')
USE_X_FORWARDED_HOST = True
USE_X_FORWARDED_PORT = True

# ── Security headers (production only) ────────────────────────────────────────
if not DEBUG:
    SECURE_SSL_REDIRECT = True
    SECURE_HSTS_SECONDS = 31536000          # 1 year
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_HSTS_PRELOAD = True
    SECURE_CONTENT_TYPE_NOSNIFF = True
    SECURE_BROWSER_XSS_FILTER = True

X_FRAME_OPTIONS = 'DENY'
