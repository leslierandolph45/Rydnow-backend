from django.db import models
from django.contrib.auth.models import User
import uuid
from django.utils import timezone


COMMISSION_RATE = 0.15

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
    ('approved', 'Approved'),
    ('rejected', 'Rejected'),
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
    ('cancelled', 'Cancelled/Refund'),
]

TRANSACTION_TYPES = [
    ('job_payment', 'Job Payment'),
    ('withdrawal', 'Withdrawal'),
    ('commission', 'Platform Commission'),
    ('deposit', 'Deposit'),
    ('refund', 'Refund'),
    ('bonus', 'Bonus Credit'),
]

WITHDRAWAL_STATUS = [
    ('pending', 'Processing'),
    ('completed', 'Sent'),
    ('failed', 'Failed'),
]

COMPLAINT_STATUS = [
    ('pending', 'Under Review'),
    ('resolved', 'Resolved — No Action'),
    ('warning_issued', 'Warning Issued'),
    ('restricted', 'Restricted'),
    ('banned', 'Banned'),
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


class CustomerWallet(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name='customer_wallet')
    balance = models.DecimalField(max_digits=12, decimal_places=2, default=0.00)
    withdrawable_balance = models.DecimalField(max_digits=12, decimal_places=2, default=0.00)
    bonus_credit = models.DecimalField(max_digits=12, decimal_places=2, default=0.00)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Wallet: {self.id}"


class WalletTransaction(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    wallet = models.ForeignKey(CustomerWallet, on_delete=models.CASCADE, related_name='transactions', null=True, blank=True)
    wallet_type = models.CharField(max_length=20, default='customer')
    transaction_type = models.CharField(max_length=20, choices=TRANSACTION_TYPES)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    description = models.CharField(max_length=255, blank=True)
    phone_number = models.CharField(max_length=20, blank=True, null=True)
    status = models.CharField(max_length=20, default='completed')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"Tx: {self.id}"


class CustomerProfile(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name='customer_profile')
    full_name = models.CharField(max_length=200, blank=True)
    email = models.EmailField(blank=True)
    phone = models.CharField(max_length=20, blank=True, null=True)
    profile_photo = models.ImageField(upload_to='profiles/', blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Customer: {self.id}"


class RiderProfile(models.Model):
    STATUS_CHOICES = RIDER_STATUS_CHOICES
    user = models.OneToOneField(User, on_delete=models.CASCADE)
    full_name = models.CharField(max_length=200)
    email = models.EmailField()
    phone = models.CharField(max_length=30, blank=True)
    motorbike_model = models.CharField(max_length=200, blank=True)
    license_number = models.CharField(max_length=100, blank=True)
    current_lat = models.FloatField(null=True, blank=True, help_text="Current latitude")
    current_lng = models.FloatField(null=True, blank=True, help_text="Current longitude")
    last_location_update = models.DateTimeField(null=True, blank=True, help_text="Last time location was updated")
    ghana_card_doc = models.CharField(max_length=500, blank=True)
    passport_photo = models.CharField(max_length=500, blank=True)
    license_doc = models.CharField(max_length=500, blank=True)
    status = models.CharField(max_length=50, choices=RIDER_STATUS_CHOICES, default='pending')
    is_online = models.BooleanField(default=False)
    is_restricted = models.BooleanField(default=False, help_text="If checked, rider cannot accept bookings")
    restriction_notes = models.TextField(blank=True, help_text="Why was this rider restricted?")
    restricted_until = models.DateTimeField(null=True, blank=True, help_text="Leave blank for permanent restriction")
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"RiderProfile: {self.id}"


class Rider(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE, null=True, blank=True)
    name = models.CharField(max_length=100)
    email = models.EmailField(null=True, blank=True)
    phone = models.CharField(max_length=20)
    location = models.CharField(max_length=255, blank=True, null=True)
    current_lat = models.FloatField(null=True, blank=True, help_text="Current latitude")
    current_lng = models.FloatField(null=True, blank=True, help_text="Current longitude")
    last_location_update = models.DateTimeField(null=True, blank=True, help_text="Last time location was updated")
    is_online = models.BooleanField(default=False)
    verification_status = models.CharField(max_length=20, choices=VERIFICATION_STATUS, default='pending')
    is_restricted = models.BooleanField(default=False, help_text="If checked, rider cannot accept bookings")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Rider: {self.id}"

    @property
    def is_approved(self):
        return self.verification_status == 'approved' if self.verification_status else False


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
        return f"Booking: {self.id}"


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
        return f"RiderWallet: {self.id}"


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
        return f"Transaction: {self.id}"


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
        return f"Withdrawal: {self.id}"


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
        return f"Complaint: {self.id}"
