"""
RYDNOW — consolidated models.py
================================
Requires Django 5.1+ (CheckConstraint uses `condition=`; on older Django use `check=`).

Sections
  0. Imports, validators, helpers, constants
  1. Platform: SystemConfig, Service, Zone, PricingRule, SurgeRule
  2. Customers: CustomerProfile, SavedAddress, CustomerWallet, Loyalty
  3. Riders: RiderProfile (+history, qualification, documents, payout, emergency, sessions, pings)
  4. Money: RiderWallet, WalletTransaction, WithdrawalRequest, Payment, Refund, WebhookEvent
  5. Growth: PromoCode, PromoRedemption, Referral, BusinessAccount, WasteSubscription
  6. Booking core: Booking, BookingStatusHistory, ParcelDetail, BookingOffer, Review, CustomerRiderPreference
  7. Service & trust: RiderComplaint, RiderIncident, SupportTicket, SafetyAlert
  8. Rider growth: RiderPerformanceCommit
  9. Platform ops: Notification, DeviceToken, AuditLog, SystemAlert, DailySummary
"""
import calendar
import logging
import mimetypes
import os
import re
import secrets
import uuid
from datetime import datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal
from math import asin, cos, radians, sin, sqrt

from django.conf import settings
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.core.validators import (
    FileExtensionValidator, MaxValueValidator, MinValueValidator, RegexValidator,
)
from django.db import IntegrityError, models, transaction
from django.db.models import Avg, Count, F, Q, Sum
from django.utils import timezone
from phonenumber_field.modelfields import PhoneNumberField
from .wallet_deposit import WalletDeposit
from django.urls import reverse

logger = logging.getLogger(__name__)


class WalletError(Exception):
    """ Raised inside a wallet mutation to refuse it (e.g. insufficient balance)."""

# =====================================================================
# 0. VALIDATORS, HELPERS, CONSTANTS
# =====================================================================
phone_validator = RegexValidator(
    regex=r"^\+[1-9]\d{7,14}$",
    message="Enter phone in international format, e.g. +233241234567.",
)
ghana_card_validator = RegexValidator(
    regex=r"^GHA-\d{9}-\d$",
    message="Ghana Card must look like GHA-123456789-0.",
)


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
    if len(digits) < 9 or len(digits) > 15:
        raise ValidationError("Phone number must contain 9-15 digits.")


def validate_latitude(value):
    if value is not None and not (-90.0 <= float(value) <= 90.0):
        raise ValidationError("Latitude must be between -90 and 90 degrees.")


def validate_longitude(value):
    if value is not None and not (-180.0 <= float(value) <= 180.0):
        raise ValidationError("Longitude must be between -180 and 180 degrees.")


def round_money(value):
    """Consistent 2-decimal rounding — ALL financial math uses this."""
    return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def haversine_km(lat1, lng1, lat2, lng2):
    """Great-circle distance in km (no PostGIS needed). None if any input is missing."""
    if None in (lat1, lng1, lat2, lng2):
        return None
    lat1 = max(-90.0, min(90.0, float(lat1)))
    lat2 = max(-90.0, min(90.0, float(lat2)))
    lng1 = max(-180.0, min(180.0, float(lng1)))
    lng2 = max(-180.0, min(180.0, float(lng2)))
    dlat, dlng = radians(lat2 - lat1), radians(lng2 - lng1)
    a = sin(dlat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlng / 2) ** 2
    return round(6371 * 2 * asin(sqrt(max(0.0, min(1.0, a)))), 2)


def normalize_gh_phone(value):
    """024xxxxxxx / 23324xxxxxxx -> +23324xxxxxxx"""
    value = re.sub(r"[\s\-()]", "", value or "")
    if value.startswith("0") and len(value) == 10:
        return "+233" + value[1:]
    if value.startswith("233"):
        return "+" + value
    return value


CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O/1/I confusion


def generate_code(prefix, length=6):
    return f"{prefix}-" + "".join(secrets.choice(CODE_ALPHABET) for _ in range(length))


def unique_code(model, field, prefix, length=6):
    for _ in range(10):
        code = generate_code(prefix, length)
        if not model.objects.filter(**{field: code}).exists():
            return code
    return generate_code(prefix, length + 2)


def generate_pin():
    return f"{secrets.randbelow(10000):04d}"


def generate_transaction_ref():
    return f"TX-{uuid.uuid4().hex[:12].upper()}"


def generate_booking_ref():
    return f"BK-{uuid.uuid4().hex[:12].upper()}"


def generate_withdrawal_ref():
    return f"WD-{uuid.uuid4().hex[:10].upper()}"


def generate_payment_ref():
    return f"PAY-{uuid.uuid4().hex[:10].upper()}"


def generate_refund_ref():
    return f"RF-{uuid.uuid4().hex[:10].upper()}"


def generate_ticket_ref():
    return f"TKT-{timezone.now():%Y%m%d}-{secrets.token_hex(3).upper()}"


def generate_incident_reference():
    return f"INC-{timezone.now():%Y%m%d}-{secrets.token_hex(3).upper()}"



#        ---- Business rules ----------------------------------------------------

COMMISSION_RATE = Decimal("0.15")            # default platform cut
RIDER_PAYOUT_RATE = Decimal("1.00") - COMMISSION_RATE
# Riders who perform better keep more of every fare — a retention lever.
TIER_COMMISSION_RATES = {
    "standard": Decimal("0.15"),
    "silver": Decimal("0.14"),
    "gold": Decimal("0.13"),
    "platinum": Decimal("0.12"),
}
# (tier, min completed trips, min rating, min acceptance %)  — checked top-down
RIDER_TIER_RULES = [
    ("platinum", 1000, 4.8, 90.0),
    ("gold", 400, 4.7, 85.0),
    ("silver", 100, 4.5, 80.0),
]
CURRENCY_CHOICES = [("GHS", "Ghana Cedi")]

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
POINTS_PER_GHS_REDEEMED = 100                # 100 pts = GH₵1 bonus credit
LOYALTY_TIERS = [("platinum", 5000, Decimal("1.5")), ("gold", 2000, Decimal("1.25")),
                 ("silver", 500, Decimal("1.1")), ("bronze", 0, Decimal("1.0"))]
PROVIDER_METHODS = ("momo", "visa", "debit")



def compute_fare_split(total_fare, rate=COMMISSION_RATE):
    """Returns (commission, payout) that ALWAYS sum exactly to total_fare."""
    total_fare = round_money(total_fare)
    commission = round_money(total_fare * Decimal(rate))
    return commission, total_fare - commission


def validate_fare_breakdown(total_fare, commission, payout):
    if total_fare is None or total_fare <= 0:
        return True
    diff = abs((commission + payout) - total_fare)
    if diff > Decimal("0.01"):
        raise ValidationError(
            f"Fare split error: total={total_fare}, commission={commission}, "
            f"payout={payout}, difference={diff}."
        )
    return True


# ---- Choice constants --------------------------------------------------
SERVICE_CHOICES = [
    ("okada", "Okada Ride"),
    ("parcel", "Parcel Delivery"),
    ("waste-bin", "Waste Collection"),
]

STATUS_CHOICES = [
    ("pending", "Pending"),
    ("searching_rider", "Searching Rider"),
    ("assigned", "Rider Assigned"),
    ("in_progress", "In Progress"),
    ("completed", "Completed"),
    ("cancelled", "Cancelled"),
]
STATUS_TRANSITIONS = {
    "pending": ["searching_rider", "assigned", "cancelled"],
    "searching_rider": ["assigned", "cancelled"],
    "assigned": ["in_progress", "searching_rider", "cancelled"],  # re-dispatch if rider drops
    "in_progress": ["completed", "cancelled"],
    "completed": [],
    "cancelled": [],
}

PAYMENT_STATUS = [
    ("initiated", "Payment Initiated"),
    ("pending_confirmation", "Waiting Provider Confirmation"),
    ("held", "Funds Held in Escrow"),
    ("released", "Funds Released"),
    ("failed", "Payment Failed"),
    ("refund_initiated", "Refund Processing"),
    ("partially_refunded", "Partially Refunded"),
    ("refunded", "Refund Completed"),
    ("cancelled", "Cancelled Before Processing"),
]
PAYMENT_METHOD_CHOICES = [
    ("momo", "Mobile Money"),
    ("visa", "Visa Card"),
    ("debit", "Debit Card"),
    ("wallet", "RYDNOW Wallet"),
    ("cash", "Cash"),
    ("invoice", "Business Invoice"),
]
TRANSACTION_TYPES = [
    ("job_payment", "Completed Job Payment"),
    ("deposit", "Wallet Top-up"),
    ("withdrawal", "Withdrawal"),
    ("commission", "Platform Commission Deduction"),
    ("refund", "Customer Refund"),
    ("bonus", "Platform Bonus Credit"),
    ("referral_bonus", "Referral Bonus"),
    ("tip", "Customer Tip"),
    ("adjustment", "Manual Adjustment"),
    ("reversal", "Transaction Reversal"),
    ("payout", "Rider Earnings Payout"),
]
WITHDRAWAL_STATUS = [
    ("pending", "Queued for Processing"),
    ("reserved", "Funds Reserved — Sending"),
    ("validated", "Verified — Sufficient Balance"),
    ("initiated", "Sent to Mobile Money Provider"),
    ("completed", "Successfully Sent"),
    ("failed", "Provider Failed — Funds Returned"),
    ("reversed", "System Reversed — Check Notes"),
    ("timeout", "Provider Timeout — Funds Restored"),
]

# =====================================================================
# 1. PLATFORM CONFIG, SERVICES, ZONES, PRICING
# =====================================================================
class SystemConfig(models.Model):
    """Runtime settings editable in admin (cached 60s). Doubles as a feature-flag store."""
    key = models.CharField(max_length=100, unique=True)
    value = models.TextField(blank=True)
    description = models.CharField(max_length=255, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name_plural = "System Config"

    def __str__(self):
        return f"{self.key} = {self.value}"

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        cache.delete(f"sysconfig:{self.key}")

    @classmethod
    def get_value(cls, key, default=None):
        ck = f"sysconfig:{key}"
        val = cache.get(ck)
        if val is None:
            try:
                val = cls.objects.get(key=key).value
            except cls.DoesNotExist:
                return default
            cache.set(ck, val, 60)
        return val

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
    """Canonical registry of services. Deactivate to hide from new bookings; history is kept."""
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
    """Operating area (e.g. Accra Central). Drives pricing, surge, rider assignment and expansion."""
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
        """Closest active zone that contains the point (few zones, so a scan is fine)."""
        best, best_d = None, None
        for z in cls.objects.filter(is_active=True):
            d = haversine_km(z.center_lat, z.center_lng, lat, lng)
            if d is not None and d <= z.radius_km and (best_d is None or d < best_d):
                best, best_d = z, d
        return best

class PricingRule(models.Model):
    """Per-service (optionally per-zone) fare card. Zone rule beats the global rule."""
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
        ]

    def __str__(self):
        return f"{self.service_id} @ {self.zone or 'Global'}"

    @classmethod
    def get_rule(cls, service_key, zone=None):
        qs = cls.objects.filter(service_id=service_key, is_active=True)
        return (qs.filter(zone=zone).first() if zone else None) or qs.filter(zone__isnull=True).first()

    @classmethod
    def quote(cls, service_key, distance_km, duration_min, zone=None, when=None):
        """Fare estimate with surge. Returns a dict of Decimals the API can show before booking."""
        rule = cls.get_rule(service_key, zone)
        if rule is None:
            raise ValidationError(f"No pricing configured for service '{service_key}'.")
        subtotal = (rule.base_fare + rule.per_km * Decimal(str(distance_km or 0))
                    + rule.per_minute * Decimal(str(duration_min or 0)) + rule.booking_fee)
        subtotal = max(round_money(subtotal), rule.minimum_fare)
        surge = SurgeRule.multiplier_for(service_key, zone, when)
        return {
            "subtotal": subtotal,
            "surge_multiplier": surge,
            "total_fare": round_money(subtotal * surge),
            "cancellation_fee": rule.cancellation_fee,
        }

class SurgeRule(models.Model):
    """Time-boxed demand pricing (rain, rush hour, festive peaks). Create manually or from a Celery demand job."""
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
        return f"x{self.multiplier} {self.zone or 'All zones'} ({self.reason or 'surge'})"

    @classmethod
    def multiplier_for(cls, service_key, zone=None, when=None):
        when = when or timezone.now()
        qs = cls.objects.filter(is_active=True, starts_at__lte=when, ends_at__gte=when)
        qs = qs.filter(Q(service__isnull=True) | Q(service_id=service_key))
        qs = qs.filter(Q(zone__isnull=True) | Q(zone=zone)) if zone else qs.filter(zone__isnull=True)
        best = qs.order_by("-multiplier").first()
        return best.multiplier if best else Decimal("1.00")


# =====================================================================
# 2. CUSTOMERS
# =====================================================================

class CustomerProfile(models.Model):

    user = models.ForeignKey(
    settings.AUTH_USER_MODEL,
    on_delete=models.CASCADE
    )


    class Segment(models.TextChoices):
        NEW = "new", "New"
        ACTIVE = "active", "Active"
        VIP = "vip", "VIP"
        AT_RISK = "at_risk", "At Risk"
        CHURNED = "churned", "Churned"

    class Language(models.TextChoices):
        ENGLISH = "en", "English"
        TWI = "tw", "Twi"
        GA = "gaa", "Ga"
        EWE = "ee", "Ewe"
        HAUSA = "ha", "Hausa"

    class Source(models.TextChoices):
        ORGANIC = "organic", "Organic"
        REFERRAL = "referral", "Referral"
        PROMO = "promo", "Promo"
        SOCIAL = "social", "Social Media"
        AGENT = "agent", "Field Agent"
        OTHER = "other", "Other"

    settings.AUTH_USER_MODEL = settings.AUTH_USER_MODEL

    # Identity
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="customer_profile"
    )
    full_name = models.CharField(max_length=200, blank=True, default="")
    email = models.EmailField(blank=True, default="")
    phone = models.CharField(
        max_length=30,
        blank=True,
        null=True,
        # validators=[validate_phone_number]  # uncomment if you have this validator
    )
    mobile = PhoneNumberField(region="GH", blank=True, null=True)
    profile_photo = models.ImageField(
        upload_to="profiles/customer/%Y/%m/",
        blank=True,
        null=True
    )

    # CRM
    preferred_language = models.CharField(
        max_length=5,
        choices=Language.choices,
        default=Language.ENGLISH
    )
    acquisition_source = models.CharField(
        max_length=20,
        choices=Source.choices,
        default=Source.ORGANIC
    )
    marketing_opt_in = models.BooleanField(
        default=False,
        help_text="Explicit consent for promo SMS/push."
    )
    segment = models.CharField(
        max_length=10,
        choices=Segment.choices,
        default=Segment.NEW,
        db_index=True
    )
    total_bookings = models.PositiveIntegerField(default=0)
    total_spent = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=Decimal("0.00")
    )
    last_booking_at = models.DateTimeField(null=True, blank=True)
    rating_average = models.FloatField(
        default=5.0,
        validators=[MinValueValidator(0), MaxValueValidator(5)]
    )
    rating_count = models.PositiveIntegerField(default=0)
    outstanding_fees = models.DecimalField(
        max_digits=8,
        decimal_places=2,
        default=Decimal("0.00"),
        help_text="Unpaid cancellation fees; block new bookings until cleared."
    )
    internal_notes = models.TextField(
        blank=True,
        default="",
        help_text="Staff only."
    )
    is_blocked = models.BooleanField(default=False, db_index=True)
    blocked_reason = models.CharField(max_length=255, blank=True, default="")

    # Growth
    referral_code = models.CharField(
        max_length=16,
        unique=True,
        blank=True,
        null=True,
        editable=False
    )
    referred_by = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="referrals_made"
    )

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["segment", "-last_booking_at"])]

    def __str__(self):
        return (
            self.full_name
            or getattr(self.user, "username", None)
            or f"CustomerProfile-{self.pk}"
        )

    def save(self, *args, **kwargs):
        if self.email:
            self.email = self.email.strip().lower()
        if not self.referral_code and not kwargs.get("update_fields"):
            # Make sure `unique_code` is imported — uncomment when ready
            # self.referral_code = unique_code(CustomerProfile, "referral_code", "RYD", 6)
            pass
        super().save(*args, **kwargs)


    @property
    def can_book(self):
        return not self.is_blocked and (self.outstanding_fees or Decimal("0")) <= 0

    @property
    def loyalty_tier(self):
        acct = getattr(self, "loyalty", None)
        return acct.tier if acct else "bronze"

    def get_wallet(self):
        wallet, _ = CustomerWallet.objects.get_or_create(user=self.user)
        return wallet

    def recompute_segment(self, save=True):
        now = timezone.now()
        bookings = self.total_bookings or 0
        spent = self.total_spent or Decimal("0")
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
            total_spent=F("total_spent") + booking.total_fare,
            last_booking_at=timezone.now(),
        )
        self.refresh_from_db(fields=["total_bookings", "total_spent", "last_booking_at"])
        self.recompute_segment()

    def apply_rating(self, rating):
        with transaction.atomic():
            c = CustomerProfile.objects.select_for_update().get(pk=self.pk)
            avg = c.rating_average if c.rating_average is not None else 5.0
            count = c.rating_count or 0
            total = avg * count + rating
            c.rating_count = count + 1
            c.rating_average = round(total / c.rating_count, 2)
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
        constraints = [
            models.UniqueConstraint(fields=["customer"], condition=Q(is_default=True),
                                    name="unique_default_address_per_customer"),
        ]

    def __str__(self):
        return f"{self.customer} — {self.get_label_display()}: {self.address}"

    def save(self, *args, **kwargs):
        with transaction.atomic():
            if self.is_default:
                SavedAddress.objects.filter(customer=self.customer, is_default=True).exclude(pk=self.pk).update(is_default=False)
            super().save(*args, **kwargs)


