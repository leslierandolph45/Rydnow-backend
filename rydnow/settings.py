"""
Django settings for the Rydnow project (rides, delivery & waste collection).


All environment-specific values come from environment variables (a local .env file is read
in development). Nothing secret is stored in this file.


Required in production:
    DJANGO_SECRET_KEY, DATABASE_URL


REQUIRED for rider documents to survive deploys (Render's disk is wiped on every deploy):
    AWS_STORAGE_BUCKET_NAME, AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY
    (+ AWS_S3_ENDPOINT_URL and AWS_S3_REGION_NAME for Cloudflare R2 / Backblaze B2 / Supabase)
    and in requirements.txt:  django-storages[s3]  boto3


Recommended in production:
    PAYSTACK_SECRET_KEY, BOOKING_WEBHOOK_SECRET, SENTRY_DSN, REDIS_URL


Optional:
    DEBUG, ALLOWED_HOSTS, CORS_ALLOWED_ORIGINS, CORS_ALLOWED_ORIGIN_REGEXES,
    CSRF_TRUSTED_ORIGINS,
    LOG_LEVEL, EMAIL_*, SECURE_SSL_REDIRECT, SECURE_HSTS_SECONDS, ADMIN_SESSION_HOURS,
    DATABASE_SSL_REQUIRE, NUM_PROXIES, ADMIN_URL,
    PRICING_QUOTE_VALIDITY_MINUTES, PRICING_SIGNING_KEY, LEGACY_QUOTE_SUNSET,
    PAYSTACK_CALLBACK_URL, WALLET_MIN_BALANCE, MIN_DEPOSIT, MAX_DEPOSIT, PAYMENT_CURRENCY,
    REQUIRE_PHONE_VERIFICATION, SMS_BACKEND (dotted path of a function(phone, message)),
    TRACKING_BASE_URL, SENTRY_TRACES_SAMPLE_RATE,
    MEDIA_ROOT (only if you mount a persistent disk instead of using a bucket),
    AWS_S3_ADDRESSING_STYLE ("path" or "virtual"), AWS_QUERYSTRING_EXPIRE,
    STATIC_MANIFEST (set to True to re-enable hashed static filenames),
    TEMPLATES_DIR (absolute path to your project templates folder, overrides the search below)


IMPORTANT: ALLOWED_HOSTS, CORS_ALLOWED_ORIGINS, CORS_ALLOWED_ORIGIN_REGEXES and
CSRF_TRUSTED_ORIGINS REPLACE the defaults below when set as environment variables. If you set
any of them on Render, include every origin you need, or delete the variable to use the defaults.


Local development: set DEBUG=True in your shell or .env file.
"""
import importlib.util
import os
import re
import warnings
from pathlib import Path


import dj_database_url
from dotenv import load_dotenv
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




def env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw in (None, ""):
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ImproperlyConfigured(f"{name} must be a number, got {raw!r}.") from exc




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


# A leading dot matches the domain and all its subdomains (".vercel.app" matches
# "my-app.vercel.app"). A bare "vercel.app" would match only that exact host.
ALLOWED_HOSTS = env_list(
    "ALLOWED_HOSTS",
    default=[
        "rydnow-backend.onrender.com",
        "localhost",
        "127.0.0.1",
        "rydnow.app",
        ".rydnow.app",
        ".vercel.app",
    ],
)
_render_host = os.environ.get("RENDER_EXTERNAL_HOSTNAME")
if _render_host and _render_host not in ALLOWED_HOSTS:
    ALLOWED_HOSTS.append(_render_host)


ROOT_URLCONF = "rydnow.urls"
WSGI_APPLICATION = "rydnow.wsgi.application"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
APPEND_SLASH = True  # the API relies on trailing slashes; the frontend proxy adds them back


# Read by rydnow/urls.py (getattr(settings, "ADMIN_URL", "admin/")). Must end with "/".
ADMIN_URL = os.environ.get("ADMIN_URL", "admin/")


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




# ─────────────────────── Admin dashboard (Unfold) ───────────────────────
# DASHBOARD_CALLBACK feeds data into the admin home page. It is only used when the home
# page template is yours: put your file at   <templates folder>/admin/index.html
# The callback is api/dashboard.py -> dashboard_callback(request, context).
UNFOLD = {
    "SITE_TITLE": "Rydnow Admin",
    "SITE_HEADER": "Rydnow",
    "DASHBOARD_CALLBACK": "api.dashboard.dashboard_callback",
}




