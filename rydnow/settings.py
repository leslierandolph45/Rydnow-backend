"""
Django settings for the Rydnow project (rides, delivery & waste collection).


All environment-specific values come from environment variables.


Required in production:
    DJANGO_SECRET_KEY, DATABASE_URL


REQUIRED for rider documents to survive deploys (Render's disk is wiped on every deploy):
    AWS_STORAGE_BUCKET_NAME, AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY
    (+ AWS_S3_ENDPOINT_URL and AWS_S3_REGION_NAME for Cloudflare R2 / Backblaze B2 / Supabase)


Recommended in production:
    PAYSTACK_SECRET_KEY, SENTRY_DSN, REDIS_URL


Optional:
    DEBUG, ALLOWED_HOSTS, CORS_ALLOWED_ORIGINS, CSRF_TRUSTED_ORIGINS,
    LOG_LEVEL, EMAIL_*, SECURE_HSTS_SECONDS, ADMIN_SESSION_HOURS,
    MEDIA_ROOT (only if you mount a persistent disk instead of using a bucket),
    AWS_S3_ADDRESSING_STYLE ("path" or "virtual"), AWS_QUERYSTRING_EXPIRE


Local development: set DEBUG=True in your shell or .env file.
"""
import os
import warnings
from decimal import Decimal
from pathlib import Path


import dj_database_url
from dotenv import load_dotenv
from django.core.exceptions import ImproperlyConfigured
from django.templatetags.static import static
from django.urls import reverse_lazy


load_dotenv()
BASE_DIR = Path(__file__).resolve().parent.parent


# NOTE: the old hard-coded CLOUDINARY_STORAGE block was removed on purpose.
# It was never wired into STORAGES / INSTALLED_APPS (so it did nothing), and it
# exposed an API secret in source code. Rotate that secret in the Cloudinary dashboard.




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
# Secure by default: DEBUG is OFF unless you explicitly turn it on (e.g. in a local .env).
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
    # Admin theme: MUST come before django.contrib.admin.
    "unfold",
    "unfold.contrib.filters",
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    # WhiteNoise's runserver helper must come before staticfiles.
    "whitenoise.runserver_nostatic",
    "django.contrib.staticfiles",
    # Third-party
    "rest_framework",
    "rest_framework.authtoken",
    "corsheaders",
    "phonenumber_field",
    # Local
    "api",
]


TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        # Project-level templates/ so templates/admin/index.html overrides Unfold's dashboard.
        "DIRS": [BASE_DIR / "templates"],
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


MIDDLEWARE = [
    "corsheaders.middleware.CorsMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]




# ─────────────────────── Payments ───────────────────────
# Never hard-code keys. If a real Paystack key was ever committed to git, rotate it now.
PAYSTACK_SECRET_KEY = os.environ.get("PAYSTACK_SECRET_KEY", "")
PAYSTACK_CALLBACK_URL = os.environ.get(
    "PAYSTACK_CALLBACK_URL", "https://rydnow.app/rider/wallet/callback"
)
WALLET_MIN_BALANCE = os.environ.get("WALLET_MIN_BALANCE", "50.00")
if not DEBUG and not PAYSTACK_SECRET_KEY:
    warnings.warn("PAYSTACK_SECRET_KEY is not set: card/MoMo payments will fail.", RuntimeWarning)




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


# Admins are logged out after this many hours (default 12).
SESSION_COOKIE_AGE = env_int("ADMIN_SESSION_HOURS", 12) * 3600




# ────────────────────── CORS / CSRF ──────────────────────
CORS_ALLOWED_ORIGINS = env_list(
    "CORS_ALLOWED_ORIGINS",
    default=[
        "http://localhost:3000",
        "http://127.0.0.1:3000",
        "https://rydnow.app",
        "https://www.rydnow.app",
    ],
)
CORS_ALLOW_CREDENTIALS = True


CSRF_TRUSTED_ORIGINS = env_list(
    "CSRF_TRUSTED_ORIGINS",
    default=["https://rydnow-backend.onrender.com"],
)




# ─────────────────── Internationalization ───────────────────
LANGUAGE_CODE = "en-us"
TIME_ZONE = "Africa/Accra"  # same offset as UTC, but "today" is the Ghana day in the dashboard
USE_I18N = True
USE_TZ = True
PHONENUMBER_DEFAULT_REGION = "GH"




# ───────────────────── Static & Media ─────────────────────
STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "static"] if (BASE_DIR / "static").is_dir() else []


