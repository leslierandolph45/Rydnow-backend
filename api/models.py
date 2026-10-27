from django.db import models, transaction, IntegrityError
from django.contrib.auth.models import User
from django.utils import timezone
from django.core.exceptions import ValidationError
from django.db.models import F, Q
import uuid
from decimal import Decimal, ROUND_HALF_UP


# ── HELPER FUNCTIONS ──────────────────────────────
def round_money(value):
    return Decimal(str(value)).quantize(Decimal('0.00'), rounding=ROUND_HALF_UP)

def validate_non_negative_amount(value):
    if value < Decimal('0'):
        raise ValidationError("Amount cannot be negative")
    return value
    
# ==========================================
#  BUSINESS RULES — RYDNOW STANDARDS
# ==========================================
COMMISSION_RATE = Decimal('0.15')        # 15% RYDNOW Commission
RIDER_PAYOUT_RATE = Decimal('0.85')       # 85% Rider Earnings
CURRENCY_CHOICES = [('GHS', 'Ghana Cedi')]


# ==========================================
#  REFERENCE GENERATORS — No Lambda!
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
#  VALIDATION HELPERS
# ==========================================

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
        raise ValidationError("Phone number must contain 9–15 digits.")

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

def round_money(value):
    """Consistent 2-decimal rounding — ALL financial math uses this"""
    return Decimal(str(value)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

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
# CUSTOMER WALLET
# ==========================================

class CustomerWallet(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.OneToOneField(
        User,
        on_delete=models.PROTECT,
        related_name='customer_wallet',
        unique=True
    )
    balance = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=Decimal('0.00'),
        validators=[validate_non_negative_amount]
    )
    withdrawable_balance = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=Decimal('0.00'),
        validators=[validate_non_negative_amount]
    )
    bonus_credit = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=Decimal('0.00'),
        validators=[validate_non_negative_amount]
    )
    currency = models.CharField(
        max_length=3,
        choices=CURRENCY_CHOICES,
        default='GHS'
    )
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
                # Check duplicate BEFORE locking row
                if WalletTransaction.objects.filter(reference=ref).exists():
                    return None, f"Duplicate transaction: {ref}"

                wallet = CustomerWallet.objects.select_for_update().get(pk=self.pk)
                wallet.full_clean(exclude=['user'])

                wallet.balance = F('balance') + amount
                wallet.withdrawable_balance = F('withdrawable_balance') + amount
                wallet.save(update_fields=[
                    'balance', 'withdrawable_balance', 'updated_at',
                ])

                WalletTransaction.objects.create(
                    wallet=wallet,
                    wallet_type='customer',
                    transaction_type='deposit',
                    amount=amount,
                    reference=ref,
                    description=description,
                    currency=wallet.currency,
                    related_booking=related_booking,
                    status='completed',
                )

                wallet.refresh_from_db()
                return wallet, "Deposit successful"

        except IntegrityError as e:
            if WalletTransaction.objects.filter(reference=ref).exists():
                return None, f"Duplicate transaction: {ref}"
            return None, f"Database error: {str(e)}"

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
                wallet.save(update_fields=[
                    'balance', 'withdrawable_balance', 'updated_at',
                ])

                WalletTransaction.objects.create(
                    wallet=wallet,
                    wallet_type='customer',
                    transaction_type='withdrawal',
                    amount=-amount,
                    reference=ref,
                    description=description,
                    currency=wallet.currency,
                    status='completed',
                )

                wallet.refresh_from_db()
                return True, "Withdrawal successful"

        except IntegrityError as e:
            if WalletTransaction.objects.filter(reference=ref).exists():
                return False, f"Duplicate transaction: {ref}"
            return False, f"Database error: {str(e)}"


# ==========================================
# WALLET TRANSACTION
# ==========================================