class CustomerWallet(models.Model):
    """balance = spendable cash; withdrawable_balance ⊆ balance; bonus_credit = promo money (spend-only, spent first)."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="customer_wallet")
    balance = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"),
                                  validators=[validate_non_negative_amount])
    withdrawable_balance = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"),
                                               validators=[validate_non_negative_amount])
    bonus_credit = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"),
                                       validators=[validate_non_negative_amount])
    currency = models.CharField(max_length=3, choices=CURRENCY_CHOICES, default="GHS")
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{getattr(self.user, 'username', 'Deleted User')} — {self.currency} {self.balance}"

    def _apply(self, tx_type, deltas_fn, *, reference=None, description="", related_booking=None):
        """
        One locked, idempotent, ledgered mutation. deltas_fn(wallet) ->
        (balance_delta, withdrawable_delta, bonus_delta, signed_tx_amount); raise WalletError to refuse.
        Returns (ok, message).
        """
        ref = reference or generate_transaction_ref()
        try:
            with transaction.atomic():
                if WalletTransaction.objects.filter(reference=ref).exists():
                    return False, f"Duplicate transaction: {ref}"
                w = CustomerWallet.objects.select_for_update().get(pk=self.pk)
                d_bal, d_wd, d_bonus, tx_amount = deltas_fn(w)
                w.balance += d_bal
                w.withdrawable_balance += d_wd
                w.bonus_credit += d_bonus
                if min(w.balance, w.withdrawable_balance, w.bonus_credit) < 0:
                    raise WalletError("Insufficient balance")
                w.save(update_fields=["balance", "withdrawable_balance", "bonus_credit", "updated_at"])
                WalletTransaction.objects.create(
                    customer_wallet=w, transaction_type=tx_type, amount=tx_amount,
                    balance_after=w.balance, reference=ref, description=description,
                    currency=w.currency, related_booking=related_booking, status="completed",
                )
                self.balance, self.withdrawable_balance, self.bonus_credit = (
                    w.balance, w.withdrawable_balance, w.bonus_credit)
                return True, "OK"
        except WalletError as e:
            return False, str(e)
        except IntegrityError:
            if WalletTransaction.objects.filter(reference=ref).exists():
                return False, f"Duplicate transaction: {ref}"
            return False, "Database error"

    def deposit(self, amount, reference=None, description="", related_booking=None):
        amount = round_money(amount)
        validate_positive_amount(amount)
        ok, msg = self._apply("deposit", lambda w: (amount, amount, Decimal("0"), amount),
                              reference=reference, description=description, related_booking=related_booking)
        return ok, "Deposit successful" if ok else msg

    def withdraw(self, amount, reference=None, description=""):
        amount = round_money(amount)
        validate_positive_amount(amount)

        def fn(w):
            if w.balance < amount:
                raise WalletError("Insufficient total balance")
            if w.withdrawable_balance < amount:
                raise WalletError("Insufficient withdrawable balance")
            return -amount, -amount, Decimal("0"), -amount
        ok, msg = self._apply("withdrawal", fn, reference=reference, description=description)
        return ok, "Withdrawal successful" if ok else msg

    def pay_for_booking(self, amount, booking, reference=None):
        """Spend bonus credit first, then cash balance."""
        amount = round_money(amount)
        validate_positive_amount(amount)

        def fn(w):
            from_bonus = min(w.bonus_credit, amount)
            from_cash = amount - from_bonus
            if w.balance < from_cash:
                raise WalletError("Insufficient wallet balance")
            from_withdrawable = min(w.withdrawable_balance, from_cash)
            return -from_cash, -from_withdrawable, -from_bonus, -amount
        return self._apply("job_payment", fn, reference=reference or f"PAYWAL-{booking.booking_ref}",
                           description=f"Payment for {booking.booking_ref}", related_booking=booking)

    def refund_to_wallet(self, amount, booking=None, reference=None, description="Refund"):
        amount = round_money(amount)
        return self._apply("refund", lambda w: (amount, amount, Decimal("0"), amount),
                           reference=reference, description=description, related_booking=booking)

    def add_bonus(self, amount, reference=None, description="Bonus", tx_type="bonus"):
        amount = round_money(amount)
        validate_positive_amount(amount)
        return self._apply(tx_type, lambda w: (Decimal("0"), Decimal("0"), amount, amount),
                           reference=reference, description=description)


class LoyaltyAccount(models.Model):
    """Points for every cedi spent. Tier unlocks a points multiplier and priority support."""
    customer = models.OneToOneField(CustomerProfile, on_delete=models.CASCADE, related_name="loyalty")
    points_balance = models.PositiveIntegerField(default=0)
    lifetime_points = models.PositiveIntegerField(default=0)
    tier = models.CharField(max_length=10, default="bronze", db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.customer} — {self.tier} ({self.points_balance} pts)"

    @staticmethod
    def tier_for(lifetime_points):
        for name, threshold, _ in LOYALTY_TIERS:
            if lifetime_points >= threshold:
                return name
        return "bronze"

    def multiplier(self):
        return next((m for n, _, m in LOYALTY_TIERS if n == self.tier), Decimal("1.0"))

    def earn(self, amount_spent, booking=None, reference=None):
        ref = reference or (f"LOY-{booking.booking_ref}" if booking else f"LOY-{uuid.uuid4().hex[:10]}")
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
            acct.save()
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



# =====================================================================
# 3. RIDERS
# =====================================================================
# No 0/0/1/I/L so codes can be read out over a phone call or shared by SMS
RIDER_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ234567890"
RIDER_CODE_PREFIX = "RYD-"
RIDER_CODE_LENGTH = 8
RIDER_CODE_MAX_ATTEMPTS = 8


def generate_rider_code():
    body = "".join(secrets.choice(RIDER_CODE_ALPHABET) for _ in range (RIDER_CODE_LENGTH))
    return f"{RIDER_CODE_PREFIX}{body}"   #12 chard, fits_length=16

def rider_profile_photo_path(instance, filename):
    ext = filename.rsplit(".",1)[-1].lower()if "." in filename else "jpg"
    return f"riders/{instance.public_id}/photo/{uuid.uuid4().hex}.{ext}"

class Gender(models.TextChoices):
    MALE = "male", "Male"
    FEMALE = "female", "Female"      # NOTE: was "famale" — run a data migration for old rows
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
        now = timezone.now()
        return self.filter(Q(suspended_at__isnull=True) | Q(suspended_until__lte=now))
    
    def not_restricted(self):
        now = timezone.now()
        return self.filter(Q(is_restricted=False) | Q (restricted_until__lte=now))

    def dispatchable(self, service=None, max_location_age_seconds=LOCATION_STALE_SECONDS, exclude_ids=None):
        """
        Riders who may receive an offer RIGHT NOW: active, verified, online, not restricted/suspended,
        licence valid, fresh GPS, qualified for the service, and not already on a job.
        """
        now = timezone.now()
        qs = self.active().verified().filter(
            is_online=True,
            current_lat__isnull=False, current_lng__isnull=False,
            location_updated_at__gte=now - timedelta(seconds=max_location_age_seconds),
        ).filter(Q(is_restricted=False) | Q(restricted_until__lt=now)).filter(
            Q(license_expiry_date__isnull=True) | Q(license_expiry_date__gte=timezone.localdate())
        ).exclude(
            pk__in=Booking.objects.filter(status__in=Booking.ACTIVE_STATUSES, assigned_rider__isnull=False)
            .values("assigned_rider_id")
        )
        if service is not None:
            qs = qs.filter(qualifications__service=service, qualifications__status=ServiceQualification.Status.APPROVED)
        if exclude_ids:
            qs = qs.exclude(pk__in=exclude_ids)
        return qs.distinct()

    def nearby(self, lat, lng, radius_km=5, limit=10):
        """Bounding-box prefilter in SQL, exact Haversine in Python. Returns [(rider, km)] nearest first."""
        dlat = radius_km / 111.0
        dlng = radius_km / (111.0 * max(cos(radians(lat)), 0.01))
        cands = self.filter(current_lat__range=(lat - dlat, lat + dlat),
                            current_lng__range=(lng - dlng, lng + dlng))
        out = []
        for r in cands:
            d = haversine_km(r.current_lat, r.current_lng, lat, lng)
            if d is not None and d <= radius_km:
                out.append((r, d))
        out.sort(key=lambda x: x[1])
        return out[:limit]

class RiderProfile(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="rider_profile")

    # Geneated automatically in save(). Rider share it to refer other rider to join;
    # Customers/Agents can use it for payment look up
    rider_code = models.CharField(max_length=16, unique=True, blank=True, editable=False)
    referred_by = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.SET_NULL, related_name="referred_riders"
    )
    zone = models.ForeignKey("Zone", null=True, blank=True, on_delete=models.SET_NULL, related_name="riders")
    services = models.ManyToManyField(
        "Service", through="ServiceQualification", blank=True, related_name="qualified_riders"
    )

    # Identity
    full_name = models.CharField(max_length=150)
    phone = models.CharField(max_length=30, unique= True, validators=[phone_validator])    
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
    verification_status = models.CharField(
        max_length=20, choices=VerificationStatus.choices,default=VerificationStatus.PENDING
    )
    rejection_reason = models.TextField(blank=True)
    submitted_at = models.DateTimeField(null=True, blank=True)
    verified_at = models.DateTimeField(null=True, blank=True)
    verified_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,related_name="verified_riders"
    )

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
    current_lat = models.FloatField(
        null=True, blank=True, validators=[MinValueValidator(-90), MaxValueValidator(90)]
    )
    current_lng = models.FloatField(
        null=True, blank=True, validators=[MinValueValidator(-180), MaxValueValidator(180)]
    )
    location_updated_at = models.DateTimeField(null=True, blank=True)

    # Performance counters (updated with F() so they are race-safe)
    tier = models.CharField(max_length=10, choices=RiderTier.choices, default=RiderTier.STANDARD)
    rating_average = models.FloatField(
        default=5.0, validators=[MinValueValidator(0.0), MaxValueValidator(5.0)]
    )
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

    @property
    def is_verified(self):
        return self.verification_status == VerificationStatus.VERIFIED

    def record_offer(self, outcome: str, response_s: float = 0):
        """Track offer outcomes for acceptance/reliability stats"""
        # Update counters as needed
        pass

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Rider profile"
        verbose_name_plural = "Rider profiles"
        constraints = [
            # Unique Ghana Card when filled
            models.UniqueConstraint(
                fields=["ghana_card_number"],
                condition=~models.Q(ghana_card_number=""),
                name="uniq_rider_ghana_card_set",
            ),
            # Unique License when filled
            models.UniqueConstraint(
                fields=["license_number"],
                condition=~models.Q(license_number=""),
                name="uniq_rider_license_set",
            ),
            # Rating between 0 and 5
            models.CheckConstraint(
                condition=models.Q(rating_average__gte=0) & models.Q(rating_average__lte=5.0),
                name="rider_rating_0_to_5",
            ),
            # Cannot refer yourself
            models.CheckConstraint(
                condition=~models.Q(referred_by=models.F("id")),
                name="rider_no_self_refer",
            ),
        ]

        indexes = [
            models.Index(fields=["verification_status", "is_active"]),
            models.Index(fields=["is_online", "is_active"]),
            models.Index(fields=["current_lat", "current_lng"]),
            models.Index(fields=["-rating_average", "-completed_trips"]),
            models.Index(fields=["zone", "is_online"]),
        ]

    def __str__(self):
        return f"{self.full_name} — {self.get_verification_status_display()}"

    def __str__ (self):
        return f"{self.full_name}  ({self.rider_code or 'no code'})"

    #------------------------------------------------
    # Normalisation + safe rider_code generation
    #---------------------------------------------
    def _normalize(self):
        self.full_name= (self.full_name or "").strip()
        self.phone = normalize_gh_phone(self.phone)
        self.ghana_card_number = (self.ghana_card_number or "").strip().upper()
        self.license_number = (self.license_number or "").strip().upper()
        self.motorbike_plate_number= (self.motorbike_plate_number or "").strip().upper()

    def save(self, *args, **kwargs):
        self._normalize()
        if self.rider_code:
            return super().save(*args, **kwargs)

       # New Rider (or legacy row without a code): generate, and retry on the
       # rare collission instead of surfacing a 500 to signup request.
        for _ in range(RIDER_CODE_MAX_ATTEMPTS):
            self.rider_code = generate_rider_code()
            try:
                with transaction.atomic():
                    return super().save(*args, **kwargs)
            except IntegrityError as exc:
                if "rider_code" not in str (exc):
                    raise
                self.rider_code=None
        raise IntegrityError("Could not generate a unique rider code.")     
                
    # ---- read-only helpers -------------------------------------------------
    @property
    def is_verified(self):
        return self.verification_status == VerificationStatus.VERIFIED
    
    @property
    def documents_uploaded(self):
        # Assumes RiderDocument has FK (RiderProfile, related _name="documents").
        return self.documents.exists()

        PROFILE_COMPLETION_FIELDS=(
            "full_name","phone", "profile_photo", "date_of_birth", "gender",
            "ghana_card_number", "city" "address",
            "motorbike_model", "motorbike_plate_number", "motorbike_colour",
            "licence_number", "license_expiry_date",
        ) 

    @property
    def profile_completion(self):
        filled = sum(1 for f in self.PROFILE_COMPLETION_FIELDS if gatattr(self, f))
        return round(filled * 100 / len(self.PROFILE_COMPLETION_FIELDS))

    #----------------------------------
    # State helpers (used by dispatch / admin)
    #-----------------------------------------

    @property
    def is_suspended(self):
        if not self.suspended_at:
            return False
        return self.suspended_until is None or self.suspended_until > timezone.now()

    @property
    def is_currently_restricted(self):
        if not self.is_currently_restricted:
            return False
        return self.restricted_until is None or self.restricted_until > timezone.now() 

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
                and not self.licence_expired and not self.is_restricted_now)

    @property
    def is_approved(self):  # legacy alias used by older code
        return self.is_verified and not self.is_restricted_now

    @property
    def can_recieve_offers(self):
        return (
            self.is_active
            and self.deleted_at is None
            and self.is_verified 
            and not self.is_suspended
            and not self.is_currently_restricted
        )

    @property
    def location_is_fresh(self):
        return bool(self.location_updated_at and
                    self.location_updated_at >= timezone.now() - timedelta(seconds=LOCATION_STALE_SECONDS))

    @property
    def commission_rate(self):
        return TIER_COMMISSION_RATES.get(self.tier, COMMISSION_RATE)

    @property
    def can_take_cash_jobs(self):
        """Cash trips: rider keeps the cash, so commission is debited from wallet — needs a float."""
        min_balance = SystemConfig.get_decimal("MIN_RIDER_WALLET_BALANCE", "50.00")
        wallet = getattr(self, "wallet", None)
        return bool(wallet and wallet.balance >= min_balance)


    @property
    def has_minimum_deposit(self):
        """Returns True if rider has deposited/has at least ₵50 — required to receive bookings."""
        min_required = SystemConfig.get_decimal("MIN_RIDER_BOOKING_BALANCE", "50.00")
        wallet = getattr(self, "wallet", None)
        return bool(wallet and wallet.balance >= min_required)


    def is_qualified_for(self, service_key):
        return self.qualifications.filter(service_id=service_key,
                                          status=ServiceQualification.Status.APPROVED).exists()

    def get_distance_km(self, lat, lng):
        if self.current_lat == 0 and self.current_lng == 0:
            return None
        return haversine_km(self.current_lat, self.current_lng, lat, lng)

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


    @property
    def documents_complete(self):
        return not self.missing_required_documents()


    # ---- live state --------------------------------------------------------
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

    # ---- performance recording ---------------------------------------------
    def record_offer(self, outcome, response_seconds=None):
        """outcome: "accepted" | "declined" | "expired" """
        if outcome not in ("accepted", "declined", "expired"):
            raise ValueError("outcome must be accepted, declined or expired")
        updates = {"offers_received": F("offers_received") + 1,
                   f"offers_{outcome}": F(f"offers_{outcome}") + 1}
        if outcome != "expired" and response_seconds is not None:
            updates["response_time_total_seconds"] = F("response_time_total_seconds") + int(response_seconds)
            updates["response_count"] = F("response_count") + 1
        RiderProfile.objects.filter(pk=self.pk).update(**updates)

    def record_arrival(self, seconds):
        RiderProfile.objects.filter(pk=self.pk).update(
            arrival_time_total_seconds=F("arrival_time_total_seconds") + int(seconds),
            arrival_count=F("arrival_count") + 1,
        )

    def record_trip_completed(self):
        RiderProfile.objects.filter(pk=self.pk).update(
            completed_trips=F("completed_trips") + 1, last_job_at=timezone.now())
        self.refresh_from_db(fields=["completed_trips", "last_job_at"])
        self.recalculate_tier()
        Referral.check_rider_milestone(self)

    def record_trip_cancelled(self):
        RiderProfile.objects.filter(pk=self.pk).update(cancelled_trips=F("cancelled_trips") + 1)

    def apply_rating(self, rating):
        with transaction.atomic():
            r = RiderProfile.objects.select_for_update().get(pk=self.pk)
            avg = r.rating_average if r.rating_average is not None else 5.0
            count = r.rating_count or 0
            total = avg * count + rating
            r.rating_count = count + 1
            r.rating_average = round(total / r.rating_count, 2)
            r.save(update_fields=["rating_average", "rating_count", "updated_at"])
        self.rating_average, self.rating_count = r.rating_average, r.rating_count
        self.recalculate_tier()

    def recalculate_tier(self):
        """Promote/demote automatically; a better tier means a lower commission rate."""
        new_tier = RiderTier.STANDARD
        if self.rating_count >= 10:
            for tier, trips, rating, acceptance in RIDER_TIER_RULES:
                if (self.completed_trips >= trips and self.rating_average >= rating
                        and self.acceptance_rate >= acceptance):
                    new_tier = tier
                    break
        if new_tier != self.tier:
            self.tier = new_tier
            RiderProfile.objects.filter(pk=self.pk).update(tier=new_tier)
            Notification.notify(self.user, "Your rider tier changed",
                                f"You are now {new_tier.title()} — commission {TIER_COMMISSION_RATES[new_tier] * 100:.0f}%.",
                                category=Notification.Category.SYSTEM)
        return new_tier

    def add_strike(self, reason=""):
        RiderProfile.objects.filter(pk=self.pk).update(strike_count=F("strike_count") + 1)
        self.refresh_from_db(fields=["strike_count"])
        if self.strike_count >= MAX_STRIKES and self.verification_status == VerificationStatus.VERIFIED:
            self.suspend(by=None, reason=f"Automatic: {self.strike_count} strikes. {reason}".strip(),
                         until=timezone.now() + timedelta(days=7))

    # ---- verification workflow (atomic + audited) ---------------------------
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

    def missing_required_documents(self):
        """Labels of required documents that do not have an APPROVED upload yet."""
        have = set(self.documents.filter(status=RiderDocument.DocumentStatus.APPROVED)
                .values_list("document_type", flat=True))
        return [label for code, label in RiderDocument.REQUIRED_TYPES
                if getattr(code, "value", code) not in have]

    def sync_verification_from_documents(self, by=None):
        """
        Make the rider Verified once EVERY required document is approved
        (Ghana Card, passport photo, riding licence, vehicle registration).
        Returns True if the status changed.
        """
        if self.verification_status not in (VerificationStatus.PENDING,
                                            VerificationStatus.UNDER_REVIEW,
                                            VerificationStatus.REJECTED):
            return False
        if self.missing_required_documents():
            return False
        self._transition(VerificationStatus.VERIFIED, by=by,
                         reason="All required documents approved",
                         verified_at=timezone.now(), verified_by=by, rejection_reason="")
        return True

    def note_document_rejected(self, document, by=None):
        """
        Store why a document was rejected. A rider who was under review goes back to Rejected
        (they can fix it and resubmit). Returns True if the status changed.
        """
        reason = f"{document.get_document_type_display()}: {(document.notes or '').strip()}"
        if self.verification_status == VerificationStatus.UNDER_REVIEW:
            self._transition(VerificationStatus.REJECTED, by=by, reason=reason, rejection_reason=reason)
            return True
        if self.verification_status == VerificationStatus.PENDING:
            RiderProfile.objects.filter(pk=self.pk).update(rejection_reason=reason)
            self.rejection_reason = reason
        return False




class RiderStatusHistory(models.Model):
    class Status(models.TextChoices):
        ACTIVE = 'active', 'Active'
        SUSPENDED = 'suspended', 'Suspended'
        PENDING = 'pending', 'Pending'


    class Category(models.TextChoices):
        OTHER = 'other', 'Other'
        COMPLIANCE = 'complaince', 'Complaince'
        PERFORMANCE = 'performance', 'Performance'

    
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
        indexes = [
            models.Index(fields=["service", "status", "rider"], name="qual_service_status_idx"),
            models.Index(fields=["rider", "status"], name="qual_rider_status_idx"),
        ]

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
        GHANA_CARD_Front = "ghana_card_front", "Ghana Card(Front)"
        GHANA_CARD_BACK = "ghana_card_back", "Ghana Card(Back)"
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
        (DocumentType.GHANA_CARD_Front, "Ghana Card (Front)"),
        (DocumentType.GHANA_CARD_BACK, "Ghana Card (Back)"),
        (DocumentType.PASSPORT_PHOTO, "Passport Photo"),
        (DocumentType.RIDING_LICENSE, "Riding Licence"),
        (DocumentType.VEHICLE_REGISTRATION, "Vehicle Registration"),
    ]

    def review (self, bby, approve, notes =""):
        if notes:
            self.notes= notes
        if not approve and not (self.noted or "").strip():
            raise ValidationError ("Add a short reason before rejecting a document.")
        with transaction.atomic():
            self.status = self.DocumentStatus.APPROVED if approved else self.DocumentStatus.REJECTED
            self.reviewed_by, self.reviewed_at= by, timezone.now()
            self.save(update_fields=["status", "reviewed_by", "reviewed_at, notes"])
            return self.apply_review_effects(by=by)

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
        return f"{self.rider_profile.full_name} — {self.get_document_type_display()}"

    @property
    def is_expired(self):
        return bool(self.expiry_date and self.expiry_date < timezone.localdate())

    def clean(self):
        super().clean()
        if self.status == self.DocumentStatus.REJECTED and not (self.notes or "").strip():
            raise ValidationError({"notes": "Add a short reason so the rider knows what to fix."})

    def review(self, by, approve, notes=""):
        """
        Approve  or reject, stamp the reviewer, then sync the rider's verification status.
        Return "verified" (rider just became Verified), "rejected" (rider sent back), or "".
        """
        if notes:
            self.notes = notes 
        if not approve and not (self.noted or "").strip():
            raise ValidationError("Add a short reason before rejecting a document.")
        with transaction.atomic():
            self.status = self.DocumentStatus.APPROVED if approve else self.DocumentStatus.REJECTED
            self.reviewed_by, self.reviewed_at, self.notes = by, timezone.now(), notes
            self.save(update_fields=["status", "reviewed_by", "reviewed_at", "notes"])
            return self.apply_review_effects(by=by)

    def apply_review_effects(self, by=None):
        """
        Side effects of a decision that is Already saved on this row.
        Approved -> if every required document is approved, the rider becomes Verified.
        Rejected -> the reason is stored on the rider, they are notified, and a rider who was 
                    under review goes back to Rejected so they can fix and resubmit.
        """
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
            Notification.notify(rider.user, "A document need attention",
                                f"{self.get_document_type_display()}: {(self.notes or "").strip()}",
                                category= Notification.Category.SYSTEM)
            return "rejected" if changed else ""
        return ""

                    
    def get_absolute_url(self):
        """Tell Django where to find the edit page for this document"""
        return reverse("admin:api_riderdocument_change", args=[self.pk])

# RIDER PAYOUT ACCOUNT
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
            if not self.is_deleted:
                if not siblings.exists():
                    self.is_primary = True
                elif self.is_primary:
                    siblings.filter(is_primary=True).update(is_primary=False)
            super().save(*args, **kwargs)


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

    class Language(models.TextChoices):
        ENGLISH = "en", "English"
        TWI = "tw", "Twi"
        GA = "gaa", "Ga"
        EWE = "ee", "Ewe"
        HAUSA = "ha", "Hausa"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    rider_profile = models.ForeignKey(RiderProfile, on_delete=models.CASCADE, related_name="emergency_contacts")
    full_name = models.CharField(max_length=255)
    relationship = models.CharField(max_length=20, choices=Relationship.choices, default=Relationship.OTHER)
    relationship_note = models.CharField(max_length=100, blank=True,
                                         help_text="Details when relationship is 'Other' (e.g. neighbour).")
    phone_number = models.CharField(max_length=20, validators=[phone_validator])
    alternative_phone = models.CharField(max_length=20, blank=True, validators=[phone_validator])
    address = models.TextField(blank=True)
    preferred_language = models.CharField(max_length=5, choices=Language.choices, default=Language.ENGLISH)
    is_primary = models.BooleanField(default=False)
    notify_on_emergency = models.BooleanField(default=True,
                                              help_text="Send SMS/call alerts to this contact during incidents.")
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
        return f"{self.full_name} — {self.get_relationship_display()}"

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
    """Every online stretch — powers hours-online targets, supply heatmaps and payroll-style reports."""
    rider = models.ForeignKey(RiderProfile, on_delete=models.CASCADE, related_name="online_sessions")
    started_at = models.DateTimeField(default=timezone.now)
    ended_at = models.DateTimeField(null=True, blank=True)
    duration_seconds = models.PositiveIntegerField(default=0)
    start_lat = models.FloatField(null=True, blank=True)
    start_lng = models.FloatField(null=True, blank=True)

    class Meta:
        ordering = ["-started_at"]
        indexes = [models.Index(fields=["rider", "-started_at"])]

    def close(self, when=None):
        when = when or timezone.now()
        self.ended_at = when
        self.duration_seconds = max(0, int((when - self.started_at).total_seconds()))
        self.save(update_fields=["ended_at", "duration_seconds"])


class RiderLocationPing(models.Model):
    """
    Breadcrumb trail for trip replay, dispute evidence and fare audit.
    At scale keep LIVE position in Redis and write a sampled ping (e.g. every 15s) only during active trips;
    partition by month or prune after 90 days.
    """
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
   


# =====================================================================
# 4. MONEY — rider wallet, ledger, withdrawals, payments, refunds, webhooks
# =====================================================================
class RiderWallet(models.Model):
    """Balance may go negative only through cash-trip commission (rider owes the platform)."""
    rider = models.OneToOneField(RiderProfile, on_delete=models.PROTECT, related_name="wallet")
    balance = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    total_earned = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal("0.00"))
    total_withdrawn = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal("0.00"))
    currency = models.CharField(max_length=3, choices=CURRENCY_CHOICES, default="GHS")
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.rider} — {self.currency} {self.balance}"

    def _post(self, tx_type, delta, reference, description="", booking=None, require_funds=False,
              earned=False, withdrawn=False):
        delta = round_money(delta)
        try:
            with transaction.atomic():
                if WalletTransaction.objects.filter(reference=reference).exists():
                    return True, "Already recorded (idempotent)"
                w = RiderWallet.objects.select_for_update().get(pk=self.pk)
                if require_funds and w.balance + delta < 0:
                    return False, "Insufficient wallet balance"
                w.balance += delta
                if earned and delta > 0:
                    w.total_earned += delta
                if withdrawn and delta < 0:
                    w.total_withdrawn += -delta
                w.save(update_fields=["balance", "total_earned", "total_withdrawn", "updated_at"])
                WalletTransaction.objects.create(
                    rider_wallet=w, transaction_type=tx_type, amount=delta, balance_after=w.balance,
                    reference=reference, description=description, currency=w.currency,
                    related_booking=booking, status="completed")
                self.balance = w.balance
                return True, "OK"
        except IntegrityError:
            return True, "Already recorded (idempotent)"

    def credit_earnings(self, amount, booking_ref="", payment_verified=False, tx_type="payout",
                        description="", related_booking=None, reference=None):
        if not payment_verified:
            return False, "Payment must be verified before crediting earnings"
        amount = round_money(amount)
        validate_positive_amount(amount)
        ref = reference or (f"PAYOUT-{booking_ref}" if booking_ref else generate_transaction_ref())
        return self._post(tx_type, amount, ref, description or f"Earnings {booking_ref}".strip(),
                          related_booking, earned=True)

    def debit_commission(self, amount, booking_ref="", related_booking=None):
        """Cash trips: rider already holds the cash, so the platform's cut is taken from the wallet."""
        amount = round_money(amount)
        if amount <= 0:
            return True, "No commission due"
        return self._post("commission", -amount, f"COMM-{booking_ref}", f"Commission {booking_ref}", related_booking)