MEDIA_URL = "/media/"
# Only used when NO bucket is configured (local dev, or a Render persistent disk mounted
# at e.g. /var/data/media -> set MEDIA_ROOT=/var/data/media). Never public: rider documents
# are streamed to staff only through the admin (see RiderDocumentAdmin.file_view).
MEDIA_ROOT = Path(os.environ.get("MEDIA_ROOT") or (BASE_DIR / "media"))


STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"},
}
# A missing static reference should degrade (unstyled asset), not 500 the whole admin page.
WHITENOISE_MANIFEST_STRICT = False


if os.environ.get("AWS_STORAGE_BUCKET_NAME"):
    # Any S3-compatible bucket works: AWS S3, Cloudflare R2, Backblaze B2, Supabase Storage.
    # Needs: pip install "django-storages[s3]" boto3   (add both to requirements.txt)
    STORAGES["default"] = {"BACKEND": "storages.backends.s3.S3Storage"}
    AWS_STORAGE_BUCKET_NAME = os.environ["AWS_STORAGE_BUCKET_NAME"]
    AWS_ACCESS_KEY_ID = os.environ.get("AWS_ACCESS_KEY_ID")
    AWS_SECRET_ACCESS_KEY = os.environ.get("AWS_SECRET_ACCESS_KEY")
    AWS_S3_ENDPOINT_URL = os.environ.get("AWS_S3_ENDPOINT_URL") or None
    AWS_S3_REGION_NAME = os.environ.get("AWS_S3_REGION_NAME") or None
    AWS_S3_ADDRESSING_STYLE = os.environ.get("AWS_S3_ADDRESSING_STYLE") or None
    AWS_S3_SIGNATURE_VERSION = "s3v4"
    AWS_DEFAULT_ACL = None
    AWS_QUERYSTRING_AUTH = True  # private files (Ghana Cards, licences) use short-lived signed URLs
    AWS_QUERYSTRING_EXPIRE = env_int("AWS_QUERYSTRING_EXPIRE", 300)
    AWS_S3_FILE_OVERWRITE = False
elif not DEBUG:
    warnings.warn(
        "No object storage configured (AWS_STORAGE_BUCKET_NAME). Uploaded rider documents are "
        "written to the server disk and WILL BE LOST on the next deploy or restart.",
        RuntimeWarning,
        stacklevel=1,
    )


DATA_UPLOAD_MAX_MEMORY_SIZE = 10 * 1024 * 1024
FILE_UPLOAD_MAX_MEMORY_SIZE = 5 * 1024 * 1024




# ──────────────────── Django REST Framework ────────────────────
REST_FRAMEWORK = {
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.IsAuthenticated"],
    "DEFAULT_AUTHENTICATION_CLASSES": ["rest_framework.authentication.TokenAuthentication"]
    + (["rest_framework.authentication.SessionAuthentication"] if DEBUG else []),
    "DEFAULT_RENDERER_CLASSES": ["rest_framework.renderers.JSONRenderer"]
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
        "wallet_read": "60/min",
    },
    "EXCEPTION_HANDLER": "api.exception_handler.rydnow_exception_handler",
}




# ─────────────────────── Security ───────────────────────
if not DEBUG:
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    SECURE_SSL_REDIRECT = env_bool("SECURE_SSL_REDIRECT", True)
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    # Start at 1 hour; raise to 31536000 (1 year) once you're sure HTTPS works everywhere.
    SECURE_HSTS_SECONDS = env_int("SECURE_HSTS_SECONDS", 3600)
    SECURE_HSTS_INCLUDE_SUBDOMAINS = env_bool("SECURE_HSTS_INCLUDE_SUBDOMAINS", True)
    SECURE_HSTS_PRELOAD = env_bool("SECURE_HSTS_PRELOAD", False)


SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_SAMESITE = "Lax"
SECURE_CONTENT_TYPE_NOSNIFF = True
# "same-origin" would stop the live map's OpenStreetMap tiles loading (OSM requires a Referer).
SECURE_REFERRER_POLICY = "strict-origin-when-cross-origin"
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
        "standard": {"format": "{asctime} {levelname} {name} {message}", "style": "{"},
    },
    "handlers": {
        "console": {"class": "logging.StreamHandler", "formatter": "standard"},
    },
    "root": {"handlers": ["console"], "level": LOG_LEVEL},
    "loggers": {
        "django": {"handlers": ["console"], "level": LOG_LEVEL, "propagate": False},
        "django.request": {"handlers": ["console"], "level": "WARNING", "propagate": False},
        "django.security": {"handlers": ["console"], "level": "WARNING", "propagate": False},
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




# ─────────────────────────────────────────────────────────────
# DISPATCH SETTINGS
# ─────────────────────────────────────────────────────────────
DISPATCH = {
    "offer_timeout_s": 45,
    "radius_steps_km": (3.0, 5.0, 8.0),
    "candidates_per_round": 15,
    "max_offers_per_booking": 8,
    "min_wallet": Decimal("50.00"),
    "location_max_age_s": 90,
    "w_proximity": 0.50,
    "w_rating": 0.20,
    "w_acceptance": 0.15,
    "w_reliability": 0.10,
    "w_fairness": 0.05,
}




# ─────────────────────────────────────────────────────────────
# ADMIN THEME (django-unfold)
# ─────────────────────────────────────────────────────────────
UNFOLD = {
    "SITE_TITLE": "RYDNOW Operations",
    "SITE_HEADER": "RYDNOW",
    "SITE_SYMBOL": "two_wheeler",
    "SITE_URL": "/",
    "BORDER_RADIUS": "12px",
    "STYLES": [lambda request: static("css/rydnow_admin.css")],
    "DASHBOARD_CALLBACK": "api.admin_dashboard.dashboard_callback",
    "COLORS": {
        "primary": {
            "50": "oklch(97% 0.014 265)",
            "100": "oklch(93% 0.032 265)",
            "200": "oklch(88% 0.06 265)",
            "300": "oklch(80% 0.10 265)",
            "400": "oklch(70% 0.15 265)",
            "500": "oklch(58% 0.20 265)",
            "600": "oklch(50% 0.22 265)",
            "700": "oklch(45% 0.21 265)",
            "800": "oklch(38% 0.17 265)",
            "900": "oklch(32% 0.13 265)",
            "950": "oklch(25% 0.10 265)",
        }
    },
    "SIDEBAR": {
        "show_search": True,
        "show_all_applications": True,
        "navigation": [
            {
                "title": "Operations",
                "separator": True,
                "items": [
                    {"title": "Dashboard", "icon": "dashboard", "link": reverse_lazy("admin:index")},
                    {"title": "Live Rydnow Map", "icon": "map", "link": reverse_lazy("rn_live_map")},
                    {"title": "Bookings", "icon": "receipt_long", "link": reverse_lazy("admin:api_booking_changelist")},
                ],
            },
            {
                "title": "People",
                "separator": True,
                "items": [
                    {"title": "Riders", "icon": "two_wheeler", "link": reverse_lazy("admin:api_riderprofile_changelist")},
                    {"title": "Rider Documents", "icon": "badge", "link": reverse_lazy("admin:api_riderdocument_changelist")},
                    {"title": "Customers", "icon": "group", "link": reverse_lazy("admin:api_customerprofile_changelist")},
                ],
            },
            {
                "title": "Money",
                "separator": True,
                "items": [
                    {"title": "Withdrawals", "icon": "payments", "link": reverse_lazy("admin:api_withdrawalrequest_changelist")},
                    {"title": "Payments", "icon": "credit_card", "link": reverse_lazy("admin:api_payment_changelist")},
                    {"title": "Promo Codes", "icon": "sell", "link": reverse_lazy("admin:api_promocode_changelist")},
                ],
            },
        ],
    },
}

