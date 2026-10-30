from django.db import models, transaction, IntegrityError
from django.contrib.auth.models import User
from django.utils import timezone
from django.core.exceptions import ValidationError
from django.db.models import F, Q
import uuid
from decimal import Decimal, ROUND_HALF_UP
from math import radians, sin, cos, asin, sqrt

# ==========================================
#  SERVICE — Canonical Service Definitions
# ==========================================
class Service(models.Model):
    """
    Central registry of all services RYDNOW offers.
    Deactivating (is_active=False) hides from future assignments
    but preserves historical data forever.
    """
    SERVICE_OKADA = 'okada'
    SERVICE_PARCEL = 'parcel'
    SERVICE_WASTE_BIN = 'waste-bin'
    
    SERVICE_KEYS = [
        (SERVICE_OKADA, 'Okada Ride'),
        (SERVICE_PARCEL, 'Parcel Delivery'),
        (SERVICE_WASTE_BIN, 'Waste Collection'),
    ]

    key = models.CharField(
        max_length=30,
        primary_key=True,
        choices=SERVICE_KEYS,
        verbose_name='Service ID',
        help_text='Canonical service identifier: okada / parcel / waste-bin'
    )
    name = models.CharField(max_length=100, verbose_name='Display Name')
    description = models.TextField(blank=True, verbose_name='Description')
    is_active = models.BooleanField(
        default=True,
        verbose_name='Active',
        help_text='Inactive services are not available for new bookings but remain in historical records.'
    )

    class Meta:
        db_table = 'rydnow_service'
        ordering = ['key']
        verbose_name = 'Service'
        verbose_name_plural = 'Services'

    def __str__(self):
        return f"{self.name} ({'Active' if self.is_active else 'Inactive'})"

# ==========================================
#  HELPER FUNCTIONS
# ==========================================

def round_money(value):
    """Consistent 2-decimal rounding — ALL financial math uses this"""
    return Decimal(str(value)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

def validate_non_negative_amount(value):
    if value < Decimal('0'):
        raise ValidationError("Amount cannot be negative.")

def validate_positive_amount(value):
    if value <= Decimal('0'):
        raise ValidationError("Amount must be greater than zero.")

def validate_phone_number(value):
    if not value:
        return
    digits = ''.join(filter(str.isdigit, value))
    if len(digits) < 9 or len(digits) > 15:
        raise ValidationError("Phone number must contain 9-15 digits.")

def validate_latitude(value):
    if value is not None:
        value = float(value)
        if not (-90.0 <= value <= 90.0):
            raise ValidationError("Latitude must be between -90 and 90 degrees.")

def validate_longitude(value):
    if value is not None:
        value = float(value)
        if not (-180.0 <= value <= 180.0):
            raise ValidationError("Longitude must be between -180 and 180 degrees.")


# ==========================================
#  BUSINESS RULES — RYDNOW STANDARDS
# ==========================================
COMMISSION_RATE = Decimal('0.15')
RIDER_PAYOUT_RATE = Decimal('0.85')
CURRENCY_CHOICES = [('GHS', 'Ghana Cedi')]


# ==========================================
#  REFERENCE GENERATORS
# ==========================================
def generate_transaction_ref():
    return f"TX-{uuid.uuid4().hex[:12].upper()}"


def generate_booking_ref():
    return f"BK-{uuid.uuid4().hex[:12].upper()}"


def generate_withdrawal_ref():
    return f"WD-{uuid.uuid4().hex[:10].upper()}"


def generate_payment_ref():
    return f"PAY-{uuid.uuid4().hex[:10].upper()}"


# ==========================================
#  FARE VALIDATION
# ==========================================
def validate_fare_breakdown(total_fare, commission, payout):
    """Ensure 15% + 85% = 100% exactly within 1 pesewa tolerance"""
    if total_fare is None or total_fare <= 0:
        return True
    expected_commission = round_money(total_fare * COMMISSION_RATE)
    expected_payout = round_money(total_fare * RIDER_PAYOUT_RATE)
    diff = abs((expected_commission + expected_payout) - total_fare)
    if diff > Decimal('0.01'):
        raise ValidationError(
            f"Fare split error: Total={total_fare}, "
            f"Commission={commission} (expected {expected_commission}), "
            f"Payout={payout} (expected {expected_payout}). "
            f"Difference: {diff}. Check 15%/85% calculations."
        )
    return True


# ==========================================
#  CHOICE CONSTANTS
# ==========================================
SERVICE_CHOICES = [
    ('okada', 'Okada Ride'),
    ('parcel', 'Parcel Delivery'),
    ('waste-bin', 'Waste Collection'),
]

STATUS_CHOICES = [
    ('pending', 'Pending'),
    ('searching_rider', 'Searching Rider'),
    ('assigned', 'Rider Assigned'),
    ('in_progress', 'In Progress'),
    ('completed', 'Completed'),
    ('cancelled', 'Cancelled'),
]

STATUS_TRANSITIONS = {
    'pending': ['searching_rider', 'cancelled'],
    'searching_rider': ['assigned', 'cancelled'],
    'assigned': ['in_progress', 'cancelled'],
    'in_progress': ['completed', 'cancelled'],
    'completed': [],
    'cancelled': [],
}

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
    ('momo', 'Mobile Money'),
    ('visa', 'Visa Card'),
    ('debit', 'Debit Card'),
]

TRANSACTION_TYPES = [
    ('job_payment', 'Completed Job Payment'),
    ('deposit', 'Wallet Top-up'),
    ('withdrawal', 'Withdrawal'),
    ('commission', 'Platform Commission Deduction'),
    ('refund', 'Customer Refund'),
    ('bonus', 'Platform Bonus Credit'),
    ('adjustment', 'Manual Adjustment'),
    ('reversal', 'Transaction Reversal'),
    ('payout', 'Rider Earnings Payout'),
]

WITHDRAWAL_STATUS = [
    ('pending', 'Queued for Processing'),
    ('reserved', 'Funds Reserved — Sending'),
    ('validated', 'Verified — Sufficient Balance'),
    ('initiated', 'Sent to Mobile Money Provider'),
    ('completed', 'Successfully Sent'),
    ('failed', 'Provider Failed — Funds Returned'),
    ('reversed', 'System Reversed — Check Notes'),
    ('timeout', 'Provider Timeout — Funds Restored'),
]

