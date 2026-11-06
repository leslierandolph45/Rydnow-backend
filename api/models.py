"""
RYDNOW core models.

Design principles
-----------------
1. AUTOMATIC FIRST  - booking -> payment -> dispatch -> accept -> ride -> payout
                      all run without an admin (see services.py / tasks.py).
2. ADMIN OVERRIDE   - every automated step has a `force` / manual path, a kill
                      switch in SystemConfig, and is written to AuditLog.
3. MONEY IS SAFE    - Decimal everywhere, row locks, idempotent references,
                      DB check constraints, and a single ledger (WalletTransaction).
4. ONLY QUALIFIED RIDERS get jobs - a rider must be verified for *that service*
   (RiderServiceQualification), online, unrestricted, fresh GPS, and free.

Every state-changing method returns `Result(ok, message, obj)`.
Requires Django 5.1+ (`CheckConstraint(condition=...)`). On older Django
replace `condition=` with `check=` in CheckConstraint.
"""
import logging
import os
import secrets
import uuid
from collections import namedtuple
from datetime import datetime, time, timedelta
from decimal import Decimal, ROUND_HALF_UP
from math import asin, cos, radians, sin, sqrt

from django.conf import settings
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import IntegrityError, models, transaction
from django.db.models import Avg, Count, Exists, F, OuterRef, Q, Sum
from django.utils import timezone
from .rider import RiderProfile
from .rider  import Rider

from .validators import (
    validate_latitude,
    validate_longitude,
    validate_non_negative_amount,
    validate_phone_number,
    validate_positive_amount,
)

logger = logging.getLogger(__name__)

Result = namedtuple('Result', 'ok message obj', defaults=(None,))


class BookingError(Exception):
    """Raised inside atomic blocks to force a rollback with a message."""


# ==========================================================
#  CONSTANTS & CHOICES
# ==========================================================
COMMISSION_RATE = Decimal('0.15')
CURRENCY_CHOICES = [('GHS', 'Ghana Cedi')]

STATUS_CHOICES = [
    ('pending', 'Pending'),
    ('searching_rider', 'Searching Rider'),
    ('assigned', 'Rider Assigned'),
    ('in_progress', 'In Progress'),
    ('completed', 'Completed'),
    ('cancelled', 'Cancelled'),
    ('expired', 'No Rider Found'),
]
STATUS_TRANSITIONS = {
    'pending': ['searching_rider', 'cancelled', 'expired'],
    'searching_rider': ['assigned', 'cancelled', 'expired'],
    'assigned': ['in_progress', 'searching_rider', 'cancelled'],
    'in_progress': ['completed', 'cancelled'],
    'completed': [],
    'cancelled': [],
    'expired': [],
}
OPEN_STATUSES = ('pending', 'searching_rider', 'assigned', 'in_progress')
ACTIVE_RIDER_STATUSES = ('assigned', 'in_progress')

VERIFICATION_STATUS = [
    ('pending', 'Pending Review'),
    ('submitted', 'Documents Submitted'),
    ('approved', 'Approved'),
    ('rejected', 'Rejected'),
]
PAYMENT_STATUS = [
    ('initiated', 'Payment Initiated'),
    ('pending_confirmation', 'Waiting Provider Confirmation'),
    ('held', 'Funds Held in Escrow'),
    ('released', 'Funds Released'),
    ('failed', 'Payment Failed'),
    ('refund_initiated', 'Refund Processing'),
    ('refunded', 'Refund Completed'),
    ('cancelled', 'Cancelled Before Processing'),
]
PAYMENT_METHOD_CHOICES = [
    ('wallet', 'RYDNOW Wallet'),
    ('momo', 'Mobile Money'),
    ('visa', 'Visa Card'),
    ('debit', 'Debit Card'),
]
TRANSACTION_TYPES = [
    ('deposit', 'Wallet Top-up'),
    ('withdrawal', 'Withdrawal'),
    ('booking_payment', 'Booking Payment'),
    ('payout', 'Rider Earnings'),
    ('refund', 'Customer Refund'),
    ('bonus', 'Platform Bonus Credit'),
    ('adjustment', 'Manual Adjustment'),
    ('reversal', 'Transaction Reversal'),
]
TRANSACTION_STATUS = [
    ('pending', 'Pending'),
    ('completed', 'Completed'),
    ('failed', 'Failed'),
    ('reversed', 'Reversed'),
]
WITHDRAWAL_STATUS = [
    ('pending', 'Queued for Processing'),
    ('reserved', 'Funds Reserved - Sending'),
    ('initiated', 'Sent to Mobile Money Provider'),
    ('completed', 'Successfully Sent'),
    ('failed', 'Failed - Funds Returned'),
    ('timeout', 'Provider Timeout - Needs Reconciliation'),
]
COMPLAINT_STATUS = [
    ('pending', 'Under Review'),
    ('in_progress', 'Being Investigated'),
    ('resolved', 'Resolved - Closed'),
    ('warning_issued', 'Warning Issued'),
    ('restricted', 'Rider Restricted'),
    ('banned', 'Rider Banned'),
    ('dismissed', 'No Action Required'),
]
COMPLAINT_TYPE = [
    ('late_arrival', 'Late Arrival'),
    ('rude_behavior', 'Unprofessional Behavior'),
    ('overcharging', 'Price Dispute'),
    ('unsafe_driving', 'Unsafe Riding'),
    ('no_show', 'Rider Did Not Arrive'),
    ('item_damaged', 'Item Damaged'),
    ('cancelled_without_notice', 'Cancelled Without Notice'),
    ('other', 'Other Issue'),
]

DEFAULT_REQUIRED_DOCUMENTS = {
    'okada': ['national_id', 'driving_license', 'vehicle_registration', 'insurance', 'profile_photo'],
    'parcel': ['national_id', 'driving_license', 'vehicle_registration', 'insurance', 'profile_photo'],
    'waste-bin': ['national_id', 'vehicle_registration', 'profile_photo'],
}


# ==========================================================
#  HELPERS
# ==========================================================
def round_money(value):
    """Consistent 2-decimal rounding - ALL financial math uses this."""
    return Decimal(str(value)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)


def split_fare(total, rate):
    """Commission is rounded; payout is the remainder, so the two ALWAYS sum to total."""
    total = round_money(total)
    commission = round_money(total * Decimal(str(rate)))
    return commission, total - commission


def has_coordinates(lat, lng):
    return lat is not None and lng is not None and not (float(lat) == 0.0 and float(lng) == 0.0)


def haversine_km(lat1, lng1, lat2, lng2):
    if not (has_coordinates(lat1, lng1) and has_coordinates(lat2, lng2)):
        return None
    lat1, lng1, lat2, lng2 = (float(v) for v in (lat1, lng1, lat2, lng2))
    dlat, dlng = radians(lat2 - lat1), radians(lng2 - lng1)
    a = sin(dlat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlng / 2) ** 2
    return round(6371.0088 * 2 * asin(sqrt(min(1.0, max(0.0, a)))), 3)


def _ref(prefix, n=12):
    return f"{prefix}-{uuid.uuid4().hex[:n].upper()}"


def generate_transaction_ref():
    return _ref('TX')


def generate_booking_ref():
    return _ref('BK')


def generate_withdrawal_ref():
    return _ref('WD', 10)


def generate_refund_ref():
    return _ref('RF', 10)


def generate_otp():
    return ''.join(secrets.choice('0123456789') for _ in range(4))


def validate_fare_breakdown(total_fare, commission, payout):
    if total_fare is None or total_fare <= 0:
        return True
    if commission + payout != total_fare:
        raise ValidationError(
            f"Fare split error: commission {commission} + payout {payout} != total {total_fare}."
        )
    return True