class WalletTransaction(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    wallet = models.ForeignKey(
        CustomerWallet,
        on_delete=models.PROTECT,
        related_name='transactions',
        null=True,
        blank=True
    )
    rider_wallet = models.ForeignKey(
        'RiderWallet',
        on_delete=models.PROTECT,
        related_name='transactions',
        null=True,
        blank=True
    )
    wallet_type = models.CharField(
        max_length=20,
        default='customer',
        db_index=True
    )
    transaction_type = models.CharField(
        max_length=20,
        choices=TRANSACTION_TYPES,
        db_index=True
    )
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    currency = models.CharField(
        max_length=3,
        choices=CURRENCY_CHOICES,
        default='GHS'
    )
    reference = models.CharField(
        max_length=50,
        unique=True,
        db_index=True,
        default=generate_transaction_ref
    )
    status = models.CharField(max_length=20, default='completed', db_index=True)
    description = models.CharField(max_length=255, blank=True)
    related_booking = models.ForeignKey(
        'Booking',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='wallet_transactions'
    )
    provider_ref = models.CharField(max_length=255, blank=True)
    idempotency_key = models.CharField(
        max_length=100,
        blank=True,
        db_index=True
    )
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['wallet', '-created_at']),
            models.Index(fields=['rider_wallet', '-created_at']),
            models.Index(fields=['transaction_type', 'created_at']),
            models.Index(fields=['reference']),
            models.Index(fields=['wallet_type', 'status', '-created_at']),
        ]

    def __str__(self):
           return (
        f"{self.reference} | {self.transaction_type} | "
        f"{self.amount} {self.currency}"
    )


# ==========================================
#  CUSTOMER PROFILE
# ==========================================

