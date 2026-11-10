from rest_framework import serializers
from django.contrib.auth import get_user_model
from .validators import validate_email, validate_phone_number
from django.contrib.auth.password_validation import validate_password
from .models import Booking, CustomerProfile
from .models import RiderProfile, Service
import re
from django.conf import settings
from rest_framework import serializers
from .models import RiderProfile, RiderDocumentType, RiderStatusHistory

class MobileNumberField(serializers.CharField):
    """
    Validates a mobile number and normalizes it to E.164 (e.g. "+14155552671").

    Accepted input:
      - International format: "+1 (415) 555-2671", "+44 7911 123456"
      - "00" prefix:          "0044 7911 123456"
      - National format:      "0803 123 4567", only when a default country
        calling code is configured.

    Configuration:
      - Per field:  MobileNumberField(default_country_code="233")
      - Project:    DEFAULT_PHONE_COUNTRY_CODE = "233" in settings.py
    """

    default_error_messages = {
        "invalid": (
            "Enter a valid mobile number in international format, "
            "e.g. +14155552671."
        ),
    }

    _SEPARATORS = re.compile(r"[\s\-().]")
    _E164 = re.compile(r"^\+[1-9]\d{7,14}$")

    def __init__(self, *args, default_country_code=None, **kwargs):
        kwargs.setdefault("max_length", 20)
        super().__init__(*args, **kwargs)
        code = default_country_code or getattr(
            settings, "DEFAULT_PHONE_COUNTRY_CODE", None
        )
        self.default_country_code = str(code).lstrip("+") if code else None

    def to_internal_value(self, data):
        value = super().to_internal_value(data)
        number = self._SEPARATORS.sub("", value)

        if number.startswith("00"):
            number = f"+{number[2:]}"
        elif not number.startswith("+") and self.default_country_code:
            number = f"+{self.default_country_code}{number.lstrip('0')}"

        if not self._E164.match(number):
            self.fail("invalid")

        return number


class RiderRegistrationSerializer(serializers.Serializer):
    """
    RiderProfileSerializer for rider registration - Phase 1
    Validates input without assuming Rider.user exists
    """
    full_name = serializers.CharField(
        max_length=200,
        required=True,
        trim_whitespace=True,
    )
    email = serializers.EmailField(
        required=True,
        validators=[validate_email]
    )
    phone = serializers.CharField(
        max_length=30,
        required=True,
        trim_whitespace=True,
    )
    password = serializers.CharField(
        write_only=True,
        required=False,
        allow_blank=False,
    )
    motorbike_model = serializers.CharField(
        max_length=200,
        required=False,
        allow_blank=True,
    )
    license_number = serializers.CharField(
        max_length=100,
        required=False,
        allow_blank=True,
    )


    def validate_email(self, value):
        """Prevent duplicate email accounts"""
        if User.objects.filter(email__iexact=value).exists():
            raise serializers.ValidationError(
                "An account with this email already exists"
            )
        return value.lower()


    def validate_phone(self, value):
        """Prevent duplicate phone registrations — accept both formats"""
        cleaned = value.replace(' ', '').strip()

        # Accept: 0XXXXXXXXX
        if cleaned.startswith('0') and len(cleaned) == 10 and cleaned.isdigit():
            normalized = cleaned
        # Accept: +233XXXXXXXXX
        elif cleaned.startswith('+233') and len(cleaned) == 13:
            normalized = '0' + cleaned[4:]  # Convert to local format for DB
        # Accept: 9 digits only → prepend 0
        elif len(cleaned) == 9 and cleaned.isdigit():
            normalized = '0' + cleaned
        else:
            raise serializers.ValidationError(
                "Enter a valid Ghanaian number — e.g. 0241234567 or +233241234567"
            )

        if Rider.objects.filter(phone=normalized).exists():
            raise serializers.ValidationError(
                "This phone number is already registered with us"
            )
        return normalized


class RiderSerializer(serializers.ModelSerializer):
    class Meta:
        model = Riderfields = [
            'id',
            'name',
            'phone',
            'is_online',
            'verification_status',
            'current_lat',
            'current_lng'
    ]


class BookingSerializer(serializers.ModelSerializer):
    class Meta:
        model = Booking
        fields = [
            'id', 'booking_ref', 'service_type', 'customer',
            'pickup', 'pickup_lat', 'pickup_lng',
            'destination', 'destination_lat', 'destination_lng',
            'estimated_distance_km', 'estimated_duration_min',
            'status', 'assigned_rider', 'accepted_at',
            'total_fare', 'platform_commission', 'rider_payout',
            'payment_status', 'payment_method', 'payment_ref', 'provider_ref',
            'created_at', 'updated_at'
        ]
        read_only_fields = [
            'id', 'booking_ref', 'total_fare', 'platform_commission',
            'rider_payout', 'payment_status', 'payment_ref', 'provider_ref',
            'assigned_rider', 'accepted_at', 'created_at', 'updated_at'
        ]



#================ Serializer =============
class RiderProfileAdminNoteSerializer(serializers.ModelSerializer):
    author_name = serializers.SerializerMethodField()

    class Meta:
        model = RiderProfileAdminNotefields = ["id", "rider_profile", "author", "author_name", "note", "created_at"]
        read_only_fields = ["id", "author", "author_name", "created_at"]

    def get_author_name(self, obj):
        if not obj.author:
            return None
        return obj.author.get_full_name() or obj.author.get_username()

    def validate_note(self, value):
        value = value.strip()
        if not value:
            raise serializers.ValidationError("Note cannot be empty.")
        return value

    def update(self, instance, validated_data):
        validated_data.pop("rider_profile", None)
        return super().update(instance, validated_data)