COMPLAINT_STATUS = [
    ('pending', 'Under Review'),
    ('in_progress', 'Being Investigated'),
    ('resolved', 'Resolved — Closed'),
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


# ==========================================
#  CUSTOMER WALLET
# ==========================================
class CustomerWallet(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.OneToOneField(
        User, on_delete=models.PROTECT,
        related_name='customer_wallet', unique=True
    )
    balance = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal('0.00'),
        validators=[validate_non_negative_amount]
    )
    withdrawable_balance = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal('0.00'),
        validators=[validate_non_negative_amount]
    )
    bonus_credit = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal('0.00'),
        validators=[validate_non_negative_amount]
    )
    currency = models.CharField(max_length=3, choices=CURRENCY_CHOICES, default='GHS')
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)
class Meta:
    ordering = ['-created_at']
    

    def __str__(self):
        username = getattr(self.user, 'username', 'Deleted User')
        return f"{username} — {self.currency} {self.balance}"

    def deposit(self, amount, reference=None, description='', related_booking=None):
        amount = round_money(amount)
        validate_positive_amount(amount)
        ref = reference or generate_transaction_ref()

        try:
            with transaction.atomic():
                if WalletTransaction.objects.filter(reference=ref).exists():
                    return None, f"Duplicate transaction: {ref}"

                wallet = CustomerWallet.objects.select_for_update().get(pk=self.pk)
                wallet.full_clean(exclude=['user'])

                wallet.balance = F('balance') + amount
                wallet.withdrawable_balance = F('withdrawable_balance') + amount
                wallet.save(update_fields=['balance', 'withdrawable_balance', 'updated_at'])

                WalletTransaction.objects.create(
                    wallet=wallet, wallet_type='customer',
                    transaction_type='deposit', amount=amount,
                    reference=ref, description=description, currency=wallet.currency,
                    related_booking=related_booking, status='completed'
                )
                wallet.refresh_from_db()
                return wallet, "Deposit successful"
        except IntegrityError:
            if WalletTransaction.objects.filter(reference=ref).exists():
                return None, f"Duplicate transaction: {ref}"
            return None, "Database error"

    def withdraw(self, amount, reference=None, description=''):
        amount = round_money(amount)
        validate_positive_amount(amount)
        ref = reference or generate_transaction_ref()

        try:
            with transaction.atomic():
                if WalletTransaction.objects.filter(reference=ref).exists():
                    return False, f"Duplicate transaction: {ref}"

                wallet = CustomerWallet.objects.select_for_update().get(pk=self.pk)
                wallet.full_clean(exclude=['user'])

                if wallet.balance < amount:
                    return False, "Insufficient total balance"
                if wallet.withdrawable_balance < amount:
                    return False, "Insufficient withdrawable balance"

                wallet.balance = F('balance') - amount
                wallet.withdrawable_balance = F('withdrawable_balance') - amount
                wallet.save(update_fields=['balance', 'withdrawable_balance', 'updated_at'])

                WalletTransaction.objects.create(
                    wallet=wallet, wallet_type='customer',
                    transaction_type='withdrawal', amount=-amount,
                    reference=ref, description=description,
                    currency=wallet.currency, status='completed'
                )
                wallet.refresh_from_db()
                return True, "Withdrawal successful"
        except IntegrityError:
            if WalletTransaction.objects.filter(reference=ref).exists():
                return False, f"Duplicate transaction: {ref}"
            return False, "Database error"

# ==========================================
#  CHOICE CONSTANTS — ADD THESE NEAR THE TOP
# ==========================================

CURRENCY_CHOICES = [
    ('GHS', 'Ghana Cedi'),
]

TRANSACTION_TYPES = [
    ('deposit', 'Deposit'),
    ('withdrawal', 'Withdrawal'),
    ('payout', 'Earnings Payout'),
]

def generate_transaction_ref():
    return f"TXN-{uuid.uuid4().hex[:12].upper()}"


# ==========================================
#  CUSTOMER PROFILE
# ==========================================
class CustomerProfile(models.Model):
    user = models.OneToOneField(User,on_delete=models.PROTECT,
        related_name='customer_profile',
        unique=True
    )
    full_name = models.CharField(max_length=200, blank=True)
    email = models.EmailField(blank=True)
    phone = models.CharField(max_length=30, blank=True, null=True, validators=[validate_phone_number])
    profile_photo = models.ImageField(upload_to='profiles/customer/%Y/%m/', blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        name = self.full_name or getattr(self.user, 'username', None)
        return name or f"Customer-{str(self.pk)[:8]}"

# ==========================================
#  RIDER — Core Identity
# ==========================================
class Rider(models.Model):
    phone = models.CharField(max_length=20, validators=[])
    location = models.CharField(max_length=255, blank=True, null=True)
    current_lat = models.FloatField(null=True, blank=True, validators=[validate_latitude])
    current_lng = models.FloatField(null=True, blank=True, validators=[validate_longitude])
    last_location_update = models.DateTimeField(null=True, blank=True)
    verification_status = models.CharField(
        max_length=20, choices=VERIFICATION_STATUS,
        default='pending', db_index=True
    )
    is_restricted = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['verification_status', 'is_restricted']),
            models.Index(fields=['current_lat', 'current_lng']),
        ]

    def __str__(self):
        return f"{self.phone} — {self.verification_status}"

    @property
    def is_approved(self):
        return self.verification_status == 'approved' and not self.is_restricted

    def get_distance_km(self, lat, lng):
        """Calculate Haversine distance between this rider and given coordinates"""
        if None in (self.current_lat, self.current_lng, lat, lng):
            return None
        if self.current_lat == 0 and self.current_lng == 0:
            return None
        lat1 = radians(float(self.current_lat))
        lon1 = radians(float(self.current_lng))
        lat2 = radians(float(lat))
        lon2 = radians(float(lng))
        # ... rest of your function

    @property
    def can_go_online(self):
        """Rider can only go online if wallet balance >= ₵50"""
        min_balance = Decimal('50.00')
        wallet = getattr(self, 'wallet', None)
        if not wallet:
            return False
        return wallet.balance >= min_balance

    def deduct_commission(self, trip_amount):
        """Deduct 15% commission from wallet — returns True if successful"""
        COMMISSION_RATE = Decimal('0.15')
        commission = Decimal(str(trip_amount)) * COMMISSION_RATE
        
        wallet = self.wallet
        if wallet.balance >= commission:
            wallet.balance -= commission
            wallet.save()
            # Record transaction
            Transaction.objects.create(
                rider=self,
                amount=-commission,
                transaction_type='commission_deduction',
                description=f'Commission deducted — ₵{commission}'
            )
            return True, commission
        return False, 'Insufficient balance for commission'


    def __str__(self):
        return f"{self.name} — {self.verification_status}"

    @property
    def is_approved(self):
        return self.verification_status == 'approved' and not self.is_restricted

    def get_distance_km(self, lat, lng):
        """Calculate Haversine distance between this rider and given coordinates"""
        if None in (self.current_lat, self.current_lng, lat, lng):
            return None
        if self.current_lat == 0 and self.current_lng == 0:
            return None
        lat1 = max(-90.0, min(90.0, float(self.current_lat)))
        lon1 = max(-180.0, min(180.0, float(self.current_lng)))
        lat2 = max(-90.0, min(90.0, float(lat)))
        lon2 = max(-180.0, min(180.0, float(lng)))
        R = 6371  # Earth radius in km
        dlat = radians(lat2 - lat1)
        dlon = radians(lon2 - lon1)
        a = sin(dlat / 2)**2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon / 2)**2
        a = max(0.0, min(1.0, a))
        c = 2 * asin(sqrt(a))
        return round(R * c, 2)

    services = models.ManyToManyField(
        Service,
        blank=True,
        related_name='qualified_riders',
        verbose_name='Qualified Services',
        help_text="Services this rider is authorised and equipped to provide. Leave empty = no service yet."
    )