# ==========================================================
#  SYSTEM CONFIG - admin-editable switches & thresholds
# ==========================================================
class SystemConfig(models.Model):
    """
    Key/value platform settings, editable in admin, cached for 60s.
    Admin kill switches: set `auto_dispatch_enabled` = false to stop automatic
    matching and assign manually instead.
    """
    DEFAULTS = {
        'auto_dispatch_enabled': 'true',
        'auto_approve_riders': 'true',          # approve qualification when required docs are approved
        'auto_approve_documents': 'false',      # needs a real verifier - see services.auto_review_document
        'require_payment_before_dispatch': 'true',
        'rider_location_max_age_seconds': '120',
        'rider_arrival_timeout_minutes': '20',
        'unpaid_booking_timeout_minutes': '15',
        'stuck_ride_alert_hours': '6',
        'earnings_hold_minutes': '0',           # dispute window before earnings become withdrawable
        'rider_min_online_balance': '0.00',
        'min_withdrawal_amount': '10.00',
        'strike_limit': '3',
        'strike_restriction_hours': '24',
        'complaint_auto_restrict_threshold': '3',
    }
    key = models.CharField(max_length=100, unique=True)
    value = models.TextField(blank=True)
    description = models.CharField(max_length=255, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name_plural = "System Config"

    def __str__(self):
        return f"{self.key} = {self.value}"

    @classmethod
    def get_value(cls, key, default=None):
        ck = f"rydnow:cfg:{key}"
        val = cache.get(ck)
        if val is None:
            row = cls.objects.filter(key=key).values_list('value', flat=True).first()
            val = row if row is not None else cls.DEFAULTS.get(key, default)
            if val is not None:
                cache.set(ck, val, 60)
        return val

    @classmethod
    def get_bool(cls, key, default=False):
        val = cls.get_value(key, default)
        return str(val).strip().lower() in ('1', 'true', 'yes', 'on')

    @classmethod
    def get_int(cls, key, default=0):
        try:
            return int(cls.get_value(key, default))
        except (TypeError, ValueError):
            return default

    @classmethod
    def get_decimal(cls, key, default='0'):
        try:
            return Decimal(str(cls.get_value(key, default)))
        except Exception:
            return Decimal(str(default))

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        cache.delete(f"rydnow:cfg:{self.key}")

    def delete(self, *args, **kwargs):
        cache.delete(f"rydnow:cfg:{self.key}")
        return super().delete(*args, **kwargs)


# ==========================================================
#  SERVICE - the 3 roles + pricing + dispatch tuning
# ==========================================================
class Service(models.Model):
    SERVICE_OKADA = 'okada'
    SERVICE_PARCEL = 'parcel'
    SERVICE_WASTE_BIN = 'waste-bin'
    SERVICE_KEYS = [
        (SERVICE_OKADA, 'Okada Ride'),
        (SERVICE_PARCEL, 'Parcel Delivery'),
        (SERVICE_WASTE_BIN, 'Waste Collection'),
    ]

    key = models.CharField(max_length=30, primary_key=True, choices=SERVICE_KEYS, verbose_name='Service ID')
    name = models.CharField(max_length=100)
    description = models.TextField(blank=True)
    is_active = models.BooleanField(default=True, help_text='Inactive services cannot be booked; history is kept.')

    # Pricing
    base_fare = models.DecimalField(max_digits=8, decimal_places=2, default=Decimal('5.00'))
    per_km_rate = models.DecimalField(max_digits=8, decimal_places=2, default=Decimal('2.00'))
    per_minute_rate = models.DecimalField(max_digits=8, decimal_places=2, default=Decimal('0.20'))
    minimum_fare = models.DecimalField(max_digits=8, decimal_places=2, default=Decimal('8.00'))
    surge_multiplier = models.DecimalField(
        max_digits=4, decimal_places=2, default=Decimal('1.00'), validators=[MinValueValidator(Decimal('1.00'))],
        help_text='Admin can raise this during peak demand.'
    )
    commission_rate = models.DecimalField(
        max_digits=5, decimal_places=4, default=COMMISSION_RATE,
        validators=[MinValueValidator(Decimal('0')), MaxValueValidator(Decimal('0.5'))]
    )

    # Dispatch tuning
    dispatch_radius_km = models.FloatField(default=5.0, help_text='Round 1 radius; grows 50% each round.')
    offers_per_round = models.PositiveSmallIntegerField(default=3)
    offer_timeout_seconds = models.PositiveIntegerField(default=30)
    max_dispatch_rounds = models.PositiveSmallIntegerField(default=4)

    # Which verified documents a rider needs to be qualified for this service
    required_documents = models.JSONField(default=list, blank=True)

    class Meta:
        db_table = 'rydnow_service'
        ordering = ['key']

    def __str__(self):
        return f"{self.name} ({'Active' if self.is_active else 'Inactive'})"

    def calculate_fare(self, distance_km=0, duration_min=0):
        fare = (
            self.base_fare
            + self.per_km_rate * Decimal(str(distance_km or 0))
            + self.per_minute_rate * Decimal(str(duration_min or 0))
        )
        return round_money(max(fare, self.minimum_fare) * self.surge_multiplier)

    @classmethod
    def ensure_defaults(cls):
        """Seed the 3 canonical services (called after migrate)."""
        for key, label in cls.SERVICE_KEYS:
            cls.objects.get_or_create(
                key=key,
                defaults={'name': label, 'required_documents': DEFAULT_REQUIRED_DOCUMENTS.get(key, [])},
            )


# ==========================================================
#  CUSTOMER
# ==========================================================
class CustomerProfile(models.Model):
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='customer_profile')
    full_name = models.CharField(max_length=200, blank=True)
    email = models.EmailField(blank=True)
    phone = models.CharField(max_length=30, blank=True, null=True, validators=[validate_phone_number])
    profile_photo = models.ImageField(upload_to='profiles/customer/%Y/%m/', blank=True, null=True)
    is_blocked = models.BooleanField(default=False, help_text='Blocked customers cannot create bookings.')
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return self.full_name or getattr(self.user, 'get_username', lambda: None)() or f"Customer-{self.pk}"