# —————————————————————————
# SERIALIZERS
# —————————————————————————
class RiderIncidentReportSerializer(serializers.ModelSerializer):
    """What a rider sends when reporting an incident (no admin fields)."""

    class Meta:
        model = RiderIncidentfields = (
            "id", "reference_code", "incident_type", "severity", "service", "title",
            "description", "location", "incident_date", "status", "date_reported",
            "resolution_notes", "resolved_at",
        )
        read_only_fields = ("id", "reference_code", "status", "date_reported",
                            "resolution_notes", "resolved_at")

    def validate_incident_date(self, value):
        if value > timezone.now():
            raise serializers.ValidationError("Incident date cannot be in the future.")
        return value


class RiderIncidentAdminSerializer(serializers.ModelSerializer):
    """Full view for administrators."""

    rider_name = serializers.CharField(source="rider_profile.full_name", read_only=True)
    rider_phone = serializers.CharField(source="rider_profile.phone", read_only=True)
    incident_type_display = serializers.CharField(source="get_incident_type_display", read_only=True)
    severity_display = serializers.CharField(source="get_severity_display", read_only=True)
    status_display = serializers.CharField(source="get_status_display", read_only=True)
    assigned_to_name = serializers.CharField(
        source="assigned_to.get_full_name", read_only=True, default=None
    )
    is_open = serializers.BooleanField(read_only=True)

    class Meta:
        model = RiderIncidentfields = (
            "id", "reference_code", "rider_profile", "rider_name", "rider_phone",
            "service", "incident_type", "incident_type_display", "severity",
            "severity_display", "title", "description", "location", "incident_date",
            "date_reported", "updated_at", "reported_by", "assigned_to",
            "assigned_to_name", "status", "status_display", "is_open",
            "resolution_notes", "resolved_at", "resolved_by",
        )
        read_only_fields = ("id", "reference_code", "date_reported", "updated_at",
                            "reported_by", "resolved_at", "resolved_by")

    def validate(self, attrs):
        status = attrs.get("status", getattr(self.instance, "status", None))
        notes = attrs.get("resolution_notes", getattr(self.instance, "resolution_notes", ""))
        if status in RiderIncident.FINAL_STATUSES and not (notes or "").strip():
            raise serializers.ValidationError(
                {"resolution_notes": "Resolution notes are required to resolve or close an incident."}
            )
        return attrs


import re
from decimal import Decimal

from django.db import models
from rest_framework import serializers


class PaymentType(models.TextChoices):
    WALLET_TOPUP = "wallet_topup", "Wallet Top-up"
    BOOKING_PAYMENT = "booking_payment", "Booking Payment"
    WITHDRAWAL = "withdrawal", "Withdrawal"


SUPPORTED_CURRENCIES = ("GHS",)
MIN_AMOUNT = Decimal("1.00")
MAX_AMOUNT = Decimal("50000.00")
REFERENCE_PATTERN = re.compile(r"^[A-Za-z0-9_\-\.]+$")


class InitializePaymentSerializer(serializers.Serializer):
    """Validates and normalizes a request to initialize a payment."""

    amount = serializers.DecimalField(
        max_digits=10,
        decimal_places=2,
        min_value=MIN_AMOUNT,
        max_value=MAX_AMOUNT,
        help_text="Payment amount in major currency units (e.g. 25.50).",
    )
    currency = serializers.CharField(
        max_length=3,
        default="GHS",
        help_text="ISO 4217 currency code. Currently only GHS is supported.",
    )
    payment_type = serializers.ChoiceField(
        choices=PaymentType.choices,
        help_text="Purpose of the payment.",
    )
    booking_id = serializers.IntegerField(
        required=False,
        min_value=1,
        help_text="Required when payment_type is 'booking_payment'.",
    )
    reference = serializers.CharField(
        max_length=255,
        required=False,
        allow_blank=True,
        help_text="Optional client reference. Generated server-side if omitted.",
    )
    description = serializers.CharField(
        max_length=500,
        required=False,
        allow_blank=True,
        trim_whitespace=True,
    )

    def validate_currency(self, value):
        value = value.strip().upper()
        if value not in SUPPORTED_CURRENCIES:
            raise serializers.ValidationError(
                f"Unsupported currency. Supported: {', '.join(SUPPORTED_CURRENCIES)}."
            )
        return value

    def validate_reference(self, value):
        value = value.strip()
        if not value:
            return None  # Let the service layer generate one
        if not REFERENCE_PATTERN.match(value):
            raise serializers.ValidationError(
                "Reference may only contain letters, numbers, hyphens, underscores and dots."
            )
        return value

    def validate(self, attrs):
        payment_type = attrs["payment_type"]

        if payment_type == PaymentType.BOOKING_PAYMENT and not attrs.get("booking_id"):
            raise serializers.ValidationError(
                {"booking_id": "This field is required for booking payments."}
            )

        if payment_type != PaymentType.BOOKING_PAYMENT and attrs.get("booking_id"):
            raise serializers.ValidationError(
                {"booking_id": "Only valid for booking payments."}
            )

        if payment_type == PaymentType.WITHDRAWAL:
            user = getattr(self.context.get("request"), "user", None)
            wallet = getattr(user, "wallet", None)
            if wallet is not None and attrs["amount"] > wallet.balance:
                raise serializers.ValidationError(
                    {"amount": "Withdrawal amount exceeds available wallet balance."}
                )

        return attrs