class Transaction(models.Model):
    rider = models.ForeignKey(Rider, on_delete=models.CASCADE, related_name='transactions')
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    transaction_type = models.CharField(max_length=50, choices=[
        ('deposit', 'Deposit'),
        ('commission_deduction', 'Commission Deduction'),
        ('withdrawal', 'Withdrawal'),
        ('trip_earning', 'Trip Earning'),
    ])
    description = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.rider} — {self.transaction_type} — GH₵{abs(self.amount)}"


# ==========================================
#  WALLET TRANSACTION
# ==========================================
class WalletTransaction(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    wallet = models.ForeignKey(
        CustomerWallet, on_delete=models.PROTECT,
        related_name='transactions', null=True, blank=True
    )
    rider_wallet = models.ForeignKey(
        'RiderWallet', on_delete=models.PROTECT,
        related_name='transactions', null=True, blank=True
    )
    wallet_type = models.CharField(max_length=20, default='customer', db_index=True)
    transaction_type = models.CharField(max_length=20, choices=TRANSACTION_TYPES, db_index=True)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    currency = models.CharField(max_length=3, choices=CURRENCY_CHOICES, default='GHS')
    reference = models.CharField(
        max_length=50, unique=True, db_index=True,
        default=generate_transaction_ref
    )
    status = models.CharField(max_length=20, default='completed', db_index=True)
    description = models.CharField(max_length=255, blank=True)
    related_booking = models.ForeignKey(
        'Booking', on_delete=models.SET_NULL,
        null=True, blank=True, related_name='wallet_transactions'
    )
    provider_ref = models.CharField(max_length=255, blank=True)
    idempotency_key = models.CharField(max_length=100, blank=True, db_index=True)
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

class Meta:
    ordering=['-created_at']
    indexes=[
        models.Index(fields=['wallet','-created_at']),
        models.Index(fields=['rider_wallet', '-created_at']),
        models.Index(fields=['transaction_type', 'created_at']),
        models.Index(fields=['reference']),
        models.Index(fields=['wallet_type', 'status', '-created_at'])
    ]
rider_wallet = models.ForeignKey('rider_wallet',on_delete=models.PROTECT,related_name='withdrawals')       
def __str__(self):
    return f"{self.reference} | {self.transaction_type} | {self.amount} {self.currency}"

# ==========================================
#  WITHDRAWAL REQUEST — Safe 2-phase payout workflow
# ==========================================
class WithdrawalRequest(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    wallet = models.ForeignKey(
        'RiderWallet', on_delete=models.PROTECT,
        related_name='withdrawals'
    )
    amount = models.DecimalField(
        max_digits=10, decimal_places=2, validators=[validate_positive_amount]
    )
    reference = models.CharField(
        max_length=30, unique=True, db_index=True,
        default=generate_withdrawal_ref
    )
    payment_method = models.CharField(max_length=20, choices=PAYMENT_METHOD_CHOICES)
    provider_ref = models.CharField(max_length=255, blank=True)
    idempotency_key = models.CharField(max_length=100, blank=True, db_index=True)
    status = models.CharField(
        max_length=20, choices=WITHDRAWAL_STATUS,
        default='pending', db_index=True
    )
    failure_reason = models.TextField(blank=True)
    processed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['wallet', '-created_at']),
            models.Index(fields=['status', 'created_at']),
            models.Index(fields=['reference']),
            models.Index(fields=['idempotency_key']),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=['reference'],
                name='unique_withdrawal_reference'
            ),
            models.UniqueConstraint(
                fields=['idempotency_key'],
                condition=~models.Q(idempotency_key=''),
                name='unique_withdrawal_idempotency'
            ),
        ]

    def __str__(self):
        return f"{self.reference} — {self.amount} GHS — {self.get_status_display()}"

    def reserve_funds(self):
        with transaction.atomic():
            if self.status != 'pending':
                return False, "Cannot reserve: invalid status"
            success, msg = self.wallet.reserve_funds(self.amount)
            if success:
                self.status = 'reserved'
                self.full_clean()
                self.save()
            return success, msg

    def confirm_payout_initiated(self, provider_ref=''):
        if self.status != 'reserved':
            return False, "Funds must be reserved first"
        self.status = 'initiated'
        if provider_ref:
            self.provider_ref = provider_ref
        self.processed_at = timezone.now()
        self.full_clean()
        self.save()
        return True, "Payout initiated"

    def confirm_payout_completed(self, provider_ref=''):
        with transaction.atomic():
            if self.status not in ['reserved', 'initiated']:
                return False, "Invalid state for completion"
            success, msg = self.wallet.confirm_withdrawal_debit(self.amount)
            if success:
                self.status = 'completed'
                if provider_ref:
                    self.provider_ref = provider_ref
                self.processed_at = timezone.now()
                self.full_clean()
                self.save()
            return success, msg

    def mark_payout_failed(self, reason='Provider error'):
        """Restore reserved funds ONLY if we are certain the payout failed"""
        with transaction.atomic():
            if self.status in ['completed', 'reversed']:
                return False, "Cannot fail completed payout"

            if self.status == 'reserved':
                self.wallet.restore_funds(self.amount, reason=f"Payout failed: {reason}")

            self.status = 'failed'
            self.failure_reason = reason
            self.processed_at = timezone.now()
            self.full_clean()
            self.save()
            return True, "Funds restored, request marked failed"

    def mark_timeout(self, provider_known_failure=False):
        """
        Handle uncertain provider responses cautiously.
        Only auto-restore if provider_known_failure is confirmed.
        """
        if self.status in ['pending', 'reserved', 'initiated']:
            if provider_known_failure:
                return self.mark_payout_failed("Provider response timeout — confirmed failed")
            else:
                self.status = 'timeout'
                self.failure_reason = "Provider timeout — funds held for reconciliation"
                self.processed_at = timezone.now()
                self.full_clean()
                self.save()
                return True, "Timeout recorded — requires manual reconciliation"
        return False, "Cannot timeout in current state"

    def process_payout(self, provider_callback=None):
        """
        Safe payout workflow:
        1. Reserve funds atomically
        2. Call provider OUTSIDE the database transaction
        3. Confirm or fail based on provider response
        """
        if self.status != 'pending':
            if self.status == 'completed':
                return True, "Already completed (idempotent)"
            return False, f"Cannot process from status: {self.status}"

        success, msg = self.reserve_funds()
        if not success:
            return False, msg

        provider_result = True
        provider_response = {}
        if callable(provider_callback):
            try:
                provider_result, provider_response = provider_callback(
                    amount=self.amount,
                    reference=self.reference,
                    idempotency_key=self.idempotency_key
                )
            except Exception as e:
                self.mark_timeout(provider_known_failure=False)
                return False, f"Provider error: {str(e)}"

        if provider_result:
            provider_ref = provider_response.get('provider_ref', '')
            self.confirm_payout_initiated(provider_ref=provider_ref)
            return self.confirm_payout_completed(provider_ref=provider_ref)
        else:
            error = provider_response.get('error', 'Provider communication failed')
            return self.mark_payout_failed(reason=error)

    def retry_failed(self, new_idempotency_key=None):
        """Create new request; preserve original failure record"""
        if self.status != 'failed':
            return None, "Only failed requests can be retried"
        key = new_idempotency_key or f"RETRY-{self.reference}-{uuid.uuid4().hex[:6]}"
        return self.wallet.create_withdrawal_request(
            amount=self.amount,
            method=self.payment_method,
            idempotency_key=key
        )
    