# ─────────────────────── Templates ───────────────────────
# Django searches TEMPLATES["DIRS"] BEFORE any app's templates folder, so a file here named
# admin/index.html replaces the admin home page (including Unfold's own).
# Linux (Render) is case-sensitive: "Rydnow-backend" and "rydnow-backend" are different folders.
# Every candidate below is checked, and only folders that really exist are used.
def _find_template_dirs():
    candidates = []
    explicit = os.environ.get("TEMPLATES_DIR")
    if explicit:
        candidates.append(Path(explicit))
    candidates += [
        BASE_DIR / "templates",
        BASE_DIR / "Rydnow-backend" / "templates",
        BASE_DIR / "rydnow-backend" / "templates",
        BASE_DIR / "api" / "templates",
    ]
    found, seen = [], set()
    for path in candidates:
        if path.is_dir():
            real = path.resolve()
            if real not in seen:  # case-insensitive systems list the same folder twice
                seen.add(real)
                found.append(str(real))
    return found




TEMPLATE_DIRS = _find_template_dirs()
if not TEMPLATE_DIRS:
    warnings.warn(
        "No project templates folder found (looked for templates/, Rydnow-backend/templates, "
        "rydnow-backend/templates under BASE_DIR). Custom admin templates will NOT load. "
        "Set TEMPLATES_DIR to the folder's absolute path.",
        RuntimeWarning,
    )


TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": TEMPLATE_DIRS,
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
        # Needed for an EXTERNAL Postgres URL. Render's internal URL does not require it.
        ssl_require=env_bool("DATABASE_SSL_REQUIRE", False),
    )
}
if not DEBUG and DATABASES["default"]["ENGINE"].endswith("sqlite3"):
    raise ImproperlyConfigured("Set DATABASE_URL to a PostgreSQL database in production.")




# ───────────────────────── Cache ─────────────────────────
# The cache holds API throttle counters, OTP codes and the surge / demand counters used by
# pricing. Without REDIS_URL every gunicorn worker keeps its OWN counters (local memory), so
# limits, OTPs and surge are not shared between workers. Set REDIS_URL in production.
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
    if not DEBUG:
        warnings.warn(
            "REDIS_URL is not set: throttling, OTP and surge counters are per-process and not "
            "shared between workers.",
            RuntimeWarning,
        )




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




# ──────────────────── Django REST Framework ────────────────────
# API errors go through api/exception_handler.py. The handler's function name has changed
# between versions of that file (rydnow_exception_handler / custom_exception_handler), and a
# wrong name makes EVERY error response (401, 403, 405, validation...) crash with a 500.
# So the file is scanned (not imported, which is unsafe while settings load) for a known name.
# If none is found, DRF's standard handler is used.
_DRF_DEFAULT_HANDLER = "rest_framework.views.exception_handler"
_HANDLER_NAMES = ("rydnow_exception_handler", "custom_exception_handler", "exception_handler")




def _pick_exception_handler() -> str:
    path = BASE_DIR / "api" / "exception_handler.py"
    if not path.is_file():
        warnings.warn(
            "api/exception_handler.py not found: using DRF's default exception handler.",
            RuntimeWarning,
        )
        return _DRF_DEFAULT_HANDLER
    try:
        source = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return _DRF_DEFAULT_HANDLER
    for name in _HANDLER_NAMES:
        if re.search(rf"^(?:def\s+{name}\s*\(|{name}\s*=)", source, re.MULTILINE):
            return f"api.exception_handler.{name}"
    warnings.warn(
        "api/exception_handler.py defines none of "
        f"{', '.join(_HANDLER_NAMES)}: using DRF's default exception handler.",
        RuntimeWarning,
    )
    return _DRF_DEFAULT_HANDLER




