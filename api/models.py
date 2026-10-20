from django.db import models
from django.contrib.auth.models import User
import uuid
from django.utils import timezone


# ==========================================
#  CONFIGURATION
# ==========================================
COMMISSION_RATE = 0.15  # 15% Platform Commission


# ==========================================
#  CHOICE CONSTANTS
# ==========================================

SERVICE_CHOICES = [
    ('okada', 'Okada'),
    ('parcel', 'Parcel Delivery'),
    ('waste-bin', 'Waste Collection'),
]

STATUS_CHOICES = [
    ('pending', 'Pending'),
    ('searching_rider', 'Searching Rider'),
    ('assigned', 'Assigned'),
    ('in_progress', 'In Progress'),
    ('completed', 'Completed'),
    ('cancelled', 'Cancelled'),
]

VERIFICATION_STATUS = [
    ('pending', 'Pending Review'),
    ('submitted', 'Documents Submitted'),
    ('approved', '✅ Approved'),
    ('rejected', '❌ Rejected'),
]

RIDER_STATUS_CHOICES = [
    ('pending', 'Pending'),
    ('pending_review', 'Under Review'),
    ('approved', 'Approved'),
    ('rejected', 'Rejected'),
    ('active', 'Active'),
    ('inactive', 'Inactive'),
]

PAYMENT_STATUS = [
    ('pending', 'Held in Escrow'),
    ('released', 'Released'),
    ('cancelled', 'Cancelled/Refunded'),
]

TRANSACTION_TYPES = [
    ('job_payment', 'Job Payment'),
    ('withdrawal', 'Withdrawal'),
    ('commission', 'Platform Commission'),
]

WITHDRAWAL_STATUS = [
    ('pending', 'Processing'),
    ('completed', 'Sent'),
    ('failed', 'Failed'),
]

COMPLAINT_STATUS = [
    ('pending', '🔄 Under Review'),
    ('resolved', '✅ Resolved — No Action'),
    ('warning_issued', '⚠️ Warning Issued'),
    ('restricted', '🚫 Restricted'),
    ('banned', '❌ Banned'),
]

COMPLAINT_TYPE = [
    ('late_arrival', 'Late Arrival'),
    ('rude_behavior', 'Rude Behavior'),
    ('overcharging', 'Overcharging'),
    ('unsafe_driving', 'Unsafe Driving'),
    ('cancelled_without_notice', 'Cancelled Without Notice'),
    ('item_damaged', 'Item Damaged/Lost'),
    ('other', 'Other'),
]

RESTRICTION_REASON = [
    ('too_many_complaints', 'Too Many Complaints'),
    ('payment_issue', 'Payment Issue'),
    ('fraud_suspected', 'Suspected Fraud'),
    ('safety_concerns', 'Safety Concerns'),
    ('admin_initiated', 'Admin Decision'),
]