# ==========================================
#  BOOKING — Complete with fare breakdown & locking
# ==========================================
class Booking(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    booking_ref = models.CharField(
        max_length=16, unique=True, blank=True, db_index=True,
        default=generate_booking_ref
    )
    service_type = models.CharField(max_length=20, choices=SERVICE_CHOICES)
    customer = models.ForeignKey(
        CustomerProfile, on_delete=models.PROTECT,
        related_name='bookings', null=True, blank=True
    )

    pickup = models.CharField(max_length=255, blank=True, default='')
    pickup_lat = models.FloatField(validators=[validate_latitude], null=True, blank=True, default=0.0)
    pickup_lng = models.FloatField(validators=[validate_longitude], null=True, blank=True, default=0.0)
    destination = models.CharField(max_length=255, blank=True, default='')
    destination_lat = models.FloatField(validators=[validate_latitude], null=True, blank=True, default=0.0)
    destination_lng = models.FloatField(validators=[validate_longitude], null=True, blank=True, default=0.0)
    estimated_distance_km = models.FloatField(null=True, blank=True, default=0.0)
    estimated_duration_min = models.IntegerField(null=True, blank=True, default=0)

    status = models.CharField(
        max_length=20, choices=STATUS_CHOICES,
        default='pending', db_index=True
    )

    assigned_rider = models.ForeignKey(
        Rider, on_delete=models.PROTECT,
        related_name='assigned_bookings', null=True, blank=True
    )
    accepted_at = models.DateTimeField(null=True, blank=True)

    total_fare = models.DecimalField(
        max_digits=10, decimal_places=2, default=Decimal('0.00'),
        validators=[validate_non_negative_amount]
    )
    platform_commission = models.DecimalField(
        max_digits=10, decimal_places=2, default=Decimal('0.00'),
        validators=[validate_non_negative_amount]
    )
    rider_payout = models.DecimalField(
        max_digits=10, decimal_places=2, default=Decimal('0.00'),
        validators=[validate_non_negative_amount]
    )

    payment_status = models.CharField(
        max_length=30, choices=PAYMENT_STATUS,
        default='initiated', db_index=True
    )
    payment_method = models.CharField(
        max_length=20, choices=PAYMENT_METHOD_CHOICES,
        null=True, blank=True
    )
    payment_ref = models.CharField(max_length=100, blank=True, db_index=True)
    provider_ref = models.CharField(max_length=255, blank=True)

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    scheduled_at = models.DateTimeField(null=True, blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    cancelled_at = models.DateTimeField(null=True, blank=True)
    cancelled_by = models.ForeignKey(
        User, on_delete=models.PROTECT, null=True, blank=True
    )
    cancellation_reason = models.TextField(blank=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['customer', '-created_at']),
            models.Index(fields=['assigned_rider', 'status']),
            models.Index(fields=['status', 'created_at']),
            models.Index(fields=['booking_ref']),
            models.Index(fields=['payment_ref']),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=['payment_ref'],
                condition=~models.Q(payment_ref=''),
                name='unique_booking_payment_ref'
            )
        ]

    def __str__(self):
        return f"{self.booking_ref} — {self.get_status_display()}"

    def clean(self):
        super().clean()
        validate_fare_breakdown(self.total_fare, self.platform_commission, self.rider_payout)

    def save(self, *args, **kwargs):
        """Auto-calculate 15% / 85% split when total_fare is set"""
        if self.total_fare > 0 and self.platform_commission == 0 and self.rider_payout == 0:
            self.platform_commission = round_money(self.total_fare * COMMISSION_RATE)
            self.rider_payout = round_money(self.total_fare * RIDER_PAYOUT_RATE)
        super().save(*args, **kwargs)

    def get_distance_km(self, from_lat, from_lng, to_lat, to_lng):
        """Haversine distance calculation for pickup → destination or rider → pickup"""
        if None in (from_lat, from_lng, to_lat, to_lng):
            return None
        lat1 = max(-90.0, min(90.0, float(from_lat)))
        lon1 = max(-180.0, min(180.0, float(from_lng)))
        lat2 = max(-90.0, min(90.0, float(to_lat)))
        lon2 = max(-180.0, min(180.0, float(to_lng)))
        R = 6371
        dlat = radians(lat2 - lat1)
        dlon = radians(lon2 - lon1)
        a = sin(dlat / 2)**2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon / 2)**2
        a = max(0.0, min(1.0, a))
        c = 2 * asin(sqrt(a))
        return round(R * c, 2)

    def assign_rider(self, rider):
        """Atomic rider assignment — prevents double-booking race conditions"""
        with transaction.atomic():
            if self.status not in ['pending', 'searching_rider']:
                return False, "Booking no longer available"

            booking = Booking.objects.select_for_update().get(pk=self.pk)

            if booking.status not in ['pending', 'searching_rider']:
                return False, "Booking no longer available"

            try:
                profile = RiderProfile.objects.get(rider=rider)
                if not profile.is_eligible:
                    return False, "Rider not eligible or offline"
            except RiderProfile.DoesNotExist:
                return False, "Rider profile not found"

            active_count = Booking.objects.filter(
                assigned_rider=rider,
                status__in=['assigned', 'in_progress']
            ).exclude(pk=self.pk).count()
            if active_count > 0:
                return False, "Rider has an active booking already"

            booking.assigned_rider = rider
            booking.status = 'assigned'
            booking.accepted_at = timezone.now()
            booking.full_clean()
            booking.save()

            self.assigned_rider = rider
            self.status = 'assigned'
            self.accepted_at = booking.accepted_at
        return True, "Rider assigned successfully"

    def start_booking(self):
        if self.status != 'assigned':
            return False, "Booking must be assigned first"
        self.status = 'in_progress'
        self.started_at = timezone.now()
        self.full_clean()
        self.save()
        return True, "Booking started"

    def complete_booking(self, payment_verified=False, provider_ref=''):
        if self.status != 'in_progress':
            return False, "Booking not in progress"
        if not payment_verified:
            return False, "Payment must be verified before completing"
        if self.payment_status not in ['held', 'released']:
            return False, "Payment not confirmed"

        with transaction.atomic():
            booking = Booking.objects.select_for_update().get(pk=self.pk)

            if booking.status == 'completed':
                return True, "Booking already completed (idempotent)"
            if booking.status != 'in_progress':
                return False, "Invalid state transition"

            booking.status = 'completed'
            booking.completed_at = timezone.now()
            if provider_ref:
                booking.provider_ref = provider_ref
            booking.full_clean()
            booking.save()

            if booking.rider_payout > 0 and booking.assigned_rider:
                wallet, _ = RiderWallet.objects.get_or_create(rider=booking.assigned_rider)
                wallet.credit_earnings(
                    booking.rider_payout,
                    booking_ref=booking.booking_ref,
                    payment_verified=True
                )

            self.status = booking.status
            self.completed_at = booking.completed_at
        return True, "Booking completed & earnings credited"

    def cancel_booking(self, cancelled_by_user=None, reason=''):
        if self.status in ['completed', 'cancelled']:
            return False, "Cannot cancel completed or already cancelled booking"

        with transaction.atomic():
            booking = Booking.objects.select_for_update().get(pk=self.pk)

            if booking.status in ['completed', 'cancelled']:
                return False, "Cannot cancel booking"

            booking.status = 'cancelled'
            booking.cancelled_at = timezone.now()
            booking.cancelled_by = cancelled_by_user
            booking.cancellation_reason = reason
            booking.full_clean()
            booking.save()

            if booking.payment_status in ['held', 'released']:
                booking.process_refund(amount=None, reason=f"Booking cancelled: {reason}")

            self.status = booking.status
            self.cancelled_at = booking.cancelled_at
        return True, "Booking cancelled"

    def confirm_payment_verified(self, provider_ref, payment_ref=None):
        if self.payment_status in ['held', 'released']:
            return False, "Reference mismatch: booking already verified with a different reference"

        # Pending booking – protect existing reference from being overwritten
        if payment_ref and self.payment_ref and payment_ref != self.payment_ref:
            return False, "Reference mismatch: cannot change an existing payment reference"

        # Safe to store – new reference or empty-first-time
        if payment_ref:
            self.provider_ref = payment_ref

        # Never use provider_ref as fallback – leave empty if not provided
        self.payment_status = 'held'
        self.full_clean()
        self.save()
        return True, "Payment verified – held securely until ride completion"
    
    def process_refund(self, amount=None, reason="", provider_confirm_callback=None):
        if self.payment_status not in ['held', 'released']:
            return False, "No refundable funds for this booking"

        # Block retries after a failed attempt
        ref_intent_key = f"REF-{self.booking_ref}"
        existing_failed = WalletTransaction.objects.filter(
            reference_startswith=ref_intent_key,
            status='failed'
        ).exists()

        if existing_failed:
            fresh_booking = Booking.objects.get(pk=self.pk)
            payment_ref = getattr(fresh_booking, 'payment_ref', None)

        if not payment_ref or not str(payment_ref).strip():
            return False, "Original payment reference missing – cannot submit refund"

        refund_amount = round_money(amount or self.total_fare)
        if refund_amount <= Decimal('0'):
            return False, "Refund amount must be postive"
        if refund_amount > self.total_fare:
            return False, "Refund amount cannot exceed total fare"
        if WalletTransaction.objects.filter(reference_startswith=ref_intent_key).exists():
            return True, "Refund already processed (idempotent)"

        if callable(provider_confirm_callback):
            provider_success = provider_confirm_callback(amount = refund_amount,original_ref = self.payment_ref)

        if not provider_success:
            return False, "Provider refund failed - refund not marked complete"
        WalletTransaction.objects.create(
            wallet_type='customer',
            transaction_type='refund',
            amount=refund_amount,
            reference=ref,
            description=f"Refund:{reason}",
            related_booking=self,
            status='completed'
        )
        if refund_amount == self.total_fare:
            self.payment_status='refunded'
        else:
            self.payment_status='refund_initiated' 
            self.save()
        return True, f"Refund processed: {ref}"