class CustomerWallet(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='customer_wallet')
    balance = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0.00'),
                                  validators=[validate_non_negative_amount])
    bonus_credit = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0.00'),
                                       validators=[validate_non_negative_amount])
    currency = models.CharField(max_length=3, choices=CURRENCY_CHOICES, default='GHS')
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        constraints = [
            models.CheckConstraint(condition=Q(balance__gte=0), name='customer_wallet_balance_gte_0'),
            models.CheckConstraint(condition=Q(bonus_credit__gte=0), name='customer_wallet_bonus_gte_0'),
        ]

    def __str__(self):
        return f"{self.user} - {self.currency} {self.balance}"

    def credit(self, amount, txn_type='deposit', reference=None, description='', booking=None, metadata=None):
        """Add money (txn_type='bonus' credits bonus_credit). Idempotent on `reference`."""
        amount = round_money(amount)
        validate_positive_amount(amount)
        ref = reference or generate_transaction_ref()
        existing = WalletTransaction.objects.filter(reference=ref).first()
        if existing:
            return Result(True, 'Already processed (idempotent)', existing)
        try:
            with transaction.atomic():
                w = CustomerWallet.objects.select_for_update().get(pk=self.pk)
                if txn_type == 'bonus':
                    w.bonus_credit += amount
                else:
                    w.balance += amount
                w.save(update_fields=['balance', 'bonus_credit', 'updated_at'])
                txn = WalletTransaction.objects.create(
                    customer_wallet=w, wallet_type='customer', transaction_type=txn_type, amount=amount,
                    balance_after=w.balance, reference=ref, description=description, currency=w.currency,
                    related_booking=booking, metadata=metadata or {},
                )
        except IntegrityError:
            existing = WalletTransaction.objects.filter(reference=ref).first()
            if existing:
                return Result(True, 'Already processed (idempotent)', existing)
            return Result(False, 'Database error')
        self.balance, self.bonus_credit = w.balance, w.bonus_credit
        return Result(True, 'Credited', txn)

    def debit(self, amount, txn_type='withdrawal', reference=None, description='', booking=None,
              use_bonus=False, metadata=None):
        """Remove money. With use_bonus=True, bonus credit is spent first."""
        amount = round_money(amount)
        validate_positive_amount(amount)
        ref = reference or generate_transaction_ref()
        existing = WalletTransaction.objects.filter(reference=ref).first()
        if existing:
            return Result(True, 'Already processed (idempotent)', existing)
        try:
            with transaction.atomic():
                w = CustomerWallet.objects.select_for_update().get(pk=self.pk)
                bonus_used = min(w.bonus_credit, amount) if use_bonus else Decimal('0.00')
                cash = amount - bonus_used
                if w.balance < cash:
                    return Result(False, 'Insufficient balance')
                w.bonus_credit -= bonus_used
                w.balance -= cash
                w.save(update_fields=['balance', 'bonus_credit', 'updated_at'])
                meta = dict(metadata or {}, bonus_used=str(bonus_used))
                txn = WalletTransaction.objects.create(
                    customer_wallet=w, wallet_type='customer', transaction_type=txn_type, amount=-amount,
                    balance_after=w.balance, reference=ref, description=description, currency=w.currency,
                    related_booking=booking, metadata=meta,
                )
        except IntegrityError:
            existing = WalletTransaction.objects.filter(reference=ref).first()
            if existing:
                return Result(True, 'Already processed (idempotent)', existing)
            return Result(False, 'Database error')
        self.balance, self.bonus_credit = w.balance, w.bonus_credit
        return Result(True, 'Debited', txn)

    # backwards-compatible names
    def deposit(self, amount, reference=None, description='', related_booking=None):
        return self.credit(amount, 'deposit', reference, description, related_booking)

    def withdraw(self, amount, reference=None, description=''):
        return self.debit(amount, 'withdrawal', reference, description)


# ==========================================================
#  RIDER
# ==========================================================
class RiderQuerySet(models.QuerySet):
    def not_restricted(self):
        return self.filter(is_restricted=False).filter(
            Q(restricted_until__isnull=True) | Q(restricted_until__lte=timezone.now())
        )

    def approved(self):
        return self.not_restricted().filter(verification_status='approved')

    def available_for(self, service):
        """
        THE dispatch filter. A rider appears only if they are:
        approved + unrestricted + ONLINE + qualified (approved) for THIS service
        + have a fresh GPS fix + are not already on a job (+ optional min balance).
        """
        service_key = getattr(service, 'pk', service)
        max_age = SystemConfig.get_int('rider_location_max_age_seconds', 120)
        cutoff = timezone.now() - timedelta(seconds=max_age)
        busy = Booking.objects.filter(assigned_rider=OuterRef('pk'), status__in=ACTIVE_RIDER_STATUSES)
        qs = (
            self.approved()
            .filter(
                is_online=True,
                last_location_update__gte=cutoff,
                current_lat__isnull=False,
                current_lng__isnull=False,
                qualifications__service_id=service_key,
                qualifications__status='approved',
            )
            .exclude(Exists(busy))
        )
        min_bal = SystemConfig.get_decimal('rider_min_online_balance', '0')
        if min_bal > 0:
            qs = qs.filter(wallet__available_balance__gte=min_bal)
        return qs

    def nearby(self, lat, lng, radius_km):
        """Cheap bounding-box prefilter (exact distance is computed in Python)."""
        if not has_coordinates(lat, lng):
            return self.none()
        dlat = radius_km / 111.0
        dlng = radius_km / (111.0 * max(0.01, cos(radians(float(lat)))))
        return self.filter(
            current_lat__range=(float(lat) - dlat, float(lat) + dlat),
            current_lng__range=(float(lng) - dlng, float(lng) + dlng),
        )


class RiderServiceQualification(models.Model):
    """Which of the 3 services a rider is VERIFIED to do (per-service approval)."""
    STATUS = [('pending', 'Pending'), ('approved', 'Approved'), ('rejected', 'Rejected'), ('suspended', 'Suspended')]

    rider = models.ForeignKey(Rider, on_delete=models.CASCADE, related_name='qualifications')
    service = models.ForeignKey(Service, on_delete=models.PROTECT, related_name='qualifications')
    status = models.CharField(max_length=12, choices=STATUS, default='pending', db_index=True)
    manual_override = models.BooleanField(default=False, help_text='ON = admin decision; automation will not change it.')
    approved_at = models.DateTimeField(null=True, blank=True)
    approved_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                    related_name='+')
    notes = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['rider', 'service'], name='unique_rider_service')]
        indexes = [models.Index(fields=['service', 'status'])]

    def __str__(self):
        return f"{self.rider.full_name} / {self.service_id}: {self.status}"

    def admin_set(self, status, admin_user, notes=''):
        self.status, self.manual_override = status, True
        self.approved_by = admin_user
        self.approved_at = timezone.now() if status == 'approved' else self.approved_at
        self.notes = notes
        self.save()
        self.rider.refresh_verification()


def get_safe_upload_path(instance, filename):
    ext = os.path.splitext(filename)[1].lower() or '.bin'
    return f"rider_docs/{uuid.uuid4().hex[:8]}/{uuid.uuid4().hex}{ext}"


class RiderDocument(models.Model):
    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('approved', 'Approved'),
        ('rejected', 'Rejected'),
        ('resubmit_required', 'Resubmission Required'),
    ]
    DOCUMENT_TYPES = [
        ('national_id', 'National ID / Ghana Card'),
        ('driving_license', 'Driving Licence'),
        ('vehicle_registration', 'Vehicle Registration'),
        ('insurance', 'Insurance Certificate'),
        ('profile_photo', 'Profile Photo'),
    ]

    rider = models.ForeignKey(Rider, on_delete=models.PROTECT, related_name='documents')
    document_type = models.CharField(max_length=50, choices=DOCUMENT_TYPES)
    document_file = models.FileField(upload_to=get_safe_upload_path, max_length=255)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending', db_index=True)
    is_current = models.BooleanField(default=True, db_index=True)
    expires_at = models.DateTimeField(null=True, blank=True, help_text='Licence/insurance expiry - auto-revokes qualification.')
    uploaded_at = models.DateTimeField(auto_now_add=True)
    verified_at = models.DateTimeField(null=True, blank=True)
    verified_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                    related_name='+')
    rejection_reason = models.TextField(blank=True, null=True)
    replaced_by = models.ForeignKey('self', on_delete=models.SET_NULL, null=True, blank=True, related_name='replaces')

    class Meta:
        ordering = ['-uploaded_at']
        constraints = [
            models.UniqueConstraint(fields=['rider', 'document_type'], condition=Q(is_current=True),
                                    name='unique_current_doc_type_per_rider'),
        ]
        permissions = [('can_verify_documents', 'Can verify rider documents')]

    def __str__(self):
        return f"{self.rider.full_name} - {self.get_document_type_display()} - {self.status}"

    @classmethod
    def _cleanup_orphaned_file(cls, storage_name):
        if not storage_name:
            return
        try:
            if not cls.objects.filter(document_file=storage_name).exists():
                cls._meta.get_field('document_file').storage.delete(storage_name)
        except Exception as e:  # never mask the original error
            logger.warning("Orphan cleanup failed for %s: %s", storage_name, e)

    @classmethod
    def resubmit_document(cls, rider, document_type, document_file):
        """
        First upload OR replacement. Concurrency-safe (rider row lock).
        Raises ValueError when not allowed.
        """
        if document_type not in dict(cls.DOCUMENT_TYPES):
            raise ValueError(f"Invalid document type. Allowed: {', '.join(dict(cls.DOCUMENT_TYPES))}")
        stored_name = None
        try:
            with transaction.atomic():
                Rider.objects.select_for_update().get(pk=rider.pk)
                existing = cls.objects.select_for_update().filter(
                    rider=rider, document_type=document_type, is_current=True).first()
                if existing:
                    if existing.status == 'pending':
                        raise ValueError("A document of this type is under review. Please wait for a decision.")
                    if existing.status == 'approved':
                        raise ValueError("This document is already approved. Contact support to change it.")
                new_doc = cls.objects.create(rider=rider, document_type=document_type,
                                             document_file=document_file, status='pending', is_current=False)
                stored_name = new_doc.document_file.name
                if existing:
                    existing.is_current = False
                    existing.replaced_by = new_doc
                    existing.save(update_fields=['is_current', 'replaced_by'])
                new_doc.is_current = True
                new_doc.save(update_fields=['is_current'])
                return new_doc
        except Exception:
            cls._cleanup_orphaned_file(stored_name)
            raise