from django.contrib.auth import authenticate
from rest_framework import serializers


class LoginSerializer(serializers.Serializer):
    """Validates credentials and returns the authenticated user in validated_data["user"]."""

    email = serializers.EmailField(max_length=254)
    password = serializers.CharField(
        write_only=True,
        trim_whitespace=False,  # never alter a password
        max_length=128,
        style={"input_type": "password"},
    )

    # Single generic message so the API never reveals whether an email exists
    INVALID_CREDENTIALS = "Invalid email or password."

    default_error_messages = {
        "inactive": "This account has been deactivated. Please contact support.",
    }

    def validate_email(self, value):
        return value.strip().lower()

    def validate(self, attrs):
        request = self.context.get("request")

        user = authenticate(
            request=request,
            username=attrs["email"],  # works when USERNAME_FIELD = "email"
            password=attrs["password"],
        )

        if user is None:
            raise serializers.ValidationError(
                {"non_field_errors": [self.INVALID_CREDENTIALS]}, code="authorization"
            )

        if not user.is_active:
            self.fail("inactive")

        attrs["user"] = user
        return attrs

class CoordinatesSerializer(serializers.Serializer):
    """Validates latitude/longitude query parameters."""
    lat = serializers.FloatField(required=True, min_value=-90.0, max_value=90.0)
    lng = serializers.FloatField(required=True, min_value=-180.0, max_value=180.0)


class NearbyRiderProfilesSerializer(serializers.ModelSerializer):
    """Public representation of a rider shown to customers."""

    full_name = serializers.SerializerMethodField()
    distance_km = serializers.SerializerMethodField()

    class Meta:
        model = Riderfields = [
            "id",
            "full_name",
            "phone",
            "profile_photo",
            "motorbike_model",
            "current_lat",
            "current_lng",
            "distance_km",
            "rating",
            "is_available",
        ]
        read_only_fields = [
            "id",
            "full_name",
            "phone",
            "profile_photo",
            "motorbike_model",
            "current_lat",
            "current_lng",
            "distance_km",
            "rating",
            "is_available",
        ]

    def get_full_name(self, obj: Rider) -> str:
        return obj.user.get_full_name() or obj.user.get_username()

    def get_distance_km(self, obj: Rider) -> float | None:
        distance = getattr(obj, "distance_km", None)
        return None if distance is None else round(distance, 2)


class NearbyRiderQuerySerializer(CoordinatesSerializer):
    """Validates query parameters for the nearby-riders endpoint."""

    radius_km = serializers.FloatField(
        required=False, default=5.0, min_value=0.1, max_value=50.0
    )
    limit = serializers.IntegerField(
        required=False, default=20, min_value=1, max_value=50
    )
    service = serializers.PrimaryKeyRelatedField(
        queryset=Service.objects.all(), required=False
    )

from rest_framework import serializers

class PhoneSerializer(serializers.Serializer):
    """Simple serializer for validating and accepting a phone number only."""
    phone = MobileNumberField()


import re

from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from rest_framework import serializers

from .models import CustomerProfile

User = get_user_model()

_PHONE_RE = re.compile(r"^\+?[0-9]{9,15}$")


class MobileNumberField(serializers.CharField):
    """Strips spaces/dashes/brackets and validates a 9-15 digit number."""

    default_error_messages = {"invalid": "Enter a valid mobile number."}

    def to_internal_value(self, data):
        value = super().to_internal_value(data)
        cleaned = re.sub(r"[\s\-().]", "", value)
        if not _PHONE_RE.match(cleaned):
            self.fail("invalid")
        return cleaned


class CustomerRegisterSerializer(serializers.Serializer):
    full_name = serializers.CharField(max_length=100, trim_whitespace=True)
    email = serializers.EmailField(required=False, allow_blank=True)
    phone = MobileNumberField()
    password = serializers.CharField(write_only=True, min_length=6, max_length=128)

    def validate_email(self, value):
        value = value.strip().lower()
        if value and User.objects.filter(email__iexact=value).exists():
            raise serializers.ValidationError("An account with this email already exists.")
        return value

    def validate_phone(self, value):
        if CustomerProfile.objects.filter(phone=value).exists() or User.objects.filter(username=value).exists():
            raise serializers.ValidationError("An account with this phone number already exists.")
        return value

    def validate(self, attrs):
        try:
            validate_password(attrs["password"])
        except DjangoValidationError as exc:
            raise serializers.ValidationError({"password": list(exc.messages)})
        return attrs

    @transaction.atomic
    def create(self, validated_data):
        user = User.objects.create_user(
            username=validated_data["phone"],
            email=validated_data.get("email", ""),
            password=validated_data["password"],
            first_name=validated_data["full_name"],
        )
        return CustomerProfile.objects.create(
            user=user,
            full_name=validated_data["full_name"],
            phone=validated_data["phone"],
        )


class CustomerSerializer(serializers.ModelSerializer):
    email = serializers.EmailField(source="user.email", read_only=True)

    class Meta:
        model = CustomerProfile
        fields = ("id", "full_name", "email", "phone")