# ==========================================
#  RIDER PROFILE — Eligibility & Extended Data
# ==========================================
class RiderProfile(models.Model):
    rider = models.OneToOneField(
        Rider, on_delete=models.PROTECT,
        related_name='profile', unique=True
    )
    full_name = models.CharField(max_length=200)
    email = models.EmailField()
    phone = models.CharField(max_length=30, blank=True, validators=[validate_phone_number])
    motorbike_model = models.CharField(max_length=200, blank=True)
    license_number = models.CharField(max_length=100, blank=True)

    ghana_card_doc = models.URLField(max_length=500, blank=True)
    passport_photo = models.URLField(max_length=500, blank=True)
    license_doc = models.URLField(max_length=500, blank=True)

    is_online = models.BooleanField(default=False)
    is_restricted = models.BooleanField(default=False)
    restriction_notes = models.TextField(blank=True)
    restricted_until = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        status = "Online" if self.is_eligible else "Offline"
        return f"{self.full_name} — {status}"

    @property
    def is_eligible(self):
        if self.is_restricted:
            return False
        if self.restricted_until and timezone.now() < self.restricted_until:
            return False
        if not hasattr(self, 'rider'):
            return False
        return (
            self.is_online
            and self.rider.verification_status == 'approved'
            and not self.rider.is_restricted
        )

    def save(self, *args, **kwargs):
        if self.is_restricted or (self.restricted_until and timezone.now() < self.restricted_until):
            self.is_online = False
        super().save(*args, **kwargs)