# ==========================================
#  CUSTOMER PROFILE
# ==========================================
class CustomerProfile(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name='customer_profile')
    full_name = models.CharField(max_length=200, blank=True)
    email = models.EmailField(blank=True)
    phone = models.CharField(max_length=20, blank=True, null=True)
    profile_photo = models.ImageField(upload_to='profiles/', blank=True, null=True)

    # Email & Phone Verification
    email_verified = models.BooleanField(default=False)
    phone_verified = models.BooleanField(default=False)
    email_verification_token = models.CharField(max_length=255, blank=True, null=True)
    sms_verification_code = models.CharField(max_length=6, blank=True, null=True)
    sms_code_expires_at = models.DateTimeField(blank=True, null=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.user.username


# ==========================================
#  RIDER PROFILE
# ==========================================
class RiderProfile(models.Model):
    STATUS_CHOICES = RIDER_STATUS_CHOICES

    user = models.OneToOneField(User, on_delete=models.CASCADE)
    full_name = models.CharField(max_length=200)
    email = models.EmailField()
    phone = models.CharField(max_length=30, blank=True)
    motorbike_model = models.CharField(max_length=200, blank=True)
    license_number = models.CharField(max_length=100, blank=True)

    # Live Location
    current_lat = models.FloatField(null=True, blank=True, help_text="Current latitude")
    current_lng = models.FloatField(null=True, blank=True, help_text="Current longitude")
    last_location_update = models.DateTimeField(null=True, blank=True, help_text="Last time location was updated")

    # Document Uploads
    ghana_card_doc = models.CharField(max_length=500, blank=True)
    passport_photo = models.CharField(max_length=500, blank=True)
    license_doc = models.CharField(max_length=500, blank=True)

    # Verification & Approval
    status = models.CharField(max_length=50, choices=RIDER_STATUS_CHOICES, default='pending')
    is_online = models.BooleanField(default=False)
    email_verified = models.BooleanField(default=False)
    phone_verified = models.BooleanField(default=False)
    email_verification_token = models.CharField(max_length=255, blank=True, null=True)
    sms_verification_code = models.CharField(max_length=6, blank=True, null=True)
    sms_code_expires_at = models.DateTimeField(blank=True, null=True)

    # Restriction Flag
    is_restricted = models.BooleanField(default=False, help_text="If checked, rider cannot accept bookings")
    restriction_notes = models.TextField(blank=True, help_text="Why was this rider restricted?")
    restricted_until = models.DateTimeField(null=True, blank=True, help_text="Leave blank for permanent restriction")

    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.full_name


# ==========================================
#  RIDER
# ==========================================
class Rider(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE, null=True, blank=True)
    name = models.CharField(max_length=100)
    email = models.EmailField(null=True, blank=True)
    phone = models.CharField(max_length=20)
    location = models.CharField(max_length=255, blank=True, null=True)

    # Live Location
    current_lat = models.FloatField(null=True, blank=True, help_text="Current latitude")
    current_lng = models.FloatField(null=True, blank=True, help_text="Current longitude")
    last_location_update = models.DateTimeField(null=True, blank=True, help_text="Last time location was updated")

    is_online = models.BooleanField(default=False)
    verification_status = models.CharField(max_length=20, choices=VERIFICATION_STATUS, default='pending')

    # Rider Approval Status
    approval_status = models.CharField(
        max_length=20,
        choices=[
            ('pending', 'Pending Email & Phone Verification'),
            ('documents_submitted', 'Documents Submitted — Under Review'),
            ('approved', '✅ Approved — Can Accept Jobs'),
            ('rejected', '❌ Rejected — See Admin Notes'),
        ],
        default='pending'
    )
    approved_at = models.DateTimeField(null=True, blank=True)
    approved_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='approved_riders')

    # Documents
    id_card = models.ImageField(upload_to='documents/ids/', null=True, blank=True)
    driver_license = models.ImageField(upload_to='documents/licenses/', null=True, blank=True)
    vehicle_registration = models.ImageField(upload_to='documents/vehicles/', null=True, blank=True)
    insurance = models.ImageField(upload_to='documents/insurance/', null=True, blank=True)

    admin_notes = models.TextField(blank=True, null=True, help_text="Notes for approval/rejection")

    # Restriction Flag
    is_restricted = models.BooleanField(default=False, help_text="If checked, rider cannot accept bookings")
    restriction_reason = models.CharField(max_length=50, choices=RESTRICTION_REASON, blank=True)
    restriction_details = models.TextField(blank=True)
    restricted_at = models.DateTimeField(null=True, blank=True)
    restricted_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='restricted_by_admin')

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.name} — {self.get_approval_status_display() if hasattr(self, 'get_approval_status_display') else self.verification_status}"

    @property
    def is_approved(self):
        return self.approval_status == 'approved' and not self.is_restricted


# ==========================================
#  BOOKING
# ==========================================
class Booking(models.Model):
    service_type = models.CharField(max_length=20, choices=SERVICE_CHOICES)
    pickup = models.CharField(max_length=255)
    pickup_lat = models.FloatField(null=True, blank=True)
    pickup_lng = models.FloatField(null=True, blank=True)
    destination = models.CharField(max_length=255)
    destination_lat = models.FloatField(null=True, blank=True)
    destination_lng = models.FloatField(null=True, blank=True)
    distance_km = models.FloatField(null=True, blank=True)
    price = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)

    customer = models.ForeignKey(CustomerProfile, on_delete=models.SET_NULL, null=True, blank=True, related_name='bookings')
    customer_name = models.CharField(max_length=100)
    customer_phone = models.CharField(max_length=20)
    customer_email = models.EmailField(blank=True, null=True)

    rider = models.ForeignKey(Rider, on_delete=models.SET_NULL, null=True, blank=True)
    status = models.CharField(max_length=30, choices=STATUS_CHOICES, default='pending')

    parcel_details = models.TextField(blank=True, null=True)

    # Escrow Payment
    payment_status = models.CharField(max_length=20, choices=PAYMENT_STATUS, default='pending')
    total_amount = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    platform_commission = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    rider_amount = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    payment_method = models.CharField(max_length=50, blank=True)
    transaction_reference = models.CharField(max_length=255, blank=True)
    paid_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.get_service_type_display()} — {self.customer_name}"

    def split_payment(self):
        if self.total_amount and not self.platform_commission:
            self.platform_commission = round(float(self.total_amount) * COMMISSION_RATE, 2)
            self.rider_amount = round(float(self.total_amount) - float(self.platform_commission), 2)
            self.save()