from django.conf import settings
from django.db import models
from rest_framework import serializers


class RiderDocumentType(models.TextChoices):
    DRIVERS_LICENSE = "drivers_license", "Driver's License"
    NATIONAL_ID = "national_id", "National ID"
    VEHICLE_REGISTRATION = "vehicle_registration", "Vehicle Registration"
    INSURANCE = "insurance", "Insurance Certificate"
    PROFILE_PHOTO = "profile_photo", "Profile Photo"


ALLOWED_EXTENSIONS = {"pdf", "jpg", "jpeg", "png"}
MAX_FILE_SIZE = 5 * 1024 * 1024  # 5 MB


class RiderDocumentUploadSerializer(serializers.Serializer):
    """Validates a rider document submission (file upload or hosted URL)."""

    document_type = serializers.ChoiceField(choices=RiderDocumentType.choices)
    file = serializers.FileField(required=False, write_only=True)
    file_url = serializers.URLField(required=False, max_length=500)

    def validate_file(self, value):
        ext = value.name.rsplit(".", 1)[-1].lower() if "." in value.name else ""
        if ext not in ALLOWED_EXTENSIONS:
            raise serializers.ValidationError(
                f"Unsupported file type. Allowed: {', '.join(sorted(ALLOWED_EXTENSIONS))}."
            )
        if value.size > MAX_FILE_SIZE:
            raise serializers.ValidationError("File too large. Maximum size is 5 MB.")
        return value

    def validate_file_url(self, value):
        if not value.lower().startswith("https://"):
            raise serializers.ValidationError("Only HTTPS URLs are allowed.")
        allowed_hosts = getattr(settings, "RIDER_DOCUMENT_ALLOWED_HOSTS", None)
        if allowed_hosts:
            from urllib.parse import urlparse
            if urlparse(value).hostname not in allowed_hosts:
                raise serializers.ValidationError("File host is not permitted.")
        return value

    def validate(self, attrs):
        has_file = "file" in attrs
        has_url = "file_url" in attrs
        if has_file == has_url:
            raise serializers.ValidationError(
                "Provide exactly one of 'file' or 'file_url'."
            )
        return attrs


class RiderDocumentSerializer(serializers.Serializer):
    """Read representation of a stored rider document."""

    id = serializers.IntegerField(read_only=True)
    document_type = serializers.ChoiceField(choices=RiderDocumentType.choices, read_only=True)
    file_url = serializers.URLField(read_only=True)
    status = serializers.CharField(read_only=True)
    uploaded_at = serializers.DateTimeField(read_only=True)





class RiderProfileSerializer(serializers.ModelSerializer):
    """Rider sees their own profile."""
    id = serializers.UUIDField(source="public_id", read_only=True)
    is_verified = serializers.BooleanField(read_only=True)
    verification_status = serializers.ChoiceField(choices=VerificationStatus.choices, read_only=True)
    tier = serializers.ChoiceField(choices=RiderTier.choices, read_only=True)

    class Meta:
        model = RiderProfile
        fields = [
            "id",
            "full_name", "phone", "profile_photo","date_of_birth","gender", "ghana_card_number", "city",
            "motorbike_model", "motor_plate_number", "motorbike_color","license_number","address",
            "preferred_language", "license_expiry_date","tier", "rating_count", "accepted_trips_pct",
            "verification_status", "is_verified", "rejection_reason", "online_since"
            "is_online", "rating_average", "completed_trips", "created_at",
        ]
        read_only_fields = [
            "id", 
            "verification_status", "is_verified", "rejection_reason",
            "tier", "rating_average", "rating_count" "completed_trips", "created_at",
            "accepted_trips_pct", "online_since"
        ]


class RiderPublicSerializer(serializers.ModelSerializer):
    """What customers see. No phone, licence or internal status."""
    id = serializers.UUIDField(source="public_id", read_only=True)

    class Meta:
        model = RiderProfile
        fields = ["id", "full_name", "motorbike_model", "rating_average", "completed_trips"]
        read_only_fields = fields


import re
class VerifyCodeSerializer(serializers.Serializer):
    """Validate a 4-6 digit verification code for a phone number or email."""

    PHONE_REGEX = re.compile(r"^\+?[1-9]\d{6,14}$")  # E.164-style

    code = serializers.CharField(
        min_length=4,
        max_length=6,
        trim_whitespace=True,
        error_messages={
            "required": "Verification code is required.",
            "blank": "Verification code cannot be blank.",
            "min_length": "Code must be 4-6 digits.",
            "max_length": "Code must be 4-6 digits.",
        },
    )
    identifier = serializers.CharField(
        max_length=100,
        trim_whitespace=True,
        error_messages={
            "required": "A phone number or email is required.",
            "blank": "A phone number or email cannot be blank.",
        },
    )

    def validate_code(self, value):
        if not value.isdigit():
            raise serializers.ValidationError("Code must contain digits only.")
        return value

    def validate_identifier(self, value):
        if "@" in value:
            try:
                validate_email(value)
            except DjangoValidationError:
                raise serializers.ValidationError("Enter a valid email address.")
            return value.lower()

        phone = re.sub(r"[\s\-().]", "", value)
        if not self.PHONE_REGEX.match(phone):
            raise serializers.ValidationError(
                "Enter a valid phone number, including country code."
            )
        return phone