# ==========================================================
#  RIDER WALLET
# ==========================================================
class RiderWallet(models.Model):
    """
    available_balance : withdrawable now
    pending_earnings  : earned, inside the dispute-hold window
    reserved_balance  : locked for an in-flight withdrawal
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    rider = models.OneToOneField(Rider, on_delete=models.PROTECT, related_name='wallet')
    available_balance = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0.00'))
    pending_earnings = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0.00'))
    reserved_balance = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0.00'))
    currency = models.CharField(max_length=3, choices=CURRENCY_CHOICES, default='GHS')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        constraints = [
            models.CheckConstraint(condition=Q(available_balance__gte=0), name='rider_wallet_available_gte_0'),
            models.CheckConstraint(condition=Q(pending_earnings__gte=0), name='rider_wallet_pending_gte_0'),
            models.CheckConstraint(condition=Q(reserved_balance__gte=0), name='rider_wallet_reserved_gte_0'),
        ]

    def __str__(self):
        return f"{self.rider.full_name} - {self.currency} {self.balance}"

    @property
    def balance(self):
        return self.available_balance + self.pending_earnings + self.reserved_balance

    def _locked(self):
        return RiderWallet.objects.select_for_update().get(pk=self.pk)

    def credit_earnings(self, amount, booking, hold_minutes=0):
        """Credit ride earnings. Idempotent per booking. hold_minutes>0 => pending first."""
        amount = round_money(amount)
        validate_positive_amount(amount)
        ref = f"PAY-{booking.booking_ref}"
        existing = WalletTransaction.objects.filter(reference=ref).first()
        if existing:
            return Result(True, 'Earnings already credited (idempotent)', existing)
        try:
            with transaction.atomic():
                w = self._locked()
                held = hold_minutes > 0
                if held:
                    w.pending_earnings += amount
                else:
                    w.available_balance += amount
                w.save(update_fields=['available_balance', 'pending_earnings', 'updated_at'])
                txn = WalletTransaction.objects.create(
                    rider_wallet=w, wallet_type='rider', transaction_type='payout', amount=amount,
                    balance_after=w.available_balance, reference=ref, currency=w.currency,
                    description=f"Earnings: booking {booking.booking_ref}", related_booking=booking,
                    status='pending' if held else 'completed',
                )
        except IntegrityError:
            return Result(True, 'Earnings already credited (idempotent)')
        return Result(True, 'Earnings credited', txn)

    def release_earnings(self, booking):
        """Move held earnings to available once the dispute window has passed."""
        with transaction.atomic():
            w = self._locked()
            txn = (WalletTransaction.objects.select_for_update()
                   .filter(reference=f"PAY-{booking.booking_ref}", status='pending').first())
            if not txn:
                return Result(False, 'Nothing to release')
            w.pending_earnings -= txn.amount
            w.available_balance += txn.amount
            w.save(update_fields=['available_balance', 'pending_earnings', 'updated_at'])
            txn.status, txn.balance_after = 'completed', w.available_balance
            txn.save(update_fields=['status', 'balance_after'])
            Booking.objects.filter(pk=booking.pk).update(earnings_released_at=timezone.now())
        return Result(True, 'Earnings released', txn)

    def reserve_funds(self, amount):
        amount = round_money(amount)
        validate_positive_amount(amount)
        with transaction.atomic():
            w = self._locked()
            if w.available_balance < amount:
                return Result(False, 'Insufficient available balance')
            w.available_balance -= amount
            w.reserved_balance += amount
            w.save(update_fields=['available_balance', 'reserved_balance', 'updated_at'])
        return Result(True, 'Funds reserved')

    def confirm_withdrawal_debit(self, amount, reference):
        """Provider confirmed payout: reserved funds leave the platform + ledger entry."""
        amount = round_money(amount)
        with transaction.atomic():
            w = self._locked()
            if w.reserved_balance < amount:
                return Result(False, 'Reserved balance too low - reconcile manually')
            if WalletTransaction.objects.filter(reference=reference).exists():
                return Result(True, 'Already debited (idempotent)')
            w.reserved_balance -= amount
            w.save(update_fields=['reserved_balance', 'updated_at'])
            WalletTransaction.objects.create(
                rider_wallet=w, wallet_type='rider', transaction_type='withdrawal', amount=-amount,
                balance_after=w.available_balance, reference=reference, currency=w.currency,
                description='Withdrawal to mobile money',
            )
        return Result(True, 'Funds debited')

    def restore_funds(self, amount, reason=''):
        amount = round_money(amount)
        with transaction.atomic():
            w = self._locked()
            take = min(amount, w.reserved_balance)
            w.reserved_balance -= take
            w.available_balance += take
            w.save(update_fields=['available_balance', 'reserved_balance', 'updated_at'])
        return Result(True, 'Funds restored')

    def create_withdrawal_request(self, amount, method, idempotency_key=''):
        amount = round_money(amount)
        validate_positive_amount(amount)
        if idempotency_key:
            existing = WithdrawalRequest.objects.filter(idempotency_key=idempotency_key).first()
            if existing:
                return Result(True, 'Existing request returned (idempotent)', existing)
        minimum = SystemConfig.get_decimal('min_withdrawal_amount', '10')
        if amount < minimum:
            return Result(False, f'Minimum withdrawal is GHS {minimum}')
        if self.available_balance < amount:
            return Result(False, 'Insufficient available balance')
        try:
            with transaction.atomic():
                req = WithdrawalRequest.objects.create(
                    wallet=self, amount=amount, payment_method=method,
                    idempotency_key=idempotency_key or '', status='pending')
        except IntegrityError:
            return Result(False, 'A withdrawal request is already in progress')
        return Result(True, 'Withdrawal request created', req)


# ==========================================================
#  LEDGER
# ==========================================================
class WalletTransaction(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    customer_wallet = models.ForeignKey(CustomerWallet, on_delete=models.PROTECT, related_name='transactions',
                                        null=True, blank=True)
    rider_wallet = models.ForeignKey(RiderWallet, on_delete=models.PROTECT, related_name='transactions',
                                     null=True, blank=True)
    wallet_type = models.CharField(max_length=20, default='customer', db_index=True)
    transaction_type = models.CharField(max_length=20, choices=TRANSACTION_TYPES, db_index=True)
    amount = models.DecimalField(max_digits=12, decimal_places=2, help_text='Signed: + in, - out')
    balance_after = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0.00'))
    currency = models.CharField(max_length=3, choices=CURRENCY_CHOICES, default='GHS')
    reference = models.CharField(max_length=50, unique=True, default=generate_transaction_ref)
    status = models.CharField(max_length=20, choices=TRANSACTION_STATUS, default='completed', db_index=True)
    description = models.CharField(max_length=255, blank=True)
    related_booking = models.ForeignKey('Booking', on_delete=models.SET_NULL, null=True, blank=True,
                                        related_name='wallet_transactions')
    provider_ref = models.CharField(max_length=255, blank=True)
    idempotency_key = models.CharField(max_length=100, blank=True, db_index=True)
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['customer_wallet', '-created_at']),
            models.Index(fields=['rider_wallet', '-created_at']),
            models.Index(fields=['transaction_type', 'created_at']),
            models.Index(fields=['wallet_type', 'status', '-created_at']),
        ]
        constraints = [
            models.CheckConstraint(
                condition=(Q(customer_wallet__isnull=False, rider_wallet__isnull=True)
                           | Q(customer_wallet__isnull=True, rider_wallet__isnull=False)),
                name='wallet_txn_exactly_one_wallet'),
        ]

    def __str__(self):
        return f"{self.reference} | {self.transaction_type} | {self.amount} {self.currency}"


class WithdrawalRequest(models.Model):
    """Safe payout workflow: reserve -> call provider OUTSIDE the DB transaction -> confirm/fail."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    wallet = models.ForeignKey(RiderWallet, on_delete=models.PROTECT, related_name='withdrawals')
    amount = models.DecimalField(max_digits=10, decimal_places=2, validators=[validate_positive_amount])
    reference = models.CharField(max_length=30, unique=True, default=generate_withdrawal_ref)
    payment_method = models.CharField(max_length=20, choices=PAYMENT_METHOD_CHOICES)
    provider_ref = models.CharField(max_length=255, blank=True)
    idempotency_key = models.CharField(max_length=100, blank=True)
    status = models.CharField(max_length=20, choices=WITHDRAWAL_STATUS, default='pending', db_index=True)
    failure_reason = models.TextField(blank=True)
    processed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['wallet', '-created_at']),
            models.Index(fields=['status', 'created_at']),
        ]
        constraints = [
            models.UniqueConstraint(fields=['idempotency_key'], condition=~Q(idempotency_key=''),
                                    name='unique_withdrawal_idempotency'),
            models.UniqueConstraint(fields=['wallet'], condition=Q(status__in=['pending', 'reserved', 'initiated']),
                                    name='one_active_withdrawal_per_wallet'),
        ]

    def __str__(self):
        return f"{self.reference} - {self.amount} GHS - {self.get_status_display()}"

    def _lock(self):
        return WithdrawalRequest.objects.select_for_update().get(pk=self.pk)

    def reserve_funds(self):
        with transaction.atomic():
            req = self._lock()
            if req.status != 'pending':
                return Result(False, 'Cannot reserve: invalid status')
            res = req.wallet.reserve_funds(req.amount)
            if not res.ok:
                return res
            req.status = 'reserved'
            req.save(update_fields=['status'])
        self.refresh_from_db()
        return Result(True, 'Funds reserved')

    def confirm_payout_initiated(self, provider_ref=''):
        with transaction.atomic():
            req = self._lock()
            if req.status != 'reserved':
                return Result(False, 'Funds must be reserved first')
            req.status = 'initiated'
            req.provider_ref = provider_ref or req.provider_ref
            req.processed_at = timezone.now()
            req.save(update_fields=['status', 'provider_ref', 'processed_at'])
        self.refresh_from_db()
        return Result(True, 'Payout initiated')

    def confirm_payout_completed(self, provider_ref=''):
        with transaction.atomic():
            req = self._lock()
            if req.status == 'completed':
                return Result(True, 'Already completed (idempotent)')
            if req.status not in ('reserved', 'initiated', 'timeout'):
                return Result(False, 'Invalid state for completion')
            res = req.wallet.confirm_withdrawal_debit(req.amount, req.reference)
            if not res.ok:
                return res
            req.status = 'completed'
            req.provider_ref = provider_ref or req.provider_ref
            req.processed_at = timezone.now()
            req.save(update_fields=['status', 'provider_ref', 'processed_at'])
        self.refresh_from_db()
        return Result(True, 'Payout completed')

    def mark_payout_failed(self, reason='Provider error'):
        """Return reserved funds - ONLY call when the provider definitely did not pay."""
        with transaction.atomic():
            req = self._lock()
            if req.status in ('completed', 'failed'):
                return Result(False, f'Cannot fail a {req.status} payout')
            if req.status in ('reserved', 'initiated', 'timeout'):
                req.wallet.restore_funds(req.amount, reason=reason)
            req.status, req.failure_reason, req.processed_at = 'failed', reason, timezone.now()
            req.save(update_fields=['status', 'failure_reason', 'processed_at'])
        self.refresh_from_db()
        return Result(True, 'Funds restored, request marked failed')

    def mark_timeout(self, provider_known_failure=False):
        """Uncertain provider result: hold funds and alert for reconciliation (never guess)."""
        if provider_known_failure:
            return self.mark_payout_failed('Provider timeout - confirmed failed')
        with transaction.atomic():
            req = self._lock()
            if req.status not in ('pending', 'reserved', 'initiated'):
                return Result(False, 'Cannot timeout in current state')
            req.status = 'timeout'
            req.failure_reason = 'Provider timeout - funds held for reconciliation'
            req.processed_at = timezone.now()
            req.save(update_fields=['status', 'failure_reason', 'processed_at'])
        SystemAlert.create_alert('critical', 'Withdrawal needs reconciliation',
                                 f'{self.reference}: provider timeout, GHS {self.amount} held.')
        self.refresh_from_db()
        return Result(True, 'Timeout recorded - requires reconciliation')

    def process_payout(self, provider_callback=None):
        if self.status == 'completed':
            return Result(True, 'Already completed (idempotent)')
        if self.status != 'pending':
            return Result(False, f'Cannot process from status: {self.status}')
        res = self.reserve_funds()
        if not res.ok:
            return self.mark_payout_failed(res.message)
        ok, response = True, {}
        if callable(provider_callback):  # runs OUTSIDE any DB transaction
            try:
                ok, response = provider_callback(amount=self.amount, reference=self.reference,
                                                 idempotency_key=self.idempotency_key)
            except Exception as e:
                self.mark_timeout(provider_known_failure=False)
                return Result(False, f'Provider error: {e}')
        if ok:
            ref = response.get('provider_ref', '')
            self.confirm_payout_initiated(provider_ref=ref)
            return self.confirm_payout_completed(provider_ref=ref)
        return self.mark_payout_failed(response.get('error', 'Provider communication failed'))

    def retry_failed(self, new_idempotency_key=None):
        if self.status != 'failed':
            return Result(False, 'Only failed requests can be retried')
        key = new_idempotency_key or f"RETRY-{self.reference}-{uuid.uuid4().hex[:6]}"
        return self.wallet.create_withdrawal_request(self.amount, self.payment_method, key)


