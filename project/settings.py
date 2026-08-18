import os
from pathlib import Path

from dotenv import load_dotenv
import cloudinary


BASE_DIR = Path(__file__).resolve().parent.parent

load_dotenv(BASE_DIR / ".env")
SECRET_KEY = os.getenv("SECRET_KEY")

DEBUG = os.getenv("DEBUG", "False").lower() == "true"

ALLOWED_HOSTS = [
    host.strip()
    for host in os.getenv("ALLOWED_HOSTS", "").split(",")
    if host.strip()
]
# Application definition

INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',

    'rest_framework',
    'django_celery_beat',
    'django_celery_results',
    'cloudinary',
    'cloudinary_storage',
    'django_filters',
    'drf_spectacular',

    'campaigns',
    'users',
    'mailings',
    'templates',
    'client',
    'test_mailing',

]
REST_FRAMEWORK = {
    'DEFAULT_AUTHENTICATION_CLASSES': (
        'rest_framework_simplejwt.authentication.JWTAuthentication',
    ),
    'DEFAULT_FILTER_BACKENDS': [
        'django_filters.rest_framework.DjangoFilterBackend',
    ],
    'DEFAULT_THROTTLE_CLASSES': [
        'rest_framework.throttling.ScopedRateThrottle',
    ],
    'DEFAULT_THROTTLE_RATES': {
        'one-time-scheduled-mailing': '10/min',
        'one-time-schedule-mailing_trigger': '5/min',
        'bulk_send_trigger': '5/min',
        'recurring-scheduled-mailing': '100/min',
        'recurring-schedule': '10/min',
        'file_upload': '20/min',
    },
    'DEFAULT_SCHEMA_CLASS': 'drf_spectacular.openapi.AutoSchema',
    'DEFAULT_PARSER_CLASSES': [
        'rest_framework.parsers.JSONParser',
        'rest_framework.parsers.MultiPartParser',
        'rest_framework.parsers.FormParser',
    ],
}


MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'project.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [BASE_DIR / 'templates'],
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

WSGI_APPLICATION = 'project.wsgi.application'


# Database
# https://docs.djangoproject.com/en/5.2/ref/settings/#databases

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": os.getenv("DB_NAME"),
        "USER": os.getenv("DB_USER"),
        "PASSWORD": os.getenv("DB_PASSWORD"),
        "HOST": os.getenv("DB_HOST"),
        "PORT": os.getenv("DB_PORT"),
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

# TIME_ZONE = 'UTC'
TIME_ZONE = 'Asia/Kathmandu'
USE_I18N = True

USE_TZ = True


# Static files (CSS, JavaScript, Images)
# https://docs.djangoproject.com/en/5.2/howto/static-files/

STATIC_URL = 'static/'
STATIC_ROOT = os.path.join(BASE_DIR, 'staticfiles')

# 2. MEDIA_ROOT 
MEDIA_ROOT = os.path.join(BASE_DIR, 'media') 
MEDIA_URL = '/media/'

# Default primary key field type
# https://docs.djangoproject.com/en/5.2/ref/settings/#default-auto-field

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'
CACHES = {
    "default": {
        "BACKEND": "django_redis.cache.RedisCache",
        "LOCATION": os.getenv("REDIS_CACHE_URL"),
        "OPTIONS": {
            "CLIENT_CLASS": "django_redis.client.DefaultClient",
        },
        "KEY_PREFIX": "mailings_",
        "TIMEOUT": 86400,
    }
}

# Celery Configuration
CELERY_BROKER_URL = os.getenv("CELERY_BROKER_URL")
CELERY_RESULT_BACKEND = 'django-db'
CELERY_ACCEPT_CONTENT = ['json']
CELERY_TASK_SERIALIZER = 'json'
CELERY_RESULT_SERIALIZER = 'json'
# CELERY_TIMEZONE = 'UTC'
CELERY_TIMEZONE = 'Asia/Kathmandu'  # Set to Nepali timezone
CELERY_BEAT_SCHEDULER = 'django_celery_beat.schedulers:DatabaseScheduler'


# smtp settings
EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"
EMAIL_HOST = os.getenv("EMAIL_HOST")
EMAIL_PORT = int(os.getenv("EMAIL_PORT", "587"))
EMAIL_USE_TLS = os.getenv("EMAIL_USE_TLS", "True").lower() == "true"