class VerifyPaymentSerializer(serializers.Serializer):
    """Validate input for verifying and confirming a payment transaction."""

    TRANSACTION_ID_PATTERN = r"^[A-Za-z0-9_\-.:]+$"

    transaction_id = serializers.RegexField(
        regex=TRANSACTION_ID_PATTERN,
        max_length=100,
        trim_whitespace=True,
        error_messages={
            "required": "Transaction ID is required.",
            "blank": "Transaction ID cannot be blank.",
            "max_length": "Transaction ID is too long.",
            "invalid": "Transaction ID contains invalid characters.",
        },
    )
    code = serializers.CharField(
        required=False,
        allow_blank=True,
        min_length=4,
        max_length=6,
        trim_whitespace=True,
        error_messages={
            "min_length": "Verification code must be 4-6 digits.",
            "max_length": "Verification code must be 4-6 digits.",
        },
    )

    def validate_code(self, value):
        if value and not value.isdigit():
            raise serializers.ValidationError(
                "Verification code must contain digits only."
            )
        return value

class RiderLocationSerializer(serializers.Serializer):
    latitude = serializers.DecimalField(
        max_digits=9, decimal_places=6, min_value=-90, max_value=90
    )
    longitude = serializers.DecimalField(
        max_digits=9, decimal_places=6, min_value=-180, max_value=180
    )


from rest_framework import serializers


class NearbyRidersQuerySerializer(serializers.Serializer):
    latitude = serializers.FloatField(min_value=-90, max_value=90)
    longitude = serializers.FloatField(min_value=-180, max_value=180)
    service = serializers.CharField()  # service slug or id
    radius_km = serializers.FloatField(min_value=0.5, max_value=50, default=5)
    limit = serializers.IntegerField(min_value=1, max_value=50, default=20)


class NearbyRiderSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    full_name = serializers.CharField()
    latitude = serializers.FloatField()
    longitude = serializers.FloatField()
    distance_km = serializers.FloatField()
    location_updated_at = serializers.DateTimeField()








# —————————————————————————
# SERIALIZERS
# —————————————————————————
class _ComputedFieldsMixin(serializers.Serializer):
    monthly_target_hours = serializers.ReadOnlyField()
    trips_progress = serializers.ReadOnlyField()
    hours_progress = serializers.ReadOnlyField()
    meets_acceptance = serializers.ReadOnlyField()
    performance_status_display = serializers.CharField(
        source="get_performance_status_display", read_only=True
    )


class RiderPerformanceCommitRiderSerializer(_ComputedFieldsMixin, serializers.ModelSerializer):
    """What a rider sees: their own progress, recognition and support."""
    acknowledged = serializers.SerializerMethodField()

    class Meta:model = RiderPerformanceCommitfields = (
        "id", "month", "target_trips", "target_online_hours", "monthly_target_hours",
        "min_acceptance_rate", "completed_trips", "online_hours", "acceptance_rate",
        "average_rating", "trips_progress", "hours_progress", "meets_acceptance",
        "performance_score", "performance_status", "performance_status_display",
        "is_exempt", "exempt_reason", "appreciation_message", "bonus_amount",
        "coaching_plan", "follow_up_date", "review_status", "acknowledged",
        "rider_acknowledged_at",
    )
    read_only_fields = [
        "id",
        "month",
        "target_trips",
        "target_online_hours",
        "monthly_target_hours",
        "min_acceptance_rate",
        "completed_trips",
        "online_hours",
        "acceptance_rate",
        "average_rating",
        "trips_progress",
        "hours_progress",
        "performance_score",
        "performance_status",
        "meets_acceptance",
        "is_exempt",
        "exempt_reason",
        "appreciation_message",
        "bonus_amount",
        "coaching_plan",
        "follow_up_date",
        "review_status",
        "acknowledged",
        "rider_acknowledged_at",
    ]


    def get_acknowledged(self, obj):
        return obj.rider_acknowledged_at is not None


class RiderPerformanceCommitAdminSerializer(_ComputedFieldsMixin, serializers.ModelSerializer):
    rider_name = serializers.CharField(source="rider_profile.full_name", read_only=True)
    rider_phone = serializers.CharField(source="rider_profile.phone", read_only=True)
    review_status_display = serializers.CharField(source="get_review_status_display", read_only=True)

    class Meta:
        model = RiderPerformanceCommitfields = (
            "id", "rider_profile", "rider_name", "rider_phone", "month",
            "target_trips", "target_online_hours", "monthly_target_hours", "min_acceptance_rate",
            "completed_trips", "online_hours", "acceptance_rate", "average_rating",
            "trips_progress", "hours_progress", "meets_acceptance",
            "performance_score", "performance_status", "performance_status_display",
            "is_exempt", "exempt_reason", "appreciation_message", "bonus_amount",
            "coaching_plan", "follow_up_date", "review_status", "review_status_display",
            "notes", "reviewed_by", "finalized_at", "rider_acknowledged_at",
            "created_at", "updated_at",
        )
        read_only_fields = ("id", "performance_score", "performance_status", "reviewed_by",
        "finalized_at", "rider_acknowledged_at", "created_at", "updated_at")

    def validate_month(self, value):
        return value.replace(day=1)

    def validate(self, attrs):
        if self.instance and self.instance.is_finalized:
            raise serializers.ValidationError(
                "This review is finalized. Reopen it before making changes."
            )
        is_exempt = attrs.get("is_exempt", getattr(self.instance, "is_exempt", False))
        reason = attrs.get("exempt_reason", getattr(self.instance, "exempt_reason", ""))
        if is_exempt and not reason.strip():
            raise serializers.ValidationError({
                "exempt_reason": "Give a reason when exempting a rider"
            })
        return attrs