class WalletTransaction(models.Model):
    """Immutable double-entry-style ledger line. Corrections are NEW rows (reversal/adjustment)."""
    STATUS = [("pending", "Pending"), ("completed", "Completed"), ("failed", "Failed"), ("reversed", "Reversed")]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    customer_wallet = models.ForeignKey(CustomerWallet, null=True, blank=True, on_delete=models.PROTECT,
                                        related_name="transactions")
    rider_wallet = models.ForeignKey(RiderWallet, null=True, blank=True, on_delete=models.PROTECT,
                                     related_name="transactions")
    transaction_type = models.CharField(
        max_length=50,
        choices=TRANSACTION_TYPES,
        default="debit",
        db_index= True
    )
    amount = models.DecimalField(max_digits=12, decimal_places=2, help_text="Signed: + credit, − debit.")
    balance_after = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    currency = models.CharField(max_length=3, choices=CURRENCY_CHOICES, default="GHS")
    reference = models.CharField(max_length=60, unique=True, default=generate_transaction_ref)
    description = models.CharField(max_length=255, blank=True)
    related_booking = models.ForeignKey("Booking", null=True, blank=True, on_delete=models.PROTECT,
                                        related_name="wallet_transactions")
    status = models.CharField(max_length=12, choices=STATUS, default="completed", db_index=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["customer_wallet", "-created_at"]),
            models.Index(fields=["rider_wallet", "-created_at"]),
            models.Index(fields=["transaction_type", "created_at"]),
        ]
        constraints = [
            models.CheckConstraint(
                condition=(Q(customer_wallet__isnull=False, rider_wallet__isnull=True)
                           | Q(customer_wallet__isnull=True, rider_wallet__isnull=False)),
                name="wallet_tx_exactly_one_wallet"),
        ]

    def __str__(self):
        return f"{self.reference} {self.transaction_type} {self.amount}"

    def save(self, *args, **kwargs):
        if not self._state.adding:
            # Only the status of a pending line may change (e.g. pending withdrawal -> completed/failed).
            allowed = {"status"}
            if set(kwargs.get("update_fields") or []) - allowed or not kwargs.get("update_fields"):
                raise ValidationError("Ledger entries are immutable; post a reversal instead.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Ledger entries cannot be deleted.")


class WithdrawalRequest(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    reference = models.CharField(max_length=20, unique=True, default=generate_withdrawal_ref)
    rider_wallet = models.ForeignKey(RiderWallet, on_delete=models.PROTECT, related_name="withdrawals")
    payout_account = models.ForeignKey(RiderPayoutAccount, on_delete=models.PROTECT, related_name="withdrawals")
    amount = models.DecimalField(max_digits=10, decimal_places=2, validators=[validate_positive_amount])
    fee = models.DecimalField(max_digits=8, decimal_places=2, default=Decimal("0.00"))
    status = models.CharField(max_length=12, choices=WITHDRAWAL_STATUS, default="pending", db_index=True)
    provider_ref = models.CharField(max_length=255, blank=True)
    failure_reason = models.CharField(max_length=255, blank=True)
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    processed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["status", "created_at"])]

    def __str__(self):
        return f"{self.reference} GH₵{self.amount} ({self.status})"

    @classmethod
    def create_request(cls, wallet, payout_account, amount):
        """Reserve funds immediately so the same cedis can't be withdrawn twice."""
        amount = round_money(amount)
        minimum = SystemConfig.get_decimal("MIN_WITHDRAWAL", "10.00")
        fee = SystemConfig.get_decimal("WITHDRAWAL_FEE", "0.00")
        if amount < minimum:
            return None, f"Minimum withdrawal is GH₵{minimum}"
        if payout_account.rider_profile_id != wallet.rider_id or payout_account.is_deleted:
            return None, "Invalid payout account"
        with transaction.atomic():
            ref = generate_withdrawal_ref()
            ok, msg = wallet._post("withdrawal", -(amount + fee), f"WDTX-{ref}",
                                   f"Withdrawal {ref}", require_funds=True, withdrawn=True)
            if not ok:
                return None, msg
            req = cls.objects.create(reference=ref, rider_wallet=wallet, payout_account=payout_account,
                                     amount=amount, fee=fee, status="reserved")
        return req, "Withdrawal queued"

    def mark_completed(self, provider_ref=""):
        self.status, self.provider_ref, self.processed_at = "completed", provider_ref, timezone.now()
        self.save(update_fields=["status", "provider_ref", "processed_at"])

    def mark_failed(self, reason, status="failed"):
        """Return reserved funds (idempotent via reference)."""
        if self.status in ("failed", "reversed", "timeout", "completed"):
            return
        with transaction.atomic():
            self.rider_wallet._post("reversal", self.amount + self.fee, f"WDREV-{self.reference}",
                                    f"Withdrawal {self.reference} returned: {reason}")
            self.status, self.failure_reason, self.processed_at = status, reason[:255], timezone.now()
            self.save(update_fields=["status", "failure_reason", "processed_at"])


class Payment(models.Model):
    """One row per provider attempt (Paystack/Hubtel/MoMo). A booking can have several (retries)."""
    class Provider(models.TextChoices):
        PAYSTACK = "paystack", "Paystack"
        HUBTEL = "hubtel", "Hubtel"
        MTN_MOMO = "mtn_momo", "MTN MoMo"
        OTHER = "other", "Other"

    class Status(models.TextChoices):
        INITIATED = "initiated", "Initiated"
        PENDING = "pending", "Pending"
        SUCCESS = "success", "Success"
        FAILED = "failed", "Failed"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    booking = models.ForeignKey("Booking", on_delete=models.PROTECT, related_name="payments")
    provider = models.CharField(max_length=20, choices=Provider.choices, default=Provider.PAYSTACK)
    method = models.CharField(max_length=20, choices=PAYMENT_METHOD_CHOICES, blank=True)
    reference = models.CharField(max_length=40, unique=True, default=generate_payment_ref)
    provider_ref = models.CharField(max_length=255, blank=True, db_index=True)
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.INITIATED, db_index=True)
    failure_reason = models.CharField(max_length=255, blank=True)
    raw_response = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["booking", "status"])]

    def __str__(self):
        return f"{self.reference} {self.status} GH₵{self.amount}"

    def mark_success(self, provider_ref="", raw=None):
        with transaction.atomic():
            p = Payment.objects.select_for_update().get(pk=self.pk)
            if p.status == self.Status.SUCCESS:
                return True, "Already confirmed (idempotent)"
            p.status, p.provider_ref = self.Status.SUCCESS, provider_ref or p.provider_ref
            if raw:
                p.raw_response = raw
            p.save()
            ok, msg = p.booking.confirm_payment_verified(p.provider_ref, payment_ref=p.reference)
            self.status = p.status
            return ok, msg

    def mark_failed(self, reason="", raw=None):
        self.status, self.failure_reason = self.Status.FAILED, reason[:255]
        if raw:
            self.raw_response = raw
        self.save(update_fields=["status", "failure_reason", "raw_response", "updated_at"])
        Booking.objects.filter(pk=self.booking_id, payment_status__in=["initiated", "pending_confirmation"]) \
            .update(payment_status="failed")


