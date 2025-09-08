import os
from pathlib import Path
from decouple import config

# Build paths inside the project like this: BASE_DIR / 'subdir'.
BASE_DIR = Path(__file__).resolve().parent.parent

from corsheaders.defaults import default_headers
from datetime import timedelta
from urllib.parse import urlparse, parse_qsl

# Quick-start development settings - unsuitable for production
# See https://docs.djangoproject.com/en/5.2/howto/deployment/checklist/

# SECURITY WARNING: keep the secret key used in production secret!
SECRET_KEY = 'django-insecure-8gpxy)w^wzbxel%al+0+63j_fr*c@16gf*q_y=#&#m_@4%zv@g'

# SECURITY WARNING: don't run with debug turned on in production!
DEBUG = config("DEBUG", default=False, cast=bool)

ALLOWED_HOSTS = config("ALLOWED_HOSTS").split(",")
DEVELOPMENT_MODE = config("DEVELOPMENT_MODE")
ENV = config("DJANGO_ENV", "development")  # "development" or "production"


# Application definition

INSTALLED_APPS = [
    'jazzmin',
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',

    'storages',
    'corsheaders',
    'rest_framework',
    "djoser",
    'social_django',
    'core',
    'rest_framework_simplejwt.token_blacklist',
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
]

MIDDLEWARE = [
    'corsheaders.middleware.CorsMiddleware',
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'core.middleware.CurrencyMiddleware',
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


# Database
# https://docs.djangoproject.com/en/5.2/ref/settings/#databases

tmpPostgres = urlparse(config("DATABASE_URL"))

if DEBUG:
    DATABASES = {
        'default': {
            'ENGINE': 'django.db.backends.sqlite3',
            'NAME': BASE_DIR / 'db.sqlite3',
        }
    }
else:
    DATABASES = {
        'default': {
            'ENGINE': 'django.db.backends.postgresql',
            'NAME': tmpPostgres.path.replace('/', ''),
            'USER': tmpPostgres.username,
            'PASSWORD': tmpPostgres.password,
            'HOST': tmpPostgres.hostname,
            'PORT': 5432,
            'CONN_MAX_AGE': 60,  # keep connections open
            'OPTIONS': {
                **dict(parse_qsl(tmpPostgres.query)),
                'connect_timeout': 10,  # fail fast if DNS fails
            },
        }
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

MEDIA_URL = '/media/'
MEDIA_ROOT = os.path.join(BASE_DIR, 'media')
STATIC_URL = 'static/'
STATIC_ROOT = os.path.join(BASE_DIR, 'staticfiles')

# DigitalOcean Spaces configuration
AWS_ACCESS_KEY_ID = config("AWS_ACCESS_KEY_ID")
AWS_SECRET_ACCESS_KEY = config("AWS_SECRET_ACCESS_KEY")
AWS_STORAGE_BUCKET_NAME = "negromart-space"
AWS_S3_REGION_NAME = "sfo3"
AWS_S3_ENDPOINT_URL = f"https://{AWS_S3_REGION_NAME}.digitaloceanspaces.com"

# # CDN domain for serving public files
AWS_S3_CUSTOM_DOMAIN = f"{AWS_STORAGE_BUCKET_NAME}.{AWS_S3_REGION_NAME}.cdn.digitaloceanspaces.com"
AWS_LOCATION = 'media'

AWS_DEFAULT_ACL = "public-read"
AWS_QUERYSTRING_AUTH = False
AWS_S3_FILE_OVERWRITE = False
AWS_S3_OBJECT_PARAMETERS = {
    'CacheControl': 'max-age=86400'
}


STORAGES = {
    "default": {  # Media files → Spaces
        "BACKEND": "storages.backends.s3boto3.S3Boto3Storage",
    },
}

# Default primary key field type
# https://docs.djangoproject.com/en/5.2/ref/settings/#default-auto-field

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

CKEDITOR_BASEPATH = 'uploads/'

STORAGES["staticfiles"] = {
    "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage",
}

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
        "anon": "2000/day",     # enough for product browsing/search
        # Browsing (logged-in)
        "user": "5000/day",     # generous since users are trusted
        "auth_refresh": "60/min",
        "auth_verify": "200/min",
    },
}


#Paystack configuration
PAYSTACK_SECRET_KEY = "sk_test_08697652e07898b20f337875bdd241b668a2abaa"
PAYSTACK_PUBLIC_KEY = "pk_test_1a9405c84346cd5f9b41a65524aa546d859be3d0"

# DJOSER CONFIGURATION
SITE_NAME = "Negromart"
DOMAIN = config('DOMAIN')
FRONTEND_LOGIN_URL = config("FRONTEND_LOGIN_URL")

# Emailing settings
SITE_URL = config('FRONTEND_BASE_URL')   # set correctly in each environment