# —————————————————————————
# SERIALIZERS
# —————————————————————————
class RiderStatusHistorySerializer(serializers.ModelSerializer):
    """Full read-only view for administrators."""

    rider_name = serializers.CharField(source="rider_profile.full_name", read_only=True)
    rider_phone = serializers.CharField(source="rider_profile.phone", read_only=True)
    old_status_display = serializers.CharField(source="get_old_status_display", read_only=True)
    new_status_display = serializers.CharField(source="get_new_status_display", read_only=True)
    reason_category_display = serializers.CharField(source="get_reason_category_display", read_only=True)
    source_display = serializers.CharField(source="get_source_display", read_only=True)
    is_reinstatement = serializers.BooleanField(read_only=True)

    class Meta:
        model = RiderStatusHistoryfields = (
            "id", "rider_profile", "rider_name", "rider_phone", "old_status", "old_status_display",
            "new_status", "new_status_display", "reason_category", "reason_category_display",
            "reason", "internal_notes", "effective_until", "is_reinstatement",
            "changed_at", "changed_by", "changed_by_name", "source", "source_display",
            "ip_address", "metadata",
        )
        read_only_fields = [
            "id",
            "rider_profile",
            "rider_name",
            "rider_phone",
            "old_status",
            "old_status_display",
            "new_status",
            "new_status_display",
            "reason_category",
            "reason_category_display",
            "reason",
            "internal_notes",
            "effective_until",
            "is_reinstatement",
            "changed_at",
            "changed_by",
            "changed_by_name",
            "source",
            "source_display",
            "ip_address",
            "metadata",
        ]


class RiderStatusHistoryRiderSerializer(serializers.ModelSerializer):
    """What a rider sees of their own timeline."""

    old_status_display = serializers.CharField(source="get_old_status_display", read_only=True)
    new_status_display = serializers.CharField(source="get_new_status_display", read_only=True)

    class Meta:
        model = RiderStatusHistoryfields = ("id", "old_status", "old_status_display", "new_status", "new_status_display",
        "reason", "effective_until", "changed_at")
        read_only_fields = [
            "id",
            "old_status",
            "old_status_display",
            "new_status",
            "new_status_display",
            "reason",
            "effective_until",
            "changed_at",
    ]



class RiderStatusChangeSerializer(serializers.Serializer):
    """Input for changing a rider's status."""
    rider_profile = serializers.PrimaryKeyRelatedField(queryset=RiderProfile.objects.all())
    new_status = serializers.ChoiceField(choices=RiderStatusHistory.Status.choices)
    reason_category = serializers.ChoiceField(
        choices=RiderStatusHistory.Category.choices, 
        default=RiderStatusHistory.Category.OTHER
    )
    reason = serializers.CharField(required=False, allow_blank=True, default="")
    internal_notes = serializers.CharField(required=False, allow_blank=True, default="")
    effective_until = serializers.DateTimeField(required=False, allow_null=True, default=None)
    force = serializers.BooleanField(required=False, default=False)

    def validate(self, attrs):
        user = self.context["request"].user
        if attrs["force"] and not user.is_superuser:
            raise serializers.ValidationError({"force": "Only a superuser can override transition rules."})
        try:
            validate_status_change(
                attrs["rider_profile"], attrs["new_status"], attrs["reason"],
                attrs["effective_until"], attrs["force"],
            )
        except DjangoValidationError as exc:
            raise serializers.ValidationError(
                exc.message_dict if hasattr(exc, "error_dict") else exc.messages
            )
        return attrs

    def create(self, validated_data):
        request = self.context["request"]
        try:
            return change_rider_status(
            rider_profile=validated_data["rider_profile"],
            new_status=validated_data["new_status"],
            changed_by=request.user,
            reason=validated_data.get("reason", ""),
            reason_category=validated_data.get("reason_category", "other"),
            internal_notes=validated_data.get("internal_notes", ""),
            effective_until=validated_data.get("effective_until"),
            source=RiderStatusHistory.Source.API,
            ip_address=get_client_ip(request),
            force=validated_data.get("force", False),
        )
        except DjangoValidationError as exc:
            raise serializers.ValidationError(
                exc.message_dict if hasattr(exc, "error_dict") else exc.messages
            )


class CustomerProfileSerializer(serializers.ModelSerializer):
    """Customer views and updates their own profile."""
    
    id = serializers.UUIDField(source="public_id", read_only=True)
    email = serializers.EmailField(source="user.email", read_only=True)
    full_name = serializers.CharField(read_only=True)
    phone = serializers.CharField(required=False, allow_blank = True)
    profile_photo = serializers.ImageField(required=False, allow_null=True)

    class Meta:
        model = CustomerProfile
        fields = [
            "id",
            "email",
            "full_name",
            "phone",
            "profile_photo",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "id",
            "email",
            "full_name",
            "created_at",
            "updated_at",
        ]



