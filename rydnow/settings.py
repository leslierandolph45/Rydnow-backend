"""
Django settings for the Rydnow project (rides, delivery & waste collection).

All environment-specific values come from environment variables.

Required in production:
    DJANGO_SECRET_KEY, DATABASE_URL

Optional:
    DEBUG, ALLOWED_HOSTS, CORS_ALLOWED_ORIGINS, CSRF_TRUSTED_ORIGINS,
    REDIS_URL, SENTRY_DSN, LOG_LEVEL, EMAIL_*,
    AWS_STORAGE_BUCKET_NAME (+ AWS_* for S3 / Cloudflare R2 file storage)

Local development: set DEBUG=True in your shell or .env file.
"""

import os
import warnings
from pathlib import Path
from dotenv import load_dotenv

import dj_database_url
from django.core.exceptions import ImproperlyConfigured

load_dotenv()
BASE_DIR = Path(__file__).resolve().parent.parent


# ────────────────────────── Env helpers ──────────────────────────
def env_bool(name: str, default: bool = False) -> bool:
    return os.environ.get(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


def env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw in (None, ""):
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ImproperlyConfigured(f"{name} must be an integer, got {raw!r}.") from exc


def env_list(name: str, default=None) -> list:
    raw = os.environ.get(name, "")
    items = [item.strip() for item in raw.split(",") if item.strip()]
    return items or list(default or [])


# ─────────────────────────── Core ───────────────────────────
# Secure by default: production never runs with DEBUG on by accident.
DEBUG = env_bool("DEBUG", False)

SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY")
if not SECRET_KEY:
    if DEBUG:
        SECRET_KEY = "dev-insecure-key"
    else:
        raise ImproperlyConfigured("DJANGO_SECRET_KEY must be set when DEBUG is False.")

ALLOWED_HOSTS = env_list(
    "ALLOWED_HOSTS",
    default=["rydnow-backend.onrender.com", "localhost", "127.0.0.1"],
)
_render_host = os.environ.get("RENDER_EXTERNAL_HOSTNAME")
if _render_host and _render_host not in ALLOWED_HOSTS:
    ALLOWED_HOSTS.append(_render_host)

ROOT_URLCONF = "rydnow.urls"
WSGI_APPLICATION = "rydnow.wsgi.application"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# AUTH_USER_MODEL = "api.User"


# ─────────────────────── Applications ───────────────────────
INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "whitenoise.runserver_nostatic",
    # Third-party
    "rest_framework",
    "rest_framework.authtoken",
    "corsheaders",
    # Local
    "api",
    "phonenumber_field"
]

MIDDLEWARE = [
    "corsheaders.middleware.CorsMiddleware",  # MUST be first!
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]


PAYSTACK_SECRET_KEY = "sk_..."
PAYSTACK_CALLBACK_URL = "https://rydnow.app/rider/wallet/callback"
WALLET_MIN_BALANCE = "50.00"


TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]


# ─────────────────────── Database ───────────────────────
DATABASES = {
    "default": dj_database_url.config(
        env="DATABASE_URL",
        default=f"sqlite:///{BASE_DIR / 'db.sqlite3'}",
        conn_max_age=600,
        conn_health_checks=True,
    )
}
if not DEBUG and DATABASES["default"]["ENGINE"].endswith("sqlite3"):
    raise ImproperlyConfigured("Set DATABASE_URL to a PostgreSQL database in production.")


# ───────────────────────── Cache ─────────────────────────
if os.environ.get("REDIS_URL"):
    CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.redis.RedisCache",
            "LOCATION": os.environ["REDIS_URL"],
        }
    }
else:
    CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
            "LOCATION": "rydnow-local",
        }
    }


# ─────────────────── Auth & Passwords ───────────────────
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
        "OPTIONS": {"min_length": 10},
    },
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

CORS_ALLOWED_ORIGINS = [
    "http://localhost:3000",
    "http://127.0.0.1:3000",
    "https://rydnow.app",
    "https://www.rydnow.app",
]


# ─────────────────── Internationalization ───────────────────
LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True


# ───────────────────── Static & Media ─────────────────────
STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"

MEDIA_URL = "/media/"
MEDIA_ROOT = BASE_DIR / "media"

STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage",
    },
}

if os.environ.get("AWS_STORAGE_BUCKET_NAME"):
    STORAGES["default"] = {"BACKEND": "storages.backends.s3.S3Storage"}
    AWS_STORAGE_BUCKET_NAME = os.environ.get("AWS_STORAGE_BUCKET_NAME")
    AWS_ACCESS_KEY_ID = os.environ.get("AWS_ACCESS_KEY_ID")
    AWS_SECRET_ACCESS_KEY = os.environ.get("AWS_SECRET_ACCESS_KEY")
    AWS_S3_ENDPOINT_URL = os.environ.get("AWS_S3_ENDPOINT_URL") or None
    AWS_S3_REGION_NAME = os.environ.get("AWS_S3_REGION_NAME") or None
    AWS_S3_SIGNATURE_VERSION = "s3v4"
    AWS_DEFAULT_ACL = None
    AWS_QUERYSTRING_AUTH = True
    AWS_QUERYSTRING_EXPIRE = env_int("AWS_QUERYSTRING_EXPIRE", 300)
    AWS_S3_FILE_OVERWRITE = False