EMAIL_TIMEOUT = 30  # seconds
EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"
EMAIL_HOST = "smtp.sendgrid.net"
EMAIL_PORT = 2525
EMAIL_USE_TLS = True
EMAIL_HOST_USER = "apikey"   # keep this literal
EMAIL_HOST_PASSWORD = config("SENDGRID_API_KEY")
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

# Redis URL (set in .env)
REDIS_URL = config("REDIS_URL", default="redis://redis:6379/0")

# Caches (Redis as default backend)
CACHES = {
    "default": {
        "BACKEND": "django_redis.cache.RedisCache",
        "LOCATION": REDIS_URL,
        "OPTIONS": {
            "CLIENT_CLASS": "django_redis.client.DefaultClient",
            "MAX_CONNECTIONS": 50,  # pool size
            "IGNORE_EXCEPTIONS": True,  # fail silently if Redis is down
        },
    }
}

# Sessions stored in Redis
SESSION_ENGINE = "django.contrib.sessions.backends.cache"
SESSION_CACHE_ALIAS = "default"

# Celery settings
CELERY_BROKER_URL = config("REDIS_URL", default="redis://redis:6379/0")
CELERY_RESULT_BACKEND = config("REDIS_RESULT_URL", default="redis://redis:6379/1")

CELERY_ACCEPT_CONTENT = ["json"]
CELERY_TASK_SERIALIZER = "json"
CELERY_TIMEZONE = "UTC"
CELERY_ENABLE_UTC = True
CELERY_BEAT_SCHEDULER = "django_celery_beat.schedulers:DatabaseScheduler"

#SIMPLE JWT CONFIGURATION
AUTH_COOKIE = 'access'
AUTH_ACCESS_MAX_AGE = timedelta(hours=1).total_seconds()
AUTH_REFRESH_MAX_AGE = timedelta(days=60).total_seconds()
AUTH_COOKIE_SECURE = not DEBUG 
AUTH_COOKIE_HTTP_ONLY = True
AUTH_COOKIE_PATH = '/'
AUTH_COOKIE_SAMESITE = "None" if not DEBUG else "Lax"  
AUTH_COOKIE_DOMAIN = None
if not DEBUG:
    AUTH_COOKIE_DOMAIN = ".negromart.com"

from datetime import timedelta

SIMPLE_JWT = {
    # Access Token Lifetime - 1 hour to balance security and UX
    'ACCESS_TOKEN_LIFETIME': timedelta(hours=1),
    
    # Refresh Token Lifetime - 60 days, for long-term sessions
    'REFRESH_TOKEN_LIFETIME': timedelta(days=60),
    
    # Enable token rotation for better security
    'ROTATE_REFRESH_TOKENS': True,
    
    # Blacklist old refresh tokens after they are rotated
    'BLACKLIST_AFTER_ROTATION': False,
    
    # Algorithm for signing the JWT
    'ALGORITHM': 'HS256',
    
    # HTTP Header for token authorization
    'AUTH_HEADER_TYPES': ('Bearer',),

    
    # Enabling sliding token lifetimes for smoother sessions
    'SLIDING_TOKEN_LIFETIME': timedelta(hours=1),  # Sliding token lifetime 1 hour
    'SLIDING_TOKEN_REFRESH_LIFETIME': timedelta(days=60),  # Refresh token sliding window
    
    # Token user class (use custom user model if required)
    'TOKEN_USER_CLASS': 'rest_framework_simplejwt.models.TokenUser',
    
    # Enable JTI claim for each token (JWT ID)
    'JTI_CLAIM': 'jti',

    # Security leeway for potential timing discrepancies
    'LEEWAY': 30,  # Allow a 30-second leeway for clock discrepancies
}

CORS_ALLOW_CREDENTIALS = True

CSRF_TRUSTED_ORIGINS = [
    "https://negromart.com",      # frontend domain
    "https://api.negromart.com",
    "http://localhost:3000",  
]

CORS_ALLOWED_ORIGINS = [
    "https://negromart.com",
    "https://www.negromart.com",
    "http://localhost:3000",  # Next.js frontend URL
    "http://159.223.143.103",  # Next.js frontend URL
    "https://frontend-sigma-khaki-70.vercel.app",  # Next.js frontend URL
    "https://negromart-space.sfo3.cdn.digitaloceanspaces.com",
    "https://negromart-space.sfo3.digitaloceanspaces.com",
]

CORS_ALLOWED_ORIGIN_REGEXES = [
    r"^https://.*\.negromart\.com$",
]


CORS_ALLOW_HEADERS = list(default_headers) + [
    "x-guest-cart",
    "X-Currency",
    "X-Recently-Viewed",
    "x-ssr-refresh",
    'cache-control',
    'access-control-allow-origin',
]

EXCHANGE_RATE_API_KEY = config('EXCHANGE_RATE_API_KEY')


# CKEDITOR CONFIGURATION

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