_EXCEPTION_HANDLER = _pick_exception_handler()


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
    # Every `throttle_scope` used by a view MUST have a rate here. A view whose scope is missing
    # raises ImproperlyConfigured on every request (a 500).
    # (Throttle classes in api/views.py that define their own `rate` do not need an entry.)
    "DEFAULT_THROTTLE_RATES": {
        # Global
        "anon": "60/min",
        "user": "300/min",
        # Auth
        "login": "5/min",
        "otp": "3/min",
        "register": "10/hour",
        # Wallet
        "wallet_read": "60/min",
        # Booking API (api/booking_views.py)
        "quote": "60/min",           # QuoteView: re-quotes while the customer edits the trip
        "booking": "120/min",        # BookingViewSet: list / detail / cancel (polled while active)
        "booking_create": "10/min",  # BookingViewSet.create
        "riders_nearby": "60/min",   # NearbyRidersView (polled about every 20 s)
        "promo_validate": "20/min",  # promo_validate: stops code guessing
    },
    "EXCEPTION_HANDLER": _EXCEPTION_HANDLER,
}
# Number of reverse proxies in front of the app (read from X-Forwarded-For for throttling).
# Leave unset unless you know the exact number.
_num_proxies = env_int("NUM_PROXIES", 0)
if _num_proxies:
    REST_FRAMEWORK["NUM_PROXIES"] = _num_proxies




# ────────────────────── CORS / CSRF ──────────────────────
# Browser calls from the website go through the Next.js proxy (server to server), so CORS only
# matters for direct browser calls. If a mobile web view calls the API directly, add its
# origin through CORS_ALLOWED_ORIGINS (for example capacitor://localhost).
CORS_ALLOWED_ORIGINS = env_list(
    "CORS_ALLOWED_ORIGINS",
    default=[
        "http://localhost:3000",
        "http://127.0.0.1:3000",
        "https://rydnow.app",
        "https://www.rydnow.app",
        "https://rydnow-backend.onrender.com",
    ],
)
# django-cors-headers does NOT accept wildcards in CORS_ALLOWED_ORIGINS, so Vercel preview
# deployments (https://<name>.vercel.app) are matched with a regex instead.
CORS_ALLOWED_ORIGIN_REGEXES = env_list(
    "CORS_ALLOWED_ORIGIN_REGEXES",
    default=[r"^https://[a-z0-9-]+\.vercel\.app$"],
)
CORS_ALLOW_CREDENTIALS = True


# CSRF_TRUSTED_ORIGINS DOES accept a leading wildcard ("https://*.vercel.app").
CSRF_TRUSTED_ORIGINS = env_list(
    "CSRF_TRUSTED_ORIGINS",
    default=[
        "https://rydnow-backend.onrender.com",
        "https://rydnow.app",
        "https://www.rydnow.app",
        "https://*.vercel.app",
    ],
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


# /admin/ 500 fix:
# CompressedManifestStaticFilesStorage raises ValueError when static("css/x.css") points at a
# file that was not collected, which crashed the admin page. CompressedStaticFilesStorage just
# builds the URL, so a missing file can no longer crash a page.
# Set STATIC_MANIFEST=True once rydnow_admin.css is confirmed to be collected, to get hashed names.
if env_bool("STATIC_MANIFEST", False):
    _STATICFILES_BACKEND = "whitenoise.storage.CompressedManifestStaticFilesStorage"
    WHITENOISE_MANIFEST_STRICT = False
else:
    _STATICFILES_BACKEND = "whitenoise.storage.CompressedStaticFilesStorage"


STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": _STATICFILES_BACKEND},
}


_HAS_BUCKET = bool(os.environ.get("AWS_STORAGE_BUCKET_NAME"))
_HAS_S3_LIBS = (
    importlib.util.find_spec("storages") is not None
    and importlib.util.find_spec("boto3") is not None
)


if _HAS_BUCKET and _HAS_S3_LIBS:
    # Any S3-compatible bucket works: AWS S3, Cloudflare R2, Backblaze B2, Supabase Storage.
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
elif _HAS_BUCKET:
    # A bucket is configured but the libraries are missing. Booting on local disk keeps the API
    # up, but uploads are lost on the next deploy. Add them to requirements.txt to fix.
    warnings.warn(
        "AWS_STORAGE_BUCKET_NAME is set but django-storages and/or boto3 are not installed: "
        'add "django-storages[s3]" and "boto3" to requirements.txt. '
        "Uploads are being written to local disk and will be lost on deploy.",
        RuntimeWarning,
    )
elif not DEBUG:
    warnings.warn(
        "No object storage configured (AWS_STORAGE_BUCKET_NAME). Uploaded rider documents are "
        "written to the server disk and WILL BE LOST on the next deploy or restart.",
        RuntimeWarning,
    )


DATA_UPLOAD_MAX_MEMORY_SIZE = 10 * 1024 * 1024
FILE_UPLOAD_MAX_MEMORY_SIZE = 5 * 1024 * 1024




# ─────────────────────── Payments & wallet ───────────────────────
# Never hard-code keys. If a real Paystack key was ever committed to git, rotate it now.
PAYSTACK_SECRET_KEY = os.environ.get("PAYSTACK_SECRET_KEY", "")
PAYSTACK_CALLBACK_URL = os.environ.get(
    "PAYSTACK_CALLBACK_URL", "https://rydnow.app/rider/wallet/callback"
)
PAYMENT_CURRENCY = os.environ.get("PAYMENT_CURRENCY", "GHS")  # read by api/views.py
WALLET_MIN_BALANCE = os.environ.get("WALLET_MIN_BALANCE", "50.00")
MIN_DEPOSIT = os.environ.get("MIN_DEPOSIT", "10.00")          # read by api/views.py
MAX_DEPOSIT = os.environ.get("MAX_DEPOSIT", "100000.00")      # read by api/views.py
if not DEBUG and not PAYSTACK_SECRET_KEY:
    warnings.warn("PAYSTACK_SECRET_KEY is not set: card/MoMo payments will fail.", RuntimeWarning)


# Shared secret for POST /booking/webhook/ (X-Signature = hex HMAC-SHA256 of the raw body).
# While this is empty the endpoint rejects every call with 401.
BOOKING_WEBHOOK_SECRET = os.environ.get("BOOKING_WEBHOOK_SECRET", "")
if not DEBUG and not BOOKING_WEBHOOK_SECRET:
    warnings.warn(
        "BOOKING_WEBHOOK_SECRET is not set: the booking webhook will reject every request.",
        RuntimeWarning,
    )




# ─────────────────────── Phone verification & SMS ───────────────────────
# Read by api/views.py. With REQUIRE_PHONE_VERIFICATION on, registration needs a verified
# phone, so SMS_BACKEND must point at a working sender (dotted path to function(phone, message)).
REQUIRE_PHONE_VERIFICATION = env_bool("REQUIRE_PHONE_VERIFICATION", False)
SMS_BACKEND = os.environ.get("SMS_BACKEND") or None
if REQUIRE_PHONE_VERIFICATION and not SMS_BACKEND and not DEBUG:
    warnings.warn(
        "REQUIRE_PHONE_VERIFICATION is on but SMS_BACKEND is not set: no verification codes "
        "can be sent.",
        RuntimeWarning,
    )
TRACKING_BASE_URL = os.environ.get("TRACKING_BASE_URL", "https://rydnow.app/track")




# ─────────────────────── Pricing ───────────────────────
# Read by api/pricing.py. The website's fallback price-lock timer assumes 5 minutes
# (QUOTE_FALLBACK_TTL_SECONDS in lib/rydnow/config.ts), so change both together.
PRICING_QUOTE_VALIDITY_MINUTES = env_int("PRICING_QUOTE_VALIDITY_MINUTES", 5)
# Optional separate key for signing quotes. Falls back to SECRET_KEY when unset.
# Changing either key invalidates quotes that are already issued (they live a few minutes).
PRICING_SIGNING_KEY = os.environ.get("PRICING_SIGNING_KEY") or None
# HTTP-date sent in the Sunset header of the deprecated /bookings/quote/ endpoint (optional).
LEGACY_QUOTE_SUNSET = os.environ.get("LEGACY_QUOTE_SUNSET", "")




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




# ─────────────────── Error monitoring (optional) ───────────────────
# Set SENTRY_DSN to enable. Needs: pip install sentry-sdk (add it to requirements.txt).
SENTRY_DSN = os.environ.get("SENTRY_DSN", "")
if SENTRY_DSN:
    try:
        import sentry_sdk


        sentry_sdk.init(
            dsn=SENTRY_DSN,
            environment="development" if DEBUG else "production",
            traces_sample_rate=env_float("SENTRY_TRACES_SAMPLE_RATE", 0.0),
            send_default_pii=False,  # never send phone numbers, emails or tokens
        )
    except ImportError:
        warnings.warn(
            "SENTRY_DSN is set but sentry-sdk is not installed: errors will not be reported.",
            RuntimeWarning,
        )