elif not DEBUG:
    warnings.warn(
        "No object storage configured. Uploaded files will be lost on deploy.",
        RuntimeWarning,
        stacklevel=1,
    )

DATA_UPLOAD_MAX_MEMORY_SIZE = 10 * 1024 * 1024
FILE_UPLOAD_MAX_MEMORY_SIZE = 5 * 1024 * 1024


# ────────────────────── CORS / CSRF ──────────────────────
CSRF_TRUSTED_ORIGINS = env_list(
    "CSRF_TRUSTED_ORIGINS",
    default=["https://rydnow-backend.onrender.com"],
)


# ──────────────────── Django REST Framework ────────────────────
REST_FRAMEWORK = {
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.IsAuthenticated",
    ],
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework.authentication.TokenAuthentication",
    ]
    + (["rest_framework.authentication.SessionAuthentication"] if DEBUG else []),
    "DEFAULT_RENDERER_CLASSES": [
        "rest_framework.renderers.JSONRenderer",
    ]
    + (["rest_framework.renderers.BrowsableAPIRenderer"] if DEBUG else []),
    "DEFAULT_PARSER_CLASSES": [
        "rest_framework.parsers.JSONParser",
        "rest_framework.parsers.MultiPartParser",
        "rest_framework.parsers.FormParser",
    ],
    "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.PageNumberPagination",
    "PAGE_SIZE": 20,
    "DEFAULT_THROTTLE_CLASSES": [
        "rest_framework.throttling.AnonRateThrottle",
        "rest_framework.throttling.UserRateThrottle",
        "rest_framework.throttling.ScopedRateThrottle",
    ],
    "DEFAULT_THROTTLE_RATES": {
        "anon": "60/min",
        "user": "300/min",
        "login": "5/min",
        "otp": "3/min",
        "register": "10/hour",
    },
    "EXCEPTION_HANDLER": "api.exception_handler.rydnow_exception_handler",
}


# ─────────────────────── Security ───────────────────────
if not DEBUG:
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    SECURE_SSL_REDIRECT = env_bool("SECURE_SSL_REDIRECT", True)

    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True

    SECURE_HSTS_SECONDS = env_int("SECURE_HSTS_SECONDS", 31536000)
    SECURE_HSTS_INCLUDE_SUBDOMAINS = env_bool("SECURE_HSTS_INCLUDE_SUBDOMAINS", True)
    SECURE_HSTS_PRELOAD = env_bool("SECURE_HSTS_PRELOAD", False)

SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_SAMESITE = "Lax"
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"
X_FRAME_OPTIONS = "DENY"


# ───────────────────────── Email ─────────────────────────
if DEBUG and not os.environ.get("EMAIL_HOST"):
    EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"
else:
    EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"
    EMAIL_HOST = os.environ.get("EMAIL_HOST", "")
    EMAIL_PORT = env_int("EMAIL_PORT", 587)
    EMAIL_HOST_USER = os.environ.get("EMAIL_HOST_USER", "")
    EMAIL_HOST_PASSWORD = os.environ.get("EMAIL_HOST_PASSWORD", "")
    EMAIL_USE_TLS = env_bool("EMAIL_USE_TLS", True)
DEFAULT_FROM_EMAIL = os.environ.get("DEFAULT_FROM_EMAIL", "Rydnow <no-reply@rydnow.app>")


# ─────────────────────── Logging ───────────────────────
LOG_LEVEL = os.environ.get("LOG_LEVEL", "DEBUG" if DEBUG else "INFO")

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "standard": {
            "format": "{asctime} {levelname} {name} {message}",
            "style": "{",
        },
    },
    "handlers": {
        "console": {"class": "logging.StreamHandler", "formatter": "standard"},
    },
    "root": {"handlers": ["console"], "level": LOG_LEVEL},
    "loggers": {
        "django": {"handlers": ["console"], "level": LOG_LEVEL, "propagate": False},
        "django.request": {"handlers": ["console"], "level": "WARNING", "propagate": False},
        "api": {"handlers": ["console"], "level": LOG_LEVEL, "propagate": False},
    },
}


# ────────────────────── Error Monitoring ──────────────────────
_sentry_dsn = os.environ.get("SENTRY_DSN")
if _sentry_dsn:
    import sentry_sdk
    sentry_sdk.init(
        dsn=_sentry_dsn,
        environment=os.environ.get("SENTRY_ENVIRONMENT", "development" if DEBUG else "production"),
        release=os.environ.get("RENDER_GIT_COMMIT"),
        traces_sample_rate=float(os.environ.get("SENTRY_TRACES_SAMPLE_RATE", 0.1)),
        send_default_pii=False,
    )

REST_FRAMEWORK = {
    # ... your existing settings ...
    "DEFAULT_THROTTLE_RATES": {
        "wallet_read": "60/min",
    },
}
