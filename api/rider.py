# riders/models.py
import secrets
import uuid
from decimal import Decimal

from django.contrib.auth.models import User
from django.core.validators import MaxValueValidator, MinValueValidator, RegexValidator
from django.db import IntegrityError, models, transaction
from django.db.models import F, Q
from django.utils import timezone

from .constants import VERIFICATION_STATUS

# Ambiguity-free alphabet (no 0/O, 1/I/L) so codes survive being read aloud or typed from a screenshot
CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"


def generate_rider_code(length: int = 6) -> str:
    return "RYD-" + "".join(secrets.choice(CODE_ALPHABET) for _ in range(length))


ghana_phone_validator = RegexValidator(
    r"^(\+233|0)\d{9}$", "Enter a valid Ghana phone number."
)
ghana_card_validator = RegexValidator(
    r"^GHA-\d{9}-\d$", "Ghana Card format: GHA-XXXXXXXXX-X"
)


class Rider(models.Model):
    """Core rider identity: auth link, location, verification state."""

    class Tier(models.TextChoices):
        STARTER = "starter", "Starter"
        SILVER = "silver", "Silver"
        GOLD = "gold", "Gold"
        ELITE = "elite", "Elite"

    id = models.BigAutoField(primary_key=True)
    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    rider_code = models.CharField(
        max_length=12, unique=True, editable=False, db_index=True,
        help_text="Public code used for referrals and payment references.",
    )
    user = models.OneToOneField(
        User, on_delete=models.PROTECT, related_name="rider_profile",
        null=True, blank=True,
    )
    phone = models.CharField(max_length=20, unique=True, validators=[ghana_phone_validator])
    location = models.CharField(max_length=255, blank=True, null=True)
    current_lat = models.FloatField(null=True, blank=True)
    current_lng = models.FloatField(null=True, blank=True)
    location_updated_at = models.DateTimeField(null=True, blank=True)

    is_online = models.BooleanField(default=False)
    is_approved = models.BooleanField(default=False)
    is_suspended = models.BooleanField(default=False)
    suspended_reason = models.CharField(max_length=255, blank=True)
    verification_status = models.CharField(
        max_length=20, choices=VERIFICATION_STATUS, default="unsubmitted"
    )
    tier = models.CharField(max_length=10, choices=Tier.choices, default=Tier.STARTER)

    # Denormalised cache only. The ledger (WalletTransaction) is the source of truth.
    balance = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))

    referred_by = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="referrals", editable=False,
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "riders"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["is_online", "is_approved", "is_suspended"]),
            models.Index(fields=["verification_status"]),
        ]
        constraints = [
            models.CheckConstraint(
                condition=~Q(referred_by=F("id")),
                name="rider_cannot_refer_self"
            ),
        ]

    def __str__(self):
        return f"{self.rider_code} · {self.user.username if self.user else self.phone}"

    @property
    def is_dispatchable(self) -> bool:
        return (
            self.is_online and self.is_approved and not self.is_suspended
            and self.verification_status == "approved"
        )

    def save(self, *args, **kwargs):
        if self.rider_code:
            return super().save(*args, **kwargs)
        # Collision-safe: retry on the (very rare) unique violation
        for _ in range(10):
            self.rider_code = generate_rider_code()
            try:
                with transaction.atomic():
                    return super().save(*args, **kwargs)
            except IntegrityError as exc:
                if "rider_code" not in str(exc):
                    raise
                self.rider_code = ""
        raise RuntimeError("Could not generate a unique rider code.")