class Refund(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        COMPLETED = "completed", "Completed"
        FAILED = "failed", "Failed"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    booking = models.ForeignKey("Booking", on_delete=models.PROTECT, related_name="refunds")
    reference = models.CharField(max_length=30, unique=True, default=generate_refund_ref)
    amount = models.DecimalField(max_digits=10, decimal_places=2, validators=[validate_positive_amount])
    method = models.CharField(max_length=20, choices=PAYMENT_METHOD_CHOICES, blank=True)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.PENDING, db_index=True)
    reason = models.CharField(max_length=255, blank=True)
    provider_ref = models.CharField(max_length=255, blank=True)
    failure_reason = models.CharField(max_length=255, blank=True)
    initiated_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["status", "created_at"])]

    def __str__(self):
        return f"{self.reference} GH₵{self.amount} ({self.status})"

    def mark_completed(self, provider_ref=""):
        with transaction.atomic():
            r = Refund.objects.select_for_update().get(pk=self.pk)
            if r.status == self.Status.COMPLETED:
                return
            r.status, r.completed_at = self.Status.COMPLETED, timezone.now()
            if provider_ref:
                r.provider_ref = provider_ref
            r.save()
            b = Booking.objects.select_for_update().get(pk=r.booking_id)
            refunded = Refund.objects.filter(booking=b, status=self.Status.COMPLETED).aggregate(s=Sum("amount"))["s"] or Decimal("0")
            b.payment_status = "refunded" if refunded >= b.total_fare else "partially_refunded"
            b.save(update_fields=["payment_status"])
            self.status, self.completed_at = r.status, r.completed_at

    def mark_failed(self, reason=""):
        self.status, self.failure_reason = self.Status.FAILED, reason[:255]
        self.save(update_fields=["status", "failure_reason"])


class WebhookEvent(models.Model):
    """Idempotency + audit for provider callbacks. Providers retry; never process the same event twice."""
    provider = models.CharField(max_length=30)
    event_id = models.CharField(max_length=120)
    event_type = models.CharField(max_length=80, blank=True)
    payload = models.JSONField(default=dict)
    processed = models.BooleanField(default=False, db_index=True)
    processed_at = models.DateTimeField(null=True, blank=True)
    error = models.TextField(blank=True)
    received_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-received_at"]
        constraints = [models.UniqueConstraint(fields=["provider", "event_id"], name="unique_webhook_event")]

    def __str__(self):
        return f"{self.provider}:{self.event_id}"

    @classmethod
    def register(cls, provider, event_id, payload, event_type=""):
        """Returns (event, created). If created is False, acknowledge the webhook and do nothing."""
        try:
            with transaction.atomic():
                return cls.objects.create(provider=provider, event_id=event_id, payload=payload,
                                          event_type=event_type), True
        except IntegrityError:
            return cls.objects.get(provider=provider, event_id=event_id), False

    def mark_processed(self, error=""):
        self.processed, self.processed_at, self.error = not error, timezone.now(), error
        self.save(update_fields=["processed", "processed_at", "error"])


# =====================================================================
# 5. GROWTH — promos, referrals, business accounts, waste subscriptions
# =====================================================================
class PromoCode(models.Model):
    class DiscountType(models.TextChoices):
        PERCENT = "percent", "Percent"
        FIXED = "fixed", "Fixed amount (GH₵)"

    code = models.CharField(max_length=30, unique=True)
    description = models.CharField(max_length=200, blank=True)
    discount_type = models.CharField(max_length=10, choices=DiscountType.choices, default=DiscountType.PERCENT)
    value = models.DecimalField(max_digits=8, decimal_places=2, validators=[validate_positive_amount])
    max_discount = models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True,
                                       help_text="Cap for percent codes.")
    min_fare = models.DecimalField(max_digits=8, decimal_places=2, default=Decimal("0.00"))
    services = models.ManyToManyField(Service, blank=True, related_name="promos",
                                      help_text="Empty = valid for all services.")
    first_booking_only = models.BooleanField(default=False)
    max_total_redemptions = models.PositiveIntegerField(null=True, blank=True)
    max_per_customer = models.PositiveSmallIntegerField(default=1)
    redeemed_count = models.PositiveIntegerField(default=0)
    starts_at = models.DateTimeField(default=timezone.now)
    expires_at = models.DateTimeField(null=True, blank=True)
    is_active = models.BooleanField(default=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return self.code

    def save(self, *args, **kwargs):
        self.code = (self.code or "").strip().upper()
        super().save(*args, **kwargs)

    def calculate_discount(self, fare):
        fare = Decimal(str(fare))
        if self.discount_type == self.DiscountType.PERCENT:
            disc = fare * self.value / Decimal("100")
            if self.max_discount:
                disc = min(disc, self.max_discount)
        else:
            disc = self.value
        return round_money(min(disc, fare))

    def validate_for(self, customer, service_key, fare):
        """Returns (ok, message, discount)."""
        now = timezone.now()
        if not self.is_active or now < self.starts_at or (self.expires_at and now > self.expires_at):
            return False, "This promo code is not active.", Decimal("0")
        if self.max_total_redemptions and self.redeemed_count >= self.max_total_redemptions:
            return False, "This promo code has been fully redeemed.", Decimal("0")
        if Decimal(str(fare)) < self.min_fare:
            return False, f"Minimum fare for this code is GH₵{self.min_fare}.", Decimal("0")
        if self.services.exists() and not self.services.filter(pk=service_key).exists():
            return False, "This code does not apply to this service.", Decimal("0")
        if self.first_booking_only and customer.total_bookings > 0:
            return False, "This code is for first bookings only.", Decimal("0")
        used = PromoRedemption.objects.filter(promo=self, customer=customer).count()
        if used >= self.max_per_customer:
            return False, "You have already used this code.", Decimal("0")
        return True, "Promo applied", self.calculate_discount(fare)


class PromoRedemption(models.Model):
    promo = models.ForeignKey(PromoCode, on_delete=models.PROTECT, related_name="redemptions")
    customer = models.ForeignKey(CustomerProfile, on_delete=models.CASCADE, related_name="promo_redemptions")
    booking = models.OneToOneField("Booking", on_delete=models.CASCADE, related_name="promo_redemption")
    discount_amount = models.DecimalField(max_digits=8, decimal_places=2)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [models.Index(fields=["promo", "customer"])]

    def __str__(self):
        return f"{self.promo.code} → {self.booking_id}"


class Referral(models.Model):
    class Kind(models.TextChoices):
        CUSTOMER = "customer", "Customer referral"
        RIDER = "rider", "Rider referral"

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        REWARDED = "rewarded", "Rewarded"
        EXPIRED = "expired", "Expired"

    kind = models.CharField(max_length=10, choices=Kind.choices)
    referrer = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="referrals_sent")
    referred = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="referral_received")
    code_used = models.CharField(max_length=16)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING, db_index=True)
    reward_amount = models.DecimalField(max_digits=8, decimal_places=2, default=Decimal("0.00"))
    rewarded_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [models.CheckConstraint(condition=~Q(referrer=F("referred")), name="no_self_referral")]

    def __str__(self):
        return f"{self.referrer_id} → {self.referred_id} ({self.status})"

    @classmethod
    def qualify_for_booking(cls, booking):
        """Both sides get bonus credit after the referred customer's first COMPLETED trip."""
        if not booking.customer_id:
            return
        with transaction.atomic():
            ref = cls.objects.select_for_update().filter(
                kind=cls.Kind.CUSTOMER, referred=booking.customer.user, status=cls.Status.PENDING).first()
            if not ref:
                return
            reward = SystemConfig.get_decimal("REFERRAL_REWARD_GHS", "5.00")
            for user, tag in ((ref.referrer, "R"), (ref.referred, "N")):
                wallet, _ = CustomerWallet.objects.get_or_create(user=user)
                wallet.add_bonus(reward, reference=f"REFBONUS-{ref.pk}-{tag}",
                                 description="Referral reward", tx_type="referral_bonus")
            ref.status, ref.reward_amount, ref.rewarded_at = cls.Status.REWARDED, reward, timezone.now()
            ref.save()

    @classmethod
    def check_rider_milestone(cls, rider):
        """Referring rider earns a bonus when the new rider completes N trips."""
        threshold = int(SystemConfig.get_decimal("RIDER_REFERRAL_TRIPS", "20"))
        if rider.completed_trips < threshold:
            return
        with transaction.atomic():
            ref = cls.objects.select_for_update().filter(
                kind=cls.Kind.RIDER, referred=rider.user, status=cls.Status.PENDING).first()
            if not ref:
                return
            referrer = RiderProfile.objects.filter(user=ref.referrer).first()
            if not referrer:
                return
            bonus = SystemConfig.get_decimal("RIDER_REFERRAL_BONUS_GHS", "30.00")
            wallet, _ = RiderWallet.objects.get_or_create(rider=referrer)
            wallet.credit_earnings(bonus, payment_verified=True, tx_type="referral_bonus",
                                   description="Rider referral bonus", reference=f"RREF-{ref.pk}")
            ref.status, ref.reward_amount, ref.rewarded_at = cls.Status.REWARDED, bonus, timezone.now()
            ref.save()