class CustomerProfile(models.Model):
    user = models.OneToOneField(
        User, on_delete=models.PROTECT,
        related_name='customer_profile', unique=True
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
        return name or f"Customer {str(self.pk)[:8]}"


# ==========================================
#  RIDER — Core Identity
# ==========================================

class Rider(models.Model):
    user = models.OneToOneField(
        User, on_delete=models.PROTECT, null=True, blank=True,
        related_name='rider_account'
    )
    name = models.CharField(max_length=100)
    email = models.EmailField(null=True, blank=True)
    phone = models.CharField(max_length=20, validators=[validate_phone_number])
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
        return f"{self.name} — {self.verification_status}"

    @property
    def is_approved(self):
        return self.verification_status == 'approved' and not self.is_restricted

    def get_distance_km(self, lat, lng):
        if None in (self.current_lat, self.current_lng, lat, lng):
            return None
        if self.current_lat == 0 and self.current_lng == 0:
            return None
        lat1 = max(-90.0, min(90.0, float(self.current_lat)))
        lon1 = max(-180.0, min(180.0, float(self.current_lng)))
        lat2 = max(-90.0, min(90.0, float(lat)))
        lon2 = max(-180.0, min(180.0, float(lng)))
        R = 6371
        dlat = radians(lat2 - lat1)
        dlon = radians(lon2 - lon1)
        a = sin(dlat / 2)**2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon / 2)**2
        a = max(0.0, min(1.0, a))
        c = 2 * asin(sqrt(a))
        return round(R * c, 2)


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
        Rider,
        on_delete=models.PROTECT,
        related_name='wallet',
        unique=True
    )
    balance = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=Decimal('0.00'),
        validators=[validate_non_negative_amount]
    )
    available_balance = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=Decimal('0.00'),
        validators=[validate_non_negative_amount]
    )
    pending_earnings = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=Decimal('0.00'),
        validators=[validate_non_negative_amount]
    )
    currency = models.CharField(
        max_length=3,
        choices=CURRENCY_CHOICES,
        default='GHS'
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.rider.name} — {self.currency} {self.balance}"


               def hold_pending_earnings(self, amount, booking_ref=''):
            """         
                 When payment is confirmed, move amount into pending earnings.
        These funds are held safely until the booking is completed.
        """
                 amount = round_money(amount)
        validate_positive_amount(amount)
        
                  transaction.atomic():
            wallet = RiderWallet.objects.select_for_update().get(pk=self.pk)
            wallet.full_clean(exclude=['rider'])
            
            wallet.pending_earnings = F('pending_earnings') + amount
            wallet.save(update_fields=['pending_earnings', 'updated_at'])
            
            wallet.refresh_from_db()
        return wallet, "Earnings held safely for release on completion"


       def credit_earnings(self, amount, booking_ref='', payment_verified=False):
        amount = round_money(amount)
        validate_positive_amount(amount)

        # Prevent releasing more than is safely held
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
            wallet.save(update_fields=['balance', 'available_balance', 'pending_earnings', 'updated_at'])

            WalletTransaction.objects.create(
                rider_wallet=wallet,
                wallet_type='rider',
                transaction_type='payout',
                amount=amount,
                reference=ref,
                description=f"Earnings credited: Booking {booking_ref}",
                currency=self.currency,
                status='completed'
            )
            wallet.refresh_from_db()
        return wallet, "Earnings credited successfully"

    def reserve_funds(self, amount):
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
        amount = round_money(amount)
        with transaction.atomic():
            wallet = RiderWallet.objects.select_for_update().get(pk=self.pk)
            wallet.full_clean(exclude=['rider'])
            wallet.balance = F('balance') - amount
            wallet.save(update_fields=['balance', 'updated_at'])
        return True, "Funds debited"

    def restore_funds(self, amount, reason=''):
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
                currency=self.currency,
                status='completed'
            )
        return True, "Funds restored"

    def create_withdrawal_request(self, amount, method, idempotency_key=''):
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

    pickup = models.CharField(max_length=255)
    pickup_lat = models.FloatField(validators=[validate_latitude])
    pickup_lng = models.FloatField(validators=[validate_longitude])
    destination = models.CharField(max_length=255)
    destination_lat = models.FloatField(validators=[validate_latitude])
    destination_lng = models.FloatField(validators=[validate_longitude])

    estimated_distance_km = models.FloatField(null=True, blank=True)
    estimated_duration_min = models.IntegerField(null=True, blank=True)

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
        if self.total_fare > 0 and self.platform_commission == 0 and self.rider_payout == 0:
            self.platform_commission = round_money(self.total_fare * COMMISSION_RATE)
            self.rider_payout = round_money(self.total_fare * RIDER_PAYOUT_RATE)
        super().save(*args, **kwargs)

    def assign_rider(self, rider):
        with transaction.atomic():
            # Validate status BEFORE acquiring lock
            if self.status not in ['pending', 'searching_rider']:
                return False, "Booking no longer available"

            booking = Booking.objects.select_for_update().get(pk=self.pk)

            # Double-check status AFTER acquiring lock — prevents race conditions
            if booking.status not in ['pending', 'searching_rider']:
                return False, "Booking no longer available"

            # Verify rider eligibility
            try:
                profile = RiderProfile.objects.get(rider=rider)
                if not profile.is_eligible:
                    return False, "Rider not eligible or offline"
            except RiderProfile.DoesNotExist:
                return False, "Rider profile not found"

            # Prevent same rider having multiple active bookings
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

            # Sync in-memory instance
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
            # Refresh from DB inside transaction for consistency
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

            # Credit rider earnings — ONLY once per booking via unique reference
            if booking.rider_payout > 0 and booking.assigned_rider:
                wallet, _ = RiderWallet.objects.get_or_create(rider=booking.assigned_rider)
                wallet.credit_earnings(
                    booking.rider_payout,
                    booking_ref=booking.booking_ref,
                    payment_verified=True
                )

            # Sync in-memory instance
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

            # Issue refund if funds already held/released
            if booking.payment_status in ['held', 'released']:
                booking.process_refund(amount=None, reason=f"Booking cancelled: {reason}")

            # Sync in-memory instance
            self.status = booking.status
            self.cancelled_at = booking.cancelled_at
        return True, "Booking cancelled"

    def confirm_payment_verified(self, provider_ref='', payment_ref=''):
               # Prevent duplicate processing
        if self.payment_status in ['held', 'released']:
            return True, "Payment already verified"

        if payment_ref:
            self.payment_ref = payment_ref
        if provider_ref:
            self.provider_ref = provider_ref
            
        # ✅ Hold funds — DON'T release earnings yet!
        self.payment_status = 'held'
        self.full_clean()
        self.save()
        
        return True, "Payment verified — held securely until ride completion"

    

    def process_refund(self, amount=None, reason='', provider_confirm_callback=None):
        if self.payment_status not in ['held', 'released']:
            return False, "No refundable funds for this booking"
        if self.payment_status in ['refund_initiated', 'refunded']:
            return True, "Refund already processed (idempotent)"

        refund_amount = round_money(amount or self.total_fare)
        if refund_amount <= Decimal('0'):
            return False, "Refund amount must be positive"
        if refund_amount > self.total_fare:
            return False, "Refund amount cannot exceed total fare"

        ref = f"REF-{self.booking_ref}"
        if WalletTransaction.objects.filter(reference=ref).exists():
            return True, "Refund already processed (idempotent)"

        # Provider confirmation if provided
        if callable(provider_confirm_callback):
            provider_success = provider_confirm_callback(
                amount=refund_amount,
                original_ref=self.payment_ref
            )
            if not provider_success:
                return False, "Provider refund failed — refund not marked complete"

        # Record the refund transaction
        WalletTransaction.objects.create(
            wallet_type='customer',
            transaction_type='refund',
            amount=-refund_amount,
            reference=ref,
            description=f"Refund: {reason}",
            related_booking=self,
            status='completed'
        )

        # Update payment status — partial vs full refund
        if refund_amount == self.total_fare:
            self.payment_status = 'refunded'
        else:
            self.payment_status = 'refund_initiated'
        self.save()
        return True, f"Refund processed: {ref}"