# ==========================================
#  RIDER WALLET — Separate from Customer Wallet
# ==========================================
class RiderWallet(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    rider = models.OneToOneField(
        Rider, on_delete=models.PROTECT,
        related_name='wallet', unique=True
    )
    balance = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal('0.00'),
        validators=[validate_non_negative_amount]
    )
    available_balance = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal('0.00'),
        validators=[validate_non_negative_amount]
    )
    pending_earnings = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal('0.00')
    )
    currency = models.CharField(max_length=3, choices=CURRENCY_CHOICES, default='GHS')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.rider.full_name} — {self.currency} {self.balance}"

    def hold_pending_earnings(self, amount, booking_ref=''):
        """Hold earnings securely until booking completes"""
        amount = round_money(amount)
        validate_positive_amount(amount)

        with transaction.atomic():
            wallet = RiderWallet.objects.select_for_update().get(pk=self.pk)
            wallet.full_clean(exclude=['rider'])

            wallet.pending_earnings = F('pending_earnings') + amount
            wallet.save(update_fields=['pending_earnings', 'updated_at'])
            wallet.refresh_from_db()
        return wallet, "Earnings held safely for release on completion"

    def credit_earnings(self, amount, booking_ref='', payment_verified=False):
        """Release held earnings to rider balance — ONLY once payment is verified"""
        amount = round_money(amount)
        validate_positive_amount(amount)

        if self.pending_earnings < amount:
            return None, "Insufficient pending earnings to release"
        if not payment_verified:
            return None, "Payment must be verified before crediting earnings"

        ref = f"PAY-{booking_ref or generate_transaction_ref()}"

        with transaction.atomic():
            if WalletTransaction.objects.filter(reference=ref).exists():
                return None, f"Earnings already credited: {ref}"

            wallet = RiderWallet.objects.select_for_update().get(pk=self.pk)
            wallet.full_clean(exclude=['rider'])

            wallet.balance = F('balance') + amount
            wallet.available_balance = F('available_balance') + amount
            wallet.pending_earnings = F('pending_earnings') - amount
            wallet.save(update_fields=[
                'balance', 'available_balance', 'pending_earnings', 'updated_at'
            ])

            WalletTransaction.objects.create(
                rider_wallet=wallet,
                wallet_type='rider',
                transaction_type='payout',
                amount=amount,
                reference=ref,
                description=f"Earnings credited: Booking {booking_ref}",
                currency=wallet.currency,
                status='completed'
            )
            wallet.refresh_from_db()
        return wallet, "Earnings credited successfully"

    def reserve_funds(self, amount):
        """Reserve funds during withdrawal process"""
        amount = round_money(amount)
        validate_positive_amount(amount)

        with transaction.atomic():
            wallet = RiderWallet.objects.select_for_update().get(pk=self.pk)
            wallet.full_clean(exclude=['rider'])
            if wallet.available_balance < amount:
                return False, "Insufficient available balance"
            wallet.available_balance = F('available_balance') - amount
            wallet.save(update_fields=['available_balance', 'updated_at'])
        return True, "Funds reserved"

    def confirm_withdrawal_debit(self, amount):
        """Final debit after provider confirms payout"""
        amount = round_money(amount)
        with transaction.atomic():
            wallet = RiderWallet.objects.select_for_update().get(pk=self.pk)
            wallet.full_clean(exclude=['rider'])
            wallet.balance = F('balance') - amount
            wallet.save(update_fields=['balance', 'updated_at'])
        return True, "Funds debited"

    def restore_funds(self, amount, reason=''):
        """Restore reserved funds if payout fails"""
        amount = round_money(amount)
        validate_positive_amount(amount)

        with transaction.atomic():
            wallet = RiderWallet.objects.select_for_update().get(pk=self.pk)
            wallet.full_clean(exclude=['rider'])
            wallet.available_balance = F('available_balance') + amount
            wallet.save(update_fields=['available_balance', 'updated_at'])

            WalletTransaction.objects.create(
                rider_wallet=wallet,
                wallet_type='rider',
                transaction_type='adjustment',
                amount=amount,
                reference=f"RESTORE-{generate_transaction_ref()}",
                description=f"Funds restored: {reason}",
                currency=wallet.currency,
                status='completed'
            )
        return True, "Funds restored"

    def create_withdrawal_request(self, amount, method, idempotency_key=''):
        """Initiate a withdrawal request — idempotent"""
        amount = round_money(amount)
        validate_positive_amount(amount)

        if WithdrawalRequest.objects.filter(
            wallet=self, status__in=['pending', 'reserved', 'validated', 'initiated']
        ).exists():
            return None, "A withdrawal request is already in progress"

        ref = generate_withdrawal_ref()
        key = idempotency_key or ref

        if WithdrawalRequest.objects.filter(idempotency_key=key).exists():
            return None, "Duplicate request detected"

        return WithdrawalRequest.objects.create(
            wallet=self,
            amount=amount,
            reference=ref,
            payment_method=method,
            idempotency_key=key,
            status='pending'
        ), "Withdrawal request created"

# ==========================================
#  RIDER COMPLAINT — Dispute & Support Records
# ==========================================
class RiderComplaint(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    booking = models.ForeignKey(
        Booking, on_delete=models.PROTECT,
        related_name='complaints'
    )
    complainant = models.ForeignKey(
        User, on_delete=models.PROTECT,
        related_name='filed_complaints'
    )
    complaint_type = models.CharField(max_length=40, choices=COMPLAINT_TYPE)
    description = models.TextField()
    status = models.CharField(
        max_length=20, choices=COMPLAINT_STATUS,
        default='pending', db_index=True
    )
    resolution_notes = models.TextField(blank=True)
    action_taken = models.CharField(max_length=100, blank=True)
    reviewed_by = models.ForeignKey(
        User, on_delete=models.PROTECT, null=True, blank=True,
        related_name='reviewed_complaints'
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['booking', 'status']),
            models.Index(fields=['status', 'created_at']),
        ]

    def __str__(self):
        return f"{self.booking.booking_ref} — {self.get_complaint_type_display()} — {self.status}"

    def resolve_complaint(self, reviewer, resolution_notes, action_taken=''):
        self.status = 'resolved'
        self.reviewed_by = reviewer
        self.reviewed_at = timezone.now()
        self.resolution_notes = resolution_notes
        self.action_taken = action_taken
        self.full_clean()
        self.save()
        return True

    def issue_warning(self, reviewer, notes):
        self.status = 'warning_issued'
        self.reviewed_by = reviewer
        self.reviewed_at = timezone.now()
        self.resolution_notes = notes
        self.action_taken = 'warning_issued'
        self.full_clean()
        self.save()
        return True

    def restrict_rider(self, reviewer, notes, restricted_until=None):
        if not self.booking.assigned_rider:
            return False, "No rider assigned to booking"
        with transaction.atomic():
            self.status = 'restricted'
            self.reviewed_by = reviewer
            self.reviewed_at = timezone.now()
            self.resolution_notes = notes
            self.action_taken = 'rider_restricted'
            self.save()
            try:
                profile = RiderProfile.objects.get(rider=self.booking.assigned_rider)
                profile.is_restricted = True
                profile.restriction_notes = f"Complaint {self.pk}: {notes}"
                profile.restricted_until = restricted_until
                profile.full_clean()
                profile.save()
            except RiderProfile.DoesNotExist:
                pass
        return True, "Rider restricted"

    def dismiss_complaint(self, reviewer, notes=''):
        self.status = 'dismissed'
        self.reviewed_by = reviewer
        self.reviewed_at = timezone.now()
        self.resolution_notes = notes or "No action required"
        self.full_clean()
        self.save()
        return True


# ==========================================
#  SYSTEM CONFIGURATION — Singleton Platform Settings
# ==========================================
class SystemConfig(models.Model):
    """Singleton: Platform-wide settings — editable via admin only"""
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
        try:
            return cls.objects.get(key=key).value
        except cls.DoesNotExist:
            return default


# ==========================================
#  SYSTEM ALERTS — Get Notified Before Problems Grow
# ==========================================
class SystemAlert(models.Model):
    LEVEL_CHOICES = [
        ('info', 'Info'),
        ('warning', 'Warning'),
        ('error', 'Error'),
        ('critical', 'Critical'),
    ]
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
        status = "✓ Resolved" if self.resolved else "⚠️ Active"
        return f"{self.get_level_display()}: {self.title} — {status}"

    @classmethod
    def create_alert(cls, level, title, message, booking_ref=None):
        alert = cls.objects.create(
            level=level,
            title=title,
            message=message,
            booking_ref=booking_ref
        )
        return alert

    def mark_resolved(self):
        self.resolved = True
        self.resolved_at = timezone.now()
        self.save()


# ==========================================
#  DAILY SUMMARY — Reconciliation & Reporting
# ==========================================
class DailySummary(models.Model):
    date = models.DateField(unique=True, db_index=True)
    total_bookings = models.IntegerField(default=0)
    completed_bookings = models.IntegerField(default=0)
    cancelled_bookings = models.IntegerField(default=0)
    total_revenue = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal('0.00')
    )
    total_commission = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal('0.00')
    )
    total_payouts = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal('0.00')
    )
    total_refunds = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal('0.00')
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-date']
        verbose_name_plural = "Daily Summaries"

    def __str__(self):
        return f"{self.date} — ₵{self.total_revenue} / {self.completed_bookings} rides"

    @classmethod
    def generate_for_date(cls, target_date=None):
        """Rebuild daily totals from transaction records — safe to rerun"""
        from django.db.models import Sum, Count, Q

        if target_date is None:
            target_date = timezone.now().date()

        start = timezone.make_aware(timezone.datetime.combine(target_date, timezone.datetime.min.time()))
        end = timezone.make_aware(timezone.datetime.combine(target_date, timezone.datetime.max.time()))

        bookings = Booking.objects.filter(created_at__date=target_date)
        completed = bookings.filter(status='completed')
        cancelled = bookings.filter(status='cancelled')

        total_revenue = completed.aggregate(s=Sum('total_fare'))['s'] or Decimal('0.00')
        total_commission = completed.aggregate(s=Sum('platform_commission'))['s'] or Decimal('0.00')
        total_payouts = completed.aggregate(s=Sum('rider_payout'))['s'] or Decimal('0.00')

        refunds = WalletTransaction.objects.filter(
            transaction_type='refund',
            created_at__range=(start, end)
        ).aggregate(s=Sum('amount'))['s'] or Decimal('0.00')

        summary, _ = cls.objects.update_or_create(
            date=target_date,
            defaults={
                'total_bookings': bookings.count(),
                'completed_bookings': completed.count(),
                'cancelled_bookings': cancelled.count(),
                'total_revenue': total_revenue,
                'total_commission': total_commission,
                'total_payouts': total_payouts,
                'total_refunds': abs(refunds),
            }
        )
        return summary

import uuid
import os
import logging
from django.conf import settings
from django.db import models, transaction
from django.db.utils import IntegrityError
from django.core.exceptions import ValidationError

logger = logging.getLogger(__name__)