class ReferralCode(models.Model):
    """
    Unique referral code per user.
    Used to track who invited whom and manage referral bonuses/credits.
    One user = one active code by default.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='referral_code',
        verbose_name='Owner',
    )
    code = models.CharField(
        max_length=16,
        unique=True,
        db_index=True,
        help_text='Unique alphanumeric referral code',
    )
    is_active = models.BooleanField(default=True, db_index=True)
    times_used = models.PositiveIntegerField(default=0, help_text='How many times this code has been used')
    max_uses = models.PositiveIntegerField(
        null=True, blank=True,
        help_text='Optional limit — leave blank for unlimited'
    )
    expires_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ('-created_at',)
        verbose_name = 'Referral Code'
        verbose_name_plural = 'Referral Codes'

    def __str__(self):
        return f"{self.code} — {self.user.username}"

    @property
    def is_valid(self):
        """Code can be used only if active, not expired, and under usage limit."""
        if not self.is_active:
            return False
        if self.expires_at and timezone.now() > self.expires_at:
            return False
        if self.max_uses and self.times_used >= self.max_uses:
            return False
        return True

    def increment_usage(self):
        """Call this when someone successfully uses this code."""
        self.times_used += 1
        self.save(update_fields=['times_used', 'updated_at'])


class BusinessAccount(models.Model):
    """B2B: shops, pharmacies and offices ship on credit and get one invoice per cycle."""
    class Cycle(models.TextChoices):
        WEEKLY = "weekly", "Weekly"
        MONTHLY = "monthly", "Monthly"

    name = models.CharField(max_length=150)
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="owned_business_accounts")
    members = models.ManyToManyField(CustomerProfile, blank=True, related_name="business_accounts")
    contact_phone = models.CharField(max_length=30, blank=True, validators=[phone_validator])
    contact_email = models.EmailField(blank=True)
    tin = models.CharField("Tax ID (TIN)", max_length=30, blank=True)
    credit_limit = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    outstanding_balance = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    billing_cycle = models.CharField(max_length=10, choices=Cycle.choices, default=Cycle.MONTHLY)
    discount_percent = models.DecimalField(max_digits=4, decimal_places=2, default=Decimal("0.00"),
                                           validators=[MaxValueValidator(Decimal("50"))])
    is_active = models.BooleanField(default=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name

    @property
    def available_credit(self):
        return max(Decimal("0.00"), self.credit_limit - self.outstanding_balance)

    def can_charge(self, amount):
        return self.is_active and self.available_credit >= Decimal(str(amount))

    def charge(self, amount):
        with transaction.atomic():
            acct = BusinessAccount.objects.select_for_update().get(pk=self.pk)
            acct.outstanding_balance += round_money(amount)
            acct.save(update_fields=["outstanding_balance"])
            self.outstanding_balance = acct.outstanding_balance

    def record_payment(self, amount):
        with transaction.atomic():
            acct = BusinessAccount.objects.select_for_update().get(pk=self.pk)
            acct.outstanding_balance = max(Decimal("0.00"), acct.outstanding_balance - round_money(amount))
            acct.save(update_fields=["outstanding_balance"])
            self.outstanding_balance = acct.outstanding_balance


class WasteSubscription(models.Model):
    """Recurring waste pick-ups: predictable revenue + route density. Celery calls generate_due_bookings() daily."""
    class BinSize(models.TextChoices):
        SMALL = "small", "Small bin"
        MEDIUM = "medium", "Medium bin"
        LARGE = "large", "Large / skip bag"

    class Frequency(models.TextChoices):
        WEEKLY = "weekly", "Weekly"
        BIWEEKLY = "biweekly", "Every 2 weeks"
        MONTHLY = "monthly", "Monthly"

    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        PAUSED = "paused", "Paused"
        CANCELLED = "cancelled", "Cancelled"

    customer = models.ForeignKey(CustomerProfile, on_delete=models.PROTECT, related_name="waste_subscriptions")
    address = models.CharField(max_length=255)
    lat = models.FloatField(validators=[validate_latitude])
    lng = models.FloatField(validators=[validate_longitude])
    bin_size = models.CharField(max_length=10, choices=BinSize.choices, default=BinSize.MEDIUM)
    frequency = models.CharField(max_length=10, choices=Frequency.choices, default=Frequency.WEEKLY)
    price_per_pickup = models.DecimalField(max_digits=8, decimal_places=2, validators=[validate_positive_amount])
    payment_method = models.CharField(max_length=10, choices=[("wallet", "Wallet"), ("cash", "Cash"), ("momo", "Mobile Money")],
                                      default="wallet")
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.ACTIVE, db_index=True)
    next_pickup_date = models.DateField(db_index=True)
    last_pickup_date = models.DateField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["next_pickup_date"]
        indexes = [models.Index(fields=["status", "next_pickup_date"])]

    def __str__(self):
        return f"{self.customer} — {self.get_frequency_display()} {self.get_bin_size_display()}"

    def _step(self):
        return {"weekly": 7, "biweekly": 14, "monthly": 30}[self.frequency]

    def advance(self):
        self.last_pickup_date = self.next_pickup_date
        self.next_pickup_date = self.next_pickup_date + timedelta(days=self._step())
        self.save(update_fields=["last_pickup_date", "next_pickup_date"])

    def pause(self):
        self.status = self.Status.PAUSED
        self.save(update_fields=["status"])

    def resume(self, next_date=None):
        self.status = self.Status.ACTIVE
        self.next_pickup_date = max(next_date or timezone.localdate(), timezone.localdate())
        self.save(update_fields=["status", "next_pickup_date"])

    @classmethod
    def generate_due_bookings(cls, today=None, lead_days=1):
        """Idempotent: a unique constraint on (subscription, scheduled_at) stops duplicates on re-runs."""
        today = today or timezone.localdate()
        created = 0
        due = cls.objects.filter(status=cls.Status.ACTIVE, next_pickup_date__lte=today + timedelta(days=lead_days))
        for sub in due.select_related("customer"):
            when = timezone.make_aware(datetime.combine(sub.next_pickup_date, time(8, 0)))
            try:
                with transaction.atomic():
                    Booking.objects.create(
                        service_type="waste-bin", customer=sub.customer, subscription=sub,
                        pickup=sub.address, pickup_lat=sub.lat, pickup_lng=sub.lng,
                        fare_before_discount=sub.price_per_pickup, total_fare=sub.price_per_pickup,
                        payment_method=sub.payment_method, scheduled_at=when, source="subscription",
                        notes=f"Subscription pick-up ({sub.get_bin_size_display()})")
                    sub.advance()
                    created += 1
            except IntegrityError:
                sub.advance()
        return created


# =====================================================================
# 6. BOOKING CORE
# =====================================================================
class Booking(models.Model):
    ACTIVE_STATUSES = ["assigned", "in_progress"]

    class Source(models.TextChoices):
        APP = "app", "Mobile App"
        WEB = "web", "Website"
        CALL_CENTER = "call_center", "Call Centre"
        SUBSCRIPTION = "subscription", "Subscription"
        API = "api", "Business API"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    booking_ref = models.CharField(max_length=16, unique=True, blank=True, db_index=True, default=generate_booking_ref)
    service_type = models.CharField(max_length=20, choices=SERVICE_CHOICES)
    source = models.CharField(max_length=15, choices=Source.choices, default=Source.APP)
    customer = models.ForeignKey(CustomerProfile, on_delete=models.PROTECT, related_name="bookings", null=True, blank=True)
    business_account = models.ForeignKey(BusinessAccount, null=True, blank=True, on_delete=models.PROTECT, related_name="bookings")
    subscription = models.ForeignKey(WasteSubscription, null=True, blank=True, on_delete=models.SET_NULL, related_name="bookings")
    zone = models.ForeignKey(Zone, null=True, blank=True, on_delete=models.SET_NULL, related_name="bookings")

    pickup = models.CharField(max_length=255, blank=True, default="")
    pickup_lat = models.FloatField(validators=[validate_latitude], null=True, blank=True, default=0.0)
    pickup_lng = models.FloatField(validators=[validate_longitude], null=True, blank=True, default=0.0)
    destination = models.CharField(max_length=255, blank=True, default="")
    destination_lat = models.FloatField(validators=[validate_latitude], null=True, blank=True, default=0.0)
    destination_lng = models.FloatField(validators=[validate_longitude], null=True, blank=True, default=0.0)
    estimated_distance_km = models.FloatField(null=True, blank=True, default=0.0)
    estimated_duration_min = models.IntegerField(null=True, blank=True, default=0)
    notes = models.CharField(max_length=500, blank=True, help_text="Instructions for the rider.")

    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="pending", db_index=True)
    assigned_rider = models.ForeignKey(RiderProfile, on_delete=models.PROTECT, related_name="assigned_bookings",
                                       null=True, blank=True)
    accepted_at = models.DateTimeField(null=True, blank=True)
    start_code = models.CharField(max_length=4, default=generate_pin, editable=False,
                                  help_text="Customer reads this to the rider to start the trip (anti-fraud).")

    # Money
    fare_before_discount = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0.00"))
    surge_multiplier = models.DecimalField(max_digits=4, decimal_places=2, default=Decimal("1.00"))
    promo_code = models.ForeignKey(PromoCode, null=True, blank=True, on_delete=models.SET_NULL, related_name="bookings")
    discount_amount = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0.00"),
                                          validators=[validate_non_negative_amount])
    total_fare = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0.00"),
                                     validators=[validate_non_negative_amount], help_text="What the customer pays.")
    platform_commission = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0.00"),
                                              validators=[validate_non_negative_amount])
    rider_payout = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0.00"),
                                       validators=[validate_non_negative_amount])
    tip_amount = models.DecimalField(max_digits=8, decimal_places=2, default=Decimal("0.00"),
                                     validators=[validate_non_negative_amount])
    cancellation_fee = models.DecimalField(max_digits=8, decimal_places=2, default=Decimal("0.00"))

    payment_status = models.CharField(max_length=30, choices=PAYMENT_STATUS, default="initiated", db_index=True)
    payment_method = models.CharField(max_length=20, choices=PAYMENT_METHOD_CHOICES, null=True, blank=True)
    payment_ref = models.CharField(max_length=100, blank=True, db_index=True)
    provider_ref = models.CharField(max_length=255, blank=True)

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    scheduled_at = models.DateTimeField(null=True, blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    cancelled_at = models.DateTimeField(null=True, blank=True)
    cancelled_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="+")
    cancelled_by_role = models.CharField(max_length=10, blank=True)
    cancellation_reason = models.TextField(blank=True)

    
    class Status(models.TextChoices):
        #NOTE: the `status` field above uses STATUS_CHOICES ("searching_rider", ...), not this enum.
        SEARCHING = "searching", "Searching"
        ASSIGNED = "assigned", "Assigned"
        NO_RIDER_FOUND = "no_rider_found", "No Rider Found"
        # Add your existing statuses below here if you have them:
        PENDING = "pending", "Pending"
        CONFIRMED = "confirmed", "Confirmed"
        IN_PROGRESS = "in_progress", "In Progress"
        COMPLETED = "completed", "Completed"
        CANCELLED = "cancelled", "Cancelled"

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["customer", "-created_at"]),
            models.Index(fields=["assigned_rider", "status"]),
            models.Index(fields=["status", "created_at"]),
            models.Index(fields=["payment_ref"]),
            models.Index(fields=["zone", "status"]),
            models.Index(fields=["service_type", "status", "created_at"]),
            models.Index(fields=["status", "completed_at"]),
        ]
        constraints = [
            models.UniqueConstraint(fields=["payment_ref"], condition=~Q(payment_ref=""),
                                    name="unique_booking_payment_ref"),
            models.UniqueConstraint(fields=["subscription", "scheduled_at"], condition=Q(subscription__isnull=False),
                                    name="unique_subscription_pickup"),
        ]

    def __str__(self):
        return f"{self.booking_ref} — {self.get_status_display()}"

    # ---- fare --------------------------------------------------------------
    def clean(self):
        super().clean()
        validate_fare_breakdown(self.total_fare, self.platform_commission, self.rider_payout)

    def apply_fare_split(self, rate=None):
        rate = rate if rate is not None else (self.assigned_rider.commission_rate if self.assigned_rider_id else COMMISSION_RATE)
        self.platform_commission, self.rider_payout = compute_fare_split(self.total_fare, rate)

    def save(self, *args, **kwargs):
        if self.total_fare > 0 and self.platform_commission == 0 and self.rider_payout == 0:
            self.apply_fare_split()
        if self.fare_before_discount == 0 and self.total_fare > 0 and self.discount_amount == 0:
            self.fare_before_discount = self.total_fare
        if self._state.adding and not self.zone_id and self.pickup_lat and self.pickup_lng:
            self.zone = Zone.for_point(self.pickup_lat, self.pickup_lng)
        super().save(*args, **kwargs)

    def apply_quote(self, quote):
        """quote = PricingRule.quote(...). Call before saving a new booking."""
        self.fare_before_discount = quote["subtotal"] * quote["surge_multiplier"]
        self.fare_before_discount = round_money(self.fare_before_discount)
        self.surge_multiplier = quote["surge_multiplier"]
        self.total_fare = quote["total_fare"] - self.discount_amount
        self.platform_commission = self.rider_payout = Decimal("0.00")  # recomputed on save

    def get_distance_km(self, from_lat, from_lng, to_lat, to_lng):
        return haversine_km(from_lat, from_lng, to_lat, to_lng)

    # ---- promo -------------------------------------------------------------
    def apply_promo(self, code):
        """Validate + redeem atomically. Returns (ok, message)."""
        if not self.customer_id:
            return False, "Promo codes need a customer account."
        with transaction.atomic():
            try:
                promo = PromoCode.objects.select_for_update().get(code=(code or "").strip().upper())
            except PromoCode.DoesNotExist:
                return False, "Invalid promo code."
            if self.promo_code_id:
                return False, "A promo code is already applied."
            base = self.fare_before_discount or self.total_fare
            ok, msg, discount = promo.validate_for(self.customer, self.service_type, base)
            if not ok:
                return False, msg
            if self._state.adding:
                self.save()
            PromoRedemption.objects.create(promo=promo, customer=self.customer, booking=self, discount_amount=discount)
            PromoCode.objects.filter(pk=promo.pk).update(redeemed_count=F("redeemed_count") + 1)
            self.fare_before_discount, self.promo_code, self.discount_amount = base, promo, discount
            self.total_fare = base - discount
            self.apply_fare_split()
            self.save(update_fields=["fare_before_discount", "promo_code", "discount_amount", "total_fare",
                                     "platform_commission", "rider_payout"])
        return True, f"{promo.code} applied: -GH₵{discount}"

    def release_promo(self):
        red = PromoRedemption.objects.filter(booking=self).first()
        if red:
            PromoCode.objects.filter(pk=red.promo_id, redeemed_count__gt=0).update(redeemed_count=F("redeemed_count") - 1)
            red.delete()

    # ---- lifecycle ---------------------------------------------------------
    def _log(self, from_status, to_status, by=None, note=""):
        BookingStatusHistory.objects.create(booking=self, from_status=from_status, to_status=to_status,
                                            changed_by=by, note=note)

    def mark_searching(self):
        with transaction.atomic():
            b = Booking.objects.select_for_update().get(pk=self.pk)
            if "searching_rider" not in STATUS_TRANSITIONS.get(b.status, []):
                return False, f"Cannot search from '{b.status}'"
            old = b.status
            b.status = "searching_rider"
            b.save(update_fields=["status"])
            self._log(old, "searching_rider")
            self.status = b.status
        return True, "Searching for a rider"

    def assign_rider(self, rider):
        """Atomic assignment — locks rider AND booking so nobody is double-booked."""
        with transaction.atomic():
            rider = RiderProfile.objects.select_for_update().get(pk=rider.pk)
            b = Booking.objects.select_for_update().get(pk=self.pk)
            if b.status not in ("pending", "searching_rider"):
                return False, "Booking no longer available"
            if not rider.can_accept_jobs:
                return False, "Rider not eligible"
            if not rider.is_online:
                return False, "Rider is offline"
            if not rider.is_qualified_for(b.service_type):
                return False, "Rider is not qualified for this service"
            if b.customer_id and CustomerRiderPreference.objects.filter(
                    customer_id=b.customer_id, rider=rider, preference="blocked").exists():
                return False, "Rider unavailable for this customer"
            if Booking.objects.filter(assigned_rider=rider, status__in=self.ACTIVE_STATUSES).exclude(pk=b.pk).exists():
                return False, "Rider has an active booking already"
            if b.payment_method == "cash" and not rider.can_take_cash_jobs:
                return False, "Top up your wallet to accept cash jobs"

            old = b.status
            b.assigned_rider, b.status, b.accepted_at = rider, "assigned", timezone.now()
            b.apply_fare_split(rider.commission_rate)
            b.save(update_fields=["assigned_rider", "status", "accepted_at", "platform_commission", "rider_payout"])
            self._log(old, "assigned", by=rider.user)
            self.assigned_rider, self.status, self.accepted_at = rider, b.status, b.accepted_at
            self.platform_commission, self.rider_payout = b.platform_commission, b.rider_payout
        if b.customer_id:
            Notification.notify(b.customer.user, "Rider on the way",
                                f"{rider.full_name} accepted your request. Start code: {b.start_code}",
                                category=Notification.Category.BOOKING, data={"booking": b.booking_ref})
        return True, "Rider assigned successfully"

    def release_rider(self, reason=""):
        """Rider drops out before starting: put the booking back into dispatch."""
        with transaction.atomic():
            b = Booking.objects.select_for_update().get(pk=self.pk)
            if b.status != "assigned" or not b.assigned_rider_id:
                return False, "Nothing to release"
            rider = b.assigned_rider
            b.assigned_rider, b.status, b.accepted_at = None, "searching_rider", None
            b.save(update_fields=["assigned_rider", "status", "accepted_at"])
            self._log("assigned", "searching_rider", note=reason or "Rider released")
            rider.record_trip_cancelled()
            self.assigned_rider, self.status = None, b.status
        return True, "Booking returned to dispatch"

    def start_booking(self, code=None, by=None):
        with transaction.atomic():
            b = Booking.objects.select_for_update().get(pk=self.pk)
            if b.status != "assigned":
                return False, "Booking must be assigned first"
            if SystemConfig.is_enabled("REQUIRE_START_CODE", default=True) and code != b.start_code:
                return False, "Incorrect start code"
            b.status, b.started_at = "in_progress", timezone.now()
            b.save(update_fields=["status", "started_at"])
            self._log("assigned", "in_progress", by=by)
            self.status, self.started_at = b.status, b.started_at
        return True, "Booking started"

    def complete_booking(self, payment_verified=False, provider_ref="", recipient_code=None, by=None):
        is_offline_pay = self.payment_method in ("cash", "invoice")
        if self.status == "completed":
            return True, "Booking already completed (idempotent)"
        if self.status != "in_progress":
            return False, "Booking not in progress"
        if not is_offline_pay:
            if not payment_verified:
                return False, "Payment must be verified before completing"
            if self.payment_status not in ("held", "released"):
                return False, "Payment not confirmed"
        parcel = getattr(self, "parcel_detail", None)
        if parcel and parcel.require_recipient_code and recipient_code != parcel.recipient_code:
            return False, "Incorrect recipient code"

        with transaction.atomic():
            b = Booking.objects.select_for_update().get(pk=self.pk)
            if b.status == "completed":
                return True, "Booking already completed (idempotent)"
            if b.status != "in_progress":
                return False, "Invalid state transition"

            b.status, b.completed_at = "completed", timezone.now()
            if provider_ref:
                b.provider_ref = provider_ref
            if is_offline_pay or b.payment_status == "held":
                b.payment_status = "released"
            b.save(update_fields=["status", "completed_at", "provider_ref", "payment_status"])
            self._log("in_progress", "completed", by=by)

            rider = b.assigned_rider
            if rider:
                wallet, _ = RiderWallet.objects.get_or_create(rider=rider)
                if b.payment_method == "cash":
                    wallet.debit_commission(b.platform_commission, booking_ref=b.booking_ref, related_booking=b)
                elif b.payment_method != "invoice" and b.rider_payout > 0:
                    wallet.credit_earnings(b.rider_payout, booking_ref=b.booking_ref,
                                           payment_verified=True, related_booking=b)
                elif b.payment_method == "invoice" and b.rider_payout > 0:
                    wallet.credit_earnings(b.rider_payout, booking_ref=b.booking_ref,
                                           payment_verified=True, related_booking=b)  # platform fronts payout
                rider.record_trip_completed()

            if b.customer_id:
                b.customer.record_completed_booking(b)
                acct, _ = LoyaltyAccount.objects.get_or_create(customer=b.customer)
                acct.earn(b.total_fare, booking=b)
                Referral.qualify_for_booking(b)
            if b.payment_method == "invoice" and b.business_account_id:
                b.business_account.charge(b.total_fare)

            self.status, self.completed_at, self.payment_status = b.status, b.completed_at, b.payment_status
        if b.customer_id:
            Notification.notify(b.customer.user, "Trip complete",
                                f"Thanks for riding with RYDNOW. Total: GH₵{b.total_fare}. Rate your rider!",
                                category=Notification.Category.BOOKING, data={"booking": b.booking_ref})
        return True, "Booking completed & earnings credited"

    def cancel_booking(self, cancelled_by_user=None, reason="", by_role="customer"):
        if self.status in ("completed", "cancelled"):
            return False, "Cannot cancel completed or already cancelled booking"
        with transaction.atomic():
            b = Booking.objects.select_for_update().get(pk=self.pk)
            if b.status in ("completed", "cancelled"):
                return False, "Cannot cancel booking"

            fee = Decimal("0.00")
            if by_role == "customer" and b.status in self.ACTIVE_STATUSES and b.total_fare > 0:
                rule = PricingRule.get_rule(b.service_type, b.zone)
                if rule:
                    fee = min(rule.cancellation_fee, b.total_fare)

            old, rider = b.status, b.assigned_rider
            b.status, b.cancelled_at, b.cancelled_by = "cancelled", timezone.now(), cancelled_by_user
            b.cancelled_by_role, b.cancellation_reason, b.cancellation_fee = by_role, reason, fee
            b.save(update_fields=["status", "cancelled_at", "cancelled_by", "cancelled_by_role",
                                  "cancellation_reason", "cancellation_fee"])
            self._log(old, "cancelled", by=cancelled_by_user, note=f"{by_role}: {reason}")
            b.release_promo()

            if rider and by_role == "rider":
                rider.record_trip_cancelled()

            paid_digitally = b.payment_status in ("held", "released") and b.payment_method not in ("cash", "invoice")
            if paid_digitally:
                if fee > 0 and rider:  # compensate the rider for the wasted trip
                    comp = compute_fare_split(fee, rider.commission_rate)[1]
                    wallet, _ = RiderWallet.objects.get_or_create(rider=rider)
                    wallet.credit_earnings(comp, booking_ref=b.booking_ref, payment_verified=True,
                                           reference=f"CXL-{b.booking_ref}", description="Cancellation compensation",
                                           related_booking=b)
                refundable = b.total_fare - fee
                if refundable > 0:
                    b.process_refund(amount=refundable, reason=f"Booking cancelled: {reason}",
                                     initiated_by=cancelled_by_user)
            elif fee > 0 and b.customer_id:  # cash customer: add to outstanding fees
                CustomerProfile.objects.filter(pk=b.customer_id).update(outstanding_fees=F("outstanding_fees") + fee)

            self.status, self.cancelled_at, self.cancellation_fee = b.status, b.cancelled_at, fee
            self.payment_status = Booking.objects.values_list("payment_status", flat=True).get(pk=b.pk)
        return True, "Booking cancelled"

    # ---- payments ----------------------------------------------------------
    def confirm_payment_verified(self, provider_ref, payment_ref=None):
        with transaction.atomic():
            b = Booking.objects.select_for_update().get(pk=self.pk)
            if b.payment_status in ("held", "released"):
                if payment_ref and b.payment_ref == payment_ref:
                    return True, "Payment already verified (idempotent)"
                return False, "Booking already paid with a different reference"
            if b.payment_status in ("refund_initiated", "partially_refunded", "refunded"):
                return False, "Booking has been refunded"
            if payment_ref and b.payment_ref and payment_ref != b.payment_ref:
                return False, "Reference mismatch: cannot change an existing payment reference"
            if payment_ref:
                b.payment_ref = payment_ref
            b.provider_ref = provider_ref or ""
            b.payment_status = "held"
            fields = ["payment_ref", "provider_ref", "payment_status"]
            if b.status == "pending":
                b.status = "searching_rider"
                fields.append("status")
                self._log("pending", "searching_rider", note="Payment confirmed")
            b.save(update_fields=fields)
            self.payment_status, self.payment_ref, self.status = b.payment_status, b.payment_ref, b.status
        return True, "Payment verified – held securely until ride completion"

    def pay_with_wallet(self):
        """Wallet checkout: debit customer wallet and move straight to dispatch."""
        if not self.customer_id:
            return False, "No customer on booking"
        ok, msg = self.customer.get_wallet().pay_for_booking(self.total_fare, self)
        if not ok:
            return False, msg
        self.payment_method = "wallet"
        self.save(update_fields=["payment_method"])
        return self.confirm_payment_verified("wallet", payment_ref=f"PAYWAL-{self.booking_ref}")

    def add_tip(self, amount):
        """100% of a tip goes to the rider. Paid from the customer wallet after completion."""
        amount = round_money(amount)
        if self.status != "completed" or not self.assigned_rider_id or not self.customer_id:
            return False, "Tips are only possible on completed trips."
        if self.tip_amount > 0:
            return False, "A tip was already added."
        ok, msg = self.customer.get_wallet()._apply(
            "tip", lambda w: (-amount, -min(w.withdrawable_balance, amount), Decimal("0"), -amount),
            reference=f"TIPPAY-{self.booking_ref}", description="Tip", related_booking=self)
        if not ok:
            return False, msg
        wallet, _ = RiderWallet.objects.get_or_create(rider=self.assigned_rider)
        wallet.credit_earnings(amount, booking_ref=self.booking_ref, payment_verified=True, tx_type="tip",
                               reference=f"TIP-{self.booking_ref}", description="Customer tip", related_booking=self)
        Booking.objects.filter(pk=self.pk).update(tip_amount=amount)
        self.tip_amount = amount
        return True, "Thank you — your rider received 100% of the tip"

    def process_refund(self, amount=None, reason="", provider_confirm_callback=None, initiated_by=None):
        """
        Wallet payments are refunded instantly. Provider payments call provider_confirm_callback
        (amount=, original_ref=, reference=) -> bool; with no callback the Refund stays PENDING for a worker.
        """
        with transaction.atomic():
            b = Booking.objects.select_for_update().get(pk=self.pk)
            if b.payment_status not in ("held", "released", "partially_refunded", "refund_initiated"):
                return False, "No refundable funds for this booking"
            method = b.payment_method
            if method in ("cash", "invoice") or not method:
                return False, "Cash/invoice bookings have no digital funds to refund"
            if method in PROVIDER_METHODS and not (b.payment_ref or "").strip():
                return False, "Original payment reference missing – cannot submit refund"

            refund_amount = round_money(b.total_fare if amount is None else amount)
            if refund_amount <= Decimal("0"):
                return False, "Refund amount must be positive"
            already = Refund.objects.filter(booking=b, status__in=["pending", "completed"]) \
                .aggregate(s=Sum("amount"))["s"] or Decimal("0")
            if already + refund_amount > b.total_fare:
                return False, "Refund would exceed the amount paid"

            refund = Refund.objects.create(booking=b, amount=refund_amount, method=method,
                                           reason=reason[:255], initiated_by=initiated_by)
            if method == "wallet":
                ok, msg = b.customer.get_wallet().refund_to_wallet(
                    refund_amount, booking=b, reference=f"RFWAL-{refund.reference}", description=f"Refund: {reason}")
                if not ok:
                    refund.mark_failed(msg)
                    return False, msg
                refund.mark_completed()
            elif callable(provider_confirm_callback):
                try:
                    success = bool(provider_confirm_callback(amount=refund_amount, original_ref=b.payment_ref,
                                                             reference=refund.reference))
                except Exception:
                    logger.exception("Refund callback failed for %s", refund.reference)
                    success = False
                if not success:
                    refund.mark_failed("Provider refund failed")
                    return False, "Provider refund failed – refund not marked complete"
                refund.mark_completed()
            else:
                b.payment_status = "refund_initiated"
                b.save(update_fields=["payment_status"])
                self.payment_status = b.payment_status
                return True, f"Refund queued: {refund.reference}"
            self.payment_status = Booking.objects.values_list("payment_status", flat=True).get(pk=b.pk)
        return True, f"Refund processed: {refund.reference}"


class BookingStatusHistory(models.Model):
    """Full audit trail of every state change — essential for disputes and ops analytics."""
    booking = models.ForeignKey(Booking, on_delete=models.CASCADE, related_name="status_history")
    from_status = models.CharField(max_length=20, blank=True)
    to_status = models.CharField(max_length=20)
    changed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    note = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]
        indexes = [models.Index(fields=["booking", "created_at"])]
        verbose_name_plural = "Booking status history"

    def __str__(self):
        return f"{self.booking_id}: {self.from_status} → {self.to_status}"


class ParcelDetail(models.Model):
    class Size(models.TextChoices):
        DOCUMENT = "document", "Document / Envelope"
        SMALL = "small", "Small (shoebox)"
        MEDIUM = "medium", "Medium (backpack)"
        LARGE = "large", "Large (bulky)"

    booking = models.OneToOneField(Booking, on_delete=models.CASCADE, related_name="parcel_detail")
    size = models.CharField(max_length=10, choices=Size.choices, default=Size.SMALL)
    weight_kg = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    description = models.CharField(max_length=255, blank=True)
    declared_value = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0.00"))
    is_fragile = models.BooleanField(default=False)
    recipient_name = models.CharField(max_length=150)
    recipient_phone = models.CharField(max_length=30, validators=[phone_validator])
    require_recipient_code = models.BooleanField(default=True)
    recipient_code = models.CharField(max_length=4, default=generate_pin, editable=False)
    # Cash on delivery — a big unlock for Ghana's social-commerce sellers
    cod_amount = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0.00"),
                                     validators=[validate_non_negative_amount])
    cod_collected_at = models.DateTimeField(null=True, blank=True)
    cod_remitted_at = models.DateTimeField(null=True, blank=True, help_text="When the seller was paid out.")
    proof_of_delivery = models.ImageField(upload_to="pod/%Y/%m/", null=True, blank=True)
    delivered_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"Parcel for {self.recipient_name} ({self.booking_id})"

    @property
    def is_cod(self):
        return self.cod_amount > 0


class BookingOffer(models.Model):
    """One dispatch offer to one rider; feeds acceptance/response metrics and supports sequential or wave dispatch."""
    class Status(models.TextChoices):
        SENT = "sent", "Sent"
        ACCEPTED = "accepted", "Accepted"
        DECLINED = "declined", "Declined"
        EXPIRED = "expired", "Expired"

    booking = models.ForeignKey(Booking, on_delete=models.CASCADE, related_name="offers")
    rider = models.ForeignKey(RiderProfile, on_delete=models.CASCADE, related_name="offers")
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.SENT, db_index=True)
    distance_to_pickup_km = models.FloatField(null=True, blank=True)
    sent_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    responded_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-sent_at"]
        constraints = [models.UniqueConstraint(fields=["booking", "rider"], name="unique_offer_per_rider_booking")]
        indexes = [models.Index(fields=["rider", "status"]), models.Index(fields=["booking", "status"]),
                   models.Index(fields=["status", "expires_at"])]

    def __str__(self):
        return f"Offer {self.booking_id} → {self.rider_id} ({self.status})"

    @classmethod
    def send(cls, booking, rider, ttl_seconds=20):
        dist = rider.get_distance_km(booking.pickup_lat, booking.pickup_lng)
        offer, _ = cls.objects.get_or_create(
            booking=booking, rider=rider,
            defaults={"expires_at": timezone.now() + timedelta(seconds=ttl_seconds), "distance_to_pickup_km": dist})
        Notification.notify(rider.user, "New job nearby", f"{booking.get_service_type_display()} request.",
                            category=Notification.Category.BOOKING, data={"offer": offer.pk})
        return offer

    def _close(self, status):
        with transaction.atomic():
            o = BookingOffer.objects.select_for_update().get(pk=self.pk)
            if o.status != self.Status.SENT:
                return False
            o.status, o.responded_at = status, timezone.now()
            o.save(update_fields=["status", "responded_at"])
            secs = (o.responded_at - o.sent_at).total_seconds()
            o.rider.record_offer(status, None if status == self.Status.EXPIRED else secs)
            self.status, self.responded_at = o.status, o.responded_at
            return True

    def accept(self):
        if self.expires_at < timezone.now():
            self._close(self.Status.EXPIRED)
            return False, "Offer expired"
        if not self._close(self.Status.ACCEPTED):
            return False, "Offer already answered"
        ok, msg = self.booking.assign_rider(self.rider)
        if ok:
            BookingOffer.objects.filter(booking=self.booking, status=self.Status.SENT).exclude(pk=self.pk) \
                .update(status=self.Status.EXPIRED, responded_at=timezone.now())
        return ok, msg

    def decline(self):
        return self._close(self.Status.DECLINED), "Offer declined"

    @classmethod
    def expire_stale(cls):
        """Celery beat (every ~10s): mark unanswered offers expired so metrics stay honest."""
        n = 0
        for o in cls.objects.filter(status=cls.Status.SENT, expires_at__lt=timezone.now()).select_related("rider")[:500]:
            n += int(o._close(cls.Status.EXPIRED))
        return n


class Review(models.Model):
    class Direction(models.TextChoices):
        CUSTOMER_TO_RIDER = "c2r", "Customer rates rider"
        RIDER_TO_CUSTOMER = "r2c", "Rider rates customer"

    booking = models.ForeignKey(Booking, on_delete=models.CASCADE, related_name="reviews")
    direction = models.CharField(max_length=3, choices=Direction.choices)
    reviewer = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="reviews_written")
    rider = models.ForeignKey(RiderProfile, on_delete=models.CASCADE, related_name="reviews")
    customer = models.ForeignKey(CustomerProfile, null=True, blank=True, on_delete=models.CASCADE, related_name="reviews")
    rating = models.PositiveSmallIntegerField(validators=[MinValueValidator(1), MaxValueValidator(5)])
    tags = models.JSONField(default=list, blank=True, help_text='e.g. ["polite","fast","safe_riding"]')
    comment = models.CharField(max_length=500, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [models.UniqueConstraint(fields=["booking", "direction"], name="one_review_per_direction")]
        indexes = [models.Index(fields=["rider", "-created_at"])]

    def __str__(self):
        return f"{self.booking_id} {self.direction} {self.rating}★"

    def clean(self):
        if self.booking_id and self.booking.status != "completed":
            raise ValidationError("Only completed bookings can be reviewed.")

    def save(self, *args, **kwargs):
        adding = self._state.adding
        with transaction.atomic():
            super().save(*args, **kwargs)
            if adding:
                if self.direction == self.Direction.CUSTOMER_TO_RIDER:
                    self.rider.apply_rating(self.rating)
                    if self.rating <= 2:  # low score → automatic follow-up for support
                        SupportTicket.objects.create(
                            customer=self.booking.customer.user if self.booking.customer_id else None,
                            booking=self.booking, category=SupportTicket.Category.RIDER_FEEDBACK,
                            subject=f"Low rating ({self.rating}★) on {self.booking.booking_ref}",
                            description=self.comment or "Customer left a low rating.", priority="high")
                elif self.customer_id:
                    self.customer.apply_rating(self.rating)


class CustomerRiderPreference(models.Model):
    """Favourite a great rider (dispatch tries them first) or block a bad experience."""
    class Preference(models.TextChoices):
        FAVORITE = "favorite", "Favourite"
        BLOCKED = "blocked", "Blocked"

    customer = models.ForeignKey(CustomerProfile, on_delete=models.CASCADE, related_name="rider_preferences")
    rider = models.ForeignKey(RiderProfile, on_delete=models.CASCADE, related_name="customer_preferences")
    preference = models.CharField(max_length=10, choices=Preference.choices)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["customer", "rider"], name="one_preference_per_pair")]

    def __str__(self):
        return f"{self.customer} {self.preference} {self.rider}"


# =====================================================================
# 7. SERVICE QUALITY & TRUST — complaints, incidents, support, SOS
# =====================================================================
class ComplaintError(Exception):
    """Raised when a complaint action is not allowed."""


class RiderComplaintQuerySet(models.QuerySet):
    def open(self):
        return self.exclude(status__in=RiderComplaint.FINAL_STATUSES)

    def closed(self):
        return self.filter(status__in=RiderComplaint.FINAL_STATUSES)

    def overdue(self):
        return self.open().filter(sla_due_at__lt=timezone.now())

    def unassigned(self):
        return self.open().filter(assigned_to__isnull=True)

    def for_booking(self, booking):
        return self.filter(booking=booking)

    def for_rider(self, rider):
        return self.filter(booking__assigned_rider=rider)

    def urgent(self):
        return self.open().filter(priority=RiderComplaint.Priority.URGENT)


class RiderComplaint(models.Model):
    class ComplaintType(models.TextChoices):
        LATE_DELIVERY = "late_delivery", "Late Delivery / Pickup"
        NO_SHOW = "no_show", "Rider No-Show"
        RUDE_BEHAVIOUR = "rude_behaviour", "Rude or Unprofessional Behaviour"
        DAMAGED_ITEM = "damaged_item", "Damaged Item"
        OVERCHARGE = "overcharge", "Overcharge / Extra Payment Request"
        SAFETY = "safety", "Safety Concern"
        HARASSMENT = "harassment", "Harassment"
        FRAUD = "fraud", "Suspected Fraud"
        OTHER = "other", "Other"

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        UNDER_REVIEW = "under_review", "Under Review"
        AWAITING_INFO = "awaiting_info", "Awaiting Information"
        ESCALATED = "escalated", "Escalated"
        RESOLVED = "resolved", "Resolved"
        WARNING_ISSUED = "warning_issued", "Warning Issued"
        RESTRICTED = "restricted", "Rider Restricted"
        DISMISSED = "dismissed", "Dismissed"
        WITHDRAWN = "withdrawn", "Withdrawn by Complainant"

    class Priority(models.TextChoices):
        LOW = "low", "Low"
        NORMAL = "normal", "Normal"
        HIGH = "high", "High"
        URGENT = "urgent", "Urgent"

    FINAL_STATUSES = {Status.RESOLVED, Status.WARNING_ISSUED, Status.RESTRICTED, Status.DISMISSED, Status.WITHDRAWN}
    REVIEWED_STATUSES = {Status.RESOLVED, Status.WARNING_ISSUED, Status.RESTRICTED, Status.DISMISSED}
    _OPEN = {Status.UNDER_REVIEW, Status.AWAITING_INFO, Status.ESCALATED}
    _OUTCOMES = {Status.RESOLVED, Status.WARNING_ISSUED, Status.RESTRICTED, Status.DISMISSED}
    ALLOWED_TRANSITIONS = {
        Status.PENDING: _OPEN | _OUTCOMES | {Status.WITHDRAWN},
        Status.UNDER_REVIEW: _OUTCOMES | {Status.AWAITING_INFO, Status.ESCALATED, Status.WITHDRAWN},
        Status.AWAITING_INFO: {Status.UNDER_REVIEW, Status.WITHDRAWN},
        Status.ESCALATED: {Status.UNDER_REVIEW} | _OUTCOMES,
        Status.RESOLVED: {Status.UNDER_REVIEW},
        Status.WARNING_ISSUED: {Status.UNDER_REVIEW},
        Status.RESTRICTED: {Status.UNDER_REVIEW},
        Status.DISMISSED: {Status.UNDER_REVIEW},
        Status.WITHDRAWN: set(),
    }
    SLA_HOURS = {Priority.URGENT: 4, Priority.HIGH: 24, Priority.NORMAL: 72, Priority.LOW: 168}
    URGENT_TYPES = {ComplaintType.SAFETY, ComplaintType.HARASSMENT}

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    reference = models.CharField(max_length=30, unique=True, editable=False, blank=True)
    booking = models.ForeignKey(Booking, on_delete=models.PROTECT, related_name="complaints")
    complainant = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="filed_complaints")
    complaint_type = models.CharField(max_length=40, choices=ComplaintType.choices, db_index=True)
    description = models.TextField()
    priority = models.CharField(max_length=10, choices=Priority.choices, default=Priority.NORMAL, db_index=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING, db_index=True)
    assigned_to = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="assigned_complaints")
    sla_due_at = models.DateTimeField(null=True, blank=True, editable=False, db_index=True)
    first_response_at = models.DateTimeField(null=True, blank=True, editable=False)
    resolution_notes = models.TextField(blank=True)
    action_taken = models.CharField(max_length=100, blank=True)
    reviewed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="reviewed_complaints")
    reviewed_at = models.DateTimeField(null=True, blank=True)
    reopen_count = models.PositiveSmallIntegerField(default=0, editable=False)
    csat_score = models.PositiveSmallIntegerField(null=True, blank=True, validators=[MinValueValidator(1), MaxValueValidator(5)],
                                                  help_text="Customer satisfaction with how we handled it.")
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = RiderComplaintQuerySet.as_manager()

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Rider Complaint"
        verbose_name_plural = "Rider Complaints"
        indexes = [
            models.Index(fields=["booking", "status"]),
            models.Index(fields=["status", "created_at"]),
            models.Index(fields=["status", "priority", "sla_due_at"]),
            models.Index(fields=["assigned_to", "status"]),
        ]

    def __str__(self):
        return f"{self.reference} | {self.get_complaint_type_display()} | {self.get_status_display()}"

    @property
    def rider(self):
        return self.booking.assigned_rider

    @property
    def is_open(self):
        return self.status not in self.FINAL_STATUSES

    @property
    def is_overdue(self):
        return self.is_open and self.sla_due_at is not None and timezone.now() > self.sla_due_at

    @property
    def hours_until_sla(self):
        if not self.is_open or not self.sla_due_at:
            return None
        return round((self.sla_due_at - timezone.now()).total_seconds() / 3600, 1)

    def clean(self):
        super().clean()
        if self.status in self.REVIEWED_STATUSES and not self.resolution_notes.strip():
            raise ValidationError({"resolution_notes": "Resolution notes are required to close a complaint."})
        if self.reviewed_by_id and self.reviewed_by_id == self.complainant_id:
            raise ValidationError({"reviewed_by": "A complainant cannot review their own complaint."})

    @staticmethod
    def _generate_reference():
        return f"CMP-{timezone.now():%Y%m%d}-{uuid.uuid4().hex[:6].upper()}"

    def save(self, *args, **kwargs):
        if self._state.adding:
            if not self.reference:
                self.reference = self._generate_reference()
            if self.complaint_type in self.URGENT_TYPES:
                self.priority = self.Priority.URGENT
            self.sla_due_at = timezone.now() + timedelta(hours=self.SLA_HOURS[self.priority])
        super().save(*args, **kwargs)

    def _log(self, event_type, actor, from_status="", to_status="", note="", is_internal=True):
        return ComplaintEvent.objects.create(complaint=self, event_type=event_type, actor=actor,
                                             from_status=from_status, to_status=to_status,
                                             note=note, is_internal=is_internal)

    @staticmethod
    def _assert_staff(user):
        if not user or not getattr(user, "is_staff", False):
            raise ComplaintError("Only staff members can perform this action.")

    def _transition(self, new_status, actor, notes="", action_taken="", staff_only=True):
        if staff_only:
            self._assert_staff(actor)
        with transaction.atomic():
            c = type(self).objects.select_for_update().get(pk=self.pk)
            if new_status not in c.ALLOWED_TRANSITIONS.get(c.status, set()):
                raise ComplaintError(f"Cannot move complaint from '{c.status}' to '{new_status}'.")
            old_status, c.status = c.status, new_status
            if new_status in c.REVIEWED_STATUSES:
                c.reviewed_by, c.reviewed_at = actor, timezone.now()
                c.resolution_notes, c.action_taken = notes, action_taken
            if c.first_response_at is None and staff_only:
                c.first_response_at = timezone.now()
            c.full_clean()
            c.save()
            c._log(ComplaintEvent.EventType.STATUS_CHANGE, actor, old_status, new_status, notes)
        self.refresh_from_db()
        if new_status in self._OUTCOMES:
            Notification.notify(self.complainant, "Your complaint has an update",
                                f"{self.reference}: {self.get_status_display()}.",
                                category=Notification.Category.SUPPORT, data={"complaint": self.reference})
        return self

    def assign(self, by, assignee):
        self._assert_staff(by)
        self._assert_staff(assignee)
        with transaction.atomic():
            previous = self.assigned_to
            self.assigned_to = assignee
            self.save(update_fields=["assigned_to", "updated_at"])
            self._log(ComplaintEvent.EventType.ASSIGNED, by,
                      note=f"Assigned to {assignee} (was {previous or 'unassigned'}).")
        return self

    def start_review(self, reviewer):
        if self.assigned_to_id is None:
            self.assign(reviewer, reviewer)
        return self._transition(self.Status.UNDER_REVIEW, reviewer, "Review started.")

    def request_info(self, reviewer, message):
        self._transition(self.Status.AWAITING_INFO, reviewer, message)
        self._log(ComplaintEvent.EventType.NOTE, reviewer, note=message, is_internal=False)
        return self

    def escalate(self, reviewer, reason, priority=None):
        self._transition(self.Status.ESCALATED, reviewer, reason)
        if priority:
            self.priority = priority
            self.sla_due_at = timezone.now() + timedelta(hours=self.SLA_HOURS[priority])
            self.save(update_fields=["priority", "sla_due_at", "updated_at"])
        return self

    def resolve_complaint(self, reviewer, resolution_notes, action_taken=""):
        return self._transition(self.Status.RESOLVED, reviewer, resolution_notes, action_taken)

    def issue_warning(self, reviewer, notes):
        self._transition(self.Status.WARNING_ISSUED, reviewer, notes, "warning_issued")
        if self.rider:
            self.rider.add_strike(reason=f"Complaint {self.reference}")
        return self

    def dismiss_complaint(self, reviewer, notes=""):
        return self._transition(self.Status.DISMISSED, reviewer, notes or "No action required.", "dismissed")

    def withdraw(self, user):
        if user.pk != self.complainant_id:
            raise ComplaintError("Only the complainant can withdraw this complaint.")
        return self._transition(self.Status.WITHDRAWN, user, "Withdrawn by complainant.", staff_only=False)

    def reopen(self, reviewer, reason):
        if not reason.strip():
            raise ComplaintError("A reason is required to reopen a complaint.")
        self._transition(self.Status.UNDER_REVIEW, reviewer, reason)
        type(self).objects.filter(pk=self.pk).update(reopen_count=F("reopen_count") + 1)
        self.refresh_from_db()
        return self

    def rate_handling(self, score):
        """Customer satisfaction (CSAT) after closure."""
        if self.is_open:
            raise ComplaintError("Rate the handling after the complaint is closed.")
        self.csat_score = score
        self.save(update_fields=["csat_score", "updated_at"])

    def restrict_rider(self, reviewer, notes, restricted_until=None):
        """Close as 'restricted' and flag the rider's profile, atomically."""
        if restricted_until and restricted_until <= timezone.now():
            raise ComplaintError("`restricted_until` must be in the future.")
        rider = self.rider
        if not rider:
            raise ComplaintError("No rider is assigned to this booking.")
        with transaction.atomic():
            self._transition(self.Status.RESTRICTED, reviewer, notes, "rider_restricted")
            RiderProfile.objects.filter(pk=rider.pk).update(
                is_restricted=True, restricted_until=restricted_until,
                restriction_notes=f"Complaint {self.reference}: {notes}")
            if rider.is_online:
                rider.refresh_from_db()
                rider.go_offline()
            rider.add_strike(reason=f"Complaint {self.reference}")
            self._log(ComplaintEvent.EventType.RIDER_RESTRICTED, reviewer,
                      note=f"Restricted until {restricted_until:%Y-%m-%d %H:%M}" if restricted_until
                      else "Restricted indefinitely.")
        return self

    def add_note(self, author, note, is_internal=True):
        if is_internal:
            self._assert_staff(author)
        return self._log(ComplaintEvent.EventType.NOTE, author, note=note, is_internal=is_internal)

    def rider_history(self):
        return (type(self).objects.for_rider(self.rider).exclude(pk=self.pk)
                if self.rider else type(self).objects.none())

    def rider_strike_count(self):
        return self.rider_history().filter(status__in=[self.Status.WARNING_ISSUED, self.Status.RESTRICTED]).count()