# ==========================================================
#  BOOKING
# ==========================================================
class Booking(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    booking_ref = models.CharField(max_length=16, unique=True, default=generate_booking_ref)
    service = models.ForeignKey(Service, on_delete=models.PROTECT, related_name='bookings')
    customer = models.ForeignKey(CustomerProfile, on_delete=models.PROTECT, related_name='bookings',
                                 null=True, blank=True)
    details = models.JSONField(default=dict, blank=True, help_text='Parcel size, bin type, notes...')

    pickup = models.CharField(max_length=255, blank=True, default='')
    pickup_lat = models.FloatField(validators=[validate_latitude], null=True, blank=True)
    pickup_lng = models.FloatField(validators=[validate_longitude], null=True, blank=True)
    destination = models.CharField(max_length=255, blank=True, default='')
    destination_lat = models.FloatField(validators=[validate_latitude], null=True, blank=True)
    destination_lng = models.FloatField(validators=[validate_longitude], null=True, blank=True)
    estimated_distance_km = models.FloatField(null=True, blank=True, default=0.0)
    estimated_duration_min = models.IntegerField(null=True, blank=True, default=0)

    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending', db_index=True)
    assigned_rider = models.ForeignKey(Rider, on_delete=models.PROTECT, related_name='assigned_bookings',
                                       null=True, blank=True)
    pickup_otp = models.CharField(max_length=6, blank=True, help_text='Rider must enter this to start the ride.')

    # dispatch state
    dispatch_round = models.PositiveSmallIntegerField(default=0)
    last_dispatch_at = models.DateTimeField(null=True, blank=True)
    arrival_deadline = models.DateTimeField(null=True, blank=True)

    # fare (snapshotted at creation)
    total_fare = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'),
                                     validators=[validate_non_negative_amount])
    commission_rate = models.DecimalField(max_digits=5, decimal_places=4, default=COMMISSION_RATE)
    platform_commission = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'))
    rider_payout = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'))

    payment_status = models.CharField(max_length=30, choices=PAYMENT_STATUS, default='initiated', db_index=True)
    payment_method = models.CharField(max_length=20, choices=PAYMENT_METHOD_CHOICES, null=True, blank=True)
    payment_ref = models.CharField(max_length=100, blank=True)
    provider_ref = models.CharField(max_length=255, blank=True)

    earnings_release_at = models.DateTimeField(null=True, blank=True)
    earnings_released_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)
    scheduled_at = models.DateTimeField(null=True, blank=True)
    accepted_at = models.DateTimeField(null=True, blank=True)
    arrived_at = models.DateTimeField(null=True, blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    cancelled_at = models.DateTimeField(null=True, blank=True)
    cancelled_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True,
                                     related_name='+')
    cancelled_by_role = models.CharField(max_length=10, blank=True)  # customer / rider / admin / system
    cancellation_reason = models.TextField(blank=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['customer', '-created_at']),
            models.Index(fields=['assigned_rider', 'status']),
            models.Index(fields=['status', 'created_at']),
            models.Index(fields=['status', 'last_dispatch_at']),
            models.Index(fields=['payment_ref']),
        ]
        constraints = [
            models.UniqueConstraint(fields=['payment_ref'], condition=~Q(payment_ref=''),
                                    name='unique_booking_payment_ref'),
            models.CheckConstraint(condition=Q(total_fare__gte=0), name='booking_total_fare_gte_0'),
        ]

    def __str__(self):
        return f"{self.booking_ref} - {self.get_status_display()}"

    @classmethod
    def from_db(cls, db, field_names, values):
        inst = super().from_db(db, field_names, values)
        inst._loaded_status = inst.__dict__.get('status')
        return inst

    # ---- properties / helpers ----
    @property
    def has_pickup_coordinates(self):
        return has_coordinates(self.pickup_lat, self.pickup_lng)

    @property
    def customer_user(self):
        return self.customer.user if self.customer_id else None

    def get_distance_km(self, from_lat, from_lng, to_lat, to_lng):
        return haversine_km(from_lat, from_lng, to_lat, to_lng)

    def estimate_trip(self):
        straight = haversine_km(self.pickup_lat, self.pickup_lng, self.destination_lat, self.destination_lng)
        if straight is not None:
            road = straight * 1.3  # straight-line -> road factor
            self.estimated_distance_km = round(road, 2)
            self.estimated_duration_min = max(1, round(road / 20 * 60))  # ~20 km/h city average

    def can_transition(self, new_status):
        return new_status in STATUS_TRANSITIONS.get(self.status, [])

    def clean(self):
        super().clean()
        validate_fare_breakdown(self.total_fare, self.platform_commission, self.rider_payout)

    def save(self, *args, **kwargs):
        if self._state.adding:
            if not self.pickup_otp:
                self.pickup_otp = generate_otp()
            if self.service_id:
                self.commission_rate = self.service.commission_rate
                if not self.estimated_distance_km:
                    self.estimate_trip()
                if not self.total_fare:
                    self.total_fare = self.service.calculate_fare(self.estimated_distance_km, self.estimated_duration_min)
        # keep the 15/85 split exact whenever the fare or rate changes (never after settlement)
        if kwargs.get('update_fields') is None and self.total_fare > 0 and self.status not in ('completed', 'cancelled'):
            self.platform_commission, self.rider_payout = split_fare(self.total_fare, self.commission_rate)
        super().save(*args, **kwargs)
        self._loaded_status = self.status

    def _lock(self):
        return Booking.objects.select_for_update().get(pk=self.pk)

    # ---- dispatch / assignment ----
    def assign_rider(self, rider, force=False, actor=None):
        """Atomic assignment. force=True is the ADMIN override (skips eligibility checks)."""
        with transaction.atomic():
            res = self._lock()._assign_locked(rider, force=force)
        if res.ok:
            self.refresh_from_db()
        return res

    def _assign_locked(self, rider, force=False):
        """Must be called on a row-locked booking inside transaction.atomic()."""
        if self.status not in ('pending', 'searching_rider'):
            return Result(False, 'Booking no longer available')
        rider = Rider.objects.select_for_update().get(pk=rider.pk)  # lock => no double-booking
        if not force:
            check = rider.check_available_for(self.service_id, exclude_booking_pk=self.pk)
            if not check.ok:
                return check
        now = timezone.now()
        timeout = SystemConfig.get_int('rider_arrival_timeout_minutes', 20)
        self.assigned_rider = rider
        self.status = 'assigned'
        self.accepted_at = now
        self.arrival_deadline = now + timedelta(minutes=timeout)
        self.save(update_fields=['assigned_rider', 'status', 'accepted_at', 'arrival_deadline', 'updated_at'])
        self.dispatch_offers.filter(status='offered', rider=rider).update(status='accepted', responded_at=now)
        self.dispatch_offers.filter(status='offered').exclude(rider=rider).update(status='cancelled', responded_at=now)
        return Result(True, 'Rider assigned successfully', self)

    def unassign_rider(self, reason='', penalize=False):
        """Rider no-show / cancelled after accepting: put the job back in the pool."""
        with transaction.atomic():
            b = self._lock()
            if b.status != 'assigned':
                return Result(False, 'Only assigned bookings can be unassigned')
            rider = b.assigned_rider
            b.assigned_rider = None
            b.status = 'searching_rider'
            b.accepted_at = b.arrival_deadline = None
            b.dispatch_round = 0                     # fresh search; previous riders stay excluded via offers
            b.last_dispatch_at = None
            b.save(update_fields=['assigned_rider', 'status', 'accepted_at', 'arrival_deadline',
                                  'dispatch_round', 'last_dispatch_at', 'updated_at'])
        if penalize and rider:
            rider.add_strike(reason or f'Dropped booking {self.booking_ref}')
        self.refresh_from_db()
        return Result(True, 'Rider unassigned - searching again', rider)

    def mark_arrived(self):
        if self.status != 'assigned':
            return Result(False, 'Booking is not assigned')
        self.arrived_at = timezone.now()
        self.save(update_fields=['arrived_at', 'updated_at'])
        return Result(True, 'Arrival recorded')

    def start_booking(self, otp=None, force=False):
        with transaction.atomic():
            b = self._lock()
            if b.status != 'assigned':
                return Result(False, 'Booking must be assigned first')
            if not force and not (otp and secrets.compare_digest(str(otp), b.pickup_otp)):
                return Result(False, 'Invalid pickup code')
            b.status, b.started_at = 'in_progress', timezone.now()
            b.save(update_fields=['status', 'started_at', 'updated_at'])
        self.refresh_from_db()
        return Result(True, 'Booking started')

    def complete_booking(self, force=False):
        """Release escrow and pay the rider - fully atomic and idempotent."""
        try:
            with transaction.atomic():
                b = self._lock()
                if b.status == 'completed':
                    return Result(True, 'Already completed (idempotent)')
                if b.status != 'in_progress':
                    return Result(False, 'Booking not in progress')
                if b.payment_status != 'held' and not force:
                    return Result(False, 'Payment must be held in escrow before completing')
                now = timezone.now()
                hold = SystemConfig.get_int('earnings_hold_minutes', 0)
                b.status, b.completed_at, b.payment_status = 'completed', now, 'released'
                b.earnings_release_at = now + timedelta(minutes=hold) if hold else now
                b.earnings_released_at = None if hold else now
                b.save(update_fields=['status', 'completed_at', 'payment_status', 'earnings_release_at',
                                      'earnings_released_at', 'updated_at'])
                if b.assigned_rider_id and b.rider_payout > 0:
                    wallet, _ = RiderWallet.objects.get_or_create(rider_id=b.assigned_rider_id)
                    res = wallet.credit_earnings(b.rider_payout, b, hold_minutes=hold)
                    if not res.ok:
                        raise BookingError(res.message)
                if b.assigned_rider_id:
                    Rider.objects.filter(pk=b.assigned_rider_id).update(total_completed=F('total_completed') + 1)
        except BookingError as e:
            return Result(False, str(e))
        self.refresh_from_db()
        return Result(True, 'Booking completed & earnings credited')

    def cancel_booking(self, cancelled_by_user=None, by_role='customer', reason=''):
        with transaction.atomic():
            b = self._lock()
            if b.status in ('completed', 'cancelled', 'expired'):
                return Result(False, 'Booking is already closed')
            b.status, b.cancelled_at = 'cancelled', timezone.now()
            b.cancelled_by, b.cancelled_by_role, b.cancellation_reason = cancelled_by_user, by_role, reason
            b.save(update_fields=['status', 'cancelled_at', 'cancelled_by', 'cancelled_by_role',
                                  'cancellation_reason', 'updated_at'])
            b.dispatch_offers.filter(status='offered').update(status='cancelled', responded_at=timezone.now())
            if b.payment_status == 'held':
                b.request_refund(reason=f"Booking cancelled: {reason}")
        self.refresh_from_db()
        return Result(True, 'Booking cancelled')

    def rider_cancel(self, reason=''):
        """Rider backs out after accepting: job returns to the pool + strike (customer is NOT cancelled)."""
        return self.unassign_rider(reason or 'Rider cancelled', penalize=True)

    # ---- payments ----
    def confirm_payment_verified(self, provider_ref, payment_ref=None):
        """Called by the payment webhook / wallet payment. Idempotent. Triggers automatic dispatch."""
        try:
            with transaction.atomic():
                b = self._lock()
                if payment_ref and b.payment_ref and payment_ref != b.payment_ref:
                    return Result(False, 'Reference mismatch: cannot change an existing payment reference')
                if b.payment_status in ('held', 'released'):
                    return Result(True, 'Already verified (idempotent)')
                if b.status in ('cancelled', 'expired', 'completed'):
                    return Result(False, 'Booking is closed - payment must be refunded')
                b.payment_ref = payment_ref or b.payment_ref
                b.provider_ref = provider_ref or b.provider_ref
                b.payment_status = 'held'
                b.save(update_fields=['payment_ref', 'provider_ref', 'payment_status', 'updated_at'])
                transaction.on_commit(lambda: _enqueue_dispatch(b.pk))
        except IntegrityError:
            return Result(False, 'Payment reference already used by another booking')
        self.refresh_from_db()
        return Result(True, 'Payment verified - held securely until ride completion')

    def mark_payment_failed(self, reason=''):
        if self.payment_status in ('held', 'released'):
            return Result(False, 'Payment already confirmed')
        self.payment_status = 'failed'
        self.save(update_fields=['payment_status', 'updated_at'])
        return Result(True, 'Payment marked failed')

    def refunded_total(self):
        return Refund.objects.filter(booking=self, status__in=['pending', 'completed']).aggregate(
            s=Sum('amount'))['s'] or Decimal('0.00')

    def request_refund(self, amount=None, reason='', force=False):
        """Queue a refund (processed automatically). force=True lets admin refund released funds."""
        with transaction.atomic():
            b = self._lock()
            allowed = ('held', 'released') if force else ('held',)
            if b.payment_status not in allowed:
                return Result(False, 'No refundable funds for this booking')
            refundable = b.total_fare - b.refunded_total()
            amount = round_money(amount if amount is not None else refundable)
            if amount <= 0 or amount > refundable:
                return Result(False, f'Refund must be between 0.01 and {refundable}')
            refund = Refund.objects.create(booking=b, amount=amount, reason=reason)
            b.payment_status = 'refund_initiated'
            b.save(update_fields=['payment_status', 'updated_at'])
            transaction.on_commit(lambda: _enqueue_refund(refund.pk))
        self.refresh_from_db()
        return Result(True, f'Refund queued: {refund.reference}', refund)

    process_refund = request_refund  # backwards-compatible name


