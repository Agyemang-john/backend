import os
from pathlib import Path
from decouple import config
import dj_database_url
# Build paths inside the project like this: BASE_DIR / 'subdir'.
BASE_DIR = Path(__file__).resolve().parent.parent

from corsheaders.defaults import default_headers
from datetime import timedelta
# from urllib.parse import urlparse, parse_qsl

# Quick-start development settings - unsuitable for production
# See https://docs.djangoproject.com/en/5.2/howto/deployment/checklist/

# SECURITY WARNING: keep the secret key used in production secret!
SECRET_KEY = 'django-insecure-8gpxy)w^wzbxel%al+0+63j_fr*c@16gf*q_y=#&#m_@4%zv@g'

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
            config('DATABASE_URL'),
            conn_max_age=0,
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
    AWS_STORAGE_BUCKET_NAME = "negromart-storage"
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
        # Browsing (guests)
        "anon": "4000/day",     # enough for product browsing/search
        # Browsing (logged-in)
        "user": "100000/day",     # generous since users are trusted
        "auth_refresh": "30/min",
        "auth_verify": "100/min",
    },
}


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
SESSION_COOKIE_HTTPONLY = False
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

CELERY_ACCEPT_CONTENT = ["json"]
CELERY_TASK_SERIALIZER = "json"
CELERY_TIMEZONE = "UTC"
CELERY_ENABLE_UTC = True
CELERY_BEAT_SCHEDULER = "django_celery_beat.schedulers:DatabaseScheduler"

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
    'REFRESH_TOKEN_LIFETIME': timedelta(days=30),
    'ROTATE_REFRESH_TOKENS': True,
    'SLIDING_TOKEN_LIFETIME': timedelta(days=1),           # access token sliding window
    'SLIDING_TOKEN_REFRESH_LIFETIME': timedelta(days=30),
    'BLACKLIST_AFTER_ROTATION': False,
    'AUTH_HEADER_TYPES': ('Bearer',),
    'TOKEN_USER_CLASS': 'rest_framework_simplejwt.models.TokenUser',  # ← works only if blacklist app is gone
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
    "https://negromart.com",      # frontend domain
    "https://www.negromart.com",
    "https://seller.negromart.com",
    "https://corporate.negromart.com",
    "http://localhost:3000",
    "https://api.negromart.com",
]


CORS_ALLOWED_ORIGINS = [
    "http://localhost:3000",  # Next.js dev
    "http://127.0.0.1:3000",
    "https://negromart.com",
    "https://www.negromart.com",
    "https://seller.negromart.com",
    "https://corporate.negromart.com",
    "https://frontend-sigma-khaki-70.vercel.app",  # Next.js frontend URL
    "https://negromart-space.sfo3.cdn.digitaloceanspaces.com",
    "https://negromart-space.sfo3.digitaloceanspaces.com",
]


CORS_ALLOW_HEADERS = list(default_headers) + [
    "X-Guest-Cart",
    "X-Currency",
    "X-Device",
    "X-Recently-Viewed",
    "X-Recently-Viewed-Vendors",
    "X-Recent-Views",
    "X-SSR-Refresh",
    "X-User-Type",
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