ALLOWED_EVIDENCE_EXTENSIONS = ["jpg", "jpeg", "png", "webp", "pdf", "mp4", "mov"]
MAX_EVIDENCE_SIZE_MB = 10


def validate_evidence_size(file):
    if file.size > MAX_EVIDENCE_SIZE_MB * 1024 * 1024:
        raise ValidationError(f"File too large. Maximum size is {MAX_EVIDENCE_SIZE_MB} MB.")


def complaint_evidence_path(instance, filename):
    ext = os.path.splitext(filename)[1].lower()
    return f"complaint-evidence/{instance.complaint.reference}/{uuid.uuid4()}{ext}"


class ComplaintEvidence(models.Model):
    complaint = models.ForeignKey(RiderComplaint, on_delete=models.CASCADE, related_name="evidence")
    file = models.FileField(upload_to=complaint_evidence_path,
                            validators=[FileExtensionValidator(allowed_extensions=ALLOWED_EVIDENCE_EXTENSIONS),
                                        validate_evidence_size])
    caption = models.CharField(max_length=200, blank=True)
    file_name = models.CharField(max_length=255, editable=False)
    file_type = models.CharField(max_length=100, blank=True, editable=False)
    file_size = models.PositiveIntegerField(null=True, blank=True, editable=False, help_text="Size in bytes.")
    uploaded_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                    related_name="complaint_evidence_uploads")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Complaint Evidence"
        verbose_name_plural = "Complaint Evidence"
        indexes = [models.Index(fields=["complaint", "-created_at"])]

    def save(self, *args, **kwargs):
        if self.file and not self.file_name:
            original = os.path.basename(self.file.name)
            self.file_name = original[:255]
            self.file_type = mimetypes.guess_type(original)[0] or ""
            self.file_size = getattr(self.file, "size", None)
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.complaint.reference} — {self.file_name}"