"""
Account serializers.

Assumes an optional one-to-one `Profile` model reachable as `user.profile`
with any of these fields (all optional; missing ones are handled safely):

    avatar (ImageField), timezone, locale, plan, referral_code,
    marketing_opt_in, marketing_opt_in_at, onboarding_completed,
    email_verified, acquisition_source
"""
from zoneinfo import ZoneInfo, available_timezones

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.db import transaction
from django.utils import timezone
from rest_framework import serializers

User = get_user_model()


def _profile(user):
    """Return the user's profile or None (never raises)."""
    return getattr(user, "customer_profile", None)


def _clean_name(value: str) -> str:
    return " ".join(value.split())  # collapse stray whitespace


class AccountSerializer(serializers.ModelSerializer):
    """Read-only account snapshot. Safe to return from /me and login."""

    full_name = serializers.SerializerMethodField()
    initials = serializers.SerializerMethodField()
    avatar_url = serializers.SerializerMethodField()
    account_age_days = serializers.SerializerMethodField()
    is_new_user = serializers.SerializerMethodField()
    email_verified = serializers.SerializerMethodField()
    onboarding_completed = serializers.SerializerMethodField()
    plan = serializers.SerializerMethodField()
    referral_code = serializers.SerializerMethodField()
    marketing_opt_in = serializers.SerializerMethodField()
    timezone = serializers.SerializerMethodField()
    locale = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = [
            "id", "username", "email",
            "first_name", "last_name", "full_name", "initials", "avatar_url",
            "is_active", "date_joined", "last_login",
            # growth / lifecycle
            "account_age_days", "is_new_user", "email_verified",
            "onboarding_completed", "plan", "referral_code",
            "marketing_opt_in", "timezone", "locale",
        ]
        read_only_fields = fields

    # --- identity ---
    def get_full_name(self, obj):
        return obj.get_full_name() or obj.get_username()

    def get_initials(self, obj):
        parts = [p for p in (obj.first_name, obj.last_name) if p]
        source = "".join(p[0] for p in parts[:2]) or obj.get_username()[:2]
        return source.upper()

    def get_avatar_url(self, obj):
        profile = _profile(obj)
        avatar = getattr(profile, "avatar", None)
        if not avatar:
            return None
        request = self.context.get("request")
        url = avatar.url
        return request.build_absolute_uri(url) if request else url

    # --- lifecycle metrics (useful for onboarding nudges & cohorts) ---
    def get_account_age_days(self, obj):
        return (timezone.now() - obj.date_joined).days

    def get_is_new_user(self, obj):
        return self.get_account_age_days(obj) < 7

    # --- profile-backed fields ---
    def _pget(self, obj, name, default=None):
        return getattr(_profile(obj), name, default)

    def get_email_verified(self, obj):
        return bool(self._pget(obj, "email_verified", False))

    def get_onboarding_completed(self, obj):
        return bool(self._pget(obj, "onboarding_completed", False))

    def get_plan(self, obj):
        return self._pget(obj, "plan", "free")

    def get_referral_code(self, obj):
        return self._pget(obj, "referral_code")

    def get_marketing_opt_in(self, obj):
        return bool(self._pget(obj, "marketing_opt_in", False))

    def get_timezone(self, obj):
        return self._pget(obj, "timezone", "UTC")

    def get_locale(self, obj):
        return self._pget(obj, "locale", settings.LANGUAGE_CODE)


class AccountUpdateSerializer(serializers.ModelSerializer):
    """Writable subset for PATCH /me. Email/username changes go through
    dedicated verified flows, never this serializer."""

    timezone = serializers.CharField(source="profile.timezone", required=False)
    locale = serializers.CharField(source="profile.locale", required=False)
    marketing_opt_in = serializers.BooleanField(
        source="profile.marketing_opt_in", required=False
    )
    onboarding_completed = serializers.BooleanField(
        source="profile.onboarding_completed", required=False
    )
    avatar = serializers.ImageField(
        source="profile.avatar", required=False, allow_null=True
    )

    MAX_AVATAR_BYTES = 5 * 1024 * 1024

    class Meta:
        model = User
        fields = [
            "first_name", "last_name", "timezone", "locale",
            "marketing_opt_in", "onboarding_completed", "avatar",
        ]

    def validate_first_name(self, value):
        return _clean_name(value)

    def validate_last_name(self, value):
        return _clean_name(value)

    def validate_timezone(self, value):
        if value not in available_timezones():
            raise serializers.ValidationError("Unknown timezone.")
        ZoneInfo(value)
        return value

    def validate_locale(self, value):
        allowed = {code for code, _ in getattr(settings, "LANGUAGES", [])}
        if allowed and value not in allowed:
            raise serializers.ValidationError("Unsupported locale.")
        return value

    def validate_avatar(self, value):
        if value and value.size > self.MAX_AVATAR_BYTES:
            raise serializers.ValidationError("Avatar must be under 5 MB.")
        return value

    @transaction.atomic
    def update(self, instance, validated_data):
        profile_data = validated_data.pop("profile", {})
        for attr, value in validated_data.items():
            setattr(instance, attr, value)
        instance.save()

        profile = _profile(instance)
        if profile and profile_data:
            # record consent timestamp for compliance (GDPR/CAN-SPAM)
            if (
                "marketing_opt_in" in profile_data
                and profile_data["marketing_opt_in"] != getattr(profile, "marketing_opt_in", None)
                and hasattr(profile, "marketing_opt_in_at")
            ):
                profile.marketing_opt_in_at = timezone.now()
            for attr, value in profile_data.items():
                setattr(profile, attr, value)
            profile.save()
        return instance

    def to_representation(self, instance):
        return AccountSerializer(instance, context=self.context).data