EMAIL_HOST_USER = os.getenv("EMAIL_HOST_USER")
EMAIL_HOST_PASSWORD = os.getenv("EMAIL_HOST_PASSWORD")



# default user
AUTH_USER_MODEL = 'users.User'


# cloudinary 
import cloudinary

# Configure Cloudinary with environment variables
cloudinary.config(
    cloud_name=os.getenv("CLOUDINARY_CLOUD_NAME"),
    api_key=os.getenv("CLOUDINARY_API_KEY"),
    api_secret=os.getenv("CLOUDINARY_API_SECRET"),
)

DEFAULT_FILE_STORAGE = 'cloudinary_storage.storage.MediaCloudinaryStorage'


# Swagger / OpenAPI Configuration
SPECTACULAR_SETTINGS = {
    'TITLE': 'Bulk Mail Service API',
    'DESCRIPTION': 'Production-grade API for scheduling and managing bulk mail campaigns.',
    'VERSION': '1.0.0',
    'SERVE_INCLUDE_SCHEMA': False,

    # ✅ Fix: Give each status enum a unique, descriptive name
    'ENUM_NAME_OVERRIDES': {
        'TestMailingStatusEnum': 'test_mailing.models.TestMailing.StatusChoices',
        'CampaignStatusEnum':    'campaigns.models.Campaign.StatusChoices',
        'MailLogStatusEnum':     'mailings.models.MailLog.StatusChoices',
    },
}


EMAIL_TIMEOUT = 60

from decouple import config
BULK_MAIL_RATE_LIMIT = config('BULK_MAIL_RATE_LIMIT', default='10/m')

MAILING_SETTINGS = {
    'CHUNK_SIZE': config('MAILING_CHUNK_SIZE', default=200, cast=int),
    'MAX_RETRIES': config('MAILING_MAX_RETRIES', default=3, cast=int),
    'CIRCUIT_BREAKER_THRESHOLD': config('CIRCUIT_BREAKER_THRESHOLD', default=50, cast=int),
    'MAX_RECIPIENTS_LIMIT': config('MAX_RECIPIENTS_LIMIT', default=50000, cast=int),
    'BULK_CREATE_BATCH_SIZE': config('BULK_CREATE_BATCH_SIZE', default=500, cast=int),
    'RATE_LIMIT': BULK_MAIL_RATE_LIMIT,
    'SMTP_THROTTLE_SECONDS': config('SMTP_THROTTLE_SECONDS', default=0.1, cast=float),
    'STUCK_CHUNK_THRESHOLD_HOURS': config('STUCK_CHUNK_THRESHOLD_HOURS', default=2, cast=int),
    'TASK_TIME_LIMIT': config('MAILING_TASK_TIME_LIMIT', default=300, cast=int),
    'TASK_SOFT_TIME_LIMIT': config('MAILING_TASK_SOFT_TIME_LIMIT', default=270, cast=int),
    'MAX_ATTACHMENT_SIZE': config('MAX_ATTACHMENT_SIZE', default=10 * 1024 * 1024, cast=int),
}

from celery.schedules import crontab

CELERY_BEAT_SCHEDULE = {
    "reconcile-stuck-mailings-immediate": {
        "task": "mailings.tasks.reconcile_stuck_mailings_immediate",
        "schedule": crontab(minute="*/15"),
    },
    "reconcile-stuck-test-mailings": {
        "task": "test_mailing.tasks.reconcile_stuck_test_mailings",
        "schedule": crontab(minute="*/15"),
    },
    "cleanup-old-celery-beat-tasks": {
        "task": "test_mailing.tasks.cleanup_old_beat_tasks",
        "schedule": crontab(hour=3, minute=0),
    },
    'cleanup-old-campaign-runs': {
        'task': 'campaigns.tasks.cleanup_old_campaign_runs',
        'schedule': crontab(hour=3, minute=0, day_of_week='sunday'),
        'kwargs': {'days': 90},
    },
    'reconcile-stuck-campaign-runs': {
        'task': 'campaigns.tasks.reconcile_stuck_campaign_runs',
        'schedule': crontab(minute='*/30'),
    },
}