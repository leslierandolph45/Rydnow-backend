#RYDNOW - api/models.py  (single consolidated file)
#==================================================
#Requires Django 5.1+ (CheckConstraint uses `condition=`), PostgreSQL, django-phonenumber-field.
"""
DESIGN RULES
  1. pricing.py is the ONLY fare engine. Booking stores the quote snapshot and never recomputes
     a fare from DB rate tables (PricingRule / SurgeRule are deprecated and unused).
  2. One Booking model, one rider model (RiderProfile), one WebhookEvent, one PromoCode.
  3. Money invariant on every booking:  price == rider_earnings + platform_fee
        platform_fee   = commission + booking_fee - promo_discount   (company revenue)
        rider_earnings = trip earnings - commission                  (promos never cut rider pay)
  4. Every wallet movement goes through a locked, idempotent ledger posting.
  5. Booking lifecycle lives on the model (accept / mark_arrived / start / complete / cancel /
     release_rider). Views and services call these; they never edit status or money directly.

Sections
  0 Helpers & constants      5 Growth (promo, referral, business, waste, recurring)
  1 Platform (Service, Zone) 6 Booking core
  2 Customers                7 Parcel, offers, reviews, complaints, incidents, support, safety
  3 Riders                   8 Rider growth
  4 Money                    9 Platform ops (notifications, audit, alerts, summaries)
"""
import calendar
import copy
import logging
import re
import secrets
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from math import asin, cos, radians, sin, sqrt
from django.db import models

from django.conf import settings
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.core.validators import (
    FileExtensionValidator, MaxValueValidator, MinValueValidator, RegexValidator,
)
from django.db import IntegrityError, models, transaction
from django.db.models import Avg, Case, Count, DecimalField, F, Q, Sum, When
from django.db.models.functions import Lower
from django.dispatch import Signal
from django.urls import reverse
from django.utils import timezone
from phonenumber_field.modelfields import PhoneNumberField
from django.db.models import UniqueConstraint
from django.utils.translation import gettext_lazy as _

from . import pricing

logger = logging.getLogger(__name__)


# =====================================================================
# 0. EXCEPTIONS, VALIDATORS, HELPERS, CONSTANTS
# =====================================================================
class WalletError(Exception):
    """Raised inside a wallet mutation to refuse it (e.g. insufficient balance)."""


class PriceLocked(Exception):
    """Raised when code tries to change a money field outside the pricing flow."""


class Weekday(models.IntegerChoices):
    """Values match Python date.weekday(). """
    MONDAY = 0, "Monday"
    TUESDAY = 1, "Tuesday"
    WEDNESDAY = 2, "Wednesday"
    THURSDAY = 3 , "Thursday"
    FRIDAY = 4 , "Friday"
    SATURDAY = 5, "Saturday"
    SUNDAY = 6, "Sunday"


class ServiceType(models.TextChoices):
    OKADA = "okada", "Okada"
    PARCEL = "parcel", "Parcel"
    WASTE_BIN = "waste-bin", "Waste Bin"


class BookingFrequency(models.TextChoices):
    WEEKLY = "weekly", "Weekly"
    BIWEEKLY = "biweekly", "Every 2 Weeks"
    MONTHLY = "monthly", "Monthly"


class LanguageChoices(models.TextChoices):
    ENGLISH = "en", "English"
    TWI = "tw", "Twi"
    GA = "gaa", "Ga"
    EWE = "ee", "Ewe"
    HAUSA = "ha", "Hausa"


class AppendOnlyModel(models.Model):
    """Rows can be created but never edited, so the record stays trustworthy."""

    class Meta:
        abstract = True

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValueError(f"{type(self).__name__} entries are immutable.")
        super().save(*args, **kwargs)


class TimeStampedModel(models.Model):
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


MONEY_STEP = Decimal("0.01")
MAX_AMOUNT = Decimal("10000000000")  # fits max_digits=12, decimal_places=2
ZERO = Decimal("0.00")
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O/1/I: readable over the phone
REFERENCE_ALPHABET = CODE_ALPHABET
PERCENT_VALIDATORS = [MinValueValidator(Decimal("0.00")), MaxValueValidator(Decimal("100.00"))]
MIN_DISCOUNT_PERCENT = Decimal("10")
PROMO_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O/1/I

phone_validator = RegexValidator(
    r"^\+[1-9]\d{7,14}$", "Enter phone in international format, e.g. +233241234567.")
ghana_card_validator = RegexValidator(r"^GHA-\d{9}-\d$", "Ghana Card must look like GHA-123456789-0.")
business_phone_validator = RegexValidator(
    r"^\+?[0-9]{7,15}$", "Enter a valid phone number: digits only, optional leading +.")


def validate_non_negative_amount(value):
    if value < Decimal("0"):
        raise ValidationError("Amount cannot be negative.")


def validate_positive_amount(value):
    if value <= Decimal("0"):
        raise ValidationError("Amount must be greater than zero.")


def validate_phone_number(value):
    if not value:
        return
    digits = "".join(filter(str.isdigit, value))
    if not 9 <= len(digits) <= 15:
        raise ValidationError("Phone number must contain 9-15 digits.")


def validate_latitude(value):
    if value is not None and not (-90.0 <= float(value) <= 90.0):
        raise ValidationError("Latitude must be between -90 and 90 degrees.")


def validate_longitude(value):
    if value is not None and not (-180.0 <= float(value) <= 180.0):
        raise ValidationError("Longitude must be between -180 and 180 degrees.")


# ---- reference generators ------------------------------------------------
def _ref(prefix, n=12):
    return f"{prefix}-{uuid.uuid4().hex[:n].upper()}"


def _dated_ref(prefix):
    return f"{prefix}-{timezone.now():%Y%m%d}-{secrets.token_hex(3).upper()}"


def generate_tracking_code(): return _ref("PCL", 10)
def generate_deposit_reference(): return _ref("DEP")
def generate_refund_reference(): return _ref("RFD")
def generate_payment_reference(): return _ref("PAY")
def generate_recurring_reference(): return _ref("RBS", 10)
def generate_complaint_reference(): return _ref("CMP", 10)
def generate_subscription_reference(): return _ref("WST", 10)
def generate_collection_reference(): return _ref("WCL", 10)
def generate_txn_reference(): return _ref("TXN")
def generate_withdrawal_reference(): return _ref("WDR")
def generate_transaction_ref(): return _ref("TX")
def generate_ticket_ref(): return _dated_ref("TKT")
def generate_incident_reference(): return _dated_ref("INC")
def generate_alert_reference(): return _dated_ref("SOS")
def generate_pin(): return f"{secrets.randbelow(10000):04d}"
generate_delivery_pin = generate_pin


def generate_code(prefix, length=6):
    return f"{prefix}-" + "".join(secrets.choice(CODE_ALPHABET) for __ in range(length))


def generate_referral_code():
    return generate_code("REF", 8)


def unique_code(model, field, prefix, length=6):
    for __ in range(10):
        code = generate_code(prefix, length)
        if not model.objects.filter(**{field: code}).exists():
            return code
    return generate_code(prefix, length + 2)


REFERRAL_VALIDITY_DAYS = 30


def default_referral_expiry():
    return timezone.now() + timedelta(days=REFERRAL_VALIDITY_DAYS)


def parcel_proof_upload_to(instance, filename):
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else "jpg"
    return f"parcel_proofs/{instance.parcel.tracking_code}/{uuid.uuid4().hex}.{ext}"


def waste_proof_upload_to(instance, filename):
    ext = re.sub(r"[^a-z0-9]", "", filename.rsplit(".", 1)[-1].lower())[:5] if "." in filename else ""
    return f"waste_proofs/{instance.reference}/{uuid.uuid4().hex}.{ext or 'jpg'}"


# ---- money & maths --------------------------------------------------------
def round_money(value):
    """Consistent 2-decimal rounding - ALL financial math uses this."""
    return Decimal(str(value)).quantize(MONEY_STEP, rounding=ROUND_HALF_UP)


def to_money(value):
    """Validate a positive amount. Floats are rejected to avoid rounding drift."""
    if isinstance(value, float):
        raise TypeError("Pass money as Decimal or str, never float.")
    try:
        amount = Decimal(str(value))
    except InvalidOperation:
        raise ValidationError("Invalid amount.", code="invalid_amount")
    if not amount.is_finite() or amount <= 0 or amount >= MAX_AMOUNT:
        raise ValidationError("Amount must be greater than zero.", code="invalid_amount")
    if amount != amount.quantize(MONEY_STEP):
        raise ValidationError("Amount can have at most 2 decimal places.", code="invalid_amount")
    return amount


def to_percent(value):
    if isinstance(value, float):
        raise TypeError("Pass percentages as Decimal or str, never float.")
    try:
        pct = Decimal(str(value))
    except InvalidOperation:
        raise ValidationError("Invalid percentage.", code="invalid_percent")
    if not pct.is_finite() or not 0 <= pct <= 100:
        raise ValidationError("Discount must be between 0 and 100.", code="invalid_percent")
    return pct.quantize(MONEY_STEP)


def apply_discount(base_price, percent):
    factor = Decimal("1") - Decimal(percent) / Decimal("100")
    return (Decimal(base_price) * factor).quantize(MONEY_STEP, rounding=ROUND_HALF_UP)


def to_coord(value):
    return None if value is None else Decimal(str(value)).quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)


def add_months(d, months, anchor_day=None):
    """Add months, clamping to the end of short months (31 Jan + 1 month = 28/29 Feb)."""
    index = d.month - 1 + months
    year, month = d.year + index // 12, index % 12 + 1
    return d.replace(year=year, month=month, day=min(anchor_day or d.day, calendar.monthrange(year, month)[1]))


def nth_weekday_of_month(year, month, weekday, ordinal):
    # Get the nth weekday of a given month/ If that date does not exist, returns the last one available. ///
    day = 1 + (weekday - date(year, month, 1).weekday()) % 7 + (ordinal - 1) * 7
    last = calendar.monthrange(year, month)[1]
    while day > last:
        day -= 7
    return date(year, month, day)


def haversine_km(lat1, lng1, lat2, lng2):
    """Great-circle distance in km (no PostGIS needed). None if any input is missing."""
    if None in (lat1, lng1, lat2, lng2):
        return None
    lat1, lat2 = (max(-90.0, min(90.0, float(v))) for v in (lat1, lat2))
    lng1, lng2 = (max(-180.0, min(180.0, float(v))) for v in (lng1, lng2))
    dlat, dlng = radians(lat2 - lat1), radians(lng2 - lng1)
    a = sin(dlat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlng / 2) ** 2
    return round(6371 * 2 * asin(sqrt(max(0.0, min(1.0, a)))), 2)

import re

def normalize_gh_phone(value):
    """
    Normalize Ghanaian phone numbers to +233 format.
    """

    value = re.sub(r"[\s\-()]", "", str(value or ""))

    if value.startswith("0"):
        value = "+233" + value[1:]

    elif value.startswith("233"):
        value = "+" + value

    if not re.fullmatch(r"\+233\d{9}", value):
        raise ValueError("Enter a valid Ghanaian phone number.")

    return value


def audit(actor, action, obj, **changes):
    """Append an AuditLog row. Values are stringified so Decimals/dates are JSON-safe."""
    AuditLog.record(actor, action, obj, changes={k: str(v) for k, v in changes.items()})


def _safely(label, fn, *args, **kwargs):
    """Run a NON-critical side effect (notification, loyalty, referral...) inside a savepoint.
    A failure is logged and swallowed so it can never block a trip completion or a payout."""
    try:
        with transaction.atomic():
            return fn(*args, **kwargs)
    except Exception:
        logger.exception("Non-critical step failed: %s", label)
        return None


def normalize_promo_code(raw):
    return re.sub(r"\s+", "", str(raw or "")).upper()


def generate_promo_code(prefix="", length=6):
    """Random code such as 'FRI-7K2M9Q' (pass prefix='FRI-'). Check uniqueness on save."""
    body = "".join(secrets.choice(PROMO_ALPHABET) for _ in range(length))
    return f"{prefix}{body}"


@dataclass(frozen=True)
class PromoQuote:
    promo: "PromoCode"
    order_amount: Decimal
    discount: Decimal
    final_amount: Decimal
    currency: str

# ---- business rules --------------------------------------------------------
COMMISSION_RATE = Decimal("0.15")
TIER_COMMISSION_RATES = {
    "standard": Decimal("0.15"), "silver": Decimal("0.14"),
    "gold": Decimal("0.13"), "platinum": Decimal("0.12"),
}
# (tier, min completed trips, min rating, min acceptance %) - checked top-down
RIDER_TIER_RULES = [("platinum", 1000, 4.8, 90.0), ("gold", 400, 4.7, 85.0), ("silver", 100, 4.5, 80.0)]
CURRENCY_CHOICES = [(pricing.CURRENCY, "Ghana Cedi")]

MIN_SAMPLE = 10
LOW_ACCEPTANCE = 70.0
HIGH_EXPIRY = 30.0
HIGH_CANCEL = 20.0
LOW_RATING = 4.0
SLOW_RESPONSE = 60
LICENCE_WARNING_DAYS = 30
MAX_CONTACTS_PER_RIDER = 3
MAX_STRIKES = 3
LOCATION_STALE_SECONDS = 120
MAX_CODE_ATTEMPTS = 5                      # start_code / recipient_code brute-force cap
DISPATCH_LEAD = timedelta(minutes=30)      # scheduled jobs become visible this long before pickup
POINTS_PER_GHS_REDEEMED = 100              # 100 pts = GH₵1 bonus credit
LOYALTY_TIERS = [("platinum", 5000, Decimal("1.5")), ("gold", 2000, Decimal("1.25")),
                 ("silver", 500, Decimal("1.1")), ("bronze", 0, Decimal("1.0"))]
OFFLINE_METHODS = ("cash", "invoice")      # no digital funds are held
PROVIDER_METHODS = ("momo", "card")        # refunded through the payment provider
DIGITAL_METHODS = ("wallet",) + PROVIDER_METHODS

SERVICE_CHOICES = [("okada", "Okada Ride"), ("parcel", "Parcel Delivery"), ("waste-bin", "Waste Collection")]
# Fail fast if models and pricing.RATES ever drift apart.
assert {k for k, __ in SERVICE_CHOICES} == set(pricing.RATES), \
    "SERVICE_CHOICES and pricing.RATES have drifted apart."


class BookingStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    ACCEPTED = "accepted", "Rider accepted"
    ARRIVED = "arrived", "Arrived at pickup"
    IN_TRANSIT = "in_transit", "In transit"
    COMPLETED = "completed", "Completed"
    CANCELLED = "cancelled", "Cancelled"


ACTIVE_BOOKING_STATUSES = ("accepted", "arrived", "in_transit")   # a rider is committed
TERMINAL_BOOKING_STATUSES = ("completed", "cancelled")
# Releasing a rider (accepted/arrived -> pending) is NOT here on purpose: it only happens through
# Booking.release_rider(), so a rider cannot PATCH a job back into the pool.
STATUS_TRANSITIONS = {
    "pending": ["accepted", "cancelled"],
    "accepted": ["arrived", "cancelled"],
    "arrived": ["in_transit", "cancelled"],
    "in_transit": ["completed"],
    "completed": [],
    "cancelled": [],
}
STATUS_CHOICES = BookingStatus.choices


class PaymentStatus(models.TextChoices):
    INITIATED = "initiated", "Awaiting payment"
    PENDING_CONFIRMATION = "pending_confirmation", "Waiting provider confirmation"
    HELD = "held", "Funds held in escrow"
    RELEASED = "released", "Funds released"
    FAILED = "failed", "Payment failed"
    REFUND_INITIATED = "refund_initiated", "Refund processing"
    PARTIALLY_REFUNDED = "partially_refunded", "Partially refunded"
    REFUNDED = "refunded", "Refund completed"
    CANCELLED = "cancelled", "Cancelled before processing"


PAYMENT_STATUS = PaymentStatus.choices
PAYMENT_METHOD_CHOICES = [
    ("momo", "Mobile Money"), ("card", "Debit / Credit Card"), ("wallet", "RYDNOW Wallet"),
    ("cash", "Cash"), ("invoice", "Business Invoice"),
]
TRANSACTION_TYPES = [   # customer wallet ledger
    ("job_payment", "Completed Job Payment"), ("deposit", "Wallet Top-up"),
    ("withdrawal", "Withdrawal"), ("refund", "Customer Refund"),
    ("bonus", "Platform Bonus Credit"), ("referral_bonus", "Referral Bonus"),
    ("adjustment", "Manual Adjustment"), ("reversal", "Transaction Reversal"),
]


# =====================================================================
# 1. PLATFORM: CONFIG, SERVICES, ZONES
# =====================================================================
class SystemConfig(models.Model):
    """ Runtime settings editable in admin (cached  for 60 seconds). Doubles as a feature-flag store """
    key = models.CharField(max_length=100, unique=True)
    value = models.TextField(blank=True)
    description = models.CharField(max_length=255, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    _MISSING = "__missing__"

    class Meta:
        verbose_name_plural = "System Config"

    def __str__(self):
        return f"{self.key} = {self.value}"

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        cache.delete(f"sysconfig:{self.key}")

    def delete(self, *args, **kwargs):
        cache.delete(f"sysconfig:{self.key}")
        return super().delete(*args, **kwargs)

    @classmethod
    def get_value(cls, key, default=None):
        ck = f"sysconfig:{key}"
        val = cache.get(ck)
        if val is None:
            try:
                val = cls.objects.get(key=key).value
            except cls.DoesNotExist:
                val = cls._MISSING            # negative-cache so a missing key doesn't hit the DB
            cache.set(ck, val, 60)
        return default if val == cls._MISSING else val

    @classmethod
    def get_decimal(cls, key, default="0"):
        try:
            return Decimal(str(cls.get_value(key, default)))
        except Exception:
            return Decimal(str(default))

    @classmethod
    def is_enabled(cls, key, default=False):
        return str(cls.get_value(key, "true" if default else "false")).lower() in ("1", "true", "yes", "on")

class Service(models.Model):
    """Canonical registry of services (the ONLY Service model). Deactivate to hide from new bookings."""
    SERVICE_OKADA = "okada"
    SERVICE_PARCEL = "parcel"
    SERVICE_WASTE_BIN = "waste-bin"
    SERVICE_KEYS = SERVICE_CHOICES

    key = models.CharField(max_length=30, primary_key=True, choices=SERVICE_KEYS, verbose_name="Service ID")
    name = models.CharField(max_length=100, verbose_name="Display Name")
    description = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = "rydnow_service"
        ordering = ["key"]

    def __str__(self):
        return f"{self.name} ({'Active' if self.is_active else 'Inactive'})"



class Zone(models.Model):
    """Operating area (e.g. Accra Central). Drives rider assignment, reporting and expansion."""
    name = models.CharField(max_length=100, unique=True)
    city = models.CharField(max_length=100, blank=True)
    center_lat = models.FloatField(validators=[validate_latitude])
    center_lng = models.FloatField(validators=[validate_longitude])
    radius_km = models.FloatField(default=10.0, validators=[MinValueValidator(0.5)])
    services = models.ManyToManyField(Service, blank=True, related_name="zones",
                                      help_text="Empty = all services available.")
    is_active = models.BooleanField(default=True, db_index=True)
    launched_at = models.DateField(null=True, blank=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name

    def contains(self, lat, lng):
        d = haversine_km(self.center_lat, self.center_lng, lat, lng)
        return d is not None and d <= self.radius_km

    @classmethod
    def for_point(cls, lat, lng):
        """Closest active zone containing the point (few zones, so a scan is fine)."""
        best, best_d = None, None
        for z in cls.objects.filter(is_active=True):
            d = haversine_km(z.center_lat, z.center_lng, lat, lng)
            if d is not None and d <= z.radius_km and (best_d is None or d < best_d):
                best, best_d = z, d
        return best


class PricingRule(models.Model):
    """DEPRECATED - NOT USED FOR FARES. pricing.RATES is the only fare engine. Kept so existing
    data/admin keep working; delete in a dedicated migration."""
    service = models.ForeignKey(Service, on_delete=models.PROTECT, related_name="pricing_rules")
    zone = models.ForeignKey(Zone, null=True, blank=True, on_delete=models.CASCADE, related_name="pricing_rules")
    base_fare = models.DecimalField(max_digits=8, decimal_places=2, default=Decimal("3.00"))
    per_km = models.DecimalField(max_digits=8, decimal_places=2, default=Decimal("1.50"))
    per_minute = models.DecimalField(max_digits=8, decimal_places=2, default=Decimal("0.20"))
    booking_fee = models.DecimalField(max_digits=8, decimal_places=2, default=Decimal("0.00"))
    minimum_fare = models.DecimalField(max_digits=8, decimal_places=2, default=Decimal("5.00"))
    cancellation_fee = models.DecimalField(max_digits=8, decimal_places=2, default=Decimal("2.00"))
    is_active = models.BooleanField(default=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["service", "zone"], condition=Q(is_active=True),
                                    name="unique_active_pricing_per_service_zone"),
            # NULL zones are "distinct" in SQL, so this stops two active GLOBAL rules per service.
            models.UniqueConstraint(fields=["service"], condition=Q(is_active=True, zone__isnull=True),
                                    name="unique_active_global_pricing_per_service"),
        ]

    def __str__(self):
        return f"[deprecated] {self.service_id} @ {self.zone or 'Global'}"


class SurgeRule(models.Model):
    """DEPRECATED - NOT READ BY pricing.py (it derives surge from demand counters / peak hours)."""
    service = models.ForeignKey(Service, null=True, blank=True, on_delete=models.CASCADE, related_name="surge_rules")
    zone = models.ForeignKey(Zone, null=True, blank=True, on_delete=models.CASCADE, related_name="surge_rules")
    multiplier = models.DecimalField(max_digits=4, decimal_places=2, default=Decimal("1.25"),
                                     validators=[MinValueValidator(Decimal("1.00")), MaxValueValidator(Decimal("3.00"))])
    reason = models.CharField(max_length=120, blank=True)
    starts_at = models.DateTimeField()
    ends_at = models.DateTimeField()
    is_active = models.BooleanField(default=True, db_index=True)

    class Meta:
        indexes = [models.Index(fields=["is_active", "starts_at", "ends_at"])]

    def __str__(self):
        return f"[deprecated] x{self.multiplier} {self.zone or 'All zones'} ({self.reason or 'surge'})"

    def clean(self):
        super().clean()
        if self.starts_at and self.ends_at and self.ends_at <= self.starts_at:
            raise ValidationError({"ends_at": "End time must be after the start time."})


# =====================================================================
# 2. CUSTOMERS
# =====================================================================
class CustomerProfile(models.Model):

    class Segment(models.TextChoices):
        NEW = "new", "New"
        ACTIVE = "active", "Active"
        VIP = "vip", "VIP"
        AT_RISK = "at_risk", "At Risk"
        CHURNED = "churned", "Churned"

    Language = LanguageChoices

    class Source(models.TextChoices):
        ORGANIC = "organic", "Organic"
        REFERRAL = "referral", "Referral"
        PROMO = "promo", "Promo"
        SOCIAL = "social", "Social Media"
        AGENT = "agent", "Field Agent"
        OTHER = "other", "Other"

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="customer_profile")
    full_name = models.CharField(max_length=200, blank=True, default="")
    email = models.EmailField(blank=True, default="")
    phone = models.CharField(max_length=30, blank=True, null=True, validators=[validate_phone_number])
    mobile = PhoneNumberField(region="GH", blank=True, null=True)
    profile_photo = models.ImageField(upload_to="profiles/customer/%Y/%m/", blank=True, null=True)

    preferred_language = models.CharField(max_length=5, choices=LanguageChoices.choices, default=LanguageChoices.ENGLISH)
    acquisition_source = models.CharField(max_length=20, choices=Source.choices, default=Source.ORGANIC)
    marketing_opt_in = models.BooleanField(default=False, help_text="Explicit consent for promo SMS/push.")
    segment = models.CharField(max_length=10, choices=Segment.choices, default=Segment.NEW, db_index=True)
    total_bookings = models.PositiveIntegerField(default=0)
    total_spent = models.DecimalField(max_digits=12, decimal_places=2, default=ZERO)
    last_booking_at = models.DateTimeField(null=True, blank=True)
    rating_average = models.FloatField(default=5.0, validators=[MinValueValidator(0), MaxValueValidator(5)])
    rating_count = models.PositiveIntegerField(default=0)
    outstanding_fees = models.DecimalField(max_digits=8, decimal_places=2, default=ZERO,
                                           help_text="Unpaid cancellation/extra-wait fees; block new bookings until cleared.")
    internal_notes = models.TextField(blank=True, default="", help_text="Staff only.")
    is_blocked = models.BooleanField(default=False, db_index=True)
    blocked_reason = models.CharField(max_length=255, blank=True, default="")

    referral_code = models.CharField(max_length=16, unique=True, blank=True, null=True, editable=False)
    referred_by = models.ForeignKey("self", null=True, blank=True, on_delete=models.SET_NULL,
                                    related_name="customers_referred")

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["segment", "-last_booking_at"])]

    def __str__(self):
        return self.full_name or getattr(self.user, "username", None) or f"CustomerProfile-{self.pk}"

    def save(self, *args, **kwargs):
        if self.email:
            self.email = self.email.strip().lower()
        if not self.referral_code and not kwargs.get("update_fields"):
            self.referral_code = unique_code(CustomerProfile, "referral_code", "RYD", 6)
        super().save(*args, **kwargs)

    @property
    def can_book(self):
        return not self.is_blocked and (self.outstanding_fees or ZERO) <= 0

    @property
    def loyalty_tier(self):
        acct = getattr(self, "loyalty", None)
        return acct.tier if acct else "bronze"

    def get_wallet(self):
        return CustomerWallet.for_user(self.user)

    def recompute_segment(self, save=True):
        now = timezone.now()
        bookings, spent = self.total_bookings or 0, self.total_spent or ZERO
        if bookings == 0:
            seg = self.Segment.NEW
        elif self.last_booking_at and self.last_booking_at < now - timedelta(days=60):
            seg = self.Segment.CHURNED
        elif self.last_booking_at and self.last_booking_at < now - timedelta(days=30):
            seg = self.Segment.AT_RISK
        elif bookings >= 20 or spent >= Decimal("1000"):
            seg = self.Segment.VIP
        else:
            seg = self.Segment.ACTIVE
        self.segment = seg
        if save:
            CustomerProfile.objects.filter(pk=self.pk).update(segment=seg)
        return seg

    def record_completed_booking(self, booking):
        CustomerProfile.objects.filter(pk=self.pk).update(
            total_bookings=F("total_bookings") + 1,
            total_spent=F("total_spent") + booking.price,
            last_booking_at=timezone.now(),
        )
        self.refresh_from_db(fields=["total_bookings", "total_spent", "last_booking_at"])
        self.recompute_segment()

    def apply_rating(self, rating):
        with transaction.atomic():
            c = CustomerProfile.objects.select_for_update().get(pk=self.pk)
            avg = c.rating_average if c.rating_average is not None else 5.0
            count = c.rating_count or 0
            c.rating_count = count + 1
            c.rating_average = round((avg * count + rating) / c.rating_count, 2)
            c.save(update_fields=["rating_average", "rating_count", "updated_at"])
        self.rating_average, self.rating_count = c.rating_average, c.rating_count


class SavedAddress(models.Model):
    class Label(models.TextChoices):
        HOME = "home", "Home"
        WORK = "work", "Work"
        OTHER = "other", "Other"

    customer = models.ForeignKey(CustomerProfile, on_delete=models.CASCADE, related_name="saved_addresses")
    label = models.CharField(max_length=10, choices=Label.choices, default=Label.OTHER)
    name = models.CharField(max_length=100, blank=True)
    address = models.CharField(max_length=255)
    lat = models.FloatField(validators=[validate_latitude])
    lng = models.FloatField(validators=[validate_longitude])
    landmark_note = models.CharField(max_length=255, blank=True,
                                     help_text="Directions help riders where street addresses are patchy.")
    is_default = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-is_default", "label"]
        constraints = [models.UniqueConstraint(fields=["customer"], condition=Q(is_default=True),
                                               name="unique_default_address_per_customer")]

    def __str__(self):
        return f"{self.customer} - {self.get_label_display()}: {self.address}"

    def save(self, *args, **kwargs):
        with transaction.atomic():
            if self.is_default:
                SavedAddress.objects.filter(customer=self.customer, is_default=True).exclude(pk=self.pk).update(is_default=False)
            super().save(*args, **kwargs)


class CustomerWallet(models.Model):
    """balance = spendable cash; withdrawable_balance <= balance; bonus_credit = promo money (spend-only, spent first)."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="customer_wallet")
    balance = models.DecimalField(max_digits=12, decimal_places=2, default=ZERO, validators=[validate_non_negative_amount])
    withdrawable_balance = models.DecimalField(max_digits=12, decimal_places=2, default=ZERO,
                                               validators=[validate_non_negative_amount])
    bonus_credit = models.DecimalField(max_digits=12, decimal_places=2, default=ZERO,
                                       validators=[validate_non_negative_amount])
    currency = models.CharField(max_length=3, choices=CURRENCY_CHOICES, default=pricing.CURRENCY)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [  # validators don't run on save(); the database is the last line of defence
            models.CheckConstraint(condition=Q(balance__gte=0), name="custwallet_balance_gte_0"),
            models.CheckConstraint(condition=Q(withdrawable_balance__gte=0), name="custwallet_withdrawable_gte_0"),
            models.CheckConstraint(condition=Q(bonus_credit__gte=0), name="custwallet_bonus_gte_0"),
            models.CheckConstraint(condition=Q(withdrawable_balance__lte=F("balance")),
                                   name="custwallet_withdrawable_lte_balance"),
        ]

    def __str__(self):
        return f"{getattr(self.user, 'username', 'Deleted User')} - {self.currency} {self.balance}"

    @classmethod
    def for_user(cls, user):
        wallet, __ = cls.objects.get_or_create(user=user)
        return wallet

    def _apply(self, tx_type, deltas_fn, *, reference=None, description="", related_booking=None):
        """
        One locked, idempotent, ledgered mutation. deltas_fn(wallet) ->
        (balance_delta, withdrawable_delta, bonus_delta, signed_tx_amount); raise WalletError to refuse.
        Returns (ok, message). A repeated reference is a successful no-op.
        """
        ref = reference or generate_transaction_ref()
        try:
            with transaction.atomic():
                if CustomerWalletTransaction.objects.filter(reference=ref).exists():
                    return True, "Already recorded (idempotent)"
                w = CustomerWallet.objects.select_for_update().get(pk=self.pk)
                d_bal, d_wd, d_bonus, tx_amount = deltas_fn(w)
                w.balance += d_bal
                w.withdrawable_balance += d_wd
                w.bonus_credit += d_bonus
                if min(w.balance, w.withdrawable_balance, w.bonus_credit) < 0:
                    raise WalletError("Insufficient balance")
                w.save(update_fields=["balance", "withdrawable_balance", "bonus_credit", "updated_at"])
                CustomerWalletTransaction.objects.create(
                    customer_wallet=w, transaction_type=tx_type, amount=tx_amount,
                    balance_after=w.balance, reference=ref, description=description[:255],
                    currency=w.currency, related_booking=related_booking, status="completed")
                self.balance, self.withdrawable_balance, self.bonus_credit = (
                    w.balance, w.withdrawable_balance, w.bonus_credit)
                return True, "OK"
        except WalletError as e:
            return False, str(e)
        except IntegrityError:
            if CustomerWalletTransaction.objects.filter(reference=ref).exists():
                return True, "Already recorded (idempotent)"
            logger.exception("Customer wallet posting failed for %s", ref)
            return False, "Database error"

    def deposit(self, amount, reference=None, description=""):
        amount = round_money(amount)
        validate_positive_amount(amount)
        return self._apply("deposit", lambda w: (amount, amount, ZERO, amount),
                           reference=reference, description=description or "Wallet deposit")

    def withdraw(self, amount, reference=None, description=""):
        amount = round_money(amount)
        validate_positive_amount(amount)

        def fn(w):
            if w.balance < amount:
                raise WalletError("Insufficient total balance")
            if w.withdrawable_balance < amount:
                raise WalletError("Insufficient withdrawable balance")
            return -amount, -amount, ZERO, -amount
        ok, msg = self._apply("withdrawal", fn, reference=reference, description=description)
        return ok, "Withdrawal successful" if ok else msg

    def pay_for_booking(self, amount, booking, reference=None):
        """Spend bonus credit first, then cash balance.

        The bonus portion is deducted from `bonus_credit` only; `balance` includes bonus_credit
        is NOT assumed - they are separate pots, so only the cash portion touches `balance`."""
        amount = round_money(amount)
        validate_positive_amount(amount)

        def fn(w):
            from_bonus = min(w.bonus_credit, amount)
            from_cash = amount - from_bonus
            if w.balance < from_cash:
                raise WalletError("Insufficient wallet balance")
            from_withdrawable = min(w.withdrawable_balance, from_cash)
            return -from_cash, -from_withdrawable, -from_bonus, -amount
        return self._apply("job_payment", fn, reference=reference or f"PAYWAL-{booking.reference}",
                           description=f"Payment for {booking.reference}", related_booking=booking)

    def refund_to_wallet(self, amount, booking=None, reference=None, description="Refund"):
        amount = round_money(amount)
        validate_positive_amount(amount)
        return self._apply("refund", lambda w: (amount, amount, ZERO, amount),
                           reference=reference, description=description, related_booking=booking)

    def add_bonus(self, amount, reference=None, description="Bonus", tx_type="bonus"):
        amount = round_money(amount)
        validate_positive_amount(amount)
        return self._apply(tx_type, lambda w: (ZERO, ZERO, amount, amount),
                           reference=reference, description=description)


class CustomerWalletTransaction(AppendOnlyModel):
    """Append-only customer wallet ledger (separate from the rider ledger, WalletTransaction)."""
    customer_wallet = models.ForeignKey(CustomerWallet, on_delete=models.PROTECT, related_name="transactions")
    transaction_type = models.CharField(max_length=20, choices=TRANSACTION_TYPES)
    amount = models.DecimalField(max_digits=12, decimal_places=2, help_text="Signed: negative = money out.")
    balance_after = models.DecimalField(max_digits=12, decimal_places=2)
    reference = models.CharField(max_length=60, unique=True)
    description = models.CharField(max_length=255, blank=True, default="")
    currency = models.CharField(max_length=3, default=pricing.CURRENCY)
    related_booking = models.ForeignKey("Booking", null=True, blank=True, on_delete=models.SET_NULL,
                                        related_name="customer_wallet_transactions")
    status = models.CharField(max_length=20, default="completed")
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        db_table = "customer_wallet_transactions"
        ordering = ["-created_at", "-id"]
        indexes = [models.Index(fields=["customer_wallet", "-created_at"], name="cwtx_wallet_created_idx")]

    def __str__(self):
        return f"{self.reference}: {self.amount:+} ({self.get_transaction_type_display()})"

    def delete(self, *args, **kwargs):
        raise ValueError("Wallet transactions cannot be deleted.")


class LoyaltyAccount(models.Model):
    """Points for every cedi spent. Tier unlocks a points multiplier and priority support."""
    customer = models.OneToOneField(CustomerProfile, on_delete=models.CASCADE, related_name="loyalty")
    points_balance = models.PositiveIntegerField(default=0)
    lifetime_points = models.PositiveIntegerField(default=0)
    tier = models.CharField(max_length=10, default="bronze", db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.customer} - {self.tier} ({self.points_balance} pts)"

    @staticmethod
    def tier_for(lifetime_points):
        for name, threshold, __ in LOYALTY_TIERS:
            if lifetime_points >= threshold:
                return name
        return "bronze"

    def multiplier(self):
        return next((m for n, __, m in LOYALTY_TIERS if n == self.tier), Decimal("1.0"))

    def earn(self, amount_spent, booking=None, reference=None):
        ref = reference or (f"LOY-{booking.reference}" if booking else f"LOY-{uuid.uuid4().hex[:10]}")
        with transaction.atomic():
            if LoyaltyTransaction.objects.filter(reference=ref).exists():
                return 0
            acct = LoyaltyAccount.objects.select_for_update().get(pk=self.pk)
            points = int(Decimal(str(amount_spent)) * acct.multiplier())
            if points <= 0:
                return 0
            acct.points_balance += points
            acct.lifetime_points += points
            acct.tier = self.tier_for(acct.lifetime_points)
            acct.save(update_fields=["points_balance", "lifetime_points", "tier", "updated_at"])
            LoyaltyTransaction.objects.create(account=acct, points=points, reason="Trip reward",
                                              booking=booking, reference=ref)
            self.points_balance, self.lifetime_points, self.tier = acct.points_balance, acct.lifetime_points, acct.tier
            return points

    def redeem(self, points):
        """Convert points into wallet bonus credit."""
        if points < POINTS_PER_GHS_REDEEMED:
            return False, f"Minimum redemption is {POINTS_PER_GHS_REDEEMED} points."
        with transaction.atomic():
            acct = LoyaltyAccount.objects.select_for_update().get(pk=self.pk)
            if acct.points_balance < points:
                return False, "Not enough points."
            cedis = round_money(Decimal(points) / POINTS_PER_GHS_REDEEMED)
            ref = f"LOYRED-{uuid.uuid4().hex[:10].upper()}"
            ok, msg = self.customer.get_wallet().add_bonus(cedis, reference=ref, description="Loyalty redemption")
            if not ok:
                return False, msg
            acct.points_balance -= points
            acct.save(update_fields=["points_balance", "updated_at"])
            LoyaltyTransaction.objects.create(account=acct, points=-points, reason="Redeemed for credit", reference=ref)
            self.points_balance = acct.points_balance
        return True, f"Redeemed {points} points for GH₵{cedis}"


class LoyaltyTransaction(models.Model):
    account = models.ForeignKey(LoyaltyAccount, on_delete=models.CASCADE, related_name="transactions")
    points = models.IntegerField()
    reason = models.CharField(max_length=100)
    booking = models.ForeignKey("Booking", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    reference = models.CharField(max_length=60, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.account_id}: {self.points:+d} ({self.reason})"




# =====================================================================
# 3. RIDERS
# =====================================================================
RIDER_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"   # no 0/O/1/I/L
RIDER_CODE_PREFIX = "RYD-"
RIDER_CODE_LENGTH = 8
RIDER_CODE_MAX_ATTEMPTS = 8
RIDER_REFERRAL_MILESTONE_TRIPS = 20     # referred rider's trips before the referrer is rewarded


def generate_rider_code():
    return RIDER_CODE_PREFIX + "".join(secrets.choice(RIDER_CODE_ALPHABET) for __ in range(RIDER_CODE_LENGTH))


def rider_profile_photo_path(instance, filename):
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else "jpg"
    return f"riders/{instance.public_id}/photo/{uuid.uuid4().hex}.{ext}"


class Gender(models.TextChoices):
    MALE = "male", "Male"
    FEMALE = "female", "Female"      # was "famale" - data migration required for old rows
    OTHER = "other", "Other"


class VerificationStatus(models.TextChoices):
    PENDING = "pending", "Pending Documents"
    UNDER_REVIEW = "under_review", "Under Review"
    VERIFIED = "verified", "Verified"
    REJECTED = "rejected", "Rejected"
    SUSPENDED = "suspended", "Suspended"


class RiderTier(models.TextChoices):
    STANDARD = "standard", "Standard"
    SILVER = "silver", "Silver"
    GOLD = "gold", "Gold"
    PLATINUM = "platinum", "Platinum"


class RiderProfileQuerySet(models.QuerySet):
    def not_deleted(self):
        return self.filter(deleted_at__isnull=True)

    def active(self):
        return self.not_deleted().filter(is_active=True)

    def verified(self):
        return self.filter(verification_status=VerificationStatus.VERIFIED)

    def not_suspended(self):
        return self.filter(Q(suspended_at__isnull=True) | Q(suspended_until__lte=timezone.now()))

    def not_restricted(self):
        return self.filter(Q(is_restricted=False) | Q(restricted_until__lte=timezone.now()))

    def dispatchable(self, service=None, max_location_age_seconds=LOCATION_STALE_SECONDS,
                     exclude_ids=None, require_deposit=True):
        """Riders who may receive an offer RIGHT NOW: active, verified, online, not restricted/suspended,
        licence valid, fresh GPS, qualified, funded (same minimum Booking.accept enforces) and idle."""
        now = timezone.now()
        qs = (self.active().verified().not_suspended().not_restricted()
              .filter(is_online=True, current_lat__isnull=False, current_lng__isnull=False,
                      location_updated_at__gte=now - timedelta(seconds=max_location_age_seconds))
              .filter(Q(license_expiry_date__isnull=True) | Q(license_expiry_date__gte=timezone.localdate()))
              .exclude(pk__in=Booking.objects.filter(status__in=ACTIVE_BOOKING_STATUSES, rider__isnull=False)
                       .values("rider_id")))
        if require_deposit:
            qs = qs.filter(wallet__balance__gte=SystemConfig.get_decimal("MIN_RIDER_BOOKING_BALANCE", "50.00"))
        if service is not None:
            qs = qs.filter(qualifications__service=service,
                           qualifications__status=ServiceQualification.Status.APPROVED)
        if exclude_ids:
            qs = qs.exclude(pk__in=exclude_ids)
        return qs.distinct()

    def nearby(self, lat, lng, radius_km=5, limit=10):
        """Bounding-box prefilter in SQL, exact Haversine in Python. Returns [(rider, km)] nearest first."""
        lat, lng = float(lat), float(lng)
        dlat = radius_km / 111.0
        dlng = radius_km / (111.0 * max(cos(radians(lat)), 0.01))
        cands = self.filter(current_lat__range=(lat - dlat, lat + dlat),
                            current_lng__range=(lng - dlng, lng + dlng))
        out = [(r, d) for r in cands
               if (d := haversine_km(r.current_lat, r.current_lng, lat, lng)) is not None and d <= radius_km]
        out.sort(key=lambda x: x[1])
        return out[:limit]


class RiderProfile(models.Model):
    """THE rider model (the legacy `Rider` model was removed - it duplicated this one)."""

    PROFILE_COMPLETION_FIELDS = (
        "full_name", "phone", "profile_photo", "date_of_birth", "gender", "ghana_card_number",
        "city", "address", "motorbike_model", "motorbike_plate_number", "motorbike_color",
        "license_number", "license_expiry_date",
    )

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="rider_profile")
    rider_code = models.CharField(max_length=16, unique=True, blank=True, editable=False)
    referred_by = models.ForeignKey("self", null=True, blank=True, on_delete=models.SET_NULL,
                                    related_name="referred_riders")
    zone = models.ForeignKey(Zone, null=True, blank=True, on_delete=models.SET_NULL, related_name="riders")
    services = models.ManyToManyField(Service, through="ServiceQualification", blank=True,
                                      related_name="qualified_riders")

    # Identity
    full_name = models.CharField(max_length=150)
    phone = models.CharField(max_length=30, unique=True, validators=[phone_validator])
    profile_photo = models.ImageField(upload_to=rider_profile_photo_path, null=True, blank=True)
    date_of_birth = models.DateField(null=True, blank=True)
    gender = models.CharField(max_length=10, choices=Gender.choices, blank=True)
    ghana_card_number = models.CharField(max_length=20, blank=True, validators=[ghana_card_validator])
    city = models.CharField(max_length=100, blank=True)
    address = models.CharField(max_length=255, blank=True)
    preferred_language = models.CharField(max_length=5, default="en")

    # Vehicle & licence
    motorbike_model = models.CharField(max_length=100, blank=True)
    motorbike_plate_number = models.CharField(max_length=20, blank=True)
    motorbike_color = models.CharField(max_length=30, blank=True)
    license_number = models.CharField(max_length=50, blank=True)
    license_expiry_date = models.DateField(null=True, blank=True)

    # Verification
    verification_status = models.CharField(max_length=20, choices=VerificationStatus.choices,
                                           default=VerificationStatus.PENDING)
    rejection_reason = models.TextField(blank=True)
    submitted_at = models.DateTimeField(null=True, blank=True)
    verified_at = models.DateTimeField(null=True, blank=True)
    verified_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                    related_name="verified_riders")

    # Suspension (admin action) / restriction (complaint outcome)
    suspended_at = models.DateTimeField(null=True, blank=True)
    suspended_until = models.DateTimeField(null=True, blank=True)
    suspension_reason = models.TextField(blank=True)
    is_restricted = models.BooleanField(default=False)
    restricted_until = models.DateTimeField(null=True, blank=True)
    restriction_notes = models.TextField(blank=True)

    # Live state
    is_online = models.BooleanField(default=False)
    last_online_at = models.DateTimeField(null=True, blank=True)
    online_since = models.DateTimeField(null=True, blank=True)
    current_lat = models.FloatField(null=True, blank=True, validators=[MinValueValidator(-90), MaxValueValidator(90)])
    current_lng = models.FloatField(null=True, blank=True, validators=[MinValueValidator(-180), MaxValueValidator(180)])
    location_updated_at = models.DateTimeField(null=True, blank=True)

    # Performance counters (updated with F() so they are race-safe)
    tier = models.CharField(max_length=10, choices=RiderTier.choices, default=RiderTier.STANDARD)
    rating_average = models.FloatField(default=5.0, validators=[MinValueValidator(0.0), MaxValueValidator(5.0)])
    rating_count = models.PositiveIntegerField(default=0)
    completed_trips = models.PositiveIntegerField(default=0)
    cancelled_trips = models.PositiveIntegerField(default=0)
    offers_received = models.PositiveIntegerField(default=0)
    offers_accepted = models.PositiveIntegerField(default=0)
    offers_declined = models.PositiveIntegerField(default=0)
    offers_expired = models.PositiveIntegerField(default=0)
    response_time_total_seconds = models.PositiveBigIntegerField(default=0)
    response_count = models.PositiveIntegerField(default=0)
    arrival_time_total_seconds = models.PositiveBigIntegerField(default=0)
    arrival_count = models.PositiveIntegerField(default=0)
    total_online_seconds = models.PositiveBigIntegerField(default=0)
    strike_count = models.PositiveSmallIntegerField(default=0)
    last_job_at = models.DateTimeField(null=True, blank=True)
    last_contacted_at = models.DateTimeField(null=True, blank=True)

    # Compliance & lifecycle
    terms_accepted_at = models.DateTimeField(null=True, blank=True)
    terms_version = models.CharField(max_length=20, blank=True)
    is_active = models.BooleanField(default=True)
    deleted_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = RiderProfileQuerySet.as_manager()

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Rider profile"
        verbose_name_plural = "Rider profiles"
        constraints = [
            models.UniqueConstraint(fields=["ghana_card_number"], condition=~Q(ghana_card_number=""),
                                    name="uniq_rider_ghana_card_set"),
            models.UniqueConstraint(fields=["license_number"], condition=~Q(license_number=""),
                                    name="uniq_rider_license_set"),
            models.CheckConstraint(condition=Q(rating_average__gte=0) & Q(rating_average__lte=5.0),
                                   name="rider_rating_0_to_5"),
            models.CheckConstraint(condition=~Q(referred_by=F("id")), name="rider_no_self_refer"),
        ]
        indexes = [
            models.Index(fields=["verification_status", "is_active"]),
            models.Index(fields=["is_online", "is_active"]),
            models.Index(fields=["current_lat", "current_lng"]),
            models.Index(fields=["-rating_average", "-completed_trips"]),
            models.Index(fields=["zone", "is_online"]),
        ]

    def __str__(self):
        return f"{self.full_name}  ({self.rider_code or 'no code'})"

    # ---- normalisation + collision-safe rider_code ------------------------
    def _normalize(self):
        self.full_name = (self.full_name or "").strip()
        self.phone = normalize_gh_phone(self.phone)
        self.ghana_card_number = (self.ghana_card_number or "").strip().upper()
        self.license_number = (self.license_number or "").strip().upper()
        self.motorbike_plate_number = (self.motorbike_plate_number or "").strip().upper()

    def save(self, *args, **kwargs):
        self._normalize()
        if self.rider_code:
            return super().save(*args, **kwargs)
        update_fields = kwargs.get("update_fields")
        if update_fields is not None:
            kwargs["update_fields"] = list(set(update_fields) | {"rider_code"})
        for __ in range(RIDER_CODE_MAX_ATTEMPTS):
            self.rider_code = generate_rider_code()
            try:
                with transaction.atomic():
                    return super().save(*args, **kwargs)
            except IntegrityError as exc:
                if "rider_code" not in str(exc):
                    raise
                self.rider_code = ""
        raise IntegrityError("Could not generate a unique rider code.")

    # ---- read-only helpers --------------------------------------------------
    @property
    def is_verified(self):
        return self.verification_status == VerificationStatus.VERIFIED

    @property
    def documents_uploaded(self):
        have = set(self.documents.values_list("document_type", flat=True))
        return {getattr(c, "value", c) for c, __ in RiderDocument.REQUIRED_TYPES} <= have

    @property
    def documents_complete(self):
        return not self.missing_required_documents()

    @property
    def profile_completion(self):
        filled = sum(1 for f in self.PROFILE_COMPLETION_FIELDS if getattr(self, f))
        return round(filled * 100 / len(self.PROFILE_COMPLETION_FIELDS))

    # aliases for serializers written against the old `Rider` model (filters must use real field names)
    @property
    def phone_number(self): return self.phone

    @property
    def plate_number(self): return self.motorbike_plate_number

    @property
    def latitude(self): return self.current_lat

    @property
    def longitude(self): return self.current_lng

    @property
    def last_location_at(self): return self.location_updated_at

    @property
    def rating(self): return self.rating_average

    @property
    def service_type(self):
        """First approved service key ('' if none). One query - avoid in list loops."""
        q = self.qualifications.filter(status=ServiceQualification.Status.APPROVED).order_by("created_at").first()
        return q.service_id if q else ""

    @property
    def status(self):
        """'offline' | 'available' | 'busy'. One query - avoid in list loops."""
        if not self.is_online:
            return "offline"
        return "busy" if self.bookings.filter(status__in=ACTIVE_BOOKING_STATUSES).exists() else "available"

    @property
    def is_suspended(self):
        if not self.suspended_at:
            return False
        return self.suspended_until is None or self.suspended_until > timezone.now()

    @property
    def is_currently_restricted(self):
        if not self.is_restricted:
            return False
        return self.restricted_until is None or self.restricted_until > timezone.now()
    is_restricted_now = is_currently_restricted

    @property
    def licence_expired(self):
        return bool(self.license_expiry_date and self.license_expiry_date < timezone.localdate())

    @property
    def licence_expiring_soon(self):
        if not self.license_expiry_date or self.licence_expired:
            return False
        return self.license_expiry_date <= timezone.localdate() + timedelta(days=LICENCE_WARNING_DAYS)

    @property
    def can_accept_jobs(self):
        return (self.is_verified and bool(self.is_active) and self.deleted_at is None
                and not self.is_suspended and not self.licence_expired and not self.is_restricted_now)

    @property
    def is_approved(self):  # legacy alias
        return self.is_verified and not self.is_restricted_now

    @property
    def can_receive_offers(self):
        return (self.is_active and self.deleted_at is None and self.is_verified
                and not self.is_suspended and not self.is_currently_restricted)
    can_recieve_offers = can_receive_offers  # legacy misspelling kept as an alias

    @property
    def location_is_fresh(self):
        return bool(self.location_updated_at and
                    self.location_updated_at >= timezone.now() - timedelta(seconds=LOCATION_STALE_SECONDS))

    @property
    def tier_commission_discount(self):
        """Fraction (e.g. 0.02) taken OFF the commission rate quoted for a trip."""
        return max(COMMISSION_RATE - TIER_COMMISSION_RATES.get(self.tier, COMMISSION_RATE), ZERO)

    @property
    def can_take_cash_jobs(self):
        """Cash trips: rider keeps the cash, so commission is debited from the wallet - needs a float."""
        wallet = getattr(self, "wallet", None)
        return bool(wallet and wallet.balance >= SystemConfig.get_decimal("MIN_RIDER_WALLET_BALANCE", "50.00"))

    @property
    def has_minimum_deposit(self):
        wallet = getattr(self, "wallet", None)
        return bool(wallet and wallet.balance >= SystemConfig.get_decimal("MIN_RIDER_BOOKING_BALANCE", "50.00"))

    def is_qualified_for(self, service_key):
        return self.qualifications.filter(service_id=service_key,
                                          status=ServiceQualification.Status.APPROVED).exists()

    def get_distance_km(self, lat, lng):
        if self.current_lat is None or self.current_lng is None or (self.current_lat == 0 and self.current_lng == 0):
            return None
        return haversine_km(self.current_lat, self.current_lng, lat, lng)

    # ---- performance metrics --------------------------------------------------
    @property
    def acceptance_rate(self):
        return round(self.offers_accepted / self.offers_received * 100, 1) if self.offers_received else 100.0

    @property
    def decline_rate(self):
        return round(self.offers_declined / self.offers_received * 100, 1) if self.offers_received else 0.0

    @property
    def expiry_rate(self):
        return round(self.offers_expired / self.offers_received * 100, 1) if self.offers_received else 0.0

    @property
    def jobs_lost_rate(self):
        if not self.offers_received:
            return 0.0
        return round((self.offers_received - self.offers_accepted) / self.offers_received * 100, 1)

    @property
    def cancellation_rate(self):
        total = self.completed_trips + self.cancelled_trips
        return round(self.cancelled_trips / total * 100, 1) if total else 0.0

    @property
    def completion_rate(self):
        total = self.completed_trips + self.cancelled_trips
        return round(self.completed_trips / total * 100, 1) if total else None

    @property
    def avg_response_seconds(self):
        return round(self.response_time_total_seconds / self.response_count, 1) if self.response_count else None

    @property
    def avg_arrival_minutes(self):
        return round(self.arrival_time_total_seconds / self.arrival_count / 60, 1) if self.arrival_count else None

    @property
    def total_online_hours(self):
        return round(self.total_online_seconds / 3600, 1)

    @property
    def performance_score(self):
        """0-100: 30% acceptance, 25% reliability, 25% rating, 20% speed. None until enough data."""
        if self.offers_received < MIN_SAMPLE:
            return None
        reliability = 100 - self.cancellation_rate
        rating_score = max(0.0, (self.rating_average - 1) / 4 * 100)
        avg = self.avg_response_seconds
        speed = 100 if avg is None else max(0, min(100, (120 - avg) / (120 - 15) * 100))
        return round(0.30 * self.acceptance_rate + 0.25 * reliability + 0.25 * rating_score + 0.20 * speed, 1)

    @property
    def performance_grade(self):
        score = self.performance_score
        if score is None:
            return "New"
        return "A" if score >= 85 else "B" if score >= 70 else "C" if score >= 55 else "D"

    @property
    def attention_reasons(self):
        r = []
        if self.offers_received >= MIN_SAMPLE:
            if self.acceptance_rate < LOW_ACCEPTANCE:
                r.append(f"Low acceptance ({self.acceptance_rate}%)")
            if self.expiry_rate > HIGH_EXPIRY:
                r.append(f"Ignoring offers ({self.expiry_rate}% timed out)")
        if self.completed_trips + self.cancelled_trips >= MIN_SAMPLE and self.cancellation_rate > HIGH_CANCEL:
            r.append(f"High cancellations ({self.cancellation_rate}%)")
        if self.rating_count >= 5 and self.rating_average < LOW_RATING:
            r.append(f"Low rating ({self.rating_average})")
        if self.response_count >= 5 and (self.avg_response_seconds or 0) > SLOW_RESPONSE:
            r.append(f"Slow to respond ({self.avg_response_seconds}s)")
        if self.strike_count >= 2:
            r.append(f"{self.strike_count} strikes")
        if self.licence_expired:
            r.append("Licence expired")
        elif self.licence_expiring_soon:
            r.append("Licence expiring soon")
        return r

    @property
    def needs_attention(self):
        return bool(self.attention_reasons)

    # ---- live state ---------------------------------------------------------------
    def go_online(self, lat=None, lng=None):
        if not self.can_accept_jobs:
            raise ValidationError("This rider is not allowed to go online.")
        now = timezone.now()
        with transaction.atomic():
            self.is_online = True
            self.last_online_at = now
            if not self.online_since:
                self.online_since = now
            fields = ["is_online", "last_online_at", "online_since", "updated_at"]
            if lat is not None and lng is not None:
                self.current_lat, self.current_lng, self.location_updated_at = lat, lng, now
                fields += ["current_lat", "current_lng", "location_updated_at"]
            self.save(update_fields=fields)
            if not self.online_sessions.filter(ended_at__isnull=True).exists():
                RiderOnlineSession.objects.create(rider=self, started_at=now, start_lat=lat, start_lng=lng)

    def go_offline(self):
        now = timezone.now()
        with transaction.atomic():
            if self.online_since:
                self.total_online_seconds += int((now - self.online_since).total_seconds())
                self.online_since = None
            self.is_online = False
            self.last_online_at = now
            self.save(update_fields=["is_online", "last_online_at", "online_since",
                                     "total_online_seconds", "updated_at"])
            for s in self.online_sessions.filter(ended_at__isnull=True):
                s.close(now)

    def update_location(self, lat, lng, booking=None, speed_kmh=None, heading=None, store_ping=False):
        """Hot path (GPS every few seconds): a single UPDATE, no model save()."""
        now = timezone.now()
        RiderProfile.objects.filter(pk=self.pk).update(current_lat=lat, current_lng=lng, location_updated_at=now)
        self.current_lat, self.current_lng, self.location_updated_at = lat, lng, now
        if store_ping:
            RiderLocationPing.objects.create(rider=self, booking=booking, lat=lat, lng=lng,
                                             speed_kmh=speed_kmh, heading=heading)

    # ---- performance recording ------------------------------------------------------
    def record_offer(self, outcome, response_seconds=None):
        """outcome: "accepted" | "declined" | "expired" """
        if outcome not in ("accepted", "declined", "expired"):
            raise ValueError("outcome must be accepted, declined or expired")
        updates = {"offers_received": F("offers_received") + 1, f"offers_{outcome}": F(f"offers_{outcome}") + 1}
        if outcome != "expired" and response_seconds is not None:
            updates["response_time_total_seconds"] = F("response_time_total_seconds") + int(response_seconds)
            updates["response_count"] = F("response_count") + 1
        RiderProfile.objects.filter(pk=self.pk).update(**updates)

    def record_arrival(self, seconds):
        RiderProfile.objects.filter(pk=self.pk).update(
            arrival_time_total_seconds=F("arrival_time_total_seconds") + int(max(seconds, 0)),
            arrival_count=F("arrival_count") + 1)

    def record_trip_completed(self):
        RiderProfile.objects.filter(pk=self.pk).update(
            completed_trips=F("completed_trips") + 1, last_job_at=timezone.now())
        self.refresh_from_db(fields=["completed_trips", "last_job_at"])
        _safely("rider tier", self.recalculate_tier)
        _safely("rider referral milestone", Referral.check_rider_milestone, self)

    def record_trip_cancelled(self):
        RiderProfile.objects.filter(pk=self.pk).update(cancelled_trips=F("cancelled_trips") + 1)

    def apply_rating(self, rating):
        with transaction.atomic():
            r = RiderProfile.objects.select_for_update().get(pk=self.pk)
            avg = r.rating_average if r.rating_average is not None else 5.0
            count = r.rating_count or 0
            r.rating_count = count + 1
            r.rating_average = round((avg * count + rating) / r.rating_count, 2)
            r.save(update_fields=["rating_average", "rating_count", "updated_at"])
        self.rating_average, self.rating_count = r.rating_average, r.rating_count
        self.recalculate_tier()

    def recalculate_tier(self):
        """Promote/demote automatically; a better tier keeps more of every fare."""
        self.refresh_from_db(fields=["completed_trips", "rating_average", "rating_count",
                                     "offers_received", "offers_accepted", "tier"])
        new_tier = RiderTier.STANDARD.value
        if self.rating_count >= 10:
            for tier, trips, rating, acceptance in RIDER_TIER_RULES:
                if (self.completed_trips >= trips and self.rating_average >= rating
                        and self.acceptance_rate >= acceptance):
                    new_tier = tier
                    break
        if new_tier != self.tier:
            self.tier = new_tier
            RiderProfile.objects.filter(pk=self.pk).update(tier=new_tier)
            points = int((COMMISSION_RATE - TIER_COMMISSION_RATES[new_tier]) * 100)
            perk = f"{points} percentage point(s) off platform commission" if points > 0 else "no commission discount"
            Notification.notify(self.user, "Your rider tier changed", f"You are now {new_tier.title()} - {perk}.",
                                category=Notification.Category.SYSTEM)
        return new_tier

    def add_strike(self, reason=""):
        RiderProfile.objects.filter(pk=self.pk).update(strike_count=F("strike_count") + 1)
        self.refresh_from_db(fields=["strike_count", "verification_status"])
        if self.strike_count >= MAX_STRIKES and self.verification_status == VerificationStatus.VERIFIED:
            self.suspend(by=None, reason=f"Automatic: {self.strike_count} strikes. {reason}".strip(),
                         until=timezone.now() + timedelta(days=7))

    # ---- verification workflow (atomic + audited) ------------------------------------
    def _transition(self, new_status, *, by=None, reason="", **extra_fields):
        with transaction.atomic():
            locked = RiderProfile.objects.select_for_update().get(pk=self.pk)
            old_status = locked.verification_status
            locked.verification_status = new_status
            for field, value in extra_fields.items():
                setattr(locked, field, value)
            locked.save()
            RiderStatusHistory.objects.create(rider=locked, from_status=old_status, to_status=new_status,
                                              changed_by=by, reason=reason)
            audit(by, "rider.status_changed", locked, from_status=old_status, to_status=new_status, reason=reason)
        self.refresh_from_db()
        return self

    def submit_for_review(self):
        if self.verification_status not in (VerificationStatus.PENDING, VerificationStatus.REJECTED):
            raise ValidationError("Only pending or rejected riders can submit for review.")
        if not self.documents_uploaded:
            raise ValidationError("Upload all required documents before submitting.")
        return self._transition(VerificationStatus.UNDER_REVIEW, reason="Documents submitted",
                                submitted_at=timezone.now(), rejection_reason="")

    def approve(self, by):
        if self.verification_status != VerificationStatus.UNDER_REVIEW:
            raise ValidationError("Only under-review riders can be approved.")
        if not self.documents_complete:
            raise ValidationError("All required documents must be approved first.")
        return self._transition(VerificationStatus.VERIFIED, by=by, reason="Approved",
                                verified_at=timezone.now(), verified_by=by, rejection_reason="")

    def reject(self, by, reason):
        if self.verification_status != VerificationStatus.UNDER_REVIEW:
            raise ValidationError("Only under-review riders can be rejected.")
        if not (reason or "").strip():
            raise ValidationError("A rejection reason is required.")
        return self._transition(VerificationStatus.REJECTED, by=by, reason=reason, rejection_reason=reason)

    def suspend(self, by, reason, until=None):
        if self.verification_status == VerificationStatus.SUSPENDED:
            raise ValidationError("Rider is already suspended.")
        if self.is_online:
            self.go_offline()
        return self._transition(VerificationStatus.SUSPENDED, by=by, reason=reason,
                                suspended_at=timezone.now(), suspended_until=until, suspension_reason=reason)

    def reinstate(self, by, reason="Reinstated"):
        if self.verification_status != VerificationStatus.SUSPENDED:
            raise ValidationError("Only suspended riders can be reinstated.")
        return self._transition(VerificationStatus.VERIFIED, by=by, reason=reason,
                                suspended_at=None, suspended_until=None, suspension_reason="")

    @classmethod
    def reinstate_expired_suspensions(cls):
        """Celery beat (hourly): lift timed suspensions (e.g. the automatic 7-day strike ban)."""
        count = 0
        for rider in cls.objects.filter(verification_status=VerificationStatus.SUSPENDED,
                                        suspended_until__isnull=False, suspended_until__lte=timezone.now()):
            try:
                rider.reinstate(by=None, reason="Suspension period ended")
                count += 1
            except ValidationError:
                continue
        return count

    def missing_required_documents(self):
        """Labels of required documents without a valid APPROVED, unexpired upload."""
        today = timezone.localdate()
        have = set(self.documents.filter(status=RiderDocument.DocumentStatus.APPROVED)
                   .filter(Q(expiry_date__isnull=True) | Q(expiry_date__gte=today))
                   .values_list("document_type", flat=True))
        return [label for code, label in RiderDocument.REQUIRED_TYPES if getattr(code, "value", code) not in have]

    def sync_verification_from_documents(self, by=None):
        """Verify the rider once EVERY required document is approved. Returns True if status changed."""
        if self.verification_status not in (VerificationStatus.PENDING, VerificationStatus.UNDER_REVIEW,
                                            VerificationStatus.REJECTED):
            return False
        if self.missing_required_documents():
            return False
        self._transition(VerificationStatus.VERIFIED, by=by, reason="All required documents approved",
                         verified_at=timezone.now(), verified_by=by, rejection_reason="")
        return True

    def note_document_rejected(self, document, by=None):
        """Store why a document was rejected; an under-review rider goes back to Rejected."""
        reason = f"{document.get_document_type_display()}: {(document.notes or '').strip()}"
        if self.verification_status == VerificationStatus.UNDER_REVIEW:
            self._transition(VerificationStatus.REJECTED, by=by, reason=reason, rejection_reason=reason)
            return True
        if self.verification_status == VerificationStatus.PENDING:
            RiderProfile.objects.filter(pk=self.pk).update(rejection_reason=reason)
            self.rejection_reason = reason
        return False


class RiderStatusHistory(models.Model):
    rider = models.ForeignKey(RiderProfile, on_delete=models.CASCADE, related_name="status_history")
    from_status = models.CharField(max_length=20)
    to_status = models.CharField(max_length=20)
    changed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    reason = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name_plural = "Rider status history"

    def __str__(self):
        return f"{self.rider_id}: {self.from_status} → {self.to_status}"


class ServiceQualification(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        APPROVED = "approved", "Approved"
        REJECTED = "rejected", "Rejected"
        SUSPENDED = "suspended", "Suspended"
        REVOKED = "revoked", "Revoked"

    rider = models.ForeignKey(RiderProfile, on_delete=models.CASCADE, related_name="qualifications")
    service = models.ForeignKey(Service, on_delete=models.CASCADE, related_name="qualifications")
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.PENDING, db_index=True)
    approved_at = models.DateTimeField(null=True, blank=True)
    approved_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    status_reason = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at"]
        constraints = [models.UniqueConstraint(fields=["rider", "service"], name="unique_rider_service")]
        indexes = [models.Index(fields=["service", "status", "rider"], name="qual_service_status_idx"),
                   models.Index(fields=["rider", "status"], name="qual_rider_status_idx")]

    def __str__(self):
        return f"{self.rider_id} / {self.service_id}: {self.status}"

    @property
    def is_approved(self):
        return self.status == self.Status.APPROVED

    def approve(self, by):
        self.status, self.approved_at, self.approved_by = self.Status.APPROVED, timezone.now(), by
        self.save()


def rider_document_path(instance, filename):
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else "bin"
    return f"rider-documents/{instance.rider_profile_id}/{instance.document_type}/{uuid.uuid4().hex}.{ext}"


class RiderDocument(models.Model):
    class DocumentType(models.TextChoices):
        GHANA_CARD_FRONT = "ghana_card_front", "Ghana Card (Front)"
        GHANA_CARD_BACK = "ghana_card_back", "Ghana Card (Back)"
        PASSPORT_PHOTO = "passport_photo", "Passport Photo"
        RIDING_LICENSE = "riding_license", "Riding Licence"
        VEHICLE_REGISTRATION = "vehicle_registration", "Vehicle Registration"
        INSURANCE = "insurance", "Insurance Certificate"

    class DocumentStatus(models.TextChoices):
        PENDING = "pending", "Pending Review"
        APPROVED = "approved", "Approved"
        REJECTED = "rejected", "Rejected"
        EXPIRED = "expired", "Expired"

    Status = DocumentStatus
    REQUIRED_TYPES = [
        (DocumentType.GHANA_CARD_FRONT, "Ghana Card (Front)"),
        (DocumentType.GHANA_CARD_BACK, "Ghana Card (Back)"),
        (DocumentType.PASSPORT_PHOTO, "Passport Photo"),
        (DocumentType.RIDING_LICENSE, "Riding Licence"),
        (DocumentType.VEHICLE_REGISTRATION, "Vehicle Registration"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    rider_profile = models.ForeignKey(RiderProfile, on_delete=models.CASCADE, related_name="documents")
    document_type = models.CharField(max_length=30, choices=DocumentType.choices)
    file = models.FileField(upload_to=rider_document_path,
                            validators=[FileExtensionValidator(["jpg", "jpeg", "png", "webp", "pdf"])])
    status = models.CharField(max_length=20, choices=DocumentStatus.choices, default=DocumentStatus.PENDING)
    expiry_date = models.DateField(null=True, blank=True)
    uploaded_at = models.DateTimeField(auto_now_add=True)
    reviewed_at = models.DateTimeField(null=True, blank=True)
    reviewed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ["-uploaded_at"]
        indexes = [models.Index(fields=["rider_profile", "document_type", "status"])]

    def __str__(self):
        return f"{self.rider_profile.full_name} - {self.get_document_type_display()}"

    @property
    def is_expired(self):
        return bool(self.expiry_date and self.expiry_date < timezone.localdate())

    def clean(self):
        super().clean()
        if self.status == self.DocumentStatus.REJECTED and not (self.notes or "").strip():
            raise ValidationError({"notes": "Add a short reason so the rider knows what to fix."})

    def review(self, by, approve, notes=""):
        #Approve/reject, stamp the reviewer, sync rider verification.///
        #Returns "verified", "rejected" or ""."""
        notes = (notes or "").strip()
        if notes:
            self.notes = notes
        if not approve and not (self.notes or "").strip():
            raise ValidationError("Add a short reason before rejecting a document.")
        with transaction.atomic():
            self.status = self.DocumentStatus.APPROVED if approve else self.DocumentStatus.REJECTED
            self.reviewed_by, self.reviewed_at = by, timezone.now()
            self.save(update_fields=["status", "reviewed_by", "reviewed_at", "notes"])
            return self.apply_review_effects(by=by)

    def apply_review_effects(self, by=None):
        # Side effects of a decision ALREADY saved on this row.///
        rider = self.rider_profile
        if self.status == self.DocumentStatus.APPROVED:
            if rider.sync_verification_from_documents(by=by):
                Notification.notify(rider.user, "You're verified!",
                                    "All your documents are approved. You can now go online and take jobs.",
                                    category=Notification.Category.SYSTEM)
                return "verified"
            return ""
        if self.status == self.DocumentStatus.REJECTED:
            changed = rider.note_document_rejected(self, by=by)
            Notification.notify(rider.user, "A document needs attention",
                                f"{self.get_document_type_display()}: {(self.notes or '').strip()}",
                                category=Notification.Category.SYSTEM)
            return "rejected" if changed else ""
        return ""

    def get_absolute_url(self):
        meta = self._meta
        return reverse(f"admin:{meta.app_label}_{meta.model_name}_change", args=[self.pk])

    def get_label_with_notes(self):
        notes = (self.notes or "").strip()
        return f"{self.get_document_type_display()}: {notes}" if notes else self.get_document_type_display()


class RiderPayoutAccount(models.Model):
    class Network(models.TextChoices):
        MTN = "mtn", "MTN MoMo"
        TELECEL = "telecel", "Telecel Cash"
        AIRTELTIGO = "airteltigo", "AirtelTigo Money"

    rider_profile = models.ForeignKey(RiderProfile, on_delete=models.CASCADE, related_name="payout_accounts")
    network = models.CharField(max_length=12, choices=Network.choices)
    momo_number = models.CharField(max_length=20, validators=[phone_validator])
    account_name = models.CharField(max_length=150, help_text="Name registered on the MoMo wallet.")
    is_verified = models.BooleanField(default=False)
    is_primary = models.BooleanField(default=False)
    is_deleted = models.BooleanField(default=False, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["rider_profile"], condition=Q(is_primary=True, is_deleted=False),
                                    name="unique_primary_payout_per_rider"),
            models.UniqueConstraint(fields=["rider_profile", "momo_number"], condition=Q(is_deleted=False),
                                    name="unique_payout_number_per_rider"),
        ]

    def __str__(self):
        return f"{self.get_network_display()} {self.masked_number}"

    @property
    def masked_number(self):
        p = self.momo_number
        return f"{p[:4]}•••••{p[-2:]}" if len(p) > 6 else p

    def save(self, *args, **kwargs):
        self.momo_number = normalize_gh_phone(self.momo_number)
        with transaction.atomic():
            siblings = RiderPayoutAccount.objects.filter(rider_profile=self.rider_profile, is_deleted=False).exclude(pk=self.pk)
            if self.is_deleted:
                self.is_primary = False
            elif not siblings.exists():
                self.is_primary = True
            elif self.is_primary:
                siblings.filter(is_primary=True).update(is_primary=False)
            super().save(*args, **kwargs)

    def soft_delete(self):
        # Delete without losing withdrawal history; promote another account if this was primary.///
        with transaction.atomic():
            was_primary = self.is_primary
            self.is_deleted, self.is_primary = True, False
            super().save()
            if was_primary:
                nxt = (RiderPayoutAccount.objects.filter(rider_profile=self.rider_profile, is_deleted=False)
                       .order_by("created_at").first())
                if nxt:
                    nxt.is_primary = True
                    nxt.save()


class EmergencyContactQuerySet(models.QuerySet):
    def active(self):
        return self.filter(is_deleted=False)


class RiderEmergencyContact(models.Model):
    class Relationship(models.TextChoices):
        PARENT = "parent", "Parent"
        SPOUSE = "spouse", "Spouse / Partner"
        SIBLING = "sibling", "Sibling"
        CHILD = "child", "Child"
        RELATIVE = "relative", "Other Relative"
        FRIEND = "friend", "Friend"
        GUARDIAN = "guardian", "Guardian"
        OTHER = "other", "Other"

    Language = LanguageChoices

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    rider_profile = models.ForeignKey(RiderProfile, on_delete=models.CASCADE, related_name="emergency_contacts")
    full_name = models.CharField(max_length=255)
    relationship = models.CharField(max_length=20, choices=Relationship.choices, default=Relationship.OTHER)
    relationship_note = models.CharField(max_length=100, blank=True,
                                         help_text="Details when relationship is 'Other' (e.g. neighbour).")
    phone_number = models.CharField(max_length=20, validators=[phone_validator])
    alternative_phone = models.CharField(max_length=20, blank=True, validators=[phone_validator])
    address = models.TextField(blank=True)
    preferred_language = models.CharField(max_length=5, choices=LanguageChoices.choices, default=LanguageChoices.ENGLISH)
    is_primary = models.BooleanField(default=False)
    notify_on_emergency = models.BooleanField(default=True, help_text="Send SMS/call alerts during incidents.")
    is_verified = models.BooleanField(default=False)
    verified_at = models.DateTimeField(null=True, blank=True)
    last_contacted_at = models.DateTimeField(null=True, blank=True)
    is_deleted = models.BooleanField(default=False, db_index=True)
    deleted_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = EmergencyContactQuerySet.as_manager()

    class Meta:
        ordering = ["-is_primary", "full_name"]
        verbose_name = "Rider Emergency Contact"
        verbose_name_plural = "Rider Emergency Contacts"
        constraints = [
            models.UniqueConstraint(fields=["rider_profile"], condition=Q(is_primary=True, is_deleted=False),
                                    name="unique_active_primary_contact_per_rider"),
            models.UniqueConstraint(fields=["rider_profile", "phone_number"], condition=Q(is_deleted=False),
                                    name="unique_active_phone_per_rider"),
        ]
        indexes = [models.Index(fields=["rider_profile", "is_deleted"]), models.Index(fields=["phone_number"])]

    def __str__(self):
        return f"{self.full_name} - {self.get_relationship_display()}"

    @staticmethod
    def normalize_phone(value):
        return normalize_gh_phone(value)

    @property
    def masked_phone(self):
        p = self.phone_number
        return f"{p[:4]}•••••{p[-2:]}" if len(p) > 6 else p

    def clean(self):
        self.phone_number = self.normalize_phone(self.phone_number)
        if self.alternative_phone:
            self.alternative_phone = self.normalize_phone(self.alternative_phone)
            if self.alternative_phone == self.phone_number:
                raise ValidationError({"alternative_phone": "Must differ from the main phone number."})
        if self.relationship == self.Relationship.OTHER and not self.relationship_note:
            raise ValidationError({"relationship_note": "Please describe the relationship."})
        if not self.is_deleted:
            rider_phone = getattr(self.rider_profile, "phone", None)
            if rider_phone and self.normalize_phone(rider_phone) == self.phone_number:
                raise ValidationError({"phone_number": "Emergency contact cannot be the rider's own number."})
            others = type(self).objects.active().filter(rider_profile_id=self.rider_profile_id).exclude(pk=self.pk)
            if self._state.adding and others.count() >= MAX_CONTACTS_PER_RIDER:
                raise ValidationError(f"A rider can have at most {MAX_CONTACTS_PER_RIDER} emergency contacts.")

    def save(self, *args, **kwargs):
        with transaction.atomic():
            siblings = type(self).objects.active().filter(rider_profile_id=self.rider_profile_id).exclude(pk=self.pk)
            if not self.is_deleted:
                if not siblings.exists():
                    self.is_primary = True
                elif self.is_primary:
                    siblings.filter(is_primary=True).update(is_primary=False)  # demote BEFORE constraint check
            self.full_clean(validate_constraints=False)
            super().save(*args, **kwargs)

    def soft_delete(self):
        with transaction.atomic():
            was_primary = self.is_primary
            self.is_deleted, self.is_primary, self.deleted_at = True, False, timezone.now()
            models.Model.save(self)
            if was_primary:
                nxt = type(self).objects.active().filter(rider_profile_id=self.rider_profile_id).order_by("created_at").first()
                if nxt:
                    nxt.is_primary = True
                    nxt.save()

    def mark_verified(self):
        self.is_verified, self.verified_at = True, timezone.now()
        self.save(update_fields=["is_verified", "verified_at", "updated_at"])

    def mark_contacted(self):
        self.last_contacted_at = timezone.now()
        self.save(update_fields=["last_contacted_at", "updated_at"])


class RiderOnlineSession(models.Model):
    # Every online stretch - powers hours-online targets, supply heatmaps and reports.///
    rider = models.ForeignKey(RiderProfile, on_delete=models.CASCADE, related_name="online_sessions")
    started_at = models.DateTimeField(default=timezone.now)
    ended_at = models.DateTimeField(null=True, blank=True)
    duration_seconds = models.PositiveIntegerField(default=0)
    start_lat = models.FloatField(null=True, blank=True)
    start_lng = models.FloatField(null=True, blank=True)

    class Meta:
        ordering = ["-started_at"]
        indexes = [models.Index(fields=["rider", "-started_at"])]

    def __str__(self):
        return f"{self.rider_id} session {self.started_at:%Y-%m-%d %H:%M}"

    def close(self, when=None):
        when = when or timezone.now()
        self.ended_at = when
        self.duration_seconds = max(0, int((when - self.started_at).total_seconds()))
        self.save(update_fields=["ended_at", "duration_seconds"])


class RiderLocationPing(models.Model):
    # Breadcrumb trail for trip replay and disputes. At scale keep LIVE position in Redis and
    #write a sampled ping only during active trips; prune after ~90 days.
    rider = models.ForeignKey(RiderProfile, on_delete=models.CASCADE, related_name="location_pings")
    booking = models.ForeignKey("Booking", null=True, blank=True, on_delete=models.SET_NULL, related_name="pings")
    lat = models.FloatField()
    lng = models.FloatField()
    speed_kmh = models.FloatField(null=True, blank=True)
    heading = models.FloatField(null=True, blank=True)
    recorded_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        ordering = ["-recorded_at"]
        indexes = [models.Index(fields=["rider", "-recorded_at"]), models.Index(fields=["booking", "recorded_at"])]

    def __str__(self):
        return f"{self.rider_id} @ {self.lat:.5f},{self.lng:.5f}"


class RiderProfileAdminNote(models.Model):
    rider_profile = models.ForeignKey(RiderProfile, on_delete=models.CASCADE, related_name="admin_notes")
    author = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    note = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["rider_profile", "-created_at"])]

    def __str__(self):
        return f"Note on {self.rider_profile}: {self.note[:40]}{'…' if len(self.note) > 40 else ''}"



class CustomerRiderPreference(models.Model):
    
    class PreferredService(models.TextChoices):
        OKADA = "okada", "Okada"
        PARCEL = "parcel", "Parcel"
        WASTEBIN = "waste-bin", "Waste Bin"

    account = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete= models.CASCADE, related_name="rider_preference")
    preferred_service = models.CharField(max_length = 20, choices= PreferredService.choices, default=PreferredService.OKADA)
    preferred_language = models.CharField(max_length = 10, default= "en")
    dark_mode = models.BooleanField(default=False)
    notification_enabled = models.BooleanField(default=True)
    location_sharing = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add= True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "rider_preferences"
        verbose_name = "Customer/Rider Preferences"
        verbose_name_plural = "Customer/Rider Preferences"

    def __str__ (self):
        full_name = self.account.get_full_name()
        display_name = full_name if full_name else self.account.get_username()
        return f"{display_name} - Preferences"



class RiderIncident(models.Model):
    """An incident, accident, or issue reported by or against a rider."""

    class Severity(models.TextChoices):
        MINOR = "minor", "Minor"
        MODERATE = "moderate", "Moderate"
        SERIOUS = "serious", "Serious"

    class Status(models.TextChoices):
        REPORTED = "reported", "Reported"
        INVESTIGATING = "investigating", "Under Investigation"
        RESOLVED = "resolved", "Resolved"
        CLOSED = "closed", "Closed"

    rider = models.ForeignKey(
        "RiderProfile",
        on_delete=models.CASCADE,
        related_name="incidents",
    )
    booking_ref = models.CharField(max_length=50, blank=True, default="")
    title = models.CharField(max_length=200)
    description = models.TextField()
    severity = models.CharField(
        max_length=20,
        choices=Severity.choices,
        default=Severity.MINOR,
    )
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.REPORTED,
    )
    reported_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="reported_rider_incidents",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "rider_incidents"
        ordering = ["-created_at"]
        verbose_name = "Rider Incident"
        verbose_name_plural = "Rider Incidents"
        indexes = [
            models.Index(fields=["rider", "status"], name="incident_rider_status_idx"),
            models.Index(fields=["booking_ref"], name="incident_booking_ref_idx"),
        ]

    def __str__(self):
        return f"{self.rider} — {self.title}"

    @property
    def is_open(self):
        return self.status in (self.Status.REPORTED, self.Status.INVESTIGATING)


class RiderComplaint(models.Model):
    """A formal complaint filed about a rider, or by a rider, through the platform."""

    class Status(models.TextChoices):
        SUBMITTED = "submitted", "Submitted"
        REVIEWING = "reviewing", "Under Review"
        RESOLVED = "resolved", "Resolved"
        DISMISSED = "dismissed", "Dismissed"

    class ComplaintType(models.TextChoices):
        BEHAVIOUR = "behaviour", "Behaviour"
        SAFETY = "safety", "Safety"
        PAYMENT = "payment", "Payment"
        VEHICLE = "vehicle", "Vehicle Condition"
        OTHER = "other", "Other"

    CLOSED_STATUSES = (Status.RESOLVED, Status.DISMISSED)

    reference = models.CharField(
        max_length=20,
        unique=True,
        editable=False,
        default=generate_complaint_reference,
    )
    complaint_type = models.CharField(
        max_length=50,
        choices=ComplaintType.choices,
        default=ComplaintType.OTHER,
    )
    subject = models.CharField(max_length=200)
    description = models.TextField()
    rider = models.ForeignKey(
        "RiderProfile",
        on_delete=models.SET_NULL,
        related_name="complaints",
        null=True,
        blank=True,
    )
    booking_ref = models.CharField(max_length=50, blank=True, default="")
    filed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name="complaints_filed",
        null=True,
        blank=True,
    )
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.SUBMITTED,
    )
    admin_notes = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "rider_complaints"
        ordering = ["-created_at"]
        verbose_name = "Rider Complaint"
        verbose_name_plural = "Rider Complaints"
        indexes = [
            models.Index(fields=["status", "-created_at"], name="complaint_status_created_idx"),
            models.Index(fields=["rider", "status"], name="complaint_rider_status_idx"),
            models.Index(fields=["booking_ref"], name="complaint_booking_ref_idx"),
        ]

    def __str__(self):
        return f"{self.reference} — {self.subject} ({self.get_status_display()})"

    @property
    def is_open(self):
        return self.status not in self.CLOSED_STATUSES

    def save(self, *args, **kwargs):
        # Keep resolved_at in sync with status.
        if self.status in self.CLOSED_STATUSES:
            if self.resolved_at is None:
                self.resolved_at = timezone.now()
        else:
            self.resolved_at = None

        update_fields = kwargs.get("update_fields")
        if update_fields is not None:
            kwargs["update_fields"] = set(update_fields) | {"resolved_at"}

        super().save(*args, **kwargs)


class BookingStatusHistory(models.Model):
    """Append-only audit log of every status change for a booking."""

    booking_ref = models.CharField(max_length=50)
    old_status = models.CharField(max_length=30, blank=True, default="")
    new_status = models.CharField(max_length=30)
    changed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="booking_status_changes",
    )
    notes = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "booking_status_history"
        ordering = ["-created_at"]
        verbose_name = "Booking Status History"
        verbose_name_plural = "Booking Status History"
        indexes = [
            models.Index(fields=["booking_ref", "-created_at"], name="bsh_ref_created_idx"),
            models.Index(fields=["new_status"], name="bsh_new_status_idx"),
        ]

    def __str__(self):
        return f"{self.booking_ref}: {self.old_status or '—'} → {self.new_status}"

    def save(self, *args, **kwargs):
        # Audit entries are written once and never edited.
        if not self._state.adding:
            raise ValueError("BookingStatusHistory entries are immutable.")
        super().save(*args, **kwargs)



class AppendOnlyModel(models.Model):
    """Rows can be created but never edited, so the record stays trustworthy."""

    class Meta:
        abstract = True

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValueError(f"{type(self).__name__} entries are immutable.")
        super().save(*args, **kwargs)


class ParcelDetail(models.Model):
    """Extra details, live status and proof trail for parcel delivery bookings."""

    class Status(models.TextChoices):
        PENDING = "pending", "Pending Pickup"
        PICKED_UP = "picked_up", "Picked Up"
        IN_TRANSIT = "in_transit", "In Transit"
        DELIVERED = "delivered", "Delivered"
        FAILED = "failed", "Delivery Failed"
        RETURNED = "returned", "Returned to Sender"
        CANCELLED = "cancelled", "Cancelled"

    class PackageSize(models.TextChoices):
        SMALL = "small", "Small"
        MEDIUM = "medium", "Medium"
        LARGE = "large", "Large"

    ALLOWED_TRANSITIONS = {
        Status.PENDING: (Status.PICKED_UP, Status.CANCELLED),
        Status.PICKED_UP: (Status.IN_TRANSIT, Status.DELIVERED, Status.FAILED, Status.RETURNED),
        Status.IN_TRANSIT: (Status.DELIVERED, Status.FAILED, Status.RETURNED),
        Status.FAILED: (Status.IN_TRANSIT, Status.RETURNED),
        Status.DELIVERED: (),
        Status.RETURNED: (),
        Status.CANCELLED: (),
    }

    booking_ref = models.CharField(max_length=50, unique=True)
    tracking_code = models.CharField(
        max_length=20, unique=True, editable=False, default=generate_tracking_code
    )
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.PENDING
    )

    recipient_name = models.CharField(max_length=100)
    recipient_phone = models.CharField(max_length=20, validators=[phone_validator])
    recipient_email = models.EmailField(blank=True, default="")
    notify_recipient = models.BooleanField(
        default=True, help_text="Send the recipient tracking updates."
    )

    item_description = models.TextField()
    item_value = models.DecimalField(
        max_digits=10, decimal_places=2, default=0, validators=[MinValueValidator(0)]
    )
    package_size = models.CharField(
        max_length=10, choices=PackageSize.choices, default=PackageSize.SMALL
    )
    weight_kg = models.FloatField(null=True, blank=True, validators=[MinValueValidator(0)])
    is_fragile = models.BooleanField(default=False)
    requires_signature = models.BooleanField(default=False)
    special_instructions = models.TextField(blank=True, default="")

    delivery_pin = models.CharField(
        max_length=4, editable=False, default=generate_delivery_pin,
        help_text="Handover code the recipient gives the rider.",
    )
    delivery_attempts = models.PositiveSmallIntegerField(default=0)
    received_by = models.CharField(max_length=100, blank=True, default="")
    picked_up_at = models.DateTimeField(null=True, blank=True)
    delivered_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "parcel_details"
        ordering = ["-created_at"]
        verbose_name = "Parcel Detail"
        verbose_name_plural = "Parcel Details"
        indexes = [
            models.Index(fields=["status", "-created_at"], name="parcel_status_created_idx"),
        ]

    def __str__(self):
        return f"{self.booking_ref} — {self.recipient_name}"

    @property
    def is_final(self):
        return not self.ALLOWED_TRANSITIONS[self.Status(self.status)]

    def save(self, *args, **kwargs):
        creating = self._state.adding
        with transaction.atomic():
            super().save(*args, **kwargs)
            if creating:
                ParcelTrackingEvent.objects.create(
                    parcel=self, status=self.status, note="Parcel booked."
                )

    def verify_delivery_pin(self, pin):
        return secrets.compare_digest(str(pin).strip(), self.delivery_pin)

    def update_status(self, new_status, *, user=None, note="",
                      latitude=None, longitude=None, location_label=""):
        """Validated status change that also writes the tracking timeline entry."""
        new_status = self.Status(new_status)
        with transaction.atomic():
            parcel = type(self).objects.select_for_update().get(pk=self.pk)
            allowed = self.ALLOWED_TRANSITIONS[self.Status(parcel.status)]
            if new_status not in allowed:
                raise ValidationError(
                    f"Cannot change status from '{parcel.get_status_display()}' "
                    f"to '{new_status.label}'."
                )
            if (
                new_status == self.Status.DELIVERED
                and parcel.requires_signature
                and not parcel.proofs.filter(
                    proof_type=ParcelProof.ProofType.SIGNATURE
                ).exists()
            ):
                raise ValidationError(
                    "A signature proof is required before delivery can be confirmed."
                )

            now = timezone.now()
            parcel.status = new_status
            update_fields = ["status", "updated_at"]
            if new_status == self.Status.PICKED_UP:
                parcel.picked_up_at = now
                update_fields.append("picked_up_at")
            elif new_status == self.Status.DELIVERED:
                parcel.delivered_at = now
                update_fields.append("delivered_at")
            elif new_status == self.Status.FAILED:
                parcel.delivery_attempts += 1
                update_fields.append("delivery_attempts")
            parcel.save(update_fields=update_fields)

            ParcelTrackingEvent.objects.create(
                parcel=parcel, status=new_status, note=note,
                latitude=latitude, longitude=longitude,
                location_label=location_label, created_by=user,
            )
        self.refresh_from_db()


class ParcelTrackingEvent(AppendOnlyModel):
    """One step in a parcel's tracking timeline."""

    parcel = models.ForeignKey(
        ParcelDetail, on_delete=models.CASCADE, related_name="events"
    )
    status = models.CharField(max_length=20, choices=ParcelDetail.Status.choices)
    note = models.CharField(max_length=255, blank=True, default="")
    location_label = models.CharField(max_length=150, blank=True, default="")
    latitude = models.DecimalField(
        max_digits=9, decimal_places=6, null=True, blank=True,
        validators=[MinValueValidator(-90), MaxValueValidator(90)],
    )
    longitude = models.DecimalField(
        max_digits=9, decimal_places=6, null=True, blank=True,
        validators=[MinValueValidator(-180), MaxValueValidator(180)],
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name="parcel_tracking_events",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "parcel_tracking_events"
        ordering = ["-created_at"]
        verbose_name = "Parcel Tracking Event"
        verbose_name_plural = "Parcel Tracking Events"
        indexes = [
            models.Index(fields=["parcel", "-created_at"], name="parcel_event_parcel_idx"),
        ]

    def __str__(self):
        return f"{self.parcel.tracking_code}: {self.get_status_display()}"


class ParcelProof(AppendOnlyModel):
    """Evidence captured at pickup or delivery (photo, signature, PIN check)."""

    class ProofType(models.TextChoices):
        PICKUP_PHOTO = "pickup_photo", "Pickup Photo"
        DELIVERY_PHOTO = "delivery_photo", "Delivery Photo"
        SIGNATURE = "signature", "Recipient Signature"
        PIN_VERIFIED = "pin_verified", "Delivery PIN Verified"

    parcel = models.ForeignKey(
        ParcelDetail, on_delete=models.CASCADE, related_name="proofs"
    )
    proof_type = models.CharField(max_length=20, choices=ProofType.choices)
    image = models.ImageField(upload_to=parcel_proof_upload_to, blank=True)
    signed_by = models.CharField(max_length=100, blank=True, default="")
    notes = models.CharField(max_length=255, blank=True, default="")
    latitude = models.DecimalField(
        max_digits=9, decimal_places=6, null=True, blank=True,
        validators=[MinValueValidator(-90), MaxValueValidator(90)],
    )
    longitude = models.DecimalField(
        max_digits=9, decimal_places=6, null=True, blank=True,
        validators=[MinValueValidator(-180), MaxValueValidator(180)],
    )
    captured_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name="parcel_proofs_captured",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "parcel_proofs"
        ordering = ["-created_at"]
        verbose_name = "Parcel Proof"
        verbose_name_plural = "Parcel Proofs"
        indexes = [
            models.Index(fields=["parcel", "proof_type"], name="parcel_proof_type_idx"),
        ]

    def __str__(self):
        return f"{self.parcel.tracking_code}: {self.get_proof_type_display()}"

    def clean(self):
        if self.proof_type != self.ProofType.PIN_VERIFIED and not self.image:
            raise ValidationError({"image": "An image is required for this proof type."})

    def save(self, *args, **kwargs):
        self.clean()
        super().save(*args, **kwargs)


class ParcelFeedback(models.Model):
    """Customer rating and comment after a parcel is delivered."""

    parcel = models.OneToOneField(
        ParcelDetail, on_delete=models.CASCADE, related_name="feedback"
    )
    rating = models.PositiveSmallIntegerField(
        validators=[MinValueValidator(1), MaxValueValidator(5)]
    )
    comment = models.TextField(blank=True, default="")
    submitted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name="parcel_feedback",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "parcel_feedback"
        ordering = ["-created_at"]
        verbose_name = "Parcel Feedback"
        verbose_name_plural = "Parcel Feedback"

    def __str__(self):
        return f"{self.parcel.tracking_code}: {self.rating}/5"

    def clean(self):
        if self.parcel_id and self.parcel.status != ParcelDetail.Status.DELIVERED:
            raise ValidationError("Feedback can only be left on delivered parcels.")

    def save(self, *args, **kwargs):
        self.clean()
        super().save(*args, **kwargs)



class BookingOfferQuerySet(models.QuerySet):
    def pending(self):
        return self.filter(status=BookingOffer.Status.PENDING)

    def active(self):
        """Pending offers that have not yet passed their expiry time."""
        return self.pending().filter(expires_at__gt=timezone.now())

    def expire_stale(self):
        """Mark pending offers past their expiry as expired. Returns the count."""
        now = timezone.now()
        return self.pending().filter(expires_at__lte=now).update(
            status=BookingOffer.Status.EXPIRED, updated_at=now
        )


class BookingOffer(models.Model):
    """Price/service offer from a rider for a booking request."""

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        ACCEPTED = "accepted", "Accepted"
        DECLINED = "declined", "Declined"
        EXPIRED = "expired", "Expired"

    booking_ref = models.CharField(max_length=50)
    rider = models.ForeignKey(
        "RiderProfile",
        on_delete=models.PROTECT,
        related_name="offers_made",
    )
    offered_price = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0.01"))],
    )
    message = models.TextField(blank=True, default="")
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.PENDING,
    )
    expires_at = models.DateTimeField()
    responded_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = BookingOfferQuerySet.as_manager()

    class Meta:
        db_table = "booking_offers"
        ordering = ["-created_at"]
        verbose_name = "Booking Offer"
        verbose_name_plural = "Booking Offers"
        indexes = [
            models.Index(fields=["booking_ref", "status"], name="offer_ref_status_idx"),
            models.Index(fields=["rider", "status"], name="offer_rider_status_idx"),
            models.Index(fields=["status", "expires_at"], name="offer_status_expiry_idx"),
        ]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(offered_price__gt=0),
                name="offer_price_positive",
            ),
            models.UniqueConstraint(
                fields=["booking_ref", "rider"],
                condition=models.Q(status="pending"),
                name="offer_one_pending_per_rider_booking",
            ),
            models.UniqueConstraint(
                fields=["booking_ref"],
                condition=models.Q(status="accepted"),
                name="offer_one_accepted_per_booking",
            ),
        ]

    def __str__(self):
        return f"{self.booking_ref}: {self.rider} — {self.offered_price}"

    @property
    def is_expired(self):
        return self.status == self.Status.EXPIRED or (
            self.status == self.Status.PENDING and self.expires_at <= timezone.now()
        )

    @property
    def is_actionable(self):
        return self.status == self.Status.PENDING and not self.is_expired

    def clean(self):
        if self._state.adding and self.expires_at and self.expires_at <= timezone.now():
            raise ValidationError("Offer expiry must be in the future.")

class ReviewQuerySet(models.QuerySet):
    def visible(self):
        return self.filter(is_visible=True)

    def for_subject(self, subject_type, profile_id):
        return self.filter(subject_type=subject_type, subject_profile_id=profile_id)

    def rating_summary(self, subject_type, profile_id):
        """Average rating and review count for one customer or rider profile."""
        return (
            self.visible()
            .for_subject(subject_type, profile_id)
            .aggregate(average=Avg("rating"), count=Count("id"))
        )


class Review(models.Model):
    """Customer or rider review/rating after a completed service."""

    class Rating(models.IntegerChoices):
        POOR = 1, "Poor"
        FAIR = 2, "Fair"
        GOOD = 3, "Good"
        VERY_GOOD = 4, "Very Good"
        EXCELLENT = 5, "Excellent"

    class SubjectType(models.TextChoices):
        CUSTOMER = "customer", "Customer"
        RIDER = "rider", "Rider"

    booking_ref = models.CharField(max_length=50)
    reviewer = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="reviews_written",
    )
    subject_type = models.CharField(max_length=20, choices=SubjectType.choices)
    subject_profile_id = models.PositiveIntegerField()
    rating = models.PositiveSmallIntegerField(choices=Rating.choices)
    comment = models.TextField(blank=True, default="")
    is_visible = models.BooleanField(
        default=True,
        help_text="Untick to hide a review from public display and rating averages.",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = ReviewQuerySet.as_manager()

    class Meta:
        db_table = "reviews"
        ordering = ["-created_at"]
        verbose_name = "Review"
        verbose_name_plural = "Reviews"
        indexes = [
            models.Index(
                fields=["subject_type", "subject_profile_id", "is_visible"],
                name="review_subject_idx",
            ),
            models.Index(fields=["reviewer", "-created_at"], name="review_reviewer_idx"),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["booking_ref", "subject_type"],
                name="review_one_per_booking_direction",
            ),
            models.CheckConstraint(
                condition=models.Q(rating__gte=1, rating__lte=5),
                name="review_rating_between_1_and_5",
            ),
        ]

    def __str__(self):
        return f"{self.booking_ref}: ★ {self.rating}/5 ({self.get_subject_type_display()})"



def to_money(value):
    """Validate and normalise an amount. Floats are rejected to avoid rounding drift."""
    if isinstance(value, float):
        raise TypeError("Pass money as Decimal or str, never float.")
    try:
        amount = Decimal(str(value))
    except InvalidOperation:
        raise ValidationError("Invalid amount.", code="invalid_amount")
    if not amount.is_finite() or amount <= 0 or amount >= MAX_AMOUNT:
        raise ValidationError("Amount must be greater than zero.", code="invalid_amount")
    if amount != amount.quantize(MONEY_STEP):
        raise ValidationError("Amount can have at most 2 decimal places.", code="invalid_amount")
    return amount


def generate_txn_reference():
    return f"TXN-{uuid.uuid4().hex[:12].upper()}"


def generate_withdrawal_reference():
    return f"WDR-{uuid.uuid4().hex[:12].upper()}"

class DispatchOffer(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        ACCEPTED = "accepted", "Accepted"
        DECLINED = "declined", "Declined"
        EXPIRED = "expired", "Expired"
        CANCELLED = "cancelled", "Cancelled"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    booking = models.ForeignKey("Booking", on_delete=models.CASCADE, related_name="dispatch_offers")
    rider = models.ForeignKey("RiderProfile", on_delete=models.CASCADE, related_name="dispatch_offers")
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.PENDING, db_index=True)
    round_no = models.PositiveSmallIntegerField(default=1)
    distance_km = models.DecimalField(max_digits=6, decimal_places=2, default=0)
    score = models.FloatField(default=0)
    offered_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField(db_index=True)
    responded_at = models.DateTimeField(null=True, blank=True)
    decline_reason = models.CharField(max_length=60, blank=True)

    class Meta:
        ordering = ["-offered_at"]
        constraints = [
            models.UniqueConstraint(fields=["booking", "rider"], name="uniq_offer_booking_rider"),
            models.UniqueConstraint(
                fields=["booking"], condition=Q(status__in=["pending", "accepted"]),
                name="one_live_offer_per_booking"),
            models.UniqueConstraint(
                fields=["rider"], condition=Q(status="pending"),
                name="one_pending_offer_per_rider"),
        ]

    @property
    def is_expired(self):
        return self.expires_at <= timezone.now()

    def __str__(self):
        return f"{self.booking_id} -> {self.rider_id} ({self.status})"





class RiderWallet(models.Model):
    """Rider balance. Every change to it goes through the WalletTransaction ledger.

    `balance` is spendable money. `held_balance` is money already deducted from
    `balance` and reserved for payouts that are still in flight.
    """

    class Currency(models.TextChoices):
        GHS = "GHS", "Ghana Cedi"
        GBP = "GBP", "British Pound"

    rider = models.OneToOneField(
        "RiderProfile",
        on_delete=models.PROTECT,
        related_name="wallet",
    )
    balance = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    held_balance = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    currency = models.CharField(
        max_length=10, choices=Currency.choices, default=Currency.GHS
    )
    total_earned = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    total_withdrawn = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))

    min_withdrawal = models.DecimalField(
        max_digits=10, decimal_places=2, default=Decimal("10.00"),
        validators=[MinValueValidator(Decimal("0.01"))],
    )
    daily_withdrawal_limit = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal("1000.00"),
        validators=[MinValueValidator(Decimal("0.01"))],
        help_text="Maximum total withdrawn in any rolling 24 hours.",
    )

    is_active = models.BooleanField(default=True)
    is_frozen = models.BooleanField(
        default=False, help_text="Frozen wallets can receive money but cannot pay out."
    )
    frozen_reason = models.CharField(max_length=255, blank=True, default="")
    frozen_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "rider_wallets"
        ordering = ["-created_at"]
        verbose_name = "Rider Wallet"
        verbose_name_plural = "Rider Wallets"
        constraints = [
            models.CheckConstraint(
                condition=models.Q(balance__gte=0), name="wallet_balance_non_negative"
            ),
            models.CheckConstraint(
                condition=models.Q(held_balance__gte=0), name="wallet_held_non_negative"
            ),
        ]

    def __str__(self):
        user = self.rider.user
        name = user.get_full_name() or user.get_username()
        return f"{name}: {self.balance} {self.currency}"

    @property
    def total_funds(self):
        return self.balance + self.held_balance

    # ---- internal helpers -------------------------------------------------

    def _lock(self):
        return type(self).objects.select_for_update().get(pk=self.pk)

    def _post_locked(self, amount, tx_type, *, description="", booking_ref="",
                     related_reference="", idempotency_key="", created_by=None,
                     held_delta=Decimal("0.00")):
        """Apply one ledger entry. Caller must hold the row lock inside a transaction."""
        amount = to_money(amount)

        if idempotency_key:
            existing = self.transactions.filter(idempotency_key=idempotency_key).first()
            if existing:
                if existing.tx_type != tx_type or existing.amount != amount:
                    raise ValidationError(
                        "Idempotency key was already used for a different transaction.",
                        code="idempotency_conflict",
                    )
                return existing

        is_debit = tx_type in WalletTransaction.DEBIT_TYPES
        if not self.is_active and tx_type != WalletTransaction.Type.WITHDRAWAL_REVERSAL:
            raise ValidationError("This wallet is inactive.", code="wallet_inactive")
        if is_debit and self.is_frozen:
            raise ValidationError("This wallet is frozen.", code="wallet_frozen")

        before = self.balance
        after = before - amount if is_debit else before + amount
        if after < 0:
            raise ValidationError("Insufficient funds.", code="insufficient_funds")

        self.balance = after
        self.held_balance += held_delta
        fields = ["balance", "held_balance", "updated_at"]
        if tx_type in WalletTransaction.EARNING_TYPES:
            self.total_earned += amount
            fields.append("total_earned")
        self.save(update_fields=fields)

        return WalletTransaction.objects.create(
            wallet=self, tx_type=tx_type, amount=amount,
            balance_before=before, balance_after=after,
            description=description, booking_ref=booking_ref,
            related_reference=related_reference,
            idempotency_key=idempotency_key, created_by=created_by,
        )

    # ---- public API -------------------------------------------------------

    def post(self, amount, tx_type, **kwargs):
        """Credit or debit the wallet (direction is fixed by `tx_type`).

        Pass `idempotency_key` for anything triggered by a webhook or retried
        request: repeating the call returns the original entry instead of paying twice.
        Withdrawals must go through `request_withdrawal()`.
        """
        if tx_type == WalletTransaction.Type.WITHDRAWAL:
            raise ValueError("Use request_withdrawal() for withdrawals.")
        with transaction.atomic():
            tx = self._lock()._post_locked(amount, tx_type, **kwargs)
        self.refresh_from_db()
        return tx

    def request_withdrawal(self, amount, *, method, account_name, account_number,
                           provider="", fee=Decimal("0.00"), idempotency_key="",
                           user=None):
        """Reserve funds and create a payout request."""
        amount = to_money(amount)
        fee = Decimal(str(fee))
        if fee < 0 or fee >= amount:
            raise ValidationError("Invalid fee.", code="invalid_fee")

        with transaction.atomic():
            wallet = self._lock()

            if idempotency_key:
                existing = WithdrawalRequest.objects.filter(
                    wallet=wallet, idempotency_key=idempotency_key
                ).first()
                if existing:
                    return existing

            if not wallet.is_active:
                raise ValidationError("This wallet is inactive.", code="wallet_inactive")
            if wallet.is_frozen:
                raise ValidationError("This wallet is frozen.", code="wallet_frozen")
            if amount < wallet.min_withdrawal:
                raise ValidationError(
                    f"Minimum withdrawal is {wallet.min_withdrawal} {wallet.currency}.",
                    code="below_minimum",
                )

            window_start = timezone.now() - timedelta(hours=24)
            used = (
                WithdrawalRequest.objects.filter(wallet=wallet, requested_at__gte=window_start)
                .exclude(status__in=[
                    WithdrawalRequest.Status.FAILED, WithdrawalRequest.Status.CANCELLED
                ])
                .aggregate(total=Sum("amount"))["total"] or Decimal("0.00")
            )
            if used + amount > wallet.daily_withdrawal_limit:
                raise ValidationError("Daily withdrawal limit exceeded.", code="limit_exceeded")

            withdrawal = WithdrawalRequest.objects.create(
                wallet=wallet, amount=amount, fee=fee, method=method,
                provider=provider, account_name=account_name,
                account_number=account_number, idempotency_key=idempotency_key,
            )
            wallet._post_locked(
                amount, WalletTransaction.Type.WITHDRAWAL,
                description=f"Withdrawal {withdrawal.reference}",
                related_reference=withdrawal.reference,
                created_by=user, held_delta=amount,
            )
        self.refresh_from_db()
        return withdrawal

    def freeze(self, reason, user=None):
        self.is_frozen = True
        self.frozen_reason = reason[:255]
        self.frozen_at = timezone.now()
        self.save(update_fields=["is_frozen", "frozen_reason", "frozen_at", "updated_at"])

    def unfreeze(self):
        self.is_frozen = False
        self.frozen_reason = ""
        self.frozen_at = None
        self.save(update_fields=["is_frozen", "frozen_reason", "frozen_at", "updated_at"])

    def ledger_matches_balance(self):
        """Integrity check: the ledger's net total must equal `balance`."""
        signed = Case(
            When(direction=WalletTransaction.Direction.CREDIT, then=F("amount")),
            default=-F("amount"),
            output_field=DecimalField(max_digits=12, decimal_places=2),
        )
        net = self.transactions.aggregate(net=Sum(signed))["net"] or Decimal("0.00")
        return net == self.balance

class WalletTransaction(models.Model):
    """Append-only ledger entry. Never edited or deleted."""

    class Type(models.TextChoices):
        EARNING = "earning", "Trip Earning"
        BONUS = "bonus", "Bonus"
        DEPOSIT = "deposit", "Deposit"
        WITHDRAWAL_REVERSAL = "withdrawal_reversal", "Withdrawal Reversal"
        ADJUSTMENT_CREDIT = "adjustment_credit", "Adjustment (Credit)"
        COMMISSION = "commission", "Platform Commission"
        WITHDRAWAL = "withdrawal", "Withdrawal"
        ADJUSTMENT_DEBIT = "adjustment_debit", "Adjustment (Debit)"

    class Direction(models.TextChoices):
        CREDIT = "credit", "Credit"
        DEBIT = "debit", "Debit"

    DEBIT_TYPES = (Type.COMMISSION, Type.WITHDRAWAL, Type.ADJUSTMENT_DEBIT)
    EARNING_TYPES = (Type.EARNING, Type.BONUS)

    wallet = models.ForeignKey(
        RiderWallet, on_delete=models.PROTECT, related_name="transactions"
    )
    reference = models.CharField(
        max_length=20, unique=True, editable=False, default=generate_txn_reference
    )
    tx_type = models.CharField(max_length=30, choices=Type.choices)
    direction = models.CharField(max_length=10, choices=Direction.choices, editable=False)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    balance_before = models.DecimalField(max_digits=12, decimal_places=2)
    balance_after = models.DecimalField(max_digits=12, decimal_places=2)
    description = models.CharField(max_length=255, blank=True, default="")
    booking_ref = models.CharField(max_length=50, blank=True, default="")
    related_reference = models.CharField(max_length=50, blank=True, default="")
    idempotency_key = models.CharField(max_length=100, blank=True, default="")
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name="wallet_transactions_created",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "wallet_transactions"
        ordering = ["-created_at", "-id"]
        verbose_name = "Wallet Transaction"
        verbose_name_plural = "Wallet Transactions"
        indexes = [
            models.Index(fields=["wallet", "-created_at"], name="wtx_wallet_created_idx"),
            models.Index(fields=["booking_ref"], name="wtx_booking_ref_idx"),
            models.Index(fields=["tx_type"], name="wtx_type_idx"),
        ]
        constraints = [
            models.CheckConstraint(condition=models.Q(amount__gt=0), name="wtx_amount_positive"),
            models.CheckConstraint(
                condition=models.Q(balance_after__gte=0), name="wtx_balance_after_non_negative"
            ),
            models.UniqueConstraint(
                fields=["wallet", "idempotency_key"],
                condition=~models.Q(idempotency_key=""),
                name="wtx_unique_idempotency_key",
            ),
        ]

    def __str__(self):
        sign = "-" if self.direction == self.Direction.DEBIT else "+"
        return f"{self.reference}: {sign}{self.amount} ({self.get_tx_type_display()})"

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValueError("Wallet transactions are immutable.")
        self.direction = (
            self.Direction.DEBIT if self.tx_type in self.DEBIT_TYPES else self.Direction.CREDIT
        )
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValueError("Wallet transactions cannot be deleted.")

class WalletDeposit(AppendOnlyModel):
    """Money added to a rider's wallet: cash top-up, bank transfer, mobile money or bonus.

    Create deposits only with `WalletDeposit.record()`. It credits the wallet
    ledger and writes the deposit in one transaction.
    """

    class SourceType(models.TextChoices):
        CASH = "cash", "Cash Payment"
        BANK_TRANSFER = "bank_transfer", "Bank Transfer"
        MOBILE_MONEY = "mobile_money", "Mobile Money"
        BONUS = "bonus", "Bonus / Incentive"
        OTHER = "other", "Other"

    # Entered by staff, so a staff member must be recorded for accountability.
    STAFF_RECORDED_SOURCES = (SourceType.CASH, SourceType.BONUS, SourceType.OTHER)
    # Come from a provider or bank, so a provider reference is required.
    REFERENCE_REQUIRED_SOURCES = (SourceType.BANK_TRANSFER, SourceType.MOBILE_MONEY)

    wallet = models.ForeignKey(
        RiderWallet,
        on_delete=models.PROTECT,
        related_name="deposits",
    )
    reference = models.CharField(
        max_length=30,
        unique=True,
        editable=False,
        default=generate_deposit_reference,
    )
    amount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0.01"))],
    )
    source = models.CharField(
        max_length=30,
        choices=SourceType.choices,
        default=SourceType.OTHER,
    )
    source_reference = models.CharField(
        max_length=255,
        blank=True,
        default="",
        help_text="Transaction ID from the payment provider, or receipt number.",
    )
    notes = models.TextField(blank=True, default="")
    ledger_entry = models.OneToOneField(
        WalletTransaction,
        on_delete=models.PROTECT,
        related_name="deposit",
        editable=False,
    )
    recorded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="wallet_deposits_recorded",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "wallet_deposits"
        ordering = ["-created_at", "-id"]
        verbose_name = "Wallet Deposit"
        verbose_name_plural = "Wallet Deposits"
        indexes = [
            models.Index(fields=["wallet", "-created_at"], name="deposit_wallet_created_idx"),
            models.Index(fields=["source", "-created_at"], name="deposit_source_created_idx"),
        ]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(amount__gt=0), name="deposit_amount_positive"
            ),
            models.UniqueConstraint(
                fields=["source", "source_reference"],
                condition=~models.Q(source_reference=""),
                name="deposit_unique_source_reference",
            ),
        ]

    def __str__(self):
        return f"{self.reference}: +{self.amount} {self.wallet.currency}"

    def delete(self, *args, **kwargs):
        raise ValueError("Wallet deposits cannot be deleted.")

    @classmethod
    def record(cls, wallet, amount, source=SourceType.OTHER, *,
               source_reference="", notes="", recorded_by=None):
        """Credit the wallet and log the deposit atomically.

        Safe to retry: repeating a call with the same `source` and
        `source_reference` returns the original deposit instead of paying twice.
        """
        amount = to_money(amount)
        source = cls.SourceType(source)
        source_reference = (source_reference or "").strip()

        if source in cls.STAFF_RECORDED_SOURCES and recorded_by is None:
            raise ValidationError(
                "A staff member must be recorded for this deposit type.",
                code="recorded_by_required",
            )
        if source in cls.REFERENCE_REQUIRED_SOURCES and not source_reference:
            raise ValidationError(
                "A provider transaction reference is required for this deposit type.",
                code="source_reference_required",
            )

        with transaction.atomic():
            locked = RiderWallet.objects.select_for_update().get(pk=wallet.pk)

            if source_reference:
                existing = cls.objects.filter(
                    source=source, source_reference=source_reference
                ).first()
                if existing:
                    if existing.wallet_id != locked.pk or existing.amount != amount:
                        raise ValidationError(
                            "This provider reference was already used for a different deposit.",
                            code="duplicate_source_reference",
                        )
                    return existing

            reference = generate_deposit_reference()
            tx_type = (
                WalletTransaction.Type.BONUS
                if source == cls.SourceType.BONUS
                else WalletTransaction.Type.DEPOSIT
            )
            entry = locked._post_locked(
                amount,
                tx_type,
                description=f"Deposit {reference} ({source.label})",
                related_reference=reference,
                idempotency_key=f"deposit:{reference}",
                created_by=recorded_by,
            )
            deposit = cls.objects.create(
                wallet=locked,
                reference=reference,
                amount=amount,
                source=source,
                source_reference=source_reference,
                notes=notes,
                recorded_by=recorded_by,
                ledger_entry=entry,
            )

        wallet.refresh_from_db()
        return deposit


class WithdrawalRequest(models.Model):
    """A payout request. Funds are reserved when it is created."""

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        PROCESSING = "processing", "Processing"
        COMPLETED = "completed", "Completed"
        FAILED = "failed", "Failed"
        CANCELLED = "cancelled", "Cancelled"

    class Method(models.TextChoices):
        MOBILE_MONEY = "mobile_money", "Mobile Money"
        BANK_TRANSFER = "bank_transfer", "Bank Transfer"

    wallet = models.ForeignKey(
        RiderWallet, on_delete=models.PROTECT, related_name="withdrawals"
    )
    reference = models.CharField(
        max_length=20, unique=True, editable=False, default=generate_withdrawal_reference
    )
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    fee = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0.00"))
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    method = models.CharField(max_length=20, choices=Method.choices)
    provider = models.CharField(max_length=50, blank=True, default="")
    account_name = models.CharField(max_length=100)
    # Store encrypted at rest (e.g. django-fernet-fields) in production.
    account_number = models.CharField(
        max_length=34,
        validators=[RegexValidator(r"^[0-9]{6,34}$", "Digits only.")],
    )
    idempotency_key = models.CharField(max_length=100, blank=True, default="")
    provider_reference = models.CharField(max_length=100, blank=True, default="")
    failure_reason = models.CharField(max_length=255, blank=True, default="")
    processed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name="withdrawals_processed",
    )
    requested_at = models.DateTimeField(auto_now_add=True)
    processed_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "wallet_withdrawals"
        ordering = ["-requested_at"]
        verbose_name = "Withdrawal Request"
        verbose_name_plural = "Withdrawal Requests"
        indexes = [
            models.Index(fields=["wallet", "-requested_at"], name="wdr_wallet_requested_idx"),
            models.Index(fields=["status", "-requested_at"], name="wdr_status_requested_idx"),
        ]
        constraints = [
            models.CheckConstraint(condition=models.Q(amount__gt=0), name="wdr_amount_positive"),
            models.CheckConstraint(condition=models.Q(fee__gte=0), name="wdr_fee_non_negative"),
            models.CheckConstraint(condition=models.Q(fee__lt=F("amount")), name="wdr_fee_below_amount"),
            models.UniqueConstraint(
                fields=["wallet", "idempotency_key"],
                condition=~models.Q(idempotency_key=""),
                name="wdr_unique_idempotency_key",
            ),
        ]

    def __str__(self):
        return f"{self.reference}: {self.amount} ({self.get_status_display()})"

    @property
    def net_amount(self):
        return self.amount - self.fee

    @property
    def masked_account(self):
        return f"{'*' * max(len(self.account_number) - 4, 0)}{self.account_number[-4:]}"

    def _transition(self, allowed_from, new_status, *, refund=False, complete=False,
                    user=None, **fields):
        with transaction.atomic():
            # Lock order is always wallet, then withdrawal.
            wallet = RiderWallet.objects.select_for_update().get(pk=self.wallet_id)
            wd = type(self).objects.select_for_update().get(pk=self.pk)
            if wd.status not in allowed_from:
                raise ValidationError(
                    f"Cannot move a {wd.get_status_display().lower()} withdrawal to "
                    f"{new_status.label.lower()}.",
                    code="invalid_transition",
                )
            if refund:
                wallet._post_locked(
                    wd.amount, WalletTransaction.Type.WITHDRAWAL_REVERSAL,
                    description=f"Reversal of {wd.reference}",
                    related_reference=wd.reference,
                    idempotency_key=f"reversal:{wd.reference}",
                    created_by=user, held_delta=-wd.amount,
                )
            elif complete:
                wallet.held_balance -= wd.amount
                wallet.total_withdrawn += wd.amount
                wallet.save(update_fields=["held_balance", "total_withdrawn", "updated_at"])

            wd.status = new_status
            for name, value in fields.items():
                setattr(wd, name, value)
            if new_status != self.Status.PROCESSING:
                wd.processed_at = timezone.now()
            if user is not None:
                wd.processed_by = user
            wd.save()
        self.refresh_from_db()

    def mark_processing(self, user=None):
        self._transition([self.Status.PENDING], self.Status.PROCESSING, user=user)

    def mark_completed(self, provider_reference="", user=None):
        self._transition(
            [self.Status.PENDING, self.Status.PROCESSING], self.Status.COMPLETED,
            complete=True, user=user, provider_reference=provider_reference,
        )

    def mark_failed(self, reason, user=None):
        self._transition(
            [self.Status.PENDING, self.Status.PROCESSING], self.Status.FAILED,
            refund=True, user=user, failure_reason=reason[:255],
        )

    def cancel(self, user=None):
        self._transition(
            [self.Status.PENDING], self.Status.CANCELLED, refund=True, user=user
        )



class PaymentQuerySet(models.QuerySet):
    def pending(self):
        return self.filter(status=Payment.Status.PENDING)

    def successful(self):
        """Money actually received (including partly refunded payments)."""
        return self.filter(status__in=Payment.SUCCESS_STATUSES)

    def for_booking(self, booking_ref):
        return self.filter(booking_ref=booking_ref)

    def expire_stale(self):
        """Fail pending payments that passed their expiry. Returns the count.

        Runs each one through mark_as_failed() so the audit log stays complete.
        """
        count = 0
        stale = self.pending().filter(expires_at__isnull=False, expires_at__lte=timezone.now())
        for payment in stale:
            try:
                payment.mark_as_failed("Payment timed out.")
                count += 1
            except ValidationError:
                continue  # settled by another process in the meantime
        return count


class Payment(models.Model):
    """Record of a payment for a booking: cash, mobile money, card, bank transfer or wallet."""

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        PAID = "paid", "Paid"
        FAILED = "failed", "Failed"
        PARTIALLY_REFUNDED = "partially_refunded", "Partially Refunded"
        REFUNDED = "refunded", "Refunded"
        CANCELLED = "cancelled", "Cancelled"

    class Method(models.TextChoices):
        CASH = "cash", "Cash"
        MOMO = "momo", "Mobile Money"
        WALLET = "wallet", "Wallet Balance"
        CARD = "card", "Debit / Credit Card"
        BANK_TRANSFER = "bank_transfer", "Bank Transfer"

    class Currency(models.TextChoices):
        GHS = "GHS", "Ghana Cedi"
        GBP = "GBP", "British Pound"

    SUCCESS_STATUSES = (Status.PAID, Status.PARTIALLY_REFUNDED, Status.REFUNDED)
    PROVIDER_METHODS = (Method.MOMO, Method.CARD, Method.BANK_TRANSFER)

    booking_ref = models.CharField(max_length=50)
    reference = models.CharField(
        max_length=30,
        unique=True,
        editable=False,
        default=generate_payment_reference,
    )
    amount = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0.01"))],
    )
    currency = models.CharField(
        max_length=3, choices=Currency.choices, default=Currency.GHS
    )
    refunded_amount = models.DecimalField(
        max_digits=10, decimal_places=2, default=Decimal("0.00"), editable=False
    )
    method = models.CharField(max_length=20, choices=Method.choices)
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.PENDING
    )
    payer_profile_id = models.PositiveIntegerField(null=True, blank=True)
    transaction_id = models.CharField(
        max_length=255,
        blank=True,
        default="",
        help_text="External transaction ID from the payment provider.",
    )
    failure_reason = models.TextField(blank=True, default="")
    notes = models.TextField(blank=True, default="")
    metadata = models.JSONField(
        default=dict,
        blank=True,
        help_text="Non-sensitive provider details. Never store card numbers or CVVs.",
    )
    recorded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="payments_recorded",
    )
    expires_at = models.DateTimeField(
        null=True, blank=True, help_text="Pending payments are failed after this time."
    )
    recorded_at = models.DateTimeField(
        null=True, blank=True, help_text="When the money was confirmed received."
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = PaymentQuerySet.as_manager()

    class Meta:
        db_table = "payments"
        ordering = ["-created_at"]
        verbose_name = "Payment"
        verbose_name_plural = "Payments"
        indexes = [
            models.Index(fields=["booking_ref", "status"], name="payment_booking_status_idx"),
            models.Index(fields=["status", "-created_at"], name="payment_status_created_idx"),
            models.Index(fields=["method", "status"], name="payment_method_status_idx"),
            models.Index(fields=["payer_profile_id", "-created_at"], name="payment_payer_idx"),
        ]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(amount__gt=0), name="payment_amount_positive"
            ),
            models.CheckConstraint(
                condition=models.Q(refunded_amount__gte=0, refunded_amount__lte=models.F("amount")),
                name="payment_refund_within_amount",
            ),
            models.CheckConstraint(
                condition=(
                    ~models.Q(status__in=["paid", "partially_refunded", "refunded"])
                    | models.Q(recorded_at__isnull=False)
                ),
                name="payment_settled_has_recorded_at",
            ),
            models.UniqueConstraint(
                fields=["method", "transaction_id"],
                condition=~models.Q(transaction_id=""),
                name="payment_unique_provider_transaction",
            ),
            models.UniqueConstraint(
                fields=["booking_ref"],
                condition=models.Q(status__in=["paid", "partially_refunded"]),
                name="payment_one_paid_per_booking",
            ),
        ]

    def __str__(self):
        return f"{self.reference}: {self.booking_ref} — {self.amount} {self.currency} ({self.get_status_display()})"

    # ---- read helpers -----------------------------------------------------

    @property
    def is_successful(self):
        return self.status in (self.Status.PAID, self.Status.PARTIALLY_REFUNDED)

    @property
    def refundable_amount(self):
        if not self.is_successful:
            return Decimal("0.00")
        return self.amount - self.refunded_amount

    @property
    def net_amount(self):
        """Money kept after refunds."""
        return self.amount - self.refunded_amount if self.recorded_at else Decimal("0.00")

    # ---- state changes (all lock the row and write an audit event) --------

    def _lock(self):
        return type(self).objects.select_for_update().get(pk=self.pk)

    @staticmethod
    def _log(payment, old, new, *, amount=None, note="", user=None):
        PaymentEvent.objects.create(
            payment=payment, from_status=old, to_status=new,
            amount=amount, note=(note or "")[:255], created_by=user,
        )

    def mark_as_paid(self, transaction_id="", user=None):
        """Confirm that the money was received. Safe to call twice with the same
        transaction ID, so provider webhook retries are harmless."""
        transaction_id = (transaction_id or "").strip()

        with transaction.atomic():
            p = self._lock()

            if p.recorded_at is not None:
                if transaction_id and p.transaction_id == transaction_id:
                    self.refresh_from_db()
                    return self  # webhook replay
                raise ValidationError("This payment has already been completed.", code="already_paid")

            # A provider can confirm after we timed the payment out; the money is real.
            late_confirmation = (
                p.status == self.Status.FAILED
                and p.method in self.PROVIDER_METHODS
                and transaction_id
            )
            if p.status != self.Status.PENDING and not late_confirmation:
                raise ValidationError(
                    f"A {p.get_status_display().lower()} payment cannot be marked as paid.",
                    code="invalid_transition",
                )
            if p.method in self.PROVIDER_METHODS and not transaction_id:
                raise ValidationError(
                    "A provider transaction ID is required.", code="transaction_id_required"
                )
            if p.method == self.Method.CASH and user is None:
                raise ValidationError(
                    "Cash payments must be confirmed by a staff member or rider.",
                    code="recorded_by_required",
                )

            old = p.status
            p.status = self.Status.PAID
            p.recorded_at = timezone.now()
            p.failure_reason = ""
            if transaction_id:
                p.transaction_id = transaction_id
            if user is not None:
                p.recorded_by = user

            try:
                with transaction.atomic():
                    p.save()
            except IntegrityError:
                raise ValidationError(
                    "This booking already has a paid payment, or this transaction ID "
                    "was already used.",
                    code="duplicate_payment",
                )
            self._log(
                p, old, p.status, amount=p.amount, user=user,
                note="Late provider confirmation." if late_confirmation else "",
            )
        self.refresh_from_db()
        return self

    def mark_as_failed(self, reason="", user=None):
        with transaction.atomic():
            p = self._lock()
            if p.status != self.Status.PENDING:
                raise ValidationError(
                    f"A {p.get_status_display().lower()} payment cannot be marked as failed.",
                    code="invalid_transition",
                )
            old = p.status
            p.status = self.Status.FAILED
            p.failure_reason = reason or ""
            p.save(update_fields=["status", "failure_reason", "updated_at"])
            self._log(p, old, p.status, note=reason, user=user)
        self.refresh_from_db()
        return self

    def cancel(self, user=None, reason=""):
        with transaction.atomic():
            p = self._lock()
            if p.status != self.Status.PENDING:
                raise ValidationError(
                    "Only pending payments can be cancelled.", code="invalid_transition"
                )
            old = p.status
            p.status = self.Status.CANCELLED
            p.save(update_fields=["status", "updated_at"])
            self._log(p, old, p.status, note=reason, user=user)
        self.refresh_from_db()
        return self

    def refund(self, amount=None, reason="", user=None):
        """Record a full or partial refund. Defaults to refunding everything left.

        This records the refund only; send the money back through your provider
        (or wallet) before or alongside calling it.
        """
        with transaction.atomic():
            p = self._lock()
            if not p.is_successful:
                raise ValidationError(
                    "Only paid payments can be refunded.", code="invalid_transition"
                )
            remaining = p.amount - p.refunded_amount
            amount = remaining if amount is None else to_money(amount)
            if amount > remaining:
                raise ValidationError(
                    f"Cannot refund more than the remaining {remaining} {p.currency}.",
                    code="refund_exceeds_amount",
                )
            old = p.status
            p.refunded_amount += amount
            p.status = (
                self.Status.REFUNDED
                if p.refunded_amount == p.amount
                else self.Status.PARTIALLY_REFUNDED
            )
            p.save(update_fields=["refunded_amount", "status", "updated_at"])
            self._log(p, old, p.status, amount=amount, note=reason, user=user)
        self.refresh_from_db()
        return self


class RefundQuerySet(models.QuerySet):
    def open(self):
        return self.filter(status__in=Refund.OPEN_STATUSES)

    def awaiting_review(self):
        return self.filter(status=Refund.Status.REQUESTED)

    def completed(self):
        return self.filter(status=Refund.Status.COMPLETED)


class Refund(models.Model):
    """Money returned to a customer, tied to the original payment.

    Create refunds with `Refund.request()`. Completing a refund records it on the
    payment (`Payment.refund()`), so the payment's refunded amount and status
    only change once the money has really gone back.
    """

    class Status(models.TextChoices):
        REQUESTED = "requested", "Requested"
        APPROVED = "approved", "Approved"
        PROCESSING = "processing", "Processing"
        COMPLETED = "completed", "Completed"
        FAILED = "failed", "Failed"
        REJECTED = "rejected", "Rejected"
        CANCELLED = "cancelled", "Cancelled"

    class Reason(models.TextChoices):
        CUSTOMER_REQUEST = "customer_request", "Customer Request"
        SERVICE_ISSUE = "service_issue", "Service / Delivery Issue"
        DUPLICATE_PAYMENT = "duplicate", "Duplicate Payment"
        WRONG_AMOUNT = "wrong_amount", "Incorrect Amount Charged"
        OTHER = "other", "Other"

    # Statuses that reserve part of the payment so refunds can't add up to more than was paid.
    OPEN_STATUSES = (Status.REQUESTED, Status.APPROVED, Status.PROCESSING)

    # Set to a number of days to refuse refund requests on old payments
    # (duplicate-payment refunds are always allowed). None disables the limit.
    REFUND_WINDOW_DAYS = None

    payment = models.ForeignKey(
        "Payment",
        on_delete=models.PROTECT,
        related_name="refunds",
    )
    reference = models.CharField(
        max_length=30,
        unique=True,
        editable=False,
        default=generate_refund_reference,
    )
    amount = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0.01"))],
    )
    reason = models.CharField(max_length=30, choices=Reason.choices)
    reason_details = models.TextField(blank=True, default="")
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.REQUESTED
    )
    idempotency_key = models.CharField(max_length=100, blank=True, default="")
    refund_transaction_id = models.CharField(
        max_length=255,
        blank=True,
        default="",
        help_text="Transaction ID from the bank or payment provider.",
    )
    failure_reason = models.CharField(max_length=255, blank=True, default="")
    rejection_reason = models.CharField(max_length=255, blank=True, default="")
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="refunds_requested",
    )
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="refunds_reviewed",
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)
    processed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = RefundQuerySet.as_manager()

    class Meta:
        db_table = "refunds"
        ordering = ["-created_at"]
        verbose_name = "Refund"
        verbose_name_plural = "Refunds"
        indexes = [
            models.Index(fields=["payment", "status"], name="refund_payment_status_idx"),
            models.Index(fields=["status", "-created_at"], name="refund_status_created_idx"),
        ]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(amount__gt=0), name="refund_amount_positive"
            ),
            models.CheckConstraint(
                condition=~models.Q(requested_by=models.F("reviewed_by")),
                name="refund_reviewer_not_requester",
            ),
            models.UniqueConstraint(
                fields=["payment", "idempotency_key"],
                condition=~models.Q(idempotency_key=""),
                name="refund_unique_idempotency_key",
            ),
            models.UniqueConstraint(
                fields=["refund_transaction_id"],
                condition=~models.Q(refund_transaction_id=""),
                name="refund_unique_provider_transaction",
            ),
        ]

    def __str__(self):
        return f"{self.reference}: {self.amount} {self.currency} → {self.get_status_display()}"

    @property
    def currency(self):
        return self.payment.currency

    @property
    def is_open(self):
        return self.status in self.OPEN_STATUSES

    # ---- amounts ------------------------------------------------------------

    @classmethod
    def reserved_for(cls, payment, exclude_pk=None):
        """Total of unfinished refunds that already claim part of this payment."""
        qs = cls.objects.filter(payment=payment, status__in=cls.OPEN_STATUSES)
        if exclude_pk:
            qs = qs.exclude(pk=exclude_pk)
        return qs.aggregate(total=Sum("amount"))["total"] or Decimal("0.00")

    @classmethod
    def available_for(cls, payment, exclude_pk=None):
        return payment.amount - payment.refunded_amount - cls.reserved_for(payment, exclude_pk)

    def clean(self):
        """Form/admin validation. `request()` is the race-safe path."""
        if not self.payment_id or self.amount is None:
            return
        if self.reason == self.Reason.OTHER and not self.reason_details.strip():
            raise ValidationError({"reason_details": "Please describe the reason."})
        exclude = self.pk if self.is_open else None
        available = self.available_for(self.payment, exclude_pk=exclude)
        if self._state.adding or self.is_open:
            if self.amount > available:
                raise ValidationError(
                    {"amount": f"Cannot refund more than the remaining {available} {self.currency}."}
                )

    # ---- creation -----------------------------------------------------------

    @classmethod
    def request(cls, payment, reason, *, amount=None, reason_details="",
                requested_by=None, idempotency_key=""):
        """Open a refund request. Defaults to the full refundable amount."""
        reason = cls.Reason(reason)
        if reason == cls.Reason.OTHER and not (reason_details or "").strip():
            raise ValidationError("Please describe the reason.", code="reason_details_required")

        with transaction.atomic():
            p = Payment.objects.select_for_update().get(pk=payment.pk)

            if idempotency_key:
                existing = cls.objects.filter(payment=p, idempotency_key=idempotency_key).first()
                if existing:
                    if amount is not None and to_money(amount) != existing.amount:
                        raise ValidationError(
                            "Idempotency key was already used for a different refund.",
                            code="idempotency_conflict",
                        )
                    return existing

            if not p.is_successful:
                raise ValidationError(
                    "Only paid payments can be refunded.", code="payment_not_refundable"
                )
            if (
                cls.REFUND_WINDOW_DAYS is not None
                and reason != cls.Reason.DUPLICATE_PAYMENT
                and p.recorded_at < timezone.now() - timedelta(days=cls.REFUND_WINDOW_DAYS)
            ):
                raise ValidationError(
                    f"Refunds must be requested within {cls.REFUND_WINDOW_DAYS} days of payment.",
                    code="refund_window_closed",
                )

            available = cls.available_for(p)
            if available <= 0:
                raise ValidationError(
                    "There is nothing left to refund on this payment.", code="nothing_to_refund"
                )
            amount = available if amount is None else to_money(amount)
            if amount > available:
                raise ValidationError(
                    f"Cannot refund more than the remaining {available} {p.currency}.",
                    code="refund_exceeds_amount",
                )

            refund = cls.objects.create(
                payment=p, amount=amount, reason=reason,
                reason_details=reason_details or "", requested_by=requested_by,
                idempotency_key=idempotency_key or "",
            )
            RefundEvent.objects.create(
                refund=refund, from_status="", to_status=refund.status,
                note="Refund requested.", created_by=requested_by,
            )
        return refund

    # ---- workflow -----------------------------------------------------------

    def _lock_pair(self):
        # Lock order is always payment, then refund.
        payment = Payment.objects.select_for_update().get(pk=self.payment_id)
        refund = type(self).objects.select_for_update().get(pk=self.pk)
        return payment, refund

    @staticmethod
    def _log(refund, old, note="", user=None):
        RefundEvent.objects.create(
            refund=refund, from_status=old, to_status=refund.status,
            note=(note or "")[:255], created_by=user,
        )

    def _require_status(self, refund, allowed, action):
        if refund.status not in allowed:
            raise ValidationError(
                f"A {refund.get_status_display().lower()} refund cannot be {action}.",
                code="invalid_transition",
            )

    def approve(self, reviewed_by):
        """Approve a request. The approver must be someone other than the requester."""
        if reviewed_by is None:
            raise ValidationError("An approver is required.", code="reviewer_required")
        with transaction.atomic():
            _, r = self._lock_pair()
            self._require_status(r, (self.Status.REQUESTED,), "approved")
            if r.requested_by_id is not None and r.requested_by_id == reviewed_by.pk:
                raise ValidationError(
                    "A refund cannot be approved by the person who requested it.",
                    code="self_approval",
                )
            old = r.status
            r.status = self.Status.APPROVED
            r.reviewed_by = reviewed_by
            r.reviewed_at = timezone.now()
            r.save(update_fields=["status", "reviewed_by", "reviewed_at", "updated_at"])
            self._log(r, old, user=reviewed_by)
        self.refresh_from_db()
        return self

    def reject(self, reviewed_by, reason=""):
        if reviewed_by is None:
            raise ValidationError("A reviewer is required.", code="reviewer_required")
        with transaction.atomic():
            _, r = self._lock_pair()
            self._require_status(r, (self.Status.REQUESTED,), "rejected")
            old = r.status
            r.status = self.Status.REJECTED
            r.reviewed_by = reviewed_by
            r.reviewed_at = timezone.now()
            r.rejection_reason = (reason or "")[:255]
            r.save(update_fields=[
                "status", "reviewed_by", "reviewed_at", "rejection_reason", "updated_at",
            ])
            self._log(r, old, note=reason, user=reviewed_by)
        self.refresh_from_db()
        return self

    def start_processing(self, user=None):
        with transaction.atomic():
            _, r = self._lock_pair()
            self._require_status(r, (self.Status.APPROVED,), "sent for processing")
            old = r.status
            r.status = self.Status.PROCESSING
            r.save(update_fields=["status", "updated_at"])
            self._log(r, old, user=user)
        self.refresh_from_db()
        return self

    def mark_as_completed(self, refund_transaction_id="", user=None):
        """Confirm the money went back, and record it on the payment.

        Call this from a verified provider webhook or a staff action. Repeating
        the call with the same transaction ID is harmless.
        """
        refund_transaction_id = (refund_transaction_id or "").strip()
        with transaction.atomic():
            payment, r = self._lock_pair()

            if r.status == self.Status.COMPLETED:
                if refund_transaction_id and r.refund_transaction_id == refund_transaction_id:
                    self.refresh_from_db()
                    return self  # webhook replay
                raise ValidationError("This refund is already completed.", code="already_completed")

            self._require_status(
                r, (self.Status.APPROVED, self.Status.PROCESSING), "marked as completed"
            )
            if payment.method in Payment.PROVIDER_METHODS and not refund_transaction_id:
                raise ValidationError(
                    "A provider refund transaction ID is required.",
                    code="transaction_id_required",
                )

            # Raises (and rolls everything back) if the payment can't take this refund.
            payment.refund(
                amount=r.amount,
                reason=f"Refund {r.reference}: {r.get_reason_display()}",
                user=user,
            )

            old = r.status
            r.status = self.Status.COMPLETED
            r.processed_at = timezone.now()
            r.refund_transaction_id = refund_transaction_id
            try:
                with transaction.atomic():
                    r.save(update_fields=[
                        "status", "processed_at", "refund_transaction_id", "updated_at",
                    ])
            except IntegrityError:
                raise ValidationError(
                    "This refund transaction ID was already used.",
                    code="duplicate_refund_transaction",
                )
            self._log(r, old, note=refund_transaction_id, user=user)
        self.refresh_from_db()
        return self

    def mark_as_failed(self, reason="", user=None):
        """The provider could not send the money. Frees the amount for a new request."""
        with transaction.atomic():
            _, r = self._lock_pair()
            self._require_status(
                r, (self.Status.APPROVED, self.Status.PROCESSING), "marked as failed"
            )
            old = r.status
            r.status = self.Status.FAILED
            r.failure_reason = (reason or "")[:255]
            r.processed_at = timezone.now()
            r.save(update_fields=["status", "failure_reason", "processed_at", "updated_at"])
            self._log(r, old, note=reason, user=user)
        self.refresh_from_db()
        return self

    def cancel(self, user=None, reason=""):
        with transaction.atomic():
            _, r = self._lock_pair()
            self._require_status(
                r, (self.Status.REQUESTED, self.Status.APPROVED), "cancelled"
            )
            old = r.status
            r.status = self.Status.CANCELLED
            r.save(update_fields=["status", "updated_at"])
            self._log(r, old, note=reason, user=user)
        self.refresh_from_db()
        return self


class Referral(models.Model):
    """One referral invite: who sent it, who signed up with it, and the rewards.

    Flow: PENDING (invite sent) → ACTIVE (friend signed up) → COMPLETED
    (friend did the qualifying action, reward earned). Use `register_signup()`,
    `complete()` and `mark_reward_paid()` rather than editing fields directly.
    """

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        ACTIVE = "active", "Active"
        COMPLETED = "completed", "Completed"
        EXPIRED = "expired", "Expired"
        CANCELLED = "cancelled", "Cancelled"

    class Currency(models.TextChoices):
        GHS = "GHS", "Ghana Cedi"
        GBP = "GBP", "British Pound"

    OPEN_STATUSES = (Status.PENDING, Status.ACTIVE)

    referrer = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="referrals_made",
        help_text="The person who sent the invite.",
    )
    referred_user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="referred_by",
        help_text="The person who signed up. Each account can be referred only once.",
    )
    code = models.CharField(
        max_length=20,
        unique=True,
        editable=False,
        default=generate_referral_code,
    )
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.PENDING
    )
    currency = models.CharField(
        max_length=3, choices=Currency.choices, default=Currency.GHS
    )
    reward_amount = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=Decimal("0.00"),
        validators=[MinValueValidator(Decimal("0.00"))],
        help_text="Reward for the referrer, fixed when the invite is created.",
    )
    referee_reward_amount = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=Decimal("0.00"),
        validators=[MinValueValidator(Decimal("0.00"))],
        help_text="Welcome reward for the new user.",
    )
    reward_reference = models.CharField(
        max_length=50,
        blank=True,
        default="",
        help_text="Reference of the wallet or payment entry that paid the referrer.",
    )
    cancel_reason = models.CharField(max_length=255, blank=True, default="")
    signed_up_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    rewarded_at = models.DateTimeField(null=True, blank=True)
    cancelled_at = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateTimeField(
        null=True,
        blank=True,
        default=default_referral_expiry,
        help_text="Unused invites expire at this time. Leave empty for no expiry.",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)


    class Meta:
        db_table = "referrals"
        ordering = ["-created_at"]
        verbose_name = "Referral"
        verbose_name_plural = "Referrals"
        indexes = [
            models.Index(fields=["referrer", "status"], name="referral_referrer_status_idx"),
            models.Index(fields=["status", "expires_at"], name="referral_status_expiry_idx"),
        ]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(reward_amount__gte=0, referee_reward_amount__gte=0),
                name="referral_rewards_non_negative",
            ),
            models.CheckConstraint(
                condition=models.Q(referred_user__isnull=True)
                | ~models.Q(referrer=models.F("referred_user")),
                name="referral_no_self_referral",
            ),
            models.CheckConstraint(
                condition=~models.Q(status="completed")
                | models.Q(completed_at__isnull=False),
                name="referral_completed_has_timestamp",
            ),
            models.UniqueConstraint(
                fields=["reward_reference"],
                condition=~models.Q(reward_reference=""),
                name="referral_unique_reward_reference",
            ),
        ]

    def __str__(self):
        name = self.referrer.get_full_name() or self.referrer.get_username()
        return f"{self.code}: {name} → {self.get_status_display()}"

    # ---- read helpers -------------------------------------------------------

    @property
    def is_expired(self):
        if self.status == self.Status.EXPIRED:
            return True
        return (
            self.status == self.Status.PENDING
            and self.expires_at is not None
            and self.expires_at <= timezone.now()
        )

    @property
    def is_usable(self):
        return self.status == self.Status.PENDING and not self.is_expired

    @property
    def reward_pending(self):
        """Earned by the referrer but not yet paid out."""
        return (
            self.status == self.Status.COMPLETED
            and self.reward_amount > 0
            and self.rewarded_at is None
        )

    @classmethod
    def normalize_code(cls, code):
        return (code or "").strip().upper()

    @classmethod
    def find_usable(cls, code):
        """Look up an invite by the code a new user typed in. Returns None if unusable."""
        return cls.objects.usable().filter(code=cls.normalize_code(code)).first()

    def clean(self):
        if self.referred_user_id and self.referred_user_id == self.referrer_id:
            raise ValidationError("A user cannot refer themselves.")

    def save(self, *args, **kwargs):
        self.code = self.normalize_code(self.code)
        super().save(*args, **kwargs)

    # ---- workflow (each locks the row, so double-clicks and retries are safe) --

    def _lock(self):
        return type(self).objects.select_for_update().get(pk=self.pk)

    def register_signup(self, referred_user):
        """The invited person created an account with this code: PENDING → ACTIVE."""
        with transaction.atomic():
            r = self._lock()

            if r.status != self.Status.PENDING:
                raise ValidationError(
                    "This referral code has already been used or is no longer valid.",
                    code="not_pending",
                )
            if r.expires_at is not None and r.expires_at <= timezone.now():
                raise ValidationError("This referral code has expired.", code="expired")
            if referred_user.pk == r.referrer_id:
                raise ValidationError(
                    "You cannot use your own referral code.", code="self_referral"
                )
            joined = getattr(referred_user, "date_joined", None)
            if joined is not None and joined < r.created_at:
                raise ValidationError(
                    "Referral codes can only be used by new accounts.",
                    code="existing_account",
                )

            r.referred_user = referred_user
            r.status = self.Status.ACTIVE
            r.signed_up_at = timezone.now()
            try:
                with transaction.atomic():
                    r.save(update_fields=["referred_user", "status", "signed_up_at", "updated_at"])
            except IntegrityError:
                raise ValidationError(
                    "This account has already been referred.", code="already_referred"
                )
        self.refresh_from_db()
        return self

    def complete(self):
        """The new user did the qualifying action (e.g. first completed booking):
        ACTIVE → COMPLETED. Calling it again is harmless."""
        with transaction.atomic():
            r = self._lock()
            if r.status == self.Status.COMPLETED:
                self.refresh_from_db()
                return self
            if r.status != self.Status.ACTIVE:
                raise ValidationError(
                    f"A {r.get_status_display().lower()} referral cannot be completed.",
                    code="invalid_transition",
                )
            r.status = self.Status.COMPLETED
            r.completed_at = timezone.now()
            r.save(update_fields=["status", "completed_at", "updated_at"])
        self.refresh_from_db()
        return self

    def mark_reward_paid(self, reference):
        """Record that the referrer's reward was paid, and by which entry.

        Call it in the same transaction that credits the money. Repeating the
        call with the same reference is harmless.
        """
        reference = (reference or "").strip()
        if not reference:
            raise ValidationError("A payment reference is required.", code="reference_required")

        with transaction.atomic():
            r = self._lock()
            if r.status != self.Status.COMPLETED:
                raise ValidationError(
                    "Only completed referrals can be rewarded.", code="invalid_transition"
                )
            if r.rewarded_at is not None:
                if r.reward_reference == reference:
                    self.refresh_from_db()
                    return self
                raise ValidationError(
                    "This referral has already been rewarded.", code="already_rewarded"
                )
            r.reward_reference = reference
            r.rewarded_at = timezone.now()
            try:
                with transaction.atomic():
                    r.save(update_fields=["reward_reference", "rewarded_at", "updated_at"])
            except IntegrityError:
                raise ValidationError(
                    "This payment reference was already used for another referral.",
                    code="duplicate_reward_reference",
                )
        self.refresh_from_db()
        return self

    def cancel(self, reason=""):
        """Withdraw an invite or revoke a suspicious referral before it completes."""
        with transaction.atomic():
            r = self._lock()
            if r.status not in self.OPEN_STATUSES:
                raise ValidationError(
                    f"A {r.get_status_display().lower()} referral cannot be cancelled.",
                    code="invalid_transition",
                )
            r.status = self.Status.CANCELLED
            r.cancelled_at = timezone.now()
            r.cancel_reason = (reason or "")[:255]
            r.save(update_fields=["status", "cancelled_at", "cancel_reason", "updated_at"])
        self.refresh_from_db()
        return self



class ReferralQuerySet(models.QuerySet):
    def open(self):
        return self.filter(status__in=Referral.OPEN_STATUSES)

    def usable(self):
        """Invites that can still be used to sign up."""
        return self.filter(status=Referral.Status.PENDING).filter(
            Q(expires_at__isnull=True) | Q(expires_at__gt=timezone.now())
        )

    def expire_stale(self):
        """Mark unused invites past their expiry as expired. Returns the count."""
        now = timezone.now()
        return self.filter(
            status=Referral.Status.PENDING,
            expires_at__isnull=False,
            expires_at__lte=now,
        ).update(status=Referral.Status.EXPIRED, updated_at=now)

    def summary_for(self, referrer):
        """Dashboard numbers for one referrer."""
        return self.filter(referrer=referrer).aggregate(
            invites=Count("id"),
            signups=Count("id", filter=Q(signed_up_at__isnull=False)),
            completed=Count("id", filter=Q(status=Referral.Status.COMPLETED)),
            rewards_earned=Sum(
                "reward_amount", filter=Q(status=Referral.Status.COMPLETED)
            ),
        )


class ReferralCode(models.Model):
    """A referral code owned by a user. Redeeming it rewards both sides."""

    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        PAUSED = "paused", "Paused"
        EXPIRED = "expired", "Expired"

    class Currency(models.TextChoices):
        GHS = "GHS", "Ghana Cedi"
        GBP = "GBP", "British Pound"

    referrer = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="referral_codes",
        help_text="The person who is inviting others.",
    )
    code = models.CharField(
        max_length=20,
        unique=True,
        blank=True,
        help_text="Auto-generated if left blank.",
    )
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.ACTIVE,
        db_index=True,
    )

    # Rewards
    referrer_reward_value = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=Decimal("5.00"),
        validators=[MinValueValidator(ZERO)],
        help_text="Credit given to the referrer per successful referral.",
    )
    referee_reward_value = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=Decimal("5.00"),
        validators=[MinValueValidator(ZERO)],
        help_text="Credit given to the new user who signs up.",
    )
    currency = models.CharField(
        max_length=3,
        choices=Currency.choices,
        default=Currency.GHS,
    )

    # Limits & stats
    max_uses = models.PositiveIntegerField(default=0, help_text="0 = unlimited.")
    times_used = models.PositiveIntegerField(default=0, editable=False)
    total_rewards_issued = models.DecimalField(
        max_digits=12, decimal_places=2, default=ZERO, editable=False
    )
    valid_until = models.DateField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "referral_codes"
        ordering = ["-created_at"]
        verbose_name = "Referral Code"
        verbose_name_plural = "Referral Codes"
        indexes = [models.Index(fields=["referrer", "status"])]

    def __str__(self):
        limit = self.max_uses or "∞"
        return f"{self.code} — {self.referrer} ({self.times_used}/{limit})"

    # ------------------------------------------------------------------
    # Code generation
    # ------------------------------------------------------------------
    def save(self, *args, **kwargs):
        if self.code:
            self.code = self.code.upper().strip()
            return super().save(*args, **kwargs)

        # Generate a code; retry if another request grabs the same one first.
        for attempt in range(5):
            self.code = self._generate_code(suffix_length=4 + attempt // 2)
            try:
                with transaction.atomic():
                    return super().save(*args, **kwargs)
            except IntegrityError:
                if attempt == 4:
                    raise
                self.code = ""

    def _generate_code(self, suffix_length=4):
        """Readable, hard-to-guess code, e.g. JOHN-7K2M."""
        username = getattr(self.referrer, "get_username", lambda: "")()
        prefix = re.sub(r"[^A-Z0-9]", "", username.upper())[:6] or "REF"
        while True:
            suffix = "".join(secrets.choice(CODE_ALPHABET) for _ in range(suffix_length))
            code = f"{prefix}-{suffix}"
            if not type(self).objects.filter(code=code).exists():
                return code


class BusinessAccount(models.Model):
    """A business that wants regular bookings and discounted rates."""

    class Status(models.TextChoices):
        PENDING = "pending", "Awaiting Review"
        APPROVED = "approved", "Active"
        SUSPENDED = "suspended", "Suspended"
        REJECTED = "rejected", "Application Rejected"

    # Kept as aliases so existing references keep working.
    BookingFrequency = BookingFrequency

    # Business info
    business_name = models.CharField(max_length=255)
    contact_person = models.CharField(max_length=255)
    email = models.EmailField()
    phone_number = models.CharField(max_length=30, validators=[business_phone_validator])
    address = models.TextField()
    industry = models.CharField(max_length=100, blank=True, default="")
    registration_number = models.CharField(max_length=100, blank=True, default="")

    # Preferred booking details
    service_type = models.CharField(max_length=50, choices=ServiceType.choices)
    preferred_frequency = models.CharField(
        max_length=20, choices=BookingFrequency.choices, default=BookingFrequency.WEEKLY
    )
    preferred_day = models.PositiveSmallIntegerField(choices=Weekday.choices)
    preferred_time = models.TimeField(help_text="Preferred pickup/delivery time")
    estimated_volume = models.PositiveIntegerField(
        default=1,
        validators=[MinValueValidator(1)],
        help_text="Number of bookings per period",
    )

    # Discount
    discount_requested = models.DecimalField(
        max_digits=5, decimal_places=2, default=Decimal("0.00"),
        validators=PERCENT_VALIDATORS, help_text="Requested discount percentage",
    )
    discount_approved = models.DecimalField(
        max_digits=5, decimal_places=2, default=Decimal("0.00"),
        validators=PERCENT_VALIDATORS, help_text="Approved discount percentage",
    )

    # Review and status
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name="businesses_reviewed",
    )
    review_notes = models.TextField(blank=True, default="")
    reviewed_at = models.DateTimeField(null=True, blank=True)
    suspension_reason = models.CharField(max_length=255, blank=True, default="")
    suspended_at = models.DateTimeField(null=True, blank=True)

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name="business_accounts",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "business_accounts"
        ordering = ["-created_at"]
        verbose_name = "Business Account"
        verbose_name_plural = "Business Accounts"
        indexes = [
            models.Index(fields=["status", "-created_at"], name="business_status_created_idx"),
        ]
        constraints = [
            models.UniqueConstraint(Lower("email"), name="business_email_ci_unique"),
            models.UniqueConstraint(
                fields=["registration_number"],
                condition=~models.Q(registration_number=""),
                name="business_registration_unique",
            ),
            models.CheckConstraint(
                condition=models.Q(discount_requested__gte=0, discount_requested__lte=100),
                name="business_discount_requested_range",
            ),
            models.CheckConstraint(
                condition=models.Q(discount_approved__gte=0, discount_approved__lte=100),
                name="business_discount_approved_range",
            ),
        ]

    def __str__(self):
        return f"{self.business_name} — {self.get_status_display()}"

    @property
    def is_active(self):
        return self.status == self.Status.APPROVED

    def save(self, *args, **kwargs):
        self.email = (self.email or "").strip().lower()
        self.phone_number = re.sub(r"[\s\-()]", "", self.phone_number or "")
        super().save(*args, **kwargs)

    # ---- review workflow (each step locks the row and checks the current state) --

    def _lock(self):
        return type(self).objects.select_for_update().get(pk=self.pk)

    def _require_status(self, allowed, action):
        if self.status not in allowed:
            raise ValidationError(
                f"A {self.get_status_display().lower()} business cannot be {action}.",
                code="invalid_transition",
            )

    def _require_independent_reviewer(self, reviewer):
        if reviewer is None:
            raise ValidationError("A reviewer is required.", code="reviewer_required")
        if self.created_by_id is not None and self.created_by_id == reviewer.pk:
            raise ValidationError(
                "An application cannot be reviewed by the person who submitted it.",
                code="self_review",
            )

    def approve(self, discount_approved=None, reviewed_by=None, notes=""):
        """Approve the business. If no discount is given, none is granted
        (the requested discount is never applied automatically)."""
        discount = None if discount_approved is None else to_percent(discount_approved)
        with transaction.atomic():
            b = self._lock()
            b._require_status((self.Status.PENDING,), "approved")
            b._require_independent_reviewer(reviewed_by)
            if discount is not None:
                b.discount_approved = discount
            b.status = self.Status.APPROVED
            b.reviewed_by = reviewed_by
            b.reviewed_at = timezone.now()
            if notes:
                b.review_notes = notes
            b.save(update_fields=[
                "discount_approved", "status", "reviewed_by", "reviewed_at",
                "review_notes", "updated_at",
            ])
        self.refresh_from_db()
        return self

    def reject(self, reason="", reviewed_by=None):
        if not (reason or "").strip():
            raise ValidationError("A reason is required.", code="reason_required")
        with transaction.atomic():
            b = self._lock()
            b._require_status((self.Status.PENDING,), "rejected")
            b._require_independent_reviewer(reviewed_by)
            b.status = self.Status.REJECTED
            b.review_notes = reason
            b.reviewed_by = reviewed_by
            b.reviewed_at = timezone.now()
            b.save(update_fields=[
                "status", "review_notes", "reviewed_by", "reviewed_at", "updated_at",
            ])
        self.refresh_from_db()
        return self

    def suspend(self, reason=""):
        """Suspend an active account and pause all its active recurring bookings."""
        if not (reason or "").strip():
            raise ValidationError("A reason is required.", code="reason_required")
        with transaction.atomic():
            b = self._lock()
            b._require_status((self.Status.APPROVED,), "suspended")
            now = timezone.now()
            b.status = self.Status.SUSPENDED
            b.suspension_reason = reason[:255]
            b.suspended_at = now
            b.save(update_fields=["status", "suspension_reason", "suspended_at", "updated_at"])
            RecurringBooking.objects.filter(
                business=b, status=RecurringBooking.Status.ACTIVE
            ).update(status=RecurringBooking.Status.PAUSED, updated_at=now)
        self.refresh_from_db()
        return self

    def reinstate(self):
        """Lift a suspension. Paused recurring bookings stay paused until resumed."""
        with transaction.atomic():
            b = self._lock()
            b._require_status((self.Status.SUSPENDED,), "reinstated")
            b.status = self.Status.APPROVED
            b.suspension_reason = ""
            b.suspended_at = None
            b.save(update_fields=["status", "suspension_reason", "suspended_at", "updated_at"])
        self.refresh_from_db()
        return self


class RecurringBookingQuerySet(models.QuerySet):
    def due(self, on=None):
        """Active schedules due on or before `on` (default today) for active businesses."""
        on = on or timezone.localdate()
        return self.filter(
            status=RecurringBooking.Status.ACTIVE,
            next_booking_date__lte=on,
            business__status=BusinessAccount.Status.APPROVED,
        )


class WasteSubscriptionQuerySet(models.QuerySet):
    def due(self, within_days=0):
        """Active subscriptions with a collection due within `within_days` (default today).

        Subscriptions of suspended businesses are excluded.
        """
        cutoff = timezone.localdate() + timedelta(days=within_days)
        return self.filter(
            status=WasteSubscription.Status.ACTIVE,
            next_collection_date__lte=cutoff,
        ).filter(
            models.Q(business__isnull=True)
            | models.Q(business__status=BusinessAccount.Status.APPROVED)
        )

    def expire_ended(self):
        """Expire paused subscriptions whose end date has passed. Returns the count.

        Active ones expire by themselves when their last collection is scheduled.
        """
        now = timezone.now()
        return self.filter(
            status=WasteSubscription.Status.PAUSED,
            end_date__lt=timezone.localdate(),
        ).update(status=WasteSubscription.Status.EXPIRED, updated_at=now)


class WasteSubscription(models.Model):
    """Regular waste collection subscription for individual customers or businesses.

    Each due date becomes a `WasteCollection` record via `schedule_collection()`,
    which gives you a history, rider assignment and proof of pickup.
    """

    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        PAUSED = "paused", "Paused"
        CANCELLED = "cancelled", "Cancelled"
        EXPIRED = "expired", "Expired"


    class WasteType(models.TextChoices):
        HAZARDOUS = "hazardous", "Hazadous"
        CONSTRUCTION = "construction",   "Construction"

    # Hazardous and construction pickups need a photo before they can be completed.
    PROOF_REQUIRED_TYPES = ("hazardous", "construction")

    # Collections per month, used for cost estimates.
    COLLECTIONS_PER_MONTH = {
        BookingFrequency.WEEKLY: Decimal("52") / Decimal("12"),
        BookingFrequency.BIWEEKLY: Decimal("26") / Decimal("12"),
        BookingFrequency.MONTHLY: Decimal("1"),
    }

    customer = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="waste_subscriptions",
    )
    business = models.ForeignKey(
        "BusinessAccount",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="waste_subscriptions",
        help_text="Leave blank for individual customer subscriptions.",
    )

    # What and where
    reference = models.CharField(
        max_length=30, unique=True, editable=False, default=generate_subscription_reference
    )
    waste_type = models.CharField(max_length=30, choices=WasteType.choices)
    quantity = models.PositiveIntegerField(
        default=1, validators=[MinValueValidator(1)], help_text="Number of bins/bags."
    )
    pickup_address = models.TextField()
    latitude = models.DecimalField(
        max_digits=9, decimal_places=6, null=True, blank=True,
        validators=[MinValueValidator(-90), MaxValueValidator(90)],
    )
    longitude = models.DecimalField(
        max_digits=9, decimal_places=6, null=True, blank=True,
        validators=[MinValueValidator(-180), MaxValueValidator(180)],
    )
    contact_phone = models.CharField(
        max_length=30, blank=True, default="", validators=[business_phone_validator],
        help_text="On-site contact the rider can call.",
    )
    special_instructions = models.TextField(blank=True, default="")

    # Schedule
    frequency = models.CharField(max_length=20, choices=BookingFrequency.choices)
    preferred_day = models.PositiveSmallIntegerField(choices=Weekday.choices)
    preferred_time = models.TimeField(help_text="Preferred pickup time.")
    start_date = models.DateField(default=timezone.localdate)
    end_date = models.DateField(null=True, blank=True)
    next_collection_date = models.DateField()

    # Pricing
    price_per_collection = models.DecimalField(
        max_digits=10, decimal_places=2, validators=[MinValueValidator(Decimal("0.01"))]
    )
    discount_percent = models.DecimalField(
        max_digits=5, decimal_places=2, null=True, blank=True,
        validators=PERCENT_VALIDATORS,
        help_text="Leave empty to copy the business's approved discount when created. "
                  "It is then fixed for the life of the subscription.",
    )
    total_price = models.DecimalField(
        max_digits=10, decimal_places=2, editable=False,
        help_text="Price of ONE collection after discount.",
    )

    # Status
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.ACTIVE)
    paused_at = models.DateTimeField(null=True, blank=True)
    cancelled_at = models.DateTimeField(null=True, blank=True)
    cancel_reason = models.CharField(max_length=255, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = WasteSubscriptionQuerySet.as_manager()

    class Meta:
        db_table = "waste_subscriptions"
        ordering = ["next_collection_date", "-created_at"]
        verbose_name = "Waste Subscription"
        verbose_name_plural = "Waste Subscriptions"
        indexes = [
            models.Index(fields=["status", "next_collection_date"], name="wastesub_due_idx"),
            models.Index(fields=["customer", "status"], name="wastesub_customer_status_idx"),
            models.Index(fields=["business", "status"], name="wastesub_business_status_idx"),
        ]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(end_date__isnull=True)
                | models.Q(end_date__gte=models.F("start_date")),
                name="waste_sub_end_after_start",
            ),
            models.CheckConstraint(
                condition=models.Q(next_collection_date__gte=models.F("start_date")),
                name="waste_sub_next_after_start",
            ),
            models.CheckConstraint(
                condition=models.Q(price_per_collection__gt=0), name="waste_sub_price_positive"
            ),
            models.CheckConstraint(
                condition=models.Q(discount_percent__isnull=True)
                | models.Q(discount_percent__gte=0, discount_percent__lte=100),
                name="waste_sub_discount_range",
            ),
            models.CheckConstraint(
                condition=models.Q(quantity__gte=1), name="waste_sub_quantity_min"
            ),
        ]

    def __str__(self):
        return f"{self.reference} — {self.get_waste_type_display()}"

    @property
    def is_active(self):
        return self.status == self.Status.ACTIVE

    @property
    def estimated_monthly_cost(self):
        rate = self.COLLECTIONS_PER_MONTH[BookingFrequency(self.frequency)]
        return (self.total_price * rate).quantize(Decimal("0.01"))

    # ---- validation and saving ---------------------------------------------

    def clean(self):
        errors = {}
        if self.next_collection_date and self.preferred_day is not None:
            if self.next_collection_date.weekday() != self.preferred_day:
                errors["next_collection_date"] = (
                    f"Next collection must be on a {self.get_preferred_day_display()}."
                )
        if self.start_date and self.next_collection_date and self.next_collection_date < self.start_date:
            errors["next_collection_date"] = "First collection cannot be before the start date."
        if self.start_date and self.end_date and self.end_date < self.start_date:
            errors["end_date"] = "End date cannot be before the start date."
        if self.waste_type == WasteType.HAZARDOUS and not (self.special_instructions or "").strip():
            errors["special_instructions"] = "Describe the hazardous waste and how it is stored."
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        """Keep `total_price` in sync. The discount is copied once, at creation."""
        if self._state.adding:
            if self.business_id and not self.business.is_active:
                raise ValidationError(
                    "Subscriptions can only be created for active businesses.",
                    code="business_inactive",
                )
            if self.discount_percent is None:
                self.discount_percent = (
                    self.business.discount_approved if self.business_id else Decimal("0.00")
                )
        self.total_price = apply_discount(
            self.price_per_collection, self.discount_percent or Decimal("0")
        )
        update_fields = kwargs.get("update_fields")
        if update_fields is not None:
            kwargs["update_fields"] = set(update_fields) | {"total_price"}
        super().save(*args, **kwargs)

    # ---- scheduling ---------------------------------------------------------

    def _lock(self):
        return type(self).objects.select_for_update().get(pk=self.pk)

    def _following_date(self, d):
        """Next date after `d`. Always lands on the preferred weekday.

        Monthly means the same week of the month (e.g. the 2nd Tuesday); a 5th
        occurrence becomes the last one when a month is shorter.
        """
        if self.frequency == BookingFrequency.WEEKLY:
            return d + timedelta(days=7)
        if self.frequency == BookingFrequency.BIWEEKLY:
            return d + timedelta(days=14)
        year, month = d.year + d.month // 12, d.month % 12 + 1
        ordinal = min((d.day - 1) // 7 + 1, 5)
        return nth_weekday_of_month(year, month, self.preferred_day, ordinal)

    def _advance_locked(self):
        """Move to the next date. Caller holds the row lock."""
        self.next_collection_date = self._following_date(self.next_collection_date)
        if self.end_date and self.next_collection_date > self.end_date:
            self.status = self.Status.EXPIRED
        self.save(update_fields=["next_collection_date", "status", "updated_at"])

    def schedule_collection(self, for_date):
        """Create the collection for `for_date` and move to the next date, atomically.

        Pass the date you saw as due. If another worker already handled it (or the
        subscription is no longer active) this does nothing and returns None, so
        retries and parallel workers can't double-book or skip a date.
        """
        with transaction.atomic():
            s = self._lock()
            if s.status != self.Status.ACTIVE or s.next_collection_date != for_date:
                self.refresh_from_db()
                return None
            if s.business_id and not BusinessAccount.objects.filter(
                pk=s.business_id, status=BusinessAccount.Status.APPROVED
            ).exists():
                raise ValidationError("The business account is not active.", code="business_inactive")
            collection = WasteCollection.objects.create(
                subscription=s, scheduled_date=for_date,
                scheduled_time=s.preferred_time, price=s.total_price,
            )
            s._advance_locked()
        self.refresh_from_db()
        return collection

    def skip_next(self, reason=""):
        """Skip the next collection (holiday, away). Leaves a SKIPPED record, then advances."""
        with transaction.atomic():
            s = self._lock()
            if s.status != self.Status.ACTIVE:
                raise ValidationError(
                    "Only active subscriptions can skip a collection.", code="invalid_transition"
                )
            WasteCollection.objects.create(
                subscription=s, scheduled_date=s.next_collection_date,
                scheduled_time=s.preferred_time, price=s.total_price,
                status=WasteCollection.Status.SKIPPED, notes=(reason or "")[:255],
            )
            s._advance_locked()
        self.refresh_from_db()
        return self

    # ---- status changes -----------------------------------------------------

    def pause(self):
        """Pause and cancel any collections already scheduled from today onward."""
        with transaction.atomic():
            s = self._lock()
            if s.status != self.Status.ACTIVE:
                raise ValidationError(
                    "Only active subscriptions can be paused.", code="invalid_transition"
                )
            now = timezone.now()
            s.status = self.Status.PAUSED
            s.paused_at = now
            s.save(update_fields=["status", "paused_at", "updated_at"])
            s.collections.filter(
                status=WasteCollection.Status.SCHEDULED,
                scheduled_date__gte=timezone.localdate(),
            ).update(status=WasteCollection.Status.CANCELLED, updated_at=now)
        self.refresh_from_db()
        return self

    def resume(self):
        """Resume, skipping dates missed while paused (no collections are created for them)."""
        with transaction.atomic():
            s = self._lock()
            if s.status != self.Status.PAUSED:
                raise ValidationError(
                    "Only paused subscriptions can be resumed.", code="invalid_transition"
                )
            if s.business_id and not BusinessAccount.objects.filter(
                pk=s.business_id, status=BusinessAccount.Status.APPROVED
            ).exists():
                raise ValidationError("The business account is not active.", code="business_inactive")
            today = timezone.localdate()
            next_date = s.next_collection_date
            while next_date < today:
                next_date = s._following_date(next_date)
            s.next_collection_date = next_date
            ended = s.end_date is not None and next_date > s.end_date
            s.status = self.Status.EXPIRED if ended else self.Status.ACTIVE
            s.paused_at = None
            s.save(update_fields=["next_collection_date", "status", "paused_at", "updated_at"])
        self.refresh_from_db()
        return self

    def cancel(self, reason=""):
        """Cancel the subscription and any upcoming scheduled collections."""
        with transaction.atomic():
            s = self._lock()
            if s.status not in (self.Status.ACTIVE, self.Status.PAUSED):
                raise ValidationError(
                    f"A {s.get_status_display().lower()} subscription cannot be cancelled.",
                    code="invalid_transition",
                )
            now = timezone.now()
            s.status = self.Status.CANCELLED
            s.cancelled_at = now
            s.cancel_reason = (reason or "")[:255]
            s.save(update_fields=["status", "cancelled_at", "cancel_reason", "updated_at"])
            s.collections.filter(
                status=WasteCollection.Status.SCHEDULED,
                scheduled_date__gte=timezone.localdate(),
            ).update(status=WasteCollection.Status.CANCELLED, updated_at=now)
        self.refresh_from_db()
        return self


class WasteCollectionQuerySet(models.QuerySet):
    def upcoming(self):
        return self.filter(
            status=WasteCollection.Status.SCHEDULED, scheduled_date__gte=timezone.localdate()
        )

    def needs_rider(self):
        return self.filter(
            status=WasteCollection.Status.SCHEDULED, assigned_rider__isnull=True
        )


class WasteCollection(models.Model):
    """One pickup of a subscription: its schedule, rider, outcome and proof."""

    class Status(models.TextChoices):
        SCHEDULED = "scheduled", "Scheduled"
        COMPLETED = "completed", "Completed"
        MISSED = "missed", "Missed"
        SKIPPED = "skipped", "Skipped by Customer"
        CANCELLED = "cancelled", "Cancelled"

    subscription = models.ForeignKey(
        WasteSubscription, on_delete=models.PROTECT, related_name="collections"
    )
    reference = models.CharField(
        max_length=30, unique=True, editable=False, default=generate_collection_reference
    )
    scheduled_date = models.DateField()
    scheduled_time = models.TimeField()
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.SCHEDULED)
    assigned_rider = models.ForeignKey(
        "RiderProfile", on_delete=models.SET_NULL,
        null=True, blank=True, related_name="waste_collections",
    )
    price = models.DecimalField(
        max_digits=10, decimal_places=2, validators=[MinValueValidator(Decimal("0.00"))],
        help_text="Price of this pickup, fixed when it was scheduled. Bill completed pickups only.",
    )
    bins_collected = models.PositiveIntegerField(null=True, blank=True)
    weight_kg = models.DecimalField(
        max_digits=7, decimal_places=2, null=True, blank=True,
        validators=[MinValueValidator(Decimal("0.00"))],
    )
    proof_photo = models.ImageField(upload_to=waste_proof_upload_to, blank=True)
    notes = models.CharField(max_length=255, blank=True, default="")
    completed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = WasteCollectionQuerySet.as_manager()

    class Meta:
        db_table = "waste_collections"
        ordering = ["-scheduled_date", "-id"]
        verbose_name = "Waste Collection"
        verbose_name_plural = "Waste Collections"
        indexes = [
            models.Index(fields=["status", "scheduled_date"], name="wastecol_status_date_idx"),
            models.Index(fields=["assigned_rider", "scheduled_date"], name="wastecol_rider_date_idx"),
            models.Index(fields=["subscription", "-scheduled_date"], name="wastecol_sub_date_idx"),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["subscription", "scheduled_date"], name="wastecol_one_per_date"
            ),
            models.CheckConstraint(
                condition=~models.Q(status="completed") | models.Q(completed_at__isnull=False),
                name="wastecol_completed_has_timestamp",
            ),
            models.CheckConstraint(
                condition=models.Q(price__gte=0), name="wastecol_price_non_negative"
            ),
            models.CheckConstraint(
                condition=models.Q(weight_kg__isnull=True) | models.Q(weight_kg__gte=0),
                name="wastecol_weight_non_negative",
            ),
        ]

    def __str__(self):
        return f"{self.reference}: {self.scheduled_date} ({self.get_status_display()})"

    def _lock(self):
        return type(self).objects.select_for_update().get(pk=self.pk)

    def _require_scheduled(self, collection, action):
        if collection.status != self.Status.SCHEDULED:
            raise ValidationError(
                f"A {collection.get_status_display().lower()} collection cannot be {action}.",
                code="invalid_transition",
            )

    def assign(self, rider):
        with transaction.atomic():
            c = self._lock()
            self._require_scheduled(c, "assigned")
            c.assigned_rider = rider
            c.save(update_fields=["assigned_rider", "updated_at"])
        self.refresh_from_db()
        return self

    def complete(self, bins_collected=None, weight_kg=None, proof_photo=None, notes=""):
        """Confirm the pickup. Needs an assigned rider; hazardous and construction
        waste also need a photo."""
        if weight_kg is not None:
            try:
                weight_kg = Decimal(str(weight_kg)).quantize(Decimal("0.01"))
            except InvalidOperation:
                raise ValidationError("Invalid weight.", code="invalid_weight")
            if weight_kg < 0:
                raise ValidationError("Weight cannot be negative.", code="invalid_weight")

        with transaction.atomic():
            c = self._lock()
            self._require_scheduled(c, "completed")
            if c.assigned_rider_id is None:
                raise ValidationError(
                    "A rider must be assigned before a collection can be completed.",
                    code="rider_required",
                )
            needs_proof = c.subscription.waste_type in WasteSubscription.PROOF_REQUIRED_TYPES
            if needs_proof and not (proof_photo or c.proof_photo):
                raise ValidationError(
                    "A proof photo is required for this type of waste.", code="proof_required"
                )
            c.status = self.Status.COMPLETED
            c.completed_at = timezone.now()
            if bins_collected is not None:
                c.bins_collected = bins_collected
            if weight_kg is not None:
                c.weight_kg = weight_kg
            if proof_photo:
                c.proof_photo = proof_photo
            if notes:
                c.notes = notes[:255]
            c.save(update_fields=[
                "status", "completed_at", "bins_collected", "weight_kg",
                "proof_photo", "notes", "updated_at",
            ])
        self.refresh_from_db()
        return self

    def mark_missed(self, reason):
        """The pickup did not happen (no access, bins not out, rider no-show)."""
        if not (reason or "").strip():
            raise ValidationError("A reason is required.", code="reason_required")
        with transaction.atomic():
            c = self._lock()
            self._require_scheduled(c, "marked as missed")
            c.status = self.Status.MISSED
            c.notes = reason[:255]
            c.save(update_fields=["status", "notes", "updated_at"])
        self.refresh_from_db()
        return self

    def cancel(self, reason=""):
        with transaction.atomic():
            c = self._lock()
            self._require_scheduled(c, "cancelled")
            c.status = self.Status.CANCELLED
            c.notes = (reason or "")[:255]
            c.save(update_fields=["status", "notes", "updated_at"])
        self.refresh_from_db()
        return self    



promo_code_validator = RegexValidator(
    regex=r"^[A-Z0-9][A-Z0-9-]{2,29}$",
    message="Use 3 to 30 letters, numbers or hyphens.",
    flags=re.IGNORECASE,
)


class PromoCode(models.Model):
    """Discount codes customers can apply to bookings.

    Every code gives MORE than 10% off (see MIN_DISCOUNT_PERCENT). Can be limited to
    chosen weekdays (e.g. a Friday deal), hours of the day, services, first-time
    customers or named users. Redeem with `redeem()`, never by editing counters.
    """

    class DiscountType(models.TextChoices):
        PERCENTAGE = "percentage", "Percentage Discount"
        FIXED = "fixed", "Fixed Amount Off"

    class Currency(models.TextChoices):
        GHS = "GHS", "Ghana Cedi"
        GBP = "GBP", "British Pound"

    code = models.CharField(
        max_length=50,
        unique=True,
        validators=[promo_code_validator],
        help_text="e.g., WELCOME50, FRIDAY15",
    )
    name = models.CharField(max_length=100, blank=True, default="")
    description = models.TextField(blank=True, default="")

    # Discount value
    discount_type = models.CharField(
        max_length=20, choices=DiscountType.choices, default=DiscountType.PERCENTAGE
    )
    discount_value = models.DecimalField(
        max_digits=8,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0.01"))],
        help_text="Percentage codes: more than 10 and up to 100. "
                  "Fixed codes: must be more than 10% of the minimum order value.",
    )
    currency = models.CharField(
        max_length=3, choices=Currency.choices, default=Currency.GHS,
        help_text="Currency of fixed-amount codes. Percentage codes work in any currency.",
    )
    minimum_order_value = models.DecimalField(
        max_digits=10, decimal_places=2, default=Decimal("0.00"),
        validators=[MinValueValidator(Decimal("0.00"))],
        help_text="Minimum booking amount to use this code. Required for fixed codes.",
    )
    maximum_discount = models.DecimalField(
        max_digits=8, decimal_places=2, null=True, blank=True,
        validators=[MinValueValidator(Decimal("0.01"))],
        help_text="Cap on the discount amount (percentage codes only).",
    )

    # Usage limits
    max_uses = models.PositiveIntegerField(
        null=True, blank=True, help_text="Total uses allowed. Empty = unlimited."
    )
    times_used = models.PositiveIntegerField(default=0, editable=False)
    usage_limit_per_user = models.PositiveSmallIntegerField(
        null=True, blank=True, default=1,
        help_text="Uses allowed per customer. Empty = unlimited.",
    )
    total_discount_budget = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True,
        help_text="Stop the code once this much discount has been given. Empty = no budget.",
    )
    total_discount_given = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal("0.00"), editable=False
    )

    # Validity
    valid_from = models.DateTimeField(default=timezone.now)
    valid_until = models.DateTimeField(null=True, blank=True)
    valid_weekdays = models.JSONField(
        default=list, blank=True,
        help_text="Days the code works, as numbers 0=Monday to 6=Sunday. "
                  "[4] = Fridays only. Empty = every day.",
    )
    valid_from_time = models.TimeField(
        null=True, blank=True, help_text="Daily start time (e.g. a happy hour). Set with the end time."
    )
    valid_until_time = models.TimeField(
        null=True, blank=True, help_text="Daily end time. Set with the start time."
    )
    is_active = models.BooleanField(default=True)
    is_public = models.BooleanField(
        default=False, help_text="Show this code in the app. Private codes are shared directly."
    )

    # Who and what it applies to
    for_service_types = models.JSONField(
        default=list, blank=True,
        help_text="Empty = all services, or list some: ['okada', 'parcel'].",
    )
    first_time_customers_only = models.BooleanField(
        default=False, help_text="Only for customers who have never booked before."
    )
    allowed_users = models.ManyToManyField(
        settings.AUTH_USER_MODEL, blank=True, related_name="private_promo_codes",
        help_text="If any are chosen, only these customers can use the code.",
    )

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name="created_promo_codes",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)


    class Meta:
        db_table = "promo_codes"
        ordering = ["-created_at"]
        verbose_name = "Promo Code"
        verbose_name_plural = "Promo Codes"
        indexes = [
            models.Index(fields=["is_active", "is_public", "valid_from"], name="promo_listing_idx"),
        ]
        constraints = [
            models.CheckConstraint(
                condition=(
                    Q(discount_type="percentage", discount_value__gt=10, discount_value__lte=100)
                    | Q(
                        discount_type="fixed",
                        minimum_order_value__gt=0,
                        discount_value__gt=F("minimum_order_value") * Decimal("0.10"),
                        discount_value__lte=F("minimum_order_value"),
                    )
                ),
                name="promo_discount_over_ten_percent",
            ),
            models.CheckConstraint(
                condition=Q(maximum_discount__isnull=True)
                | Q(discount_type="percentage", maximum_discount__gt=0),
                name="promo_max_discount_percentage_only",
            ),
            models.CheckConstraint(
                condition=Q(valid_until__isnull=True) | Q(valid_until__gt=F("valid_from")),
                name="promo_ends_after_start",
            ),
            models.CheckConstraint(
                condition=(
                    Q(valid_from_time__isnull=True, valid_until_time__isnull=True)
                    | Q(valid_from_time__isnull=False, valid_until_time__isnull=False)
                ),
                name="promo_time_window_complete",
            ),
            models.CheckConstraint(
                condition=Q(max_uses__isnull=True) | Q(max_uses__gte=1),
                name="promo_max_uses_positive",
            ),
            models.CheckConstraint(
                condition=Q(max_uses__isnull=True) | Q(times_used__lte=F("max_uses")),
                name="promo_uses_within_limit",
            ),
            models.CheckConstraint(
                condition=Q(total_discount_budget__isnull=True) | Q(total_discount_budget__gt=0),
                name="promo_budget_positive",
            ),
        ]

    def __str__(self):
        return f"{self.code} — {self.get_discount_display()}"

    # ---- display ------------------------------------------------------------

    def get_discount_display(self):
        if self.discount_type == self.DiscountType.PERCENTAGE:
            return f"{self.discount_value}% off"
        return f"{self.currency} {self.discount_value} off"

    @property
    def weekday_label(self):
        if not self.valid_weekdays:
            return "every day"
        return ", ".join(f"{Weekday(d).label}s" for d in sorted(self.valid_weekdays))

    # ---- validation and saving ----------------------------------------------

    def clean(self):
        errors = {}
        value, min_order = self.discount_value, self.minimum_order_value

        if value is not None:
            if self.discount_type == self.DiscountType.PERCENTAGE:
                if not (MIN_DISCOUNT_PERCENT < value <= 100):
                    errors["discount_value"] = (
                        f"Percentage codes must be more than {MIN_DISCOUNT_PERCENT}% and at most 100%."
                    )
            else:
                if min_order is None or min_order <= 0:
                    errors["minimum_order_value"] = (
                        "Fixed-amount codes need a minimum order value so the discount can be checked."
                    )
                else:
                    floor = min_order * MIN_DISCOUNT_PERCENT / 100
                    if value <= floor:
                        errors["discount_value"] = (
                            f"Must be more than {MIN_DISCOUNT_PERCENT}% of the minimum order value ({floor})."
                        )
                    elif value > min_order:
                        errors["discount_value"] = "Cannot exceed the minimum order value."

        if self.maximum_discount is not None and self.discount_type != self.DiscountType.PERCENTAGE:
            errors["maximum_discount"] = "A discount cap only applies to percentage codes."

        days = self.valid_weekdays
        if days:
            valid_days = set(Weekday.values)
            if (
                not isinstance(days, list)
                or any(not isinstance(d, int) or isinstance(d, bool) or d not in valid_days for d in days)
                or len(set(days)) != len(days)
            ):
                errors["valid_weekdays"] = "Use unique numbers from 0 (Monday) to 6 (Sunday), e.g. [4]."

        if bool(self.valid_from_time) != bool(self.valid_until_time):
            errors["valid_until_time"] = "Set both the start and end time, or neither."

        services = self.for_service_types
        if services:
            if not isinstance(services, list) or any(s not in ServiceType.values for s in services):
                errors["for_service_types"] = f"Choose from: {', '.join(ServiceType.values)}."

        if self.valid_until and self.valid_from and self.valid_until <= self.valid_from:
            errors["valid_until"] = "End date must be after the start date."

        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        self.code = normalize_promo_code(self.code)
        super().save(*args, **kwargs)

    @classmethod
    def find(cls, raw_code):
        """Look up a code the way a customer typed it. Returns None if not found."""
        code = normalize_promo_code(raw_code)
        return cls.objects.filter(code=code).first() if code else None

    @classmethod
    def create_friday_deal(cls, code, percent, *, created_by=None, **extra):
        """Create a Fridays-only percentage code, e.g.
        PromoCode.create_friday_deal("FRIDAY15", 15, max_uses=500)."""
        fields = {"name": "Friday Deal", "is_public": True}
        fields.update(extra)
        promo = cls(
            code=code,
            discount_type=cls.DiscountType.PERCENTAGE,
            discount_value=Decimal(str(percent)),
            valid_weekdays=[Weekday.FRIDAY.value],
            created_by=created_by,
            **fields,
        )
        promo.full_clean()
        promo.save()
        return promo

    # ---- availability ---------------------------------------------------------

    def check_available(self, at=None):
        """Raise ValidationError (with a code) if the code can't be used at `at`.
        Days and hours follow settings.TIME_ZONE."""
        at = at or timezone.now()
        if not self.is_active:
            raise ValidationError("This promo code is not active.", code="inactive")
        if at < self.valid_from:
            raise ValidationError("This promo code is not valid yet.", code="not_started")
        if self.valid_until and at >= self.valid_until:
            raise ValidationError("This promo code has expired.", code="expired")
        if self.max_uses is not None and self.times_used >= self.max_uses:
            raise ValidationError("This promo code has been fully used.", code="exhausted")
        if (
            self.total_discount_budget is not None
            and self.total_discount_given >= self.total_discount_budget
        ):
            raise ValidationError("This promo code is no longer available.", code="budget_exhausted")

        local = timezone.localtime(at)
        if self.valid_weekdays and local.weekday() not in self.valid_weekdays:
            raise ValidationError(
                f"This code is only valid on {self.weekday_label}.", code="wrong_day"
            )
        if self.valid_from_time and self.valid_until_time:
            start, end, now_t = self.valid_from_time, self.valid_until_time, local.time()
            inside = start <= now_t < end if start <= end else (now_t >= start or now_t < end)
            if not inside:
                raise ValidationError(
                    f"This code is only valid between {start:%H:%M} and {end:%H:%M}.",
                    code="outside_hours",
                )

    def is_valid_at(self, at=None):
        try:
            self.check_available(at)
        except ValidationError:
            return False
        return True

    @property
    def is_valid(self):
        return self.is_valid_at()

    # ---- pricing --------------------------------------------------------------

    def calculate_discount(self, order_amount):
        """Discount for an amount (0.00 if below the minimum order). Never exceeds the amount."""
        original = to_money(order_amount)
        if original < self.minimum_order_value:
            return Decimal("0.00")
        if self.discount_type == self.DiscountType.PERCENTAGE:
            discount = (original * self.discount_value / Decimal("100")).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )
            if self.maximum_discount is not None:
                discount = min(discount, self.maximum_discount)
        else:
            discount = self.discount_value
        return min(discount, original)

    def _validate_for(self, *, user, order_amount, service_type, currency,
                      is_first_time_customer, at):
        """Full eligibility check. Returns (amount, discount) or raises ValidationError."""
        self.check_available(at)
        if user is None:
            raise ValidationError("A customer is required.", code="user_required")
        amount = to_money(order_amount)

        if self.discount_type == self.DiscountType.FIXED and currency != self.currency:
            raise ValidationError(
                f"This code only applies to {self.currency} bookings.", code="currency_mismatch"
            )
        if amount < self.minimum_order_value:
            raise ValidationError(
                f"Minimum order for this code is {self.minimum_order_value}.", code="below_minimum"
            )
        if self.for_service_types and service_type not in self.for_service_types:
            raise ValidationError("This code does not apply to this service.", code="wrong_service")
        if self.first_time_customers_only and is_first_time_customer is not True:
            raise ValidationError(
                "This code is for first-time customers only.", code="first_time_only"
            )
        if self.allowed_users.exists() and not self.allowed_users.filter(pk=user.pk).exists():
            raise ValidationError("This code is not available for your account.", code="not_eligible")
        if self.usage_limit_per_user is not None:
            used = self.redemptions.filter(
                user=user, status=PromoRedemption.Status.REDEEMED
            ).count()
            if used >= self.usage_limit_per_user:
                raise ValidationError("You have already used this code.", code="user_limit_reached")

        discount = self.calculate_discount(amount)
        if discount <= 0:
            raise ValidationError("This code gives no discount on this order.", code="no_discount")
        if self.total_discount_budget is not None:
            if discount > self.total_discount_budget - self.total_discount_given:
                raise ValidationError(
                    "This promo code is no longer available.", code="budget_exhausted"
                )
        return amount, discount

    def quote(self, user, order_amount, service_type, *, currency=Currency.GHS,
              is_first_time_customer=None, at=None):
        """Show the customer what the code would save. Reserves nothing."""
        amount, discount = self._validate_for(
            user=user, order_amount=order_amount, service_type=service_type,
            currency=currency, is_first_time_customer=is_first_time_customer, at=at,
        )
        return PromoQuote(self, amount, discount, amount - discount, currency)

    @transaction.atomic
    def redeem(self, user, booking_ref, order_amount, service_type, *,
               currency=Currency.GHS, is_first_time_customer=None, at=None):
        """Use the code on a booking: re-checks everything under a row lock, then
        records the redemption and updates the counters. Repeating the call for the
        same booking returns the original redemption.

        Call it in the same transaction that creates the booking, and call
        `redemption.release()` if the booking is cancelled.
        """
        booking_ref = (booking_ref or "").strip()
        if not booking_ref:
            raise ValidationError("A booking reference is required.", code="booking_required")

        promo = type(self).objects.select_for_update().get(pk=self.pk)

        existing = promo.redemptions.filter(
            booking_ref=booking_ref, status=PromoRedemption.Status.REDEEMED
        ).first()
        if existing:
            if existing.user_id != getattr(user, "pk", None):
                raise ValidationError(
                    "This booking already has a promo code applied.", code="booking_has_promo"
                )
            return existing

        amount, discount = promo._validate_for(
            user=user, order_amount=order_amount, service_type=service_type,
            currency=currency, is_first_time_customer=is_first_time_customer, at=at,
        )
        redemption = PromoRedemption.objects.create(
            promo=promo, user=user, booking_ref=booking_ref, service_type=service_type,
            currency=currency, order_amount=amount, discount_amount=discount,
        )
        promo.times_used += 1
        promo.total_discount_given += discount
        promo.save(update_fields=["times_used", "total_discount_given", "updated_at"])
        return redemption

    class PromoCodeQuerySet(models.QuerySet):
        def live(self, at=None):
            """Active, in-date codes with uses left (day-of-week and hours not checked)."""
            at = at or timezone.now()
            return (
            self.filter(is_active=True, valid_from__lte=at)
            .filter(Q(valid_until__isnull=True) | Q(valid_until__gt=at))
            .filter(Q(max_uses__isnull=True) | Q(times_used__lt=F("max_uses")))
        )

    def available_now(self, at=None):
        """Public codes usable right now, including the day and hours check.
        Returns a list, for promo banners such as 'Friday deal!'."""
        return [p for p in self.live(at).filter(is_public=True) if p.is_valid_at(at)]



class PromoRedemption(models.Model):
    """One use of a promo code on a booking. Used for per-customer limits and reporting."""

    class Status(models.TextChoices):
        REDEEMED = "redeemed", "Redeemed"
        RELEASED = "released", "Released"

    promo = models.ForeignKey(PromoCode, on_delete=models.PROTECT, related_name="redemptions")
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name="promo_redemptions",
    )
    booking_ref = models.CharField(max_length=50)
    service_type = models.CharField(max_length=50, blank=True, default="")
    currency = models.CharField(max_length=3)
    order_amount = models.DecimalField(max_digits=10, decimal_places=2)
    discount_amount = models.DecimalField(max_digits=10, decimal_places=2)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.REDEEMED)
    redeemed_at = models.DateTimeField(auto_now_add=True)
    released_at = models.DateTimeField(null=True, blank=True)
    release_reason = models.CharField(max_length=255, blank=True, default="")

    class Meta:
        db_table = "promo_redemptions"
        ordering = ["-redeemed_at", "-id"]
        verbose_name = "Promo Redemption"
        verbose_name_plural = "Promo Redemptions"
        indexes = [
            models.Index(fields=["promo", "user", "status"], name="promored_promo_user_idx"),
            models.Index(fields=["booking_ref"], name="promored_booking_idx"),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["promo", "booking_ref"],
                condition=Q(status="redeemed"),
                name="promored_one_per_booking",
            ),
            models.CheckConstraint(condition=Q(order_amount__gt=0), name="promored_order_positive"),
            models.CheckConstraint(condition=Q(discount_amount__gt=0), name="promored_discount_positive"),
        ]

    def __str__(self):
        return f"{self.promo.code} on {self.booking_ref}: -{self.discount_amount} {self.currency}"

    def delete(self, *args, **kwargs):
        raise ValueError("Promo redemptions cannot be deleted. Release them instead.")

    def release(self, reason=""):
        """Give the use back (e.g. the booking was cancelled), freeing the customer's
        use, the global use and the budget."""
        with transaction.atomic():
            # Lock order is always promo, then redemption.
            promo = PromoCode.objects.select_for_update().get(pk=self.promo_id)
            r = type(self).objects.select_for_update().get(pk=self.pk)
            if r.status != self.Status.REDEEMED:
                raise ValidationError(
                    "This promo use was already released.", code="invalid_transition"
                )
            r.status = self.Status.RELEASED
            r.released_at = timezone.now()
            r.release_reason = (reason or "")[:255]
            r.save(update_fields=["status", "released_at", "release_reason"])
            promo.times_used = max(promo.times_used - 1, 0)
            promo.total_discount_given = max(
                promo.total_discount_given - r.discount_amount, Decimal("0.00")
            )
            promo.save(update_fields=["times_used", "total_discount_given", "updated_at"])
        self.refresh_from_db()
        return self


class Booking(models.Model):
    """A customer booking a ride, delivery or waste collection."""

    class ServiceType(models.TextChoices):
        RIDE = "ride", "Ride"
        DELIVERY = "delivery", "Delivery"
        WASTE_COLLECTION = "waste_collection", "Waste Collection"

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        ACCEPTED = "accepted", "Accepted"
        IN_PROGRESS = "in_progress", "In Progress"
        COMPLETED = "completed", "Completed"
        CANCELLED = "cancelled", "Cancelled"

    class Currency(models.TextChoices):
        GHS = "GHS", "Ghana Cedi"
        GBP = "GBP", "British Pound"

    # Allowed status changes. Anything not listed here is rejected.
    ALLOWED_TRANSITIONS = {
        Status.PENDING: {Status.ACCEPTED, Status.CANCELLED},
        Status.ACCEPTED: {Status.IN_PROGRESS, Status.CANCELLED},
        Status.IN_PROGRESS: {Status.COMPLETED, Status.CANCELLED},
        Status.COMPLETED: set(),
        Status.CANCELLED: set(),
    }

    # Identity
    reference = models.CharField(
        max_length=12,
        unique=True,
        editable=False,
        help_text="Customer-facing booking ID, e.g. BK-7K2M9PQX.",
    )
    customer = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="bookings",
    )
    rider = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="bookings_as_rider",
    )
    service_type = models.CharField(
        max_length=30, choices=ServiceType.choices, db_index=True
    )
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.PENDING, db_index=True
    )

    # Locations
    pickup_address = models.CharField(max_length=255)
    pickup_latitude = models.DecimalField(
        max_digits=9, decimal_places=6, null=True, blank=True
    )
    pickup_longitude = models.DecimalField(
        max_digits=9, decimal_places=6, null=True, blank=True
    )
    dropoff_address = models.CharField(
        max_length=255,
        blank=True,
        help_text="Leave blank for services with no destination (e.g. waste pickup).",
    )
    dropoff_latitude = models.DecimalField(
        max_digits=9, decimal_places=6, null=True, blank=True
    )
    dropoff_longitude = models.DecimalField(
        max_digits=9, decimal_places=6, null=True, blank=True
    )
    scheduled_for = models.DateTimeField(
        null=True, blank=True, help_text="Blank = as soon as possible."
    )
    notes = models.TextField(blank=True)

    # Pricing (total_price is always calculated, never typed in by hand)
    subtotal = models.DecimalField(
        max_digits=10, decimal_places=2, validators=[MinValueValidator(ZERO)]
    )
    discount_amount = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=ZERO,
        validators=[MinValueValidator(ZERO)],
    )
    total_price = models.DecimalField(max_digits=10, decimal_places=2, editable=False)
    currency = models.CharField(
        max_length=3, choices=Currency.choices, default=Currency.GHS
    )

    # Lifecycle timestamps
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    accepted_at = models.DateTimeField(null=True, blank=True, editable=False)
    started_at = models.DateTimeField(null=True, blank=True, editable=False)
    completed_at = models.DateTimeField(null=True, blank=True, editable=False)
    cancelled_at = models.DateTimeField(null=True, blank=True, editable=False)

    # Cancellation details
    cancelled_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        editable=False,
        related_name="bookings_cancelled",
    )
    cancellation_reason = models.CharField(max_length=255, blank=True, editable=False)

    class Meta:
        db_table = "bookings"
        ordering = ["-created_at"]
        verbose_name = "Booking"
        verbose_name_plural = "Bookings"
        indexes = [
            models.Index(fields=["customer", "-created_at"]),
            models.Index(fields=["rider", "status"]),
            models.Index(fields=["status", "-created_at"]),
        ]
        constraints = [
            models.CheckConstraint(
                condition=Q(total_price__gte=0),
                name="booking_total_price_gte_0",
            ),
            models.CheckConstraint(
                condition=Q(discount_amount__lte=F("subtotal")),
                name="booking_discount_lte_subtotal",
            ),
            models.CheckConstraint(
                condition=~Q(rider=F("customer")),
                name="booking_rider_not_customer",
            ),
        ]



    def __str__(self):
        return f"{self.reference} — {self.get_service_type_display()} ({self.get_status_display()})"

    # ------------------------------------------------------------------
    # Saving
    # ------------------------------------------------------------------
    def save(self, *args, **kwargs):
        self.total_price = max(self.subtotal - self.discount_amount, ZERO)

        if self.reference:
            return super().save(*args, **kwargs)

        # New booking: generate a reference, retrying on the rare collision.
        for attempt in range(5):
            self.reference = self._generate_reference()
            try:
                with transaction.atomic():
                    return super().save(*args, **kwargs)
            except IntegrityError:
                if attempt == 4:
                    raise
                self.reference = ""

    @staticmethod
    def _generate_reference():
        return "BK-" + "".join(secrets.choice(REFERENCE_ALPHABET) for _ in range(8))

    def clean(self):
        errors = {}
        if self.rider_id and self.rider_id == self.customer_id:
            errors["rider"] = "A rider cannot accept their own booking."
        if self.discount_amount and self.subtotal is not None:
            if self.discount_amount > self.subtotal:
                errors["discount_amount"] = "Discount cannot exceed the subtotal."
        if self.service_type != self.ServiceType.WASTE_COLLECTION and not self.dropoff_address:
            errors["dropoff_address"] = "A drop-off address is required for this service."
        if errors:
            raise ValidationError(errors)

    # ------------------------------------------------------------------
    # State helpers
    # ------------------------------------------------------------------
    @property
    def is_active(self):
        return self.status in {
            self.Status.PENDING,
            self.Status.ACCEPTED,
            self.Status.IN_PROGRESS,
        }

    @property
    def is_finished(self):
        return not self.is_active

    def can_transition_to(self, new_status):
        return new_status in self.ALLOWED_TRANSITIONS.get(self.status, set())

    def _transition(self, new_status, timestamp_field=None, **extra):
        """
        Move to `new_status` under a row lock so two people (e.g. two riders
        tapping Accept) can never both win.
        """
        with transaction.atomic():
            locked = type(self).objects.select_for_update().get(pk=self.pk)
            if not locked.can_transition_to(new_status):
                raise ValidationError(
                    f"Cannot change a {locked.get_status_display().lower()} booking "
                    f"to {self.Status(new_status).label.lower()}."
                )

            locked.status = new_status
            update_fields = ["status", "updated_at"]
            if timestamp_field:
                setattr(locked, timestamp_field, timezone.now())
                update_fields.append(timestamp_field)
            for field, value in extra.items():
                setattr(locked, field, value)
                update_fields.append(field)
            locked.save(update_fields=update_fields)

            # Sync the caller's instance with what was saved.
            for field in update_fields:
                setattr(self, field, getattr(locked, field))
        return self

    # ------------------------------------------------------------------
    # Lifecycle actions
    # ------------------------------------------------------------------
    def accept(self, rider):
        if rider.pk == self.customer_id:
            raise ValidationError("A rider cannot accept their own booking.")
        return self._transition(
            self.Status.ACCEPTED, "accepted_at", rider=rider
        )

    def start(self):
        return self._transition(self.Status.IN_PROGRESS, "started_at")

    def complete(self):
        return self._transition(self.Status.COMPLETED, "completed_at")

    def cancel(self, by_user, reason=""):
        return self._transition(
            self.Status.CANCELLED,
            "cancelled_at",
            cancelled_by=by_user,
            cancellation_reason=reason[:255],
        )



# Headers that must never be written to the database.
SENSITIVE_HEADERS = {
    "authorization",
    "cookie",
    "set-cookie",
    "proxy-authorization",
    "x-api-key",
    "x-auth-token",
}
REDACTED = "[REDACTED]"


def redact_headers(headers):
    """Return a copy of `headers` with secrets masked."""
    if not headers:
        return {}
    return {
        key: (REDACTED if str(key).lower() in SENSITIVE_HEADERS else value)
        for key, value in dict(headers).items()
    }


class WebhookEvent(models.Model):
    """
    Audit log of every incoming or outgoing webhook.

    The raw data (payload, headers, raw_body, ...) is write-once: it can't be
    changed after creation. Only processing state can be updated.
    """

    MAX_ATTEMPTS = 5
    RETRY_BASE_SECONDS = 30
    RETRY_MAX_SECONDS = 3600

    class Direction(models.TextChoices):
        INBOUND = "inbound", "Inbound"
        OUTBOUND = "outbound", "Outbound"

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        PROCESSING = "processing", "Processing"
        PROCESSED = "processed", "Processed"
        FAILED = "failed", "Failed"
        SKIPPED = "skipped", "Skipped"

    # Fields that may be updated after creation; everything else is locked.
    MUTABLE_FIELDS = frozenset(
        {
            "status",
            "attempts",
            "last_attempt_at",
            "next_retry_at",
            "error_message",
            "result_note",
            "processed_at",
        }
    )

    # What happened
    direction = models.CharField(
        max_length=10, choices=Direction.choices, default=Direction.INBOUND
    )
    event_type = models.CharField(
        max_length=100,
        db_index=True,
        help_text="e.g. payment.completed, booking.updated",
    )
    source = models.CharField(
        max_length=50,
        blank=True,
        help_text="Which service sent/receives this: momo, stripe, paystack, etc.",
    )
    external_id = models.CharField(
        max_length=255,
        blank=True,
        help_text="The provider's own event ID. Used to ignore duplicate deliveries.",
    )

    # Data, stored exactly as received
    payload = models.JSONField(default=dict)
    headers = models.JSONField(default=dict, blank=True)
    raw_body = models.TextField(
        blank=True,
        help_text="Unparsed request body, needed to re-verify signatures.",
    )
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    signature_valid = models.BooleanField(
        null=True,
        blank=True,
        help_text="None = not checked, True = verified, False = failed verification.",
    )

    # Processing state
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.PENDING, db_index=True
    )
    attempts = models.PositiveSmallIntegerField(default=0)
    last_attempt_at = models.DateTimeField(null=True, blank=True)
    next_retry_at = models.DateTimeField(null=True, blank=True)
    error_message = models.TextField(blank=True)
    result_note = models.TextField(blank=True, help_text="What we did with this event.")

    # Timestamps
    received_at = models.DateTimeField(default=timezone.now, db_index=True)
    processed_at = models.DateTimeField(null=True, blank=True)

    

    class Meta:
        db_table = "webhook_events"
        ordering = ["-received_at"]
        verbose_name = "Webhook Event"
        verbose_name_plural = "Webhook Events"
        indexes = [
            models.Index(fields=["event_type", "status"]),
            models.Index(fields=["source", "-received_at"]),
            models.Index(fields=["status", "next_retry_at"]),
        ]
        constraints = [
            # The same provider event can only be logged once per direction.
            models.UniqueConstraint(
                fields=["source", "external_id", "direction"],
                condition=~Q(external_id=""),
                name="unique_webhook_external_id",
            ),
        ]

    def __str__(self):
        return f"{self.event_type} — {self.status} @ {self.received_at:%Y-%m-%d %H:%M}"

    # ------------------------------------------------------------------
    # Write-once protection
    # ------------------------------------------------------------------
    def save(self, *args, **kwargs):
        if not self._state.adding:
            update_fields = kwargs.get("update_fields")
            if update_fields is None:
                raise ValueError(
                    "WebhookEvent rows are immutable. Pass update_fields "
                    "limited to processing fields."
                )
            locked = set(update_fields) - self.MUTABLE_FIELDS
            if locked:
                raise ValueError(
                    f"Cannot modify immutable webhook fields: {sorted(locked)}"
                )
        super().save(*args, **kwargs)

    # ------------------------------------------------------------------
    # Creating events
    # ------------------------------------------------------------------
    @classmethod
    def record(
        cls,
        event_type,
        payload,
        *,
        source="",
        headers=None,
        external_id="",
        direction=Direction.INBOUND,
        raw_body="",
        ip_address=None,
        signature_valid=None,
    ):
        """
        Log a webhook. Returns (event, created).

        If the provider re-sends an event with the same external_id, the
        original row is returned with created=False, so you can reply 200
        without processing it twice.
        """
        try:
            with transaction.atomic():
                event = cls.objects.create(
                    direction=direction,
                    event_type=event_type,
                    source=source,
                    external_id=external_id,
                    payload=payload,
                    headers=redact_headers(headers),
                    raw_body=raw_body,
                    ip_address=ip_address,
                    signature_valid=signature_valid,
                )
            return event, True
        except IntegrityError:
            if not external_id:
                raise
            existing = cls.objects.get(
                source=source, external_id=external_id, direction=direction
            )
            return existing, False

    # ------------------------------------------------------------------
    # Processing
    # ------------------------------------------------------------------
    def claim(self):
        """
        Atomically take this event for processing. Returns False if another
        worker already has it or it's finished, so no event runs twice.
        """
        with transaction.atomic():
            locked = type(self).objects.select_for_update().get(pk=self.pk)
            if locked.status not in {self.Status.PENDING, self.Status.FAILED}:
                return False
            if locked.attempts >= self.MAX_ATTEMPTS:
                return False

            locked.status = self.Status.PROCESSING
            locked.attempts += 1
            locked.last_attempt_at = timezone.now()
            locked.next_retry_at = None
            locked.save(
                update_fields=["status", "attempts", "last_attempt_at", "next_retry_at"]
            )
            self._sync_from(locked, ["status", "attempts", "last_attempt_at", "next_retry_at"])
        return True

    def process(self, handler):
        """
        Run `handler(event)` safely: claim, call, then record the outcome.
        The handler may return a short note describing what it did.
        Returns True on success.
        """
        if not self.claim():
            return False
        try:
            note = handler(self)
        except Exception as exc:
            logger.exception("Webhook %s failed (attempt %s)", self.pk, self.attempts)
            self.mark_failed(f"{type(exc).__name__}: {exc}")
            return False
        self.mark_processed(note or "")
        return True

    def mark_processed(self, note=""):
        self.status = self.Status.PROCESSED
        self.processed_at = timezone.now()
        self.result_note = note
        self.error_message = ""
        self.next_retry_at = None
        self.save(
            update_fields=[
                "status",
                "processed_at",
                "result_note",
                "error_message",
                "next_retry_at",
            ]
        )

    def mark_failed(self, error_message=""):
        """Record the failure and schedule a retry with exponential backoff."""
        self.status = self.Status.FAILED
        self.error_message = error_message
        if self.attempts < self.MAX_ATTEMPTS:
            delay = min(
                self.RETRY_BASE_SECONDS * 2 ** max(self.attempts - 1, 0),
                self.RETRY_MAX_SECONDS,
            )
            self.next_retry_at = timezone.now() + timedelta(seconds=delay)
            self.processed_at = None
        else:
            self.next_retry_at = None  # out of attempts
            self.processed_at = timezone.now()
        self.save(
            update_fields=["status", "error_message", "next_retry_at", "processed_at"]
        )

    def mark_skipped(self, note=""):
        """For events we deliberately ignore (unknown type, test ping, etc.)."""
        self.status = self.Status.SKIPPED
        self.processed_at = timezone.now()
        self.result_note = note
        self.save(update_fields=["status", "processed_at", "result_note"])

    @property
    def can_retry(self):
        return self.status == self.Status.FAILED and self.attempts < self.MAX_ATTEMPTS

    def _sync_from(self, other, fields):
        for field in fields:
            setattr(self, field, getattr(other, field))




class BookingStop(TimeStampedModel):
    """Intermediate stops for multi-drop deliveries/rides."""

    booking = models.ForeignKey(
        "Booking",
        on_delete=models.CASCADE,
        related_name="stops",
    )
    address = models.CharField(max_length=255)
    latitude = models.DecimalField(
        max_digits=9,
        decimal_places=6,
        null=True,
        blank=True,
        validators=[MinValueValidator(Decimal("-90")), MaxValueValidator(Decimal("90"))],
    )
    longitude = models.DecimalField(
        max_digits=9,
        decimal_places=6,
        null=True,
        blank=True,
        validators=[MinValueValidator(Decimal("-180")), MaxValueValidator(Decimal("180"))],
    )
    order = models.PositiveIntegerField(default=0)

    class Meta:
        db_table = "booking_stops"
        ordering = ["booking_id", "order"]
        constraints = [
            UniqueConstraint(
                fields=["booking", "order"],
                name="unique_stop_order_per_booking",
            ),
            models.CheckConstraint(
                condition=(
                    Q(latitude__isnull=True, longitude__isnull=True)
                    | Q(latitude__isnull=False, longitude__isnull=False)
                ),
                name="stop_lat_lng_both_or_neither",
            ),
        ]

    def __str__(self) -> str:
        # Uses booking_id to avoid an extra query per row.
        return f"Booking #{self.booking_id} — Stop {self.order}: {self.address[:20]}"


# ==========================================
# BookingStatusHistory Model
# ==========================================
class BookingStatusHistory(models.Model):
    """Immutable history of every status change, kept for audit."""

    booking = models.ForeignKey(
        "Booking",
        on_delete=models.CASCADE,
        related_name="status_history",
    )
    status = models.CharField(max_length=20)
    notes = models.CharField(max_length=255, blank=True)
    changed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="booking_status_changes",
    )
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        db_table = "booking_status_history"
        ordering = ["booking_id", "-created_at"]
        verbose_name = "Booking Status History"
        verbose_name_plural = "Booking Status History"
        indexes = [
            models.Index(
                fields=["booking", "-created_at"],
                name="bsh_booking_created_idx",
            ),
        ]

    def __str__(self) -> str:
        return f"Booking #{self.booking_id} → {self.status} @ {self.created_at:%Y-%m-%d %H:%M}"

    # --- Immutability (audit integrity) ---
    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError("Status history records are immutable.")
        super().save(*args, **kwargs)

    # Note: queryset.update()/bulk ops bypass save(); restrict these at the
    # manager/DB-permission level if strict immutability is required.
    # Deletion is still allowed so booking CASCADE deletes keep working.



class SystemAlertQuerySet(models.QuerySet):
    """Reusable query helpers for system alerts."""

    def live(self, now=None):
        """Alerts that are enabled and inside their display window."""
        now = now or timezone.now()
        return self.filter(
            is_active=True,
            starts_at__lte=now,
        ).filter(Q(expires_at__isnull=True) | Q(expires_at__gt=now))

    def expired(self, now=None):
        now = now or timezone.now()
        return self.filter(expires_at__lte=now)

    def upcoming(self, now=None):
        now = now or timezone.now()
        return self.filter(is_active=True, starts_at__gt=now)

    def for_user(self, user):
        """Live alerts the given user has not dismissed."""
        qs = self.live()
        if user is not None and user.is_authenticated:
            qs = qs.exclude(
                is_dismissible=True,
                dismissals__user=user,
            )
        return qs


class SystemAlert(models.Model):
    """System-wide notifications and alerts."""

    class Level(models.TextChoices):
        INFO = "info", _("Info")
        WARNING = "warning", _("Warning")
        ERROR = "error", _("Error")
        SUCCESS = "success", _("Success")

    title = models.CharField(_("title"), max_length=200)
    message = models.TextField(_("message"))
    level = models.CharField(
        _("level"),
        max_length=20,
        choices=Level.choices,
        default=Level.INFO,
    )
    is_active = models.BooleanField(_("active"), default=True)
    is_dismissible = models.BooleanField(
        _("dismissible"),
        default=True,
        help_text=_("Allow users to dismiss this alert."),
    )
    starts_at = models.DateTimeField(
        _("starts at"),
        default=timezone.now,
        help_text=_("Alert is hidden until this time."),
    )
    expires_at = models.DateTimeField(
        _("expires at"),
        null=True,
        blank=True,
        help_text=_("Leave empty for an alert that never expires."),
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_system_alerts",
        verbose_name=_("created by"),
    )
    created_at = models.DateTimeField(_("created at"), auto_now_add=True)
    updated_at = models.DateTimeField(_("updated at"), auto_now=True)

    objects = SystemAlertQuerySet.as_manager()

    class Meta:
        db_table = "system_alerts"
        ordering = ["-created_at"]
        verbose_name = _("system alert")
        verbose_name_plural = _("system alerts")
        indexes = [
            models.Index(
                fields=["is_active", "starts_at", "expires_at"],
                name="sysalert_live_idx",
            ),
        ]
        constraints = [
            # Requires Django 5.1+. On older versions use `check=` instead.
            models.CheckConstraint(
                condition=Q(expires_at__isnull=True)
                | Q(expires_at__gt=F("starts_at")),
                name="sysalert_expires_after_start",
            ),
        ]

    def __str__(self):
        return f"{self.get_level_display()}: {self.title}"

    def clean(self):
        super().clean()
        if self.expires_at and self.starts_at and self.expires_at <= self.starts_at:
            raise ValidationError(
                {"expires_at": _("Expiry must be later than the start time.")}
            )

    @property
    def is_expired(self) -> bool:
        return self.expires_at is not None and self.expires_at <= timezone.now()

    @property
    def is_live(self) -> bool:
        """True if the alert should currently be shown to users."""
        now = timezone.now()
        return (
            self.is_active
            and self.starts_at <= now
            and (self.expires_at is None or self.expires_at > now)
        )

    def dismiss(self, user):
        """Record a dismissal for a user (idempotent)."""
        if not self.is_dismissible:
            raise ValidationError(_("This alert cannot be dismissed."))
        return SystemAlertDismissal.objects.get_or_create(alert=self, user=user)[0]


class SystemAlertDismissal(models.Model):
    """Tracks which users have dismissed which alerts."""

    alert = models.ForeignKey(
        SystemAlert,
        on_delete=models.CASCADE,
        related_name="dismissals",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="dismissed_system_alerts",
    )
    dismissed_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "system_alert_dismissals"
        constraints = [
            models.UniqueConstraint(
                fields=["alert", "user"],
                name="sysalert_unique_dismissal",
            ),
        ]

    def __str__(self):
        return f"{self.user} dismissed {self.alert_id}"