class RegistrationSerializer(serializers.ModelSerializer):
    """Sign-up with growth attribution (referral + UTM) and consent."""

    password = serializers.CharField(write_only=True, style={"input_type": "password"})
    password_confirm = serializers.CharField(write_only=True, style={"input_type": "password"})
    referral_code = serializers.CharField(write_only=True, required=False, allow_blank=True, max_length=32)
    acquisition_source = serializers.CharField(write_only=True, required=False, allow_blank=True, max_length=64)
    marketing_opt_in = serializers.BooleanField(write_only=True, required=False, default=False)
    accepted_terms = serializers.BooleanField(write_only=True)

    class Meta:
        model = User
        fields = [
            "username", "email", "first_name", "last_name",
            "password", "password_confirm", "referral_code",
            "acquisition_source", "marketing_opt_in", "accepted_terms",
        ]
        extra_kwargs = {"email": {"required": True}}

    def validate_email(self, value):
        value = value.strip().lower()
        if User.objects.filter(email__iexact=value).exists():
            # Consider a generic message if account enumeration is a concern.
            raise serializers.ValidationError("An account with this email already exists.")
        return value

    def validate_accepted_terms(self, value):
        if not value:
            raise serializers.ValidationError("You must accept the terms to continue.")
        return value

    def validate(self, attrs):
        if attrs["password"] != attrs.pop("password_confirm"):
            raise serializers.ValidationError({"password_confirm": "Passwords do not match."})
        candidate = User(
            username=attrs.get("username"), email=attrs.get("email"),
            first_name=attrs.get("first_name", ""), last_name=attrs.get("last_name", ""),
        )
        validate_password(attrs["password"], user=candidate)
        return attrs

    @transaction.atomic
    def create(self, validated_data):
        validated_data.pop("accepted_terms")
        password = validated_data.pop("password")
        extras = {
            k: validated_data.pop(k, None)
            for k in ("referral_code", "acquisition_source", "marketing_opt_in")
        }
        user = User(**validated_data)
        user.set_password(password)
        user.save()

        profile = _profile(user)
        if profile:
            if hasattr(profile, "acquisition_source"):
                profile.acquisition_source = extras["acquisition_source"] or ""
            if extras["marketing_opt_in"] and hasattr(profile, "marketing_opt_in"):
                profile.marketing_opt_in = True
                if hasattr(profile, "marketing_opt_in_at"):
                    profile.marketing_opt_in_at = timezone.now()
            profile.save()

        # Hook points: attribute the referral, send verification email,
        # fire a "user_signed_up" analytics event (do via signals/on_commit).
        user._referral_code_used = extras["referral_code"]
        return user

    def to_representation(self, instance):
        return AccountSerializer(instance, context=self.context).data


class ChangePasswordSerializer(serializers.Serializer):
    current_password = serializers.CharField(write_only=True)
    new_password = serializers.CharField(write_only=True)

    def validate_current_password(self, value):
        user = self.context["request"].user
        if not user.check_password(value):
            raise serializers.ValidationError("Current password is incorrect.")
        return value

    def validate_new_password(self, value):
        validate_password(value, user=self.context["request"].user)
        return value

    def save(self, **kwargs):
        user = self.context["request"].user
        user.set_password(self.validated_data["new_password"])
        user.save(update_fields=["password"])
        return user



# ——— Transaction Serializers ———
from .models import WalletTransaction

class TransactionSerializer(serializers.ModelSerializer):
    type_display = serializers.CharField(source="get_type_display", read_only=True)
    status_display = serializers.CharField(source="get_status_display", read_only=True)

    class Meta:
        model = WalletTransaction
        fields = [
            "id", "reference", "type", "type_display", "direction",
            "status", "status_display", "amount", "balance_after",
            "description", "created_at",
        ]
        read_only_fields = fields


class TransactionQuerySerializer(serializers.Serializer):
    ORDERING = ["-created_at", "created_at", "-amount", "amount"]

    type = serializers.ChoiceField(choices=WalletTransaction.Type.choices, required=False)
    status = serializers.ChoiceField(choices=WalletTransaction.Status.choices, required=False)
    direction = serializers.ChoiceField(choices=WalletTransaction.Direction.choices, required=False)
    date_from = serializers.DateField(required=False)
    date_to = serializers.DateField(required=False)
    min_amount = serializers.DecimalField(max_digits=14, decimal_places=2, min_value=0, required=False)
    max_amount = serializers.DecimalField(max_digits=14, decimal_places=2, min_value=0, required=False)
    search = serializers.CharField(max_length=64, required=False, allow_blank=True)
    ordering = serializers.ChoiceField(choices=ORDERING, default="-created_at")
    export = serializers.ChoiceField(choices=["csv"], required=False)

    def validate(self, attrs):
        if attrs.get("date_from") and attrs.get("date_to") and attrs["date_from"] > attrs["date_to"]:
            raise serializers.ValidationError({"date_from": "Must be on or before date_to."})
        lo, hi = attrs.get("min_amount"), attrs.get("max_amount")
        if lo is not None and hi is not None and lo > hi:
            raise serializers.ValidationError({"min_amount": "Must be less than or equal to max_amount."})
        return attrs