# ==========================================
#  WITHDRAWAL REQUEST — Safe 2-phase payout workflow
# ==========================================

class WithdrawalRequest(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    wallet = models.ForeignKey(
        RiderWallet, on_delete=models.PROTECT,
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
        """Restore reserved funds ONLY if we are certain the payout failed."""
        with transaction.atomic():
            if self.status in ['completed', 'reversed']:
                return False, "Cannot fail completed payout"

            # Only restore funds if they were actually reserved
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
                # Uncertain outcome — leave funds reserved for manual reconciliation
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

        # Step 1: Reserve funds inside transaction
        success, msg = self.reserve_funds()
        if not success:
            return False, msg

        # Step 2: Call provider — OUTSIDE transaction to avoid long-lived connections
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
                # Provider exception — cautiously mark as timeout, DON'T auto-restore
                self.mark_timeout(provider_known_failure=False)
                return False, f"Provider error: {str(e)}"

        # Step 3: Finalize based on confirmed provider response
        if provider_result:
            provider_ref = provider_response.get('provider_ref', '')
            self.confirm_payout_initiated(provider_ref=provider_ref)
            return self.confirm_payout_completed(provider_ref=provider_ref)
        else:
            error = provider_response.get('error', 'Provider communication failed')
            return self.mark_payout_failed(reason=error)

    def retry_failed(self, new_idempotency_key=None):
        """Create new request; preserve original failure record."""
        if self.status != 'failed':
            return None, "Only failed requests can be retried"
        key = new_idempotency_key or f"RETRY-{self.reference}-{uuid.uuid4().hex[:6]}"
        return self.wallet.create_withdrawal_request(
            amount=self.amount,
            method=self.payment_method,
            idempotency_key=key
        )


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
#  SYSTEM CONFIGURATION & AUTOMATION — RYDNOW AUTO-RUN & SAFETY
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
        # In production: trigger email/SMS notification here
        return alert

    def mark_resolved(self):
        self.resolved = True
        self.resolved_at = timezone.now()
        self.save()


# ==========================================
#  DAILY SUMMARY LOG — Know What Happened
# ==========================================

class DailySummary(models.Model):
    date = models.DateField(unique=True, db_index=True)
    total_bookings = models.IntegerField(default=0)
    completed_rides = models.IntegerField(default=0)
    cancelled_bookings = models.IntegerField(default=0)
    total_revenue = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0.00'))
    total_commission = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0.00'))
    total_payouts = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0.00'))
    new_riders = models.IntegerField(default=0)
    new_customers = models.IntegerField(default=0)
    pending_withdrawals = models.IntegerField(default=0)
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-date']
        get_latest_by = 'date'

    def __str__(self):
        return f"{self.date} — {self.completed_rides} rides | ₵{self.total_revenue}"

    @classmethod
    def generate_for_date(cls, target_date=None):
        """Rebuild daily stats — run nightly via cron/celery or admin action"""
        target_date = target_date or timezone.now().date()
        next_day = target_date + timezone.timedelta(days=1)

        bookings = Booking.objects.filter(
            created_at__date__gte=target_date,
            created_at__date__lt=next_day
        )
        completed = bookings.filter(status='completed')
        cancelled = bookings.filter(status='cancelled')

        summary, _ = cls.objects.update_or_create(
            date=target_date,
            defaults={
                'total_bookings': bookings.count(),
                'completed_rides': completed.count(),
                'cancelled_bookings': cancelled.count(),
                'total_revenue': sum(b.total_fare for b in completed),
                'total_commission': sum(b.platform_commission for b in completed),
                'total_payouts': sum(b.rider_payout for b in completed),
                'new_riders': Rider.objects.filter(
                    created_at__date__gte=target_date,
                    created_at__date__lt=next_day
                ).count(),
                'new_customers': CustomerProfile.objects.filter(
                    created_at__date__gte=target_date,
                    created_at__date__lt=next_day
                ).count(),
                'pending_withdrawals': WithdrawalRequest.objects.filter(
                    status__in=['pending', 'reserved', 'initiated']
                ).count(),
            }
        )
        return summary


# ==========================================
#  BOOKING AUTO-MANAGEMENT — Add Methods to Existing Booking Class
# ==========================================

def auto_cancel_stale_bookings(minutes=15):
    """
    Call this from a scheduled task or admin action:
    Cancels bookings stuck in 'searching_rider' for longer than specified time
    """
    cutoff = timezone.now() - timezone.timedelta(minutes=minutes)
    stale_bookings = Booking.objects.filter(
        status='searching_rider',
        created_at__lt=cutoff
    )
    cancelled_count = 0
    for booking in stale_bookings:
        with transaction.atomic():
            booking.status = 'cancelled'
            booking.cancellation_reason = f"Auto-cancelled: no rider found after {minutes} minutes"
            booking.cancelled_at = timezone.now()
            booking.save()

            SystemAlert.create_alert(
                level='info',
                title="Booking Auto-Cancelled",
                message=f"Booking {booking.booking_ref} cancelled — no rider found within {minutes} mins",
                booking_ref=booking.booking_ref
            )
            cancelled_count += 1
    return cancelled_count


# ==========================================
#  HELPER: QUICK PERMISSION CHECKS FOR VIEWS
# ==========================================

def get_customer_bookings_queryset(user):
    """Use in views: return only bookings belonging to THIS customer"""
    try:
        profile = CustomerProfile.objects.get(user=user)
        return Booking.objects.filter(customer=profile)
    except CustomerProfile.DoesNotExist:
        return Booking.objects.none()


def get_rider_bookings_queryset(user):
    """Use in views: return only bookings assigned to THIS rider"""
    try:
        rider = Rider.objects.get(user=user)
        return Booking.objects.filter(assigned_rider=rider)
    except Rider.DoesNotExist:
        return Booking.objects.none()


def get_available_nearby_bookings(rider, max_distance_km=5.0):
    """
    Find pending bookings within reasonable distance of an online rider
    Returns: Unassigned bookings where pickup is near rider's last known location
    """
    if not hasattr(rider, 'riderprofile') or not rider.riderprofile.is_eligible:
        return Booking.objects.none()
    if None in (rider.current_lat, rider.current_lng):
        return Booking.objects.none()

    all_pending = Booking.objects.filter(
        status__in=['pending', 'searching_rider'],
        assigned_rider__isnull=True
    ).select_related('customer')

    nearby = []
    for booking in all_pending:
        distance = booking.get_distance_km(
            rider.current_lat, rider.current_lng,
            booking.pickup_lat, booking.pickup_lng
        )
        if distance and distance <= max_distance_km:
            nearby.append(booking)

    # Return filtered PKs for queryset efficiency
    return Booking.objects.filter(pk__in=[b.pk for b in nearby])