def _enqueue_dispatch(booking_id):
    from . import services
    services.enqueue('dispatch_booking_task', str(booking_id))


def _enqueue_refund(refund_id):
    from . import services
    services.enqueue('process_refund_task', str(refund_id))


class Refund(models.Model):
    STATUS = [('pending', 'Pending'), ('completed', 'Completed'), ('failed', 'Failed - needs admin')]
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    booking = models.ForeignKey(Booking, on_delete=models.PROTECT, related_name='refunds')
    amount = models.DecimalField(max_digits=10, decimal_places=2, validators=[validate_positive_amount])
    reference = models.CharField(max_length=30, unique=True, default=generate_refund_ref)
    status = models.CharField(max_length=10, choices=STATUS, default='pending', db_index=True)
    reason = models.CharField(max_length=255, blank=True)
    provider_ref = models.CharField(max_length=255, blank=True)
    attempts = models.PositiveSmallIntegerField(default=0)
    last_attempt_at = models.DateTimeField(null=True, blank=True)
    last_error = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.reference} - GHS {self.amount} - {self.status}"


# ==========================================================
#  DISPATCH OFFERS - how jobs reach qualified riders
# ==========================================================
class DispatchOffer(models.Model):
    STATUS = [('offered', 'Offered'), ('accepted', 'Accepted'), ('declined', 'Declined'),
              ('expired', 'Expired'), ('cancelled', 'Cancelled (job taken)')]
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    booking = models.ForeignKey(Booking, on_delete=models.CASCADE, related_name='dispatch_offers')
    rider = models.ForeignKey(Rider, on_delete=models.CASCADE, related_name='dispatch_offers')
    status = models.CharField(max_length=10, choices=STATUS, default='offered', db_index=True)
    round_number = models.PositiveSmallIntegerField(default=1)
    distance_km = models.FloatField(null=True, blank=True)
    offered_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField(db_index=True)
    responded_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-offered_at']
        constraints = [models.UniqueConstraint(fields=['booking', 'rider'], name='one_offer_per_rider_per_booking')]
        indexes = [models.Index(fields=['status', 'expires_at']), models.Index(fields=['rider', 'status'])]

    def __str__(self):
        return f"{self.booking.booking_ref} -> {self.rider.full_name} ({self.status})"

    @property
    def is_expired(self):
        return timezone.now() >= self.expires_at