class ComplaintEvent(models.Model):
    """Append-only audit log for a complaint."""
    class EventType(models.TextChoices):
        STATUS_CHANGE = "status_change", "Status change"
        ASSIGNED = "assigned", "Assigned"
        NOTE = "note", "Note"
        RIDER_RESTRICTED = "rider_restricted", "Rider restricted"

    complaint = models.ForeignKey(RiderComplaint, on_delete=models.CASCADE, related_name="events")
    event_type = models.CharField(max_length=20, choices=EventType.choices, default=EventType.NOTE)
    from_status = models.CharField(max_length=30, blank=True)
    to_status = models.CharField(max_length=30, blank=True)
    note = models.TextField(blank=True)
    is_internal = models.BooleanField(default=True, help_text="Internal events are hidden from the complainant.")
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="complaint_events")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Complaint Event"
        verbose_name_plural = "Complaint Events"
        indexes = [models.Index(fields=["complaint", "-created_at"])]

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError("Complaint events cannot be modified.")
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.complaint.reference} — {self.to_status or self.get_event_type_display()}"


class RiderIncidentQuerySet(models.QuerySet):
    def open(self):
        return self.filter(status__in=RiderIncident.OPEN_STATUSES)

    def urgent(self):
        return self.open().filter(severity__in=[RiderIncident.Severity.HIGH, RiderIncident.Severity.CRITICAL])


class RiderIncident(models.Model):
    """An incident reported by, or involving, a rider. Tracked end to end by admins."""
    class IncidentType(models.TextChoices):
        ACCIDENT = "accident", "Accident"
        SAFETY_ISSUE = "safety_issue", "Safety Issue"
        VEHICLE_DAMAGE = "vehicle_damage", "Vehicle Damage"
        CUSTOMER_COMPLAINT = "customer_complaint", "Customer Complaint"
        OTHER = "other", "Other"

    class Severity(models.TextChoices):
        LOW = "low", "Low"
        MEDIUM = "medium", "Medium"
        HIGH = "high", "High"
        CRITICAL = "critical", "Critical"

    class Status(models.TextChoices):
        REPORTED = "reported", "Reported"
        UNDER_REVIEW = "under_review", "Under Review"
        RESOLVED = "resolved", "Resolved"
        CLOSED = "closed", "Closed"

    OPEN_STATUSES = (Status.REPORTED, Status.UNDER_REVIEW)
    FINAL_STATUSES = (Status.RESOLVED, Status.CLOSED)

    reference_code = models.CharField(max_length=24, unique=True, editable=False, default=generate_incident_reference,
                                      help_text="Auto-generated tracking number, quote it when talking to the rider.")
    rider_profile = models.ForeignKey(RiderProfile, on_delete=models.CASCADE, related_name="incidents")
    booking = models.ForeignKey(Booking, null=True, blank=True, on_delete=models.SET_NULL, related_name="incidents")
    service = models.ForeignKey(Service, null=True, blank=True, on_delete=models.SET_NULL, related_name="incidents",
                                help_text="Service the rider was doing (optional).")
    incident_type = models.CharField(max_length=30, choices=IncidentType.choices)
    severity = models.CharField(max_length=10, choices=Severity.choices, default=Severity.MEDIUM, db_index=True)
    title = models.CharField(max_length=200)
    description = models.TextField()
    location = models.CharField(max_length=255, blank=True, default="")
    incident_date = models.DateTimeField(default=timezone.now, help_text="When the incident actually happened.")
    date_reported = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    reported_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="reported_rider_incidents")
    assigned_to = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                    related_name="assigned_rider_incidents", limit_choices_to={"is_staff": True},
                                    help_text="Admin handling this incident.")
    status = models.CharField(max_length=30, choices=Status.choices, default=Status.REPORTED, db_index=True)
    resolution_notes = models.TextField(blank=True, default="")
    resolved_at = models.DateTimeField(null=True, blank=True)
    resolved_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="resolved_rider_incidents")

    objects = RiderIncidentQuerySet.as_manager()

    class Meta:
        ordering = ["-date_reported"]
        verbose_name = "Rider incident"
        verbose_name_plural = "Rider incidents"
        indexes = [models.Index(fields=["status", "-date_reported"]),
                   models.Index(fields=["rider_profile", "-date_reported"])]

    def __str__(self):
        return f"{self.reference_code} · {self.get_incident_type_display()}: {self.title}"

    @property
    def is_open(self):
        return self.status in self.OPEN_STATUSES

    def clean(self):
        super().clean()
        if self.incident_date and self.incident_date > timezone.now():
            raise ValidationError({"incident_date": "Incident date cannot be in the future."})
        if self.status in self.FINAL_STATUSES and not self.resolution_notes.strip():
            raise ValidationError({"resolution_notes": "Resolution notes are required to resolve or close an incident."})

    def save(self, *args, **kwargs):
        if self.status in self.FINAL_STATUSES:
            if not self.resolved_at:
                self.resolved_at = timezone.now()
        else:
            self.resolved_at, self.resolved_by = None, None
        super().save(*args, **kwargs)

    def take(self, admin):
        self.assigned_to, self.status = admin, self.Status.UNDER_REVIEW
        self.save(update_fields=["assigned_to", "status", "updated_at", "resolved_at", "resolved_by"])

    def resolve(self, admin, notes):
        self.status, self.resolution_notes, self.resolved_by = self.Status.RESOLVED, notes, admin
        self.full_clean()
        self.save()


class SupportTicket(models.Model):
    """Customer-service inbox: one thread per issue, SLA-tracked, with CSAT. Gold/Platinum customers jump the queue."""
    class Category(models.TextChoices):
        BOOKING = "booking", "Booking issue"
        PAYMENT = "payment", "Payment / wallet"
        REFUND = "refund", "Refund request"
        RIDER_FEEDBACK = "rider_feedback", "Rider feedback"
        ACCOUNT = "account", "Account"
        PROMO = "promo", "Promo / referral"
        OTHER = "other", "Other"

    class Status(models.TextChoices):
        OPEN = "open", "Open"
        PENDING_CUSTOMER = "pending_customer", "Waiting for customer"
        RESOLVED = "resolved", "Resolved"
        CLOSED = "closed", "Closed"

    SLA_HOURS = {"urgent": 2, "high": 12, "normal": 24, "low": 72}

    reference = models.CharField(max_length=30, unique=True, editable=False, default=generate_ticket_ref)
    customer = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="support_tickets")
    booking = models.ForeignKey(Booking, null=True, blank=True, on_delete=models.SET_NULL, related_name="support_tickets")
    category = models.CharField(max_length=20, choices=Category.choices, default=Category.OTHER, db_index=True)
    subject = models.CharField(max_length=200)
    description = models.TextField()
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.OPEN, db_index=True)
    priority = models.CharField(max_length=10, choices=[(k, k.title()) for k in SLA_HOURS], default="normal", db_index=True)
    channel = models.CharField(max_length=15, default="app",
                               choices=[("app", "App"), ("web", "Web"), ("phone", "Phone"), ("whatsapp", "WhatsApp")])
    assigned_to = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                    related_name="assigned_tickets", limit_choices_to={"is_staff": True})
    sla_due_at = models.DateTimeField(null=True, blank=True, editable=False, db_index=True)
    first_response_at = models.DateTimeField(null=True, blank=True, editable=False)
    resolved_at = models.DateTimeField(null=True, blank=True)
    csat_score = models.PositiveSmallIntegerField(null=True, blank=True, validators=[MinValueValidator(1), MaxValueValidator(5)])
    csat_comment = models.CharField(max_length=300, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["status", "priority", "sla_due_at"]),
                   models.Index(fields=["assigned_to", "status"])]

    def __str__(self):
        return f"{self.reference} · {self.subject}"

    @property
    def is_open(self):
        return self.status in (self.Status.OPEN, self.Status.PENDING_CUSTOMER)

    @property
    def is_overdue(self):
        return self.is_open and self.sla_due_at is not None and timezone.now() > self.sla_due_at

    def save(self, *args, **kwargs):
        if self._state.adding:
            profile = getattr(self.customer, "customer_profile", None) if self.customer_id else None
            if profile and self.priority == "normal" and profile.loyalty_tier in ("gold", "platinum"):
                self.priority = "high"
            if self.category == self.Category.PAYMENT and self.priority == "normal":
                self.priority = "high"  # money problems are never "normal"
            self.sla_due_at = timezone.now() + timedelta(hours=self.SLA_HOURS[self.priority])
        super().save(*args, **kwargs)

    def reply(self, author, message, is_internal=False, attachment=None):
        msg = TicketMessage.objects.create(ticket=self, author=author, message=message,
                                           is_internal=is_internal, attachment=attachment)
        if getattr(author, "is_staff", False) and not is_internal:
            updates = {"status": self.Status.PENDING_CUSTOMER}
            if self.first_response_at is None:
                updates["first_response_at"] = timezone.now()
            type(self).objects.filter(pk=self.pk).update(**updates)
            if self.customer_id:
                Notification.notify(self.customer, "Support replied", f"Update on {self.reference}.",
                                    category=Notification.Category.SUPPORT, data={"ticket": self.reference})
        elif not getattr(author, "is_staff", False) and self.status == self.Status.PENDING_CUSTOMER:
            type(self).objects.filter(pk=self.pk).update(status=self.Status.OPEN)
        return msg

    def resolve(self, by):
        self.status, self.resolved_at = self.Status.RESOLVED, timezone.now()
        self.save(update_fields=["status", "resolved_at", "updated_at"])
        TicketMessage.objects.create(ticket=self, author=by, message="Ticket resolved.", is_internal=True)

    def rate(self, score, comment=""):
        if self.is_open:
            raise ValidationError("Rate the support after the ticket is resolved.")
        self.csat_score, self.csat_comment = score, comment[:300]
        self.save(update_fields=["csat_score", "csat_comment", "updated_at"])


class TicketMessage(models.Model):
    ticket = models.ForeignKey(SupportTicket, on_delete=models.CASCADE, related_name="messages")
    author = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+")
    message = models.TextField()
    is_internal = models.BooleanField(default=False, help_text="Staff-only note.")
    attachment = models.FileField(upload_to="tickets/%Y/%m/", null=True, blank=True,
                                  validators=[FileExtensionValidator(["jpg", "jpeg", "png", "webp", "pdf"]),
                                              validate_evidence_size])
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]


class SafetyAlert(models.Model):
    """SOS button. Creating one raises a CRITICAL SystemAlert; ops should call the rider/customer and emergency contact."""
    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        ACKNOWLEDGED = "acknowledged", "Acknowledged"
        RESOLVED = "resolved", "Resolved"
        FALSE_ALARM = "false_alarm", "False alarm"

    triggered_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="safety_alerts")
    booking = models.ForeignKey(Booking, null=True, blank=True, on_delete=models.SET_NULL, related_name="safety_alerts")
    lat = models.FloatField(null=True, blank=True)
    lng = models.FloatField(null=True, blank=True)
    status = models.CharField(max_length=15, choices=Status.choices, default=Status.ACTIVE, db_index=True)
    message = models.CharField(max_length=300, blank=True)
    acknowledged_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    acknowledged_at = models.DateTimeField(null=True, blank=True)
    resolved_at = models.DateTimeField(null=True, blank=True)
    resolution_notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["status", "-created_at"])]

    def __str__(self):
        return f"SOS #{self.pk} ({self.status})"

    def save(self, *args, **kwargs):
        adding = self._state.adding
        super().save(*args, **kwargs)
        if adding:
            SystemAlert.create_alert(
                "critical", "SOS triggered",
                f"User {self.triggered_by_id} pressed SOS. Booking: {self.booking.booking_ref if self.booking_id else 'n/a'}. "
                f"Location: {self.lat},{self.lng}",
                booking_ref=self.booking.booking_ref if self.booking_id else None)

    def acknowledge(self, staff):
        self.status, self.acknowledged_by, self.acknowledged_at = self.Status.ACKNOWLEDGED, staff, timezone.now()
        self.save(update_fields=["status", "acknowledged_by", "acknowledged_at"])

    def resolve(self, staff, notes, false_alarm=False):
        self.status = self.Status.FALSE_ALARM if false_alarm else self.Status.RESOLVED
        self.resolution_notes, self.resolved_at, self.acknowledged_by = notes, timezone.now(), staff
        self.save(update_fields=["status", "resolution_notes", "resolved_at", "acknowledged_by"])


# =====================================================================
# 8. RIDER GROWTH — monthly performance commitments
# =====================================================================
class RiderPerformanceCommitQuerySet(models.QuerySet):
    def for_month(self, month):
        return self.filter(month=month.replace(day=1))

    def needing_support(self):
        return self.filter(
            performance_status__in=[RiderPerformanceCommit.PerformanceStatus.NEEDS_SUPPORT,
                                    RiderPerformanceCommit.PerformanceStatus.AT_RISK],
            is_exempt=False)

    def top_performers(self, limit=10):
        return (self.filter(is_exempt=False, performance_score__isnull=False)
                .order_by("-performance_score", "-completed_trips")[:limit])