# ==========================================
#  RIDER WALLET
# ==========================================
class RiderWallet(models.Model):
    rider = models.OneToOneField(Rider, on_delete=models.CASCADE, related_name='wallet')
    balance = models.DecimalField(max_digits=10, decimal_places=2, default=0.00)
    total_earned = models.DecimalField(max_digits=10, decimal_places=2, default=0.00)
    total_withdrawn = models.DecimalField(max_digits=10, decimal_places=2, default=0.00)
    updated_at = models.DateTimeField(auto_now=True)

    def deposit(self, amount):
        amount = round(float(amount), 2)
        self.balance = round(float(self.balance) + amount, 2)
        self.total_earned = round(float(self.total_earned) + amount, 2)
        self.save()

    def withdraw(self, amount):
        amount = round(float(amount), 2)
        if float(self.balance) >= amount:
            self.balance = round(float(self.balance) - amount, 2)
            self.total_withdrawn = round(float(self.total_withdrawn) + amount, 2)
            self.save()
            return True
        return False

    def __str__(self):
        return f"{self.rider.name} — GH₵{self.balance}"


# ==========================================
#  TRANSACTION HISTORY
# ==========================================
class Transaction(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    rider = models.ForeignKey(Rider, on_delete=models.CASCADE, related_name='transactions')
    booking = models.ForeignKey(Booking, on_delete=models.SET_NULL, null=True, blank=True)
    transaction_type = models.CharField(max_length=20, choices=TRANSACTION_TYPES)
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    commission = models.DecimalField(max_digits=10, decimal_places=2, default=0.00)
    rider_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0.00)
    status = models.CharField(max_length=20, default='completed')
    description = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.rider.name} — {self.get_transaction_type_display()} — GH₵{self.amount}"


# ==========================================
#  WITHDRAWAL REQUEST
# ==========================================
class WithdrawalRequest(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    rider = models.ForeignKey(Rider, on_delete=models.CASCADE, related_name='withdrawals')
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    mobile_money_number = models.CharField(max_length=20)
    network = models.CharField(max_length=20, blank=True)
    status = models.CharField(max_length=20, choices=WITHDRAWAL_STATUS, default='pending')
    created_at = models.DateTimeField(auto_now_add=True)
    processed_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"Withdrawal — {self.rider.name} — GH₵{self.amount} — {self.status}"


# ==========================================
#  RIDER COMPLAINTS & RESTRICTIONS
# ==========================================
class RiderComplaint(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    rider = models.ForeignKey(Rider, on_delete=models.CASCADE, related_name='complaints', null=True, blank=True)
    rider_profile = models.ForeignKey(RiderProfile, on_delete=models.CASCADE, related_name='complaints', null=True, blank=True)

    booking = models.ForeignKey(Booking, on_delete=models.SET_NULL, null=True, blank=True, related_name='complaints')

    reporter_name = models.CharField(max_length=200, blank=True)
    reporter_phone = models.CharField(max_length=20, blank=True)

    complaint_type = models.CharField(max_length=50, choices=COMPLAINT_TYPE)
    description = models.TextField(help_text="What happened?")
    status = models.CharField(max_length=20, choices=COMPLAINT_STATUS, default='pending')

    admin_notes = models.TextField(blank=True, help_text="Your notes on this complaint")
    action_taken = models.CharField(max_length=100, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        rider_name = self.rider.name if self.rider else (self.rider_profile.full_name if self.rider_profile else "Unknown Rider")
        return f"{rider_name} — {self.get_complaint_type_display()} — {self.get_status_display()}"