# ==========================================================
#  RATINGS & COMPLAINTS
# ==========================================================
class RiderRating(models.Model):
    booking = models.OneToOneField(Booking, on_delete=models.CASCADE, related_name='rating')
    rider = models.ForeignKey(Rider, on_delete=models.CASCADE, related_name='ratings')
    customer = models.ForeignKey(CustomerProfile, on_delete=models.SET_NULL, null=True, blank=True)
    score = models.PositiveSmallIntegerField(validators=[MinValueValidator(1), MaxValueValidator(5)])
    comment = models.CharField(max_length=500, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.rider.full_name}: {self.score}/5"


class RiderComplaint(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    booking = models.ForeignKey(Booking, on_delete=models.PROTECT, related_name='complaints')
    complainant = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='filed_complaints')
    complaint_type = models.CharField(max_length=40, choices=COMPLAINT_TYPE)
    description = models.TextField()
    status = models.CharField(max_length=20, choices=COMPLAINT_STATUS, default='pending', db_index=True)
    resolution_notes = models.TextField(blank=True)
    action_taken = models.CharField(max_length=100, blank=True)
    reviewed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True,
                                    related_name='reviewed_complaints')
    reviewed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [models.Index(fields=['booking', 'status']), models.Index(fields=['status', 'created_at'])]

    def __str__(self):
        return f"{self.booking.booking_ref} - {self.get_complaint_type_display()} - {self.status}"

    def _close(self, status, reviewer, notes, action=''):
        self.status, self.reviewed_by, self.reviewed_at = status, reviewer, timezone.now()
        self.resolution_notes, self.action_taken = notes, action or status
        self.save()
        return True

    def resolve_complaint(self, reviewer, resolution_notes, action_taken=''):
        return self._close('resolved', reviewer, resolution_notes, action_taken)

    def issue_warning(self, reviewer, notes):
        return self._close('warning_issued', reviewer, notes, 'warning_issued')

    def dismiss_complaint(self, reviewer, notes=''):
        return self._close('dismissed', reviewer, notes or 'No action required', 'dismissed')

    def restrict_rider(self, reviewer, notes, restricted_until=None):
        rider = self.booking.assigned_rider
        if not rider:
            return Result(False, 'No rider assigned to booking')
        with transaction.atomic():
            self._close('restricted', reviewer, notes, 'rider_restricted')
            r = Rider.objects.select_for_update().get(pk=rider.pk)
            r.restriction_notes = f"Complaint {self.pk}: {notes}"
            r.restricted_until = restricted_until
            r.is_restricted = restricted_until is None     # no end date => until admin lifts it
            r.is_online = False
            r.save()
        return Result(True, 'Rider restricted')