class RiderPerformanceCommit(models.Model):
    """Monthly targets and results for a rider, plus recognition, coaching and bonus."""
    class PerformanceStatus(models.TextChoices):
        NO_DATA = "no_data", "No data yet"
        EXCELLENT = "excellent", "Excellent"
        ON_TRACK = "on_track", "On Track"
        NEEDS_SUPPORT = "needs_support", "Needs Support"
        AT_RISK = "at_risk", "At Risk"
        EXEMPT = "exempt", "Exempt"

    class ReviewStatus(models.TextChoices):
        DRAFT = "draft", "Draft (hidden from rider)"
        ACTIVE = "active", "Active"
        UNDER_REVIEW = "under_review", "Under Review"
        FINALIZED = "finalized", "Finalized"

    WEIGHT_TRIPS = 0.40
    WEIGHT_HOURS = 0.30
    WEIGHT_ACCEPTANCE = 0.20
    WEIGHT_RATING = 0.10

    rider_profile = models.ForeignKey(RiderProfile, on_delete=models.CASCADE, related_name="performance_commits")
    month = models.DateField(help_text="First day of the month this applies to.")
    target_trips = models.PositiveIntegerField(default=0)
    target_online_hours = models.PositiveIntegerField(default=0, help_text="Hours per week")
    min_acceptance_rate = models.FloatField(default=80.0, validators=[MinValueValidator(0), MaxValueValidator(100)],
                                            help_text="Minimum % acceptance")
    completed_trips = models.PositiveIntegerField(default=0)
    online_hours = models.FloatField(default=0.0, validators=[MinValueValidator(0)])
    acceptance_rate = models.FloatField(null=True, blank=True, validators=[MinValueValidator(0), MaxValueValidator(100)])
    average_rating = models.FloatField(null=True, blank=True, validators=[MinValueValidator(0), MaxValueValidator(5)])
    performance_score = models.FloatField(null=True, blank=True, editable=False)
    performance_status = models.CharField(max_length=20, choices=PerformanceStatus.choices,
                                          default=PerformanceStatus.NO_DATA, editable=False, db_index=True)
    is_exempt = models.BooleanField(default=False, help_text="Exempt from targets this month?")
    exempt_reason = models.CharField(max_length=255, blank=True, default="",
                                     help_text="Why the rider is exempt (e.g. sick leave, bike repair).")
    appreciation_message = models.TextField(blank=True, default="", help_text="Thank-you message shown to the rider.")
    bonus_amount = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0.00"),
                                       validators=[MinValueValidator(Decimal("0.00"))], help_text="Reward in GHS.")
    bonus_paid_at = models.DateTimeField(null=True, blank=True, editable=False)
    coaching_plan = models.TextField(blank=True, default="", help_text="Supportive plan shown to the rider.")
    follow_up_date = models.DateField(null=True, blank=True, help_text="When to check in again.")
    review_status = models.CharField(max_length=20, choices=ReviewStatus.choices, default=ReviewStatus.ACTIVE, db_index=True)
    notes = models.TextField(blank=True, default="", help_text="Internal notes (never shown to rider).")
    reviewed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                    related_name="reviewed_performance_commits")
    finalized_at = models.DateTimeField(null=True, blank=True, editable=False)
    rider_acknowledged_at = models.DateTimeField(null=True, blank=True, help_text="When the rider confirmed they saw this.")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = RiderPerformanceCommitQuerySet.as_manager()

    class Meta:
        ordering = ["-month"]
        verbose_name = "Rider performance commit"
        verbose_name_plural = "Rider performance commits"
        constraints = [models.UniqueConstraint(fields=["rider_profile", "month"], name="unique_rider_performance_month")]
        indexes = [models.Index(fields=["month", "-performance_score"]),
                   models.Index(fields=["month", "performance_status"])]

    def __str__(self):
        return f"{self.rider_profile} — {self.month:%B %Y}"

    @property
    def monthly_target_hours(self):
        if not self.month or not self.target_online_hours:
            return 0.0
        days = calendar.monthrange(self.month.year, self.month.month)[1]
        return round(self.target_online_hours * days / 7, 1)

    @staticmethod
    def _pct(actual, target):
        return round(actual / target * 100, 1) if target else None

    @property
    def trips_progress(self):
        return self._pct(self.completed_trips, self.target_trips)

    @property
    def hours_progress(self):
        return self._pct(self.online_hours, self.monthly_target_hours)

    @property
    def meets_acceptance(self):
        return None if self.acceptance_rate is None else self.acceptance_rate >= self.min_acceptance_rate

    @property
    def is_finalized(self):
        return self.review_status == self.ReviewStatus.FINALIZED

    def compute_score(self):
        parts = []
        if self.trips_progress is not None:
            parts.append((self.WEIGHT_TRIPS, min(self.trips_progress, 100)))
        if self.hours_progress is not None:
            parts.append((self.WEIGHT_HOURS, min(self.hours_progress, 100)))
        if self.acceptance_rate is not None:
            value = (min(self.acceptance_rate / self.min_acceptance_rate * 100, 100)
                     if self.min_acceptance_rate else self.acceptance_rate)
            parts.append((self.WEIGHT_ACCEPTANCE, value))
        if self.average_rating is not None:
            parts.append((self.WEIGHT_RATING, self.average_rating / 5 * 100))
        if not parts:
            return None
        total_weight = sum(w for w, _ in parts)
        return round(sum(w * v for w, v in parts) / total_weight, 1)

    def compute_status(self, score):
        S = self.PerformanceStatus
        if self.is_exempt:
            return S.EXEMPT
        if score is None:
            return S.NO_DATA
        return S.EXCELLENT if score >= 90 else S.ON_TRACK if score >= 70 else S.NEEDS_SUPPORT if score >= 50 else S.AT_RISK

    def refresh_performance(self):
        self.performance_score = self.compute_score()
        self.performance_status = self.compute_status(self.performance_score)

    def recalculate_actuals(self):
        """Pull this month's real numbers from bookings, offers, sessions and reviews."""
        start, end = self.month.replace(day=1), self.next_month(self.month)
        rider = self.rider_profile
        self.completed_trips = Booking.objects.filter(
            assigned_rider=rider, status="completed",
            completed_at__date__gte=start, completed_at__date__lt=end).count()
        secs = RiderOnlineSession.objects.filter(
            rider=rider, started_at__date__gte=start, started_at__date__lt=end
        ).aggregate(s=Sum("duration_seconds"))["s"] or 0
        self.online_hours = round(secs / 3600, 1)
        offers = BookingOffer.objects.filter(rider=rider, sent_at__date__gte=start, sent_at__date__lt=end)
        total = offers.count()
        self.acceptance_rate = round(offers.filter(status="accepted").count() / total * 100, 1) if total else None
        avg = Review.objects.filter(rider=rider, direction="c2r", created_at__date__gte=start,
                                    created_at__date__lt=end).aggregate(a=Avg("rating"))["a"]
        self.average_rating = round(avg, 2) if avg else None
        self.save()

    def clean(self):
        super().clean()
        errors = {}
        if self.month:
            self.month = self.month.replace(day=1)
            if self.month > timezone.localdate().replace(day=1) + timedelta(days=62):
                errors["month"] = "Month is too far in the future."
        if self.is_exempt and not self.exempt_reason.strip():
            errors["exempt_reason"] = "Give a reason when exempting a rider."
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        if self.month:
            self.month = self.month.replace(day=1)
        self.refresh_performance()
        if self.is_finalized:
            self.finalized_at = self.finalized_at or timezone.now()
        else:
            self.finalized_at = None
        super().save(*args, **kwargs)

    def finalize(self, by=None):
        self.review_status = self.ReviewStatus.FINALIZED
        if by:
            self.reviewed_by = by
        self.save()

    def reopen(self, by=None):
        self.review_status = self.ReviewStatus.UNDER_REVIEW
        if by:
            self.reviewed_by = by
        self.save()

    def acknowledge(self):
        if not self.rider_acknowledged_at:
            self.rider_acknowledged_at = timezone.now()
            self.save(update_fields=["rider_acknowledged_at", "updated_at"])

    def award_bonus(self):
        """Pay the monthly bonus once, only after finalisation."""
        if not self.is_finalized or self.bonus_amount <= 0 or self.bonus_paid_at:
            return False, "Nothing to pay"
        wallet, _ = RiderWallet.objects.get_or_create(rider=self.rider_profile)
        ok, msg = wallet.credit_earnings(self.bonus_amount, payment_verified=True, tx_type="bonus",
                                         reference=f"PERFBONUS-{self.pk}",
                                         description=f"Performance bonus {self.month:%b %Y}")
        if ok:
            self.bonus_paid_at = timezone.now()
            RiderPerformanceCommit.objects.filter(pk=self.pk).update(bonus_paid_at=self.bonus_paid_at)
            Notification.notify(self.rider_profile.user, "Bonus paid!",
                                self.appreciation_message or f"GH₵{self.bonus_amount} added to your wallet.",
                                category=Notification.Category.PAYMENT)
        return ok, msg

    @staticmethod
    def next_month(month):
        return (month.replace(day=1) + timedelta(days=32)).replace(day=1)

    @classmethod
    def generate_for_month(cls, month, rider_profiles=None):
        month = month.replace(day=1)
        riders = rider_profiles if rider_profiles is not None else RiderProfile.objects.active()
        rider_ids = list(riders.values_list("pk", flat=True))
        existing = set(cls.objects.filter(month=month, rider_profile_id__in=rider_ids)
                       .values_list("rider_profile_id", flat=True))
        previous_month = (month - timedelta(days=1)).replace(day=1)
        previous = {c.rider_profile_id: c
                    for c in cls.objects.filter(month=previous_month, rider_profile_id__in=rider_ids)}
        new_objects = []
        for rider_id in rider_ids:
            if rider_id in existing:
                continue
            prev = previous.get(rider_id)
            new_objects.append(cls(
                rider_profile_id=rider_id, month=month,
                target_trips=prev.target_trips if prev else 0,
                target_online_hours=prev.target_online_hours if prev else 0,
                min_acceptance_rate=prev.min_acceptance_rate if prev else 80.0,
                review_status=cls.ReviewStatus.ACTIVE))
        cls.objects.bulk_create(new_objects, ignore_conflicts=True)
        return len(new_objects)


# =====================================================================
# 9. PLATFORM OPS — notifications, devices, audit, alerts, daily summary
# =====================================================================
class Notification(models.Model):
    """In-app inbox row. A Celery task fans out push/SMS based on `channel` and the user's consent."""
    class Category(models.TextChoices):
        BOOKING = "booking", "Booking"
        PAYMENT = "payment", "Payment"
        PROMO = "promo", "Promotion"
        SAFETY = "safety", "Safety"
        SUPPORT = "support", "Support"
        SYSTEM = "system", "System"

    class Channel(models.TextChoices):
        IN_APP = "in_app", "In-app"
        PUSH = "push", "Push"
        SMS = "sms", "SMS"
        EMAIL = "email", "Email"

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="notifications")
    category = models.CharField(max_length=10, choices=Category.choices, default=Category.SYSTEM)
    channel = models.CharField(max_length=10, choices=Channel.choices, default=Channel.PUSH)
    title = models.CharField(max_length=150)
    body = models.CharField(max_length=500)
    data = models.JSONField(default=dict, blank=True)
    is_read = models.BooleanField(default=False, db_index=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["user", "is_read", "-created_at"])]

    def __str__(self):
        return f"{self.user_id}: {self.title}"

    @classmethod
    def notify(cls, user, title, body, category="system", channel="push", data=None):
        """Promo messages respect marketing consent; everything else is transactional."""
        if category == cls.Category.PROMO:
            profile = getattr(user, "customer_profile", None)
            if not (profile and profile.marketing_opt_in):
                return None
        return cls.objects.create(user=user, title=title[:150], body=body[:500], category=category,
                                  channel=channel, data=data or {})

    def mark_read(self):
        if not self.is_read:
            self.is_read = True
            self.save(update_fields=["is_read"])


class DeviceToken(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="device_tokens")
    token = models.CharField(max_length=255, unique=True)
    platform = models.CharField(max_length=10, choices=[("android", "Android"), ("ios", "iOS"), ("web", "Web")])
    app_version = models.CharField(max_length=20, blank=True)
    is_active = models.BooleanField(default=True, db_index=True)
    last_seen_at = models.DateTimeField(default=timezone.now)

    def __str__(self):
        return f"{self.user_id} {self.platform}"


class AuditLog(models.Model):
    """Append-only: who did what, when, from where. Required for money/KYC disputes and regulators."""
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    action = models.CharField(max_length=60, db_index=True)
    object_type = models.CharField(max_length=60, db_index=True)
    object_id = models.CharField(max_length=64, db_index=True)
    changes = models.JSONField(default=dict, blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["object_type", "object_id", "-created_at"])]

    def __str__(self):
        return f"{self.created_at:%Y-%m-%d %H:%M} {self.action} {self.object_type}:{self.object_id}"

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError("Audit logs are append-only.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Audit logs cannot be deleted.")

    @classmethod
    def record(cls, actor, action, obj, changes=None, request=None):
        ip = None
        if request is not None:
            fwd = request.META.get("HTTP_X_FORWARDED_FOR")
            ip = (fwd.split(",")[0].strip() if fwd else request.META.get("REMOTE_ADDR")) or None
        return cls.objects.create(actor=actor if getattr(actor, "pk", None) else None, action=action,
                                  object_type=obj.__class__.__name__, object_id=str(obj.pk),
                                  changes=changes or {}, ip_address=ip)


class SystemAlert(models.Model):
    LEVEL_CHOICES = [("info", "Info"), ("warning", "Warning"), ("error", "Error"), ("critical", "Critical")]

    level = models.CharField(max_length=20, choices=LEVEL_CHOICES, default="warning")
    title = models.CharField(max_length=200)
    message = models.TextField()
    booking_ref = models.CharField(max_length=30, blank=True, null=True, db_index=True)
    resolved = models.BooleanField(default=False, db_index=True)
    resolved_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.get_level_display()}: {self.title} — {'✓ Resolved' if self.resolved else '⚠️ Active'}"

    @classmethod
    def create_alert(cls, level, title, message, booking_ref=None):
        return cls.objects.create(level=level, title=title, message=message, booking_ref=booking_ref)

    def mark_resolved(self):
        self.resolved, self.resolved_at = True, timezone.now()
        self.save()


class DailySummary(models.Model):
    """Reconciliation + KPI snapshot. Safe to rerun for any date."""
    date = models.DateField(unique=True, db_index=True)
    total_bookings = models.IntegerField(default=0)
    completed_bookings = models.IntegerField(default=0)
    cancelled_bookings = models.IntegerField(default=0)
    total_revenue = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    total_commission = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    total_payouts = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    total_refunds = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    total_discounts = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    total_tips = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    new_customers = models.IntegerField(default=0)
    new_riders = models.IntegerField(default=0)
    by_service = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-date"]
        verbose_name_plural = "Daily Summaries"

    def __str__(self):
        return f"{self.date} — ₵{self.total_revenue} / {self.completed_bookings} rides"

    @classmethod
    def generate_for_date(cls, target_date=None):
        target_date = target_date or timezone.localdate()
        created_today = Booking.objects.filter(created_at__date=target_date)
        completed = Booking.objects.filter(status="completed", completed_at__date=target_date)
        agg = completed.aggregate(rev=Sum("total_fare"), com=Sum("platform_commission"), pay=Sum("rider_payout"),
                                  disc=Sum("discount_amount"), tips=Sum("tip_amount"))
        refunds = Refund.objects.filter(status="completed", completed_at__date=target_date) \
            .aggregate(s=Sum("amount"))["s"] or Decimal("0.00")
        by_service = {
            row["service_type"]: {"completed": row["n"], "revenue": str(row["rev"] or Decimal("0.00"))}
            for row in completed.values("service_type").annotate(n=Count("id"), rev=Sum("total_fare"))
        }
        zero = Decimal("0.00")
        summary, _ = cls.objects.update_or_create(date=target_date, defaults={
            "total_bookings": created_today.count(),
            "completed_bookings": completed.count(),
            "cancelled_bookings": Booking.objects.filter(status="cancelled", cancelled_at__date=target_date).count(),
            "total_revenue": agg["rev"] or zero,
            "total_commission": agg["com"] or zero,
            "total_payouts": agg["pay"] or zero,
            "total_discounts": agg["disc"] or zero,
            "total_tips": agg["tips"] or zero,
            "total_refunds": refunds,
            "new_customers": CustomerProfile.objects.filter(created_at__date=target_date).count(),
            "new_riders": RiderProfile.objects.filter(created_at__date=target_date).count(),
            "by_service": by_service,
        })
        return summary

class RiderDocumentType(models.TextChoices):
    GHANA_CARD = 'ghana_card', 'Ghana Card'
    PASSPORT_PHOTO = 'passport_photo', 'Passport Photo'
    RIDING_LICENSE = 'riding_license', 'Riding Licence'
    VEHICLE_REGISTRATION = 'vehicle_registration', 'Vehicle Registration'
    INSURANCE = 'insurance', 'Insurance Certificate'








class DispatchOffer(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        ACCEPTED = "accepted", "Accepted"
        DECLINED = "declined", "Declined"
        EXPIRED = "expired", "Expired"
        CANCELLED = "cancelled", "Cancelled"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    booking = models.ForeignKey(Booking, on_delete=models.CASCADE, related_name="dispatch_offers")
    rider = models.ForeignKey(RiderProfile, on_delete=models.CASCADE, related_name="dispatch_offers")
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING)
    round_no = models.PositiveSmallIntegerField(default=1)
    distance_km = models.DecimalField(max_digits=6, decimal_places=2)
    score = models.FloatField(default=0)
    decline_reason = models.CharField(max_length=60, blank=True)
    offered_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    responded_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        app_label = "api"
        db_table = "dispatch_offers"
        ordering = ["-offered_at"]
        indexes = [
            models.Index(fields=["status", "expires_at"])
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["booking"],
                condition=Q(status__in=["pending", "accepted"]),
                name="uniq_live_offer_per_booking",
            ),
            models.UniqueConstraint(
                fields=["rider"],
                condition=Q(status="pending"),
                name="uniq_pending_offer_per_rider",
            ),
        ]

    def __str__(self):
        return f"Offer {self.id} → {self.rider_id} [{self.status}]"

    @property
    def is_expired(self) -> bool:
        return timezone.now() >= self.expires_at