def get_safe_upload_path(instance, filename):
    """Generate a safe, unique filename — prevents path traversal"""
    ext = os.path.splitext(filename)[1].lower()
    if not ext:
        ext = '.bin'
    safe_name = f"{uuid.uuid4().hex}{ext}"
    return f"rider_docs/{uuid.uuid4().hex[:8]}/{safe_name}"


def validate_document_type(value):
    """Validate document type against allowed choices"""
    valid_types = [choice[0] for choice in RiderDocument.DOCUMENT_TYPES]
    if value not in valid_types:
        raise ValidationError(
            f"'{value}' is not a valid document type. "
            f"Allowed types: {', '.join(valid_types)}"
        )


class RiderDocument(models.Model):
    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('approved', 'Approved'),
        ('rejected', 'Rejected'),
        ('resubmit_required', 'Resubmission Required'),
    ]

    DOCUMENT_TYPES = [
        ('national_id', 'National ID'),
        ('driving_license', 'Driving Licence'),
        ('vehicle_registration', 'Vehicle Registration'),
        ('insurance', 'Insurance Certificate'),
        ('profile_photo', 'Profile Photo'),
    ]

    rider = models.ForeignKey(
        'Rider',
        on_delete=models.PROTECT,
        related_name='documents'
    )
    document_type = models.CharField(
        max_length=50,
        choices=DOCUMENT_TYPES,
        validators=[validate_document_type]
    )
    document_file = models.FileField(
        upload_to=get_safe_upload_path,
        max_length=255
    )
    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default='pending'
    )
    is_current = models.BooleanField(
        default=True,
        db_index=True
    )
    uploaded_at = models.DateTimeField(auto_now_add=True)
    verified_at = models.DateTimeField(null=True, blank=True)
    verified_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='+'
    )
    rejection_reason = models.TextField(blank=True, null=True)
    replaced_by = models.ForeignKey(
        'self',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='replaces'
    )

    class Meta:
        ordering = ['-uploaded_at']
        constraints = [
            models.UniqueConstraint(
                fields=['rider', 'document_type'],
                condition=models.Q(is_current=True),
                name='unique_current_doc_type_per_rider'
            )
        ]
        permissions = [
            ('can_verify_documents', 'Can verify rider documents'),
        ]

    def __str__(self):
        return f"{self.rider.user.username} — {self.get_document_type_display()} — {self.status}"

    def mark_replaced(self, new_document):
        """Mark this document as superseded — called within atomic transaction"""
        self.is_current = False
        self.replaced_by = new_document
        self.save(update_fields=['is_current', 'replaced_by'])

    @classmethod
    def _cleanup_orphaned_file(cls, storage_name):
        """
        Safely remove an uploaded file that has no database record.
        Uses Django's storage API — works with local filesystem, S3, etc.
        Logs cleanup failures but never masks the original error.
        """
        if not storage_name:
            return

        try:
            refs = cls.objects.filter(document_file=storage_name).exists()
            if not refs:
                field = cls._meta.get_field('document_file')
                field.storage.delete(storage_name)
                logger.debug(f"Cleaned up orphaned file: {storage_name}")
        except Exception as e:
            logger.warning(
                f"Failed to clean up orphaned file '{storage_name}': {str(e)}"
            )

    @classmethod
    def resubmit_document(cls, rider, document_type, document_file):
        """
        Atomic replacement workflow with concurrency protection.
        Raises ValueError if resubmission is not allowed.
        Raises IntegrityError on duplicate/collision — caller must handle.
        """
        new_doc = None
        created_storage_name = None

        # Validate document type first — before any work
        valid_types = dict(cls.DOCUMENT_TYPES)
        if document_type not in valid_types:
            raise ValueError(
                f"Invalid document type. Allowed types: {', '.join(valid_types.keys())}"
            )

        try:
            with transaction.atomic():
                # Lock the Rider row first — serialises all document submissions
                Rider.objects.select_for_update().get(id=rider.id)

                # Check for existing current document
                existing = cls.objects.select_for_update().filter(
                    rider=rider,
                    document_type=document_type,
                    is_current=True
                ).first()

                # Strict resubmission rules
                if existing:
                    if existing.status == 'pending':
                        raise ValueError(
                            "A document of this type is currently under review. "
                            "Please wait for a decision before resubmitting."
                        )
                    if existing.status == 'approved':
                        raise ValueError(
                            "This document has already been approved. "
                            "Contact support to request a change."
                        )
                    if existing.status not in ['rejected', 'resubmit_required']:
                        raise ValueError(
                            "Resubmission is not available for this document status."
                        )

                    # Create new document record
                    new_doc = cls(
                        rider=rider,
                        document_type=document_type,
                        document_file=document_file,
                        status='pending',
                        is_current=False
                    )
                    new_doc.save()

                    # Capture storage name only if file was actually committed
                    if new_doc.document_file._committed:
                        created_storage_name = new_doc.document_file.name

                    # Transfer 'current' status if old doc exists
                    if existing:
                        existing.mark_replaced(new_doc)

                    new_doc.is_current = True
                    new_doc.save(update_fields=['is_current'])

                # Transaction committed successfully — clear cleanup marker
                created_storage_name = None
                return new_doc

        except IntegrityError:
            # Recover storage name if partially saved
            if new_doc is not None and created_storage_name is None:
                if hasattr(new_doc, 'document_file') and new_doc.document_file._committed:
                    created_storage_name = new_doc.document_file.name
            if created_storage_name:
                cls._cleanup_orphaned_file(created_storage_name)
            logger.warning(
                f"Duplicate document upload rejected: rider={rider.id}, "
                f"type={document_type}"
            )
            raise

        except ValueError:
            if new_doc is not None and created_storage_name is None:
                if hasattr(new_doc, 'document_file') and new_doc.document_file._committed:
                    created_storage_name = new_doc.document_file.name
            if created_storage_name:
                cls._cleanup_orphaned_file(created_storage_name)
            raise

        except Exception as e:
            # Recover storage name on any other failure
            if new_doc is not None and created_storage_name is None:
                if hasattr(new_doc, 'document_file'):
                    if new_doc.document_file._committed:
                        created_storage_name = new_doc.document_file.name
            if created_storage_name:
                cls._cleanup_orphaned_file(created_storage_name)
            logger.error(
                f"Document upload failed: rider={rider.id}, "
                f"type={document_type}, error={str(e)}"
            )
            raise
  