# ==========================================================
#  NOTIFICATIONS, AUDIT, ALERTS, REPORTING
# ==========================================================
class Notification(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='notifications')
    title = models.CharField(max_length=200)
    body = models.TextField(blank=True)
    kind = models.CharField(max_length=30, default='info', db_index=True)
    data = models.JSONField(default=dict, blank=True)
    read_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [models.Index(fields=['user', 'read_at'])]

    def __str__(self):
        return f"{self.user_id}: {self.title}"

    def mark_read(self):
        if not self.read_at:
            self.read_at = timezone.now()
            self.save(update_fields=['read_at'])


class AuditLog(models.Model):
    """Who did what - robots ('system') and admins alike. Append-only."""
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                              related_name='+')
    actor_label = models.CharField(max_length=50, default='system')
    action = models.CharField(max_length=80, db_index=True)
    object_type = models.CharField(max_length=60, blank=True)
    object_id = models.CharField(max_length=64, blank=True, db_index=True)
    details = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"[{self.actor_label}] {self.action} {self.object_type}:{self.object_id}"

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError("Audit log entries are immutable.")
        super().save(*args, **kwargs)


class SystemAlert(models.Model):
    LEVEL_CHOICES = [('info', 'Info'), ('warning', 'Warning'), ('error', 'Error'), ('critical', 'Critical')]
    level = models.CharField(max_length=20, choices=LEVEL_CHOICES, default='warning')
    title = models.CharField(max_length=200)
    message = models.TextField()
    booking_ref = models.CharField(max_length=30, blank=True, null=True, db_index=True)
    resolved = models.BooleanField(default=False, db_index=True)
    resolved_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.get_level_display()}: {self.title} - {'Resolved' if self.resolved else 'Active'}"

    @classmethod
    def create_alert(cls, level, title, message, booking_ref=None, dedupe=False):
        if dedupe and cls.objects.filter(title=title, booking_ref=booking_ref, resolved=False).exists():
            return None
        return cls.objects.create(level=level, title=title, message=message, booking_ref=booking_ref)

    def mark_resolved(self):
        self.resolved, self.resolved_at = True, timezone.now()
        self.save(update_fields=['resolved', 'resolved_at'])


class DailySummary(models.Model):
    date = models.DateField(unique=True)
    total_bookings = models.IntegerField(default=0)
    completed_bookings = models.IntegerField(default=0)
    cancelled_bookings = models.IntegerField(default=0)
    expired_bookings = models.IntegerField(default=0)
    total_revenue = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0.00'))
    total_commission = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0.00'))
    total_payouts = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0.00'))
    total_refunds = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0.00'))
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-date']
        verbose_name_plural = "Daily Summaries"

    def __str__(self):
        return f"{self.date} - GHS {self.total_revenue} / {self.completed_bookings} jobs"

    @classmethod
    def generate_for_date(cls, target_date=None):
        """Rebuild totals from source records - safe to re-run."""
        target_date = target_date or timezone.localdate()
        completed = Booking.objects.filter(status='completed', completed_at__date=target_date)
        agg = completed.aggregate(rev=Sum('total_fare'), com=Sum('platform_commission'), pay=Sum('rider_payout'))
        refunds = Refund.objects.filter(status='completed', completed_at__date=target_date).aggregate(
            s=Sum('amount'))['s'] or Decimal('0.00')
        summary, _ = cls.objects.update_or_create(date=target_date, defaults={
            'total_bookings': Booking.objects.filter(created_at__date=target_date).count(),
            'completed_bookings': completed.count(),
            'cancelled_bookings': Booking.objects.filter(status='cancelled', cancelled_at__date=target_date).count(),
            'expired_bookings': Booking.objects.filter(status='expired', updated_at__date=target_date).count(),
            'total_revenue': agg['rev'] or Decimal('0.00'),
            'total_commission': agg['com'] or Decimal('0.00'),
            'total_payouts': agg['pay'] or Decimal('0.00'),
            'total_refunds': refunds,
        })
        return summary

from django.conf import settings
from django.db import models
from django.utils import timezone


class ServiceQualificationQuerySet(models.QuerySet):
    def approved(self):
        return self.filter(is_approved=True)

    def pending(self):
        return self.filter(is_approved=False)

    def for_rider(self, rider):
        return self.filter(rider=rider)

    def for_service(self, service):
        return self.filter(service=service)

    def with_related(self):
        return self.select_related("rider", "service", "approved_by")


class ServiceQualification(models.Model):
    """
    Tracks which services each rider is qualified/approved to provide.
    One record per rider + service combination.
    """

    rider = models.ForeignKey(
        "Rider",
        on_delete=models.CASCADE,
        related_name="service_qualifications",
    )
    service = models.ForeignKey(
        "Service",
        on_delete=models.CASCADE,
        related_name="rider_qualifications",
    )
    is_approved = models.BooleanField(default=False, db_index=True)
    approved_at = models.DateTimeField(null=True, blank=True)
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="approved_service_qualifications",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = ServiceQualificationQuerySet.as_manager()

    class Meta:
        verbose_name = "Service Qualification"
        verbose_name_plural = "Service Qualifications"
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["rider", "service"],
                name="unique_rider_service_qualification",
            ),
        ]
        indexes = [
            models.Index(fields=["service", "is_approved"]),
            models.Index(fields=["rider", "is_approved"]),
        ]

    def __str__(self):
        status = "Approved" if self.is_approved else "Pending"
        return f"{self.rider} - {self.service} ({status})"

    def approve(self, user=None):
        """Mark as approved and record who/when."""
        self.is_approved = True
        self.approved_at = timezone.now()
        self.approved_by = user
        self.save(
            update_fields=["is_approved", "approved_at", "approved_by", "updated_at"]
        )

    def revoke(self):
        """Remove approval and clear audit fields."""
        self.is_approved = False
        self.approved_at = None
        self.approved_by = None
        self.save(
            update_fields=["is_approved", "approved_at", "approved_by", "updated_at"]
        )