class RiderProfile(models.Model):
    """Extended profile info for each rider, separate from core identity."""

    class IdType(models.TextChoices):
        GHANA_CARD = "ghana_card", "Ghana Card"
        PASSPORT = "passport", "Passport"
        VOTER_ID = "voter_id", "Voter ID"

    class VehicleType(models.TextChoices):
        MOTORBIKE = "motorbike", "Motorbike (Okada)"
        TRICYCLE = "tricycle", "Tricycle (Aboboyaa)"
        CAR = "car", "Car"
        VAN = "van", "Van"

    rider = models.OneToOneField(Rider, on_delete=models.CASCADE, related_name="profile")

    # Identity
    full_name = models.CharField(max_length=150, blank=True)
    display_name = models.CharField(max_length=50, blank=True, help_text="Shown to customers.")
    id_type = models.CharField(max_length=20, choices=IdType.choices, blank=True)
    id_number = models.CharField(max_length=50, blank=True)  # encrypt at rest (see notes)
    date_of_birth = models.DateField(null=True, blank=True)
    gender = models.CharField(max_length=1, blank=True, choices=[("M", "Male"), ("F", "Female")])
    languages = models.JSONField(default=list, blank=True)  # ["English", "Twi", "Ga"]

    # Vehicle
    vehicle_type = models.CharField(max_length=20, choices=VehicleType.choices, blank=True)
    vehicle_plate = models.CharField(max_length=30, blank=True)
    vehicle_model = models.CharField(max_length=100, blank=True)
    vehicle_color = models.CharField(max_length=50, blank=True)
    vehicle_year = models.PositiveSmallIntegerField(null=True, blank=True)
    has_delivery_box = models.BooleanField(default=False)
    carries_helmet_for_passenger = models.BooleanField(default=False)

    # Compliance documents
    licence_number = models.CharField(max_length=50, blank=True)
    licence_expiry = models.DateField(null=True, blank=True)
    insurance_expiry = models.DateField(null=True, blank=True)
    roadworthy_expiry = models.DateField(null=True, blank=True)

    # Contact
    address = models.TextField(blank=True)
    emergency_contact_name = models.CharField(max_length=100, blank=True)
    emergency_contact_phone = models.CharField(
        max_length=20, blank=True, validators=[ghana_phone_validator]
    )

    # Public-facing
    bio = models.TextField(blank=True, max_length=500)
    profile_photo = models.ImageField(upload_to="rider_profiles/", blank=True, null=True)

    # Performance (aggregates, updated atomically with F())
    completed_rides = models.PositiveIntegerField(default=0)
    cancelled_rides = models.PositiveIntegerField(default=0)
    offered_rides = models.PositiveIntegerField(default=0)
    accepted_rides = models.PositiveIntegerField(default=0)
    rating_sum = models.PositiveIntegerField(default=0)
    rating_count = models.PositiveIntegerField(default=0)
    lifetime_earnings = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal("0.00"))

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "rider_profiles"
        constraints = [
            models.UniqueConstraint(
                fields=["vehicle_plate"], condition=~Q(vehicle_plate=""),
                name="unique_vehicle_plate_when_set",
            ),
            models.UniqueConstraint(
                fields=["id_type", "id_number"], condition=~Q(id_number=""),
                name="unique_identity_document",
            ),
        ]

    def __str__(self):
        return f"Profile: {self.rider}"

    @property
    def rating(self) -> float:
        return round(self.rating_sum / self.rating_count, 2) if self.rating_count else 5.0

    @property
    def acceptance_rate(self) -> float:
        return round(100 * self.accepted_rides / self.offered_rides, 1) if self.offered_rides else 100.0

    @property
    def cancellation_rate(self) -> float:
        total = self.completed_rides + self.cancelled_rides
        return round(100 * self.cancelled_rides / total, 1) if total else 0.0

    @property
    def documents_expired(self) -> bool:
        today = timezone.localdate()
        return any(
            d and d < today
            for d in (self.licence_expiry, self.insurance_expiry, self.roadworthy_expiry)
        )

    def add_rating(self, stars: int):
        RiderProfile.objects.filter(pk=self.pk).update(
            rating_sum=F("rating_sum") + stars, rating_count=F("rating_count") + 1
        )

    def clean(self):
        from django.core.exceptions import ValidationError
        if self.id_type == self.IdType.GHANA_CARD:
            try:
                ghana_card_validator(self.id_number)
            except ValidationError:
                raise ValidationError({"id_number": "Invalid Ghana Card number."})


class PayoutAccount(models.Model):
    """Where a rider's withdrawals go (Mobile Money or bank)."""

    class Method(models.TextChoices):
        MTN_MOMO = "mtn_momo", "MTN MoMo"
        TELECEL_CASH = "telecel_cash", "Telecel Cash"
        AT_MONEY = "at_money", "AT Money"
        BANK = "bank", "Bank account"

    rider = models.ForeignKey(Rider, on_delete=models.CASCADE, related_name="payout_accounts")
    method = models.CharField(max_length=20, choices=Method.choices)
    account_name = models.CharField(max_length=150)
    account_number = models.CharField(max_length=30)
    bank_name = models.CharField(max_length=100, blank=True)
    is_default = models.BooleanField(default=False)
    is_verified = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "rider_payout_accounts"
        constraints = [
            models.UniqueConstraint(
                fields=["rider"], condition=Q(is_default=True),
                name="one_default_payout_account_per_rider",
            ),
            models.UniqueConstraint(
                fields=["rider", "method", "account_number"], name="unique_payout_account"
            ),
        ]

    def __str__(self):
        return f"{self.get_method_display()} ••••{self.account_number[-4:]}"


class Referral(models.Model):
    """Tracks who referred whom; reward is paid once the referee hits the milestone."""

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        REWARDED = "rewarded", "Rewarded"
        EXPIRED = "expired", "Expired"

    referrer = models.ForeignKey(Rider, on_delete=models.CASCADE, related_name="referrals_made")
    referee = models.OneToOneField(Rider, on_delete=models.CASCADE, related_name="referral_received")
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING)
    reward_amount = models.DecimalField(max_digits=8, decimal_places=2, default=Decimal("0.00"))
    milestone_rides = models.PositiveSmallIntegerField(default=10)
    created_at = models.DateTimeField(auto_now_add=True)
    rewarded_at = models.DateTimeField(null=True, blank=True)

       # Restriction fields for complaints
    is_restricted = models.BooleanField(default=False)
    restriction_notes = models.TextField(blank=True, null=True)
    restricted_until = models.DateTimeField(null=True, blank=True)


    class Meta:
        db_table = "rider_referrals"
        constraints = [
            models.CheckConstraint(
                condition=~Q(referrer=F("referee")),
                name="no_self_referral"
                ),
        ]

