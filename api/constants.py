"""Business rules and choice lists shared by the RYDNOW models.
Nothing in this module touches the database."""

DUPLICATE_BOOKING_WINDOW_SECONDS = 120
DUPLICATE_LOCATION_TOLERANCE_KM = 0.1

ACTIVE_BOOKING_STATUSES = ["pending", "searching", "assigned" ,"in_progress"]

#Ghana geographic boundries (approximate)
GHANA_BOUNDS = (4.5, 11.0, -3.25, 1.2)

#input limits
MAX_IDEMPOTENCY_KEY_LENGTH = 64
MAX_NOTES_LENGTH = 500

from decimal import Decimal

# ── Financial ──────────────────────────────────────────────────────
COMMISSION_RATE = Decimal('0.15')
RIDER_PAYOUT_RATE = Decimal('0.85')
MIN_RIDER_ONLINE_BALANCE = Decimal('50.00')
DEFAULT_CURRENCY = 'GHS'
CURRENCY_CHOICES = [('GHS', 'Ghana Cedi')]
MINIMUM_WITHDRAWAL_AMOUNT = Decimal('10.00')

# ── Services ────────────────────────────────────────────────────
SERVICE_OKADA = 'okada'
SERVICE_PARCEL = 'parcel'
SERVICE_WASTE_BIN = 'waste-bin'

SERVICE_CHOICES = [
    (SERVICE_OKADA, 'Okada Ride'),
    (SERVICE_PARCEL, 'Parcel Delivery'),
    (SERVICE_WASTE_BIN, 'Waste Collection'),
]

# ── Booking ─────────────────────────────────────────────────────
STATUS_CHOICES = [
    ('pending', 'Pending'),
    ('searching_rider', 'Searching Rider'),
    ('assigned', 'Rider Assigned'),
    ('in_progress', 'In Progress'),
    ('completed', 'Completed'),
    ('cancelled', 'Cancelled'),
]

STATUS_TRANSITIONS = {
    'pending': ['searching_rider', 'assigned', 'cancelled'],
    'searching_rider': ['assigned', 'cancelled'],
    'assigned': ['in_progress', 'cancelled'],
    'in_progress': ['completed', 'cancelled'],
    'completed': [],
    'cancelled': [],
}

ACTIVE_BOOKING_STATUSES = ('assigned', 'in_progress')

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

PAYMENT_HELD_STATES = ('held', 'released')

PAYMENT_METHOD_CHOICES = [
    ('momo', 'Mobile Money'),
    ('visa', 'Visa Card'),
    ('debit', 'Debit Card'),
]

DUPLICATE_BOOKING_WINDOW_SECONDS = 120
DUPLICATE_LOCATION_TOLERANCE_DEG = 0.0001

# ── Wallet ledger ───────────────────────────────────────────────
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

WALLET_TYPE_CHOICES = [('customer', 'Customer'), ('rider', 'Rider')]

LEDGER_STATUS_CHOICES = [
    ('pending', 'Pending'),
    ('completed', 'Completed'),
    ('failed', 'Failed'),
]

# ── Withdrawals ─────────────────────────────────────────────────
WITHDRAWAL_STATUS = [
    ('pending', 'Queued for Processing'),
    ('reserved', 'Funds Reserved — Sending'),
    ('validated', 'Verified — Sufficient Balance'),
    ('initiated', 'Sent to Mobile Money Provider'),
    ('completed', 'Successfully Sent'),
    ('failed', 'Provider Failed — Funds Returned'),
    ('reversed', 'System Reversed — Check Notes'),
    ('timeout', 'Provider Timeout — Awaiting Reconciliation'),
]

WITHDRAWAL_ACTIVE_STATUSES = ('pending', 'reserved', 'validated', 'initiated', 'timeout')
WITHDRAWAL_FUNDS_RESERVED_STATUSES = ('reserved', 'initiated', 'timeout')

# ── Rider verification & documents ──────────────────────────────
VERIFICATION_STATUS = [
    ('unsubmitted', 'Not Submitted'),
    ('pending', 'Under Review'),
    ('submitted', 'Documents Submitted'),
    ('approved', 'Approved'),
    ('rejected', 'Rejected'),
]

DOCUMENT_TYPES = [
    ('ghana_card', 'Ghana Card'),
    ('passport_photo', 'Passport Photo'),
    ('license_doc', 'Driver\'s License'),
    ('national_id', 'National ID'),
    ('driving_license', 'Driving Licence'),
    ('vehicle_registration', 'Vehicle Registration'),
    ('insurance', 'Insurance Certificate'),
    ('profile_photo', 'Profile Photo'),
]

DOCUMENT_STATUS_CHOICES = [
    ('pending', 'Pending Review'),
    ('approved', 'Approved'),
    ('rejected', 'Rejected'),
    ('expired', 'Expired'),
    ('resubmit_required', 'Resubmission Required'),
]

RESUBMITTABLE_DOCUMENT_STATUSES = ('rejected', 'resubmit_required')

# ── Complaints ──────────────────────────────────────────────────
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

# ── System alerts ───────────────────────────────────────────────
ALERT_LEVEL_CHOICES = [
    ('info', 'Info'),
    ('warning', 'Warning'),
    ('error', 'Error'),
    ('critical', 'Critical'),
]

DOCUMENT_TYPES = [
    ("ghana_card", "Ghana Card"),
    ("passport_photo", "Passport Photo"),
    ("license_doc", "Driver's License"),
]
