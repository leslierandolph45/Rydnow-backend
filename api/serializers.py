"""
Rydnow serializers (account, rider, wallet, payment, booking, pricing).

Audit fixes in this version
  * The two pasted serializer files are merged into ONE module with one import block.
  * NearbyRidersQuerySerializer was defined twice (latitude/longitude/service vs lat/lng/service_type);
    the second silently replaced the first, so views.get_nearby_riders crashed with KeyError.
    Now:  NearbyRidersQuerySerializer        -> lat / lng / service_type  (booking.NearbyRidersView)
          LegacyNearbyRidersQuerySerializer  -> latitude / longitude / service  (views.get_nearby_riders)
  * RiderPublicSerializer and NearbyRiderSerializer pointed at RiderProfile fields that do not exist
    (phone_number, plate_number, rating, latitude, longitude, last_location_at). They now map onto the
    real fields (phone, motorbike_plate_number, rating_average, current_lat, current_lng,
    location_updated_at) while keeping the same JSON keys for the frontend.
"""
from decimal import ROUND_HALF_UP, Decimal
from math import isfinite

from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.utils import timezone
from rest_framework import serializers

from .models import (
    TRANSACTION_TYPES,
    Booking,
    BookingStatusHistory,
    BookingStop,
    CustomerProfile,
    RiderDocument,
    RiderProfile,
    RiderWallet,
    Service,
    ServiceType,
    WalletDeposit,
    WalletTransaction,
)
from .pricing import COORD, MAX_WAIT_INPUT_MINUTES, RATES
from .validators import (
    check_password_strength,
    clean_phone,
    email_is_taken,
    split_name,
)

User = get_user_model()

REFERENCE_RE = r"^[A-Za-z0-9_.\-]{6,100}$"
MAX_DOCUMENT_SIZE_MB = 10
ALLOWED_DOCUMENT_EXTENSIONS = ("jpg", "jpeg", "png", "webp", "pdf")
WALLET_TX_STATUSES = ("pending", "completed", "failed", "reversed")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _create_auth_user(*, email, password, full_name=""):
    """Single place where auth Users are created (all arguments by keyword)."""
    first, last = split_name(full_name) if full_name else ("", "")
    return User.objects.create_user(
        username=email,
        email=email,
        password=password,
        first_name=first,
        last_name=last,
    )


# ---------------------------------------------------------------------------
# Safe numeric inputs
# ---------------------------------------------------------------------------
class Coord(serializers.FloatField):
    """Latitude/longitude -> Decimal rounded to 6 dp. Rejects NaN/inf."""

    def __init__(self, lo, hi, **kwargs):
        super().__init__(min_value=lo, max_value=hi, **kwargs)

    def to_internal_value(self, data):
        value = super().to_internal_value(data)
        if not isfinite(value):
            self.fail("invalid")
        return Decimal(str(value)).quantize(COORD, rounding=ROUND_HALF_UP)


class WaitMinutes(serializers.FloatField):
    def __init__(self, **kwargs):
        kwargs.setdefault("min_value", 0)
        kwargs.setdefault("max_value", MAX_WAIT_INPUT_MINUTES)
        kwargs.setdefault("default", 0)
        super().__init__(**kwargs)

    def to_internal_value(self, data):
        value = super().to_internal_value(data)
        if not isfinite(value):
            self.fail("invalid")
        return Decimal(str(round(value, 2)))


def Lat(**kw):
    return Coord(-90, 90, **kw)


def Lng(**kw):
    return Coord(-180, 180, **kw)


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------
class LoginSerializer(serializers.Serializer):
    """`username` accepts either the username or the email address."""
    username = serializers.CharField(max_length=254)
    password = serializers.CharField(write_only=True, trim_whitespace=False)


class _SignupBase(serializers.Serializer):
    full_name = serializers.CharField(max_length=150)
    email = serializers.EmailField()
    phone = serializers.CharField(max_length=30)
    password = serializers.CharField(write_only=True, trim_whitespace=False)
    password_confirm = serializers.CharField(write_only=True, required=False, trim_whitespace=False)

    def validate_full_name(self, value):
        value = " ".join(value.split())
        if len(value) < 2:
            raise serializers.ValidationError("Enter your full name.")
        return value

    def validate_email(self, value):
        value = value.strip().lower()
        if email_is_taken(value):
            raise serializers.ValidationError("An account with this email already exists.")
        return value

    def validate_phone(self, value):
        return clean_phone(value)

    def validate(self, attrs):
        confirm = attrs.pop("password_confirm", None)
        if confirm is not None and confirm != attrs["password"]:
            raise serializers.ValidationError({"password_confirm": "Passwords do not match."})
        probe = User(
            username=attrs["email"],
            email=attrs["email"],
            first_name=split_name(attrs["full_name"])[0],
        )
        check_password_strength(attrs["password"], user=probe)
        return attrs

    def _create_user(self, validated_data):
        return _create_auth_user(
            email=validated_data["email"],
            password=validated_data["password"],
            full_name=validated_data["full_name"],
        )


class RegistrationSerializer(serializers.Serializer):
    """Customer sign-up: creates the User and its CustomerProfile together. save() returns the User."""

    email = serializers.EmailField(max_length=254)
    password = serializers.CharField(write_only=True, min_length=8, trim_whitespace=False)
    full_name = serializers.CharField(max_length=150, required=False, allow_blank=True, default="")
    phone = serializers.CharField(max_length=30, required=False, allow_blank=True, default="")

    def validate_email(self, value):
        email = value.strip().lower()
        if email_is_taken(email):
            raise serializers.ValidationError("This email is already registered.")
        return email

    def validate_full_name(self, value):
        return " ".join(value.split())

    def validate_phone(self, value):
        if not value or not value.strip():
            return ""
        try:
            phone = clean_phone(value)
        except Exception:
            raise serializers.ValidationError("Enter a valid phone number.")
        if not phone:
            raise serializers.ValidationError("Enter a valid phone number.")
        if CustomerProfile.objects.filter(phone=phone).exists():
            raise serializers.ValidationError("This phone number is already registered.")
        return phone

    def validate(self, attrs):
        probe = User(
            username=attrs["email"],
            email=attrs["email"],
            first_name=split_name(attrs["full_name"])[0] if attrs.get("full_name") else "",
        )
        check_password_strength(attrs["password"], user=probe)
        return attrs

    def create(self, validated_data):
        try:
            with transaction.atomic():
                user = _create_auth_user(
                    email=validated_data["email"],
                    password=validated_data["password"],
                    full_name=validated_data.get("full_name", ""),
                )
                CustomerProfile.objects.create(
                    user=user,
                    full_name=validated_data.get("full_name", ""),
                    email=validated_data["email"],
                    phone=validated_data.get("phone", ""),
                )
        except IntegrityError:
            raise serializers.ValidationError(
                {"detail": "An account with this email or phone already exists."}
            )
        return user


class RiderRegistrationSerializer(_SignupBase):
    """Rider sign-up: creates User -> RiderProfile -> RiderWallet. save() returns the RiderProfile."""

    def validate_phone(self, value):
        phone = clean_phone(value)
        if RiderProfile.objects.filter(phone=phone).exists():
            raise serializers.ValidationError("This phone number is already registered.")
        return phone

    def create(self, validated_data):
        try:
            with transaction.atomic():
                user = self._create_user(validated_data)
                rider = RiderProfile.objects.create(
                    user=user,
                    full_name=validated_data["full_name"],
                    phone=validated_data["phone"],
                )
                RiderWallet.objects.create(rider=rider)
        except IntegrityError:
            raise serializers.ValidationError(
                {"detail": "An account with this email or phone already exists."}
            )
        return rider


# ---------------------------------------------------------------------------
# Account
# ---------------------------------------------------------------------------
class AccountSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = ["id", "username", "email", "first_name", "last_name", "is_active", "is_staff"]
        read_only_fields = fields


class AccountUpdateSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = ["first_name", "last_name", "email"]

    def validate_email(self, value):
        value = value.strip().lower()
        if email_is_taken(value, exclude_user_id=self.instance.pk if self.instance else None):
            raise serializers.ValidationError("An account with this email already exists.")
        return value


class ChangePasswordSerializer(serializers.Serializer):
    old_password = serializers.CharField(write_only=True, trim_whitespace=False)
    new_password = serializers.CharField(write_only=True, trim_whitespace=False)
    new_password_confirm = serializers.CharField(write_only=True, trim_whitespace=False)

    def validate_old_password(self, value):
        if not self.context["request"].user.check_password(value):
            raise serializers.ValidationError("Current password is incorrect.")
        return value

    def validate(self, attrs):
        if attrs["new_password"] != attrs["new_password_confirm"]:
            raise serializers.ValidationError({"new_password_confirm": "Passwords do not match."})
        if attrs["new_password"] == attrs["old_password"]:
            raise serializers.ValidationError({"new_password": "Choose a different password."})
        check_password_strength(attrs["new_password"], user=self.context["request"].user)
        return attrs


# ---------------------------------------------------------------------------
# Phone verification (OTP)
# ---------------------------------------------------------------------------
class PhoneSerializer(serializers.Serializer):
    phone = serializers.CharField(max_length=30)

    def validate_phone(self, value):
        return clean_phone(value)


class VerifyCodeSerializer(PhoneSerializer):
    code = serializers.RegexField(r"^\d{6}$", error_messages={"invalid": "Enter the 6-digit code."})


# ---------------------------------------------------------------------------
# Customers
# ---------------------------------------------------------------------------
class CustomerSerializer(serializers.ModelSerializer):
    """Compact public view of a customer (shown to riders)."""
    class Meta:
        model = CustomerProfile
        fields = ["id", "full_name", "rating_average"]
        read_only_fields = fields


class CustomerProfileSerializer(serializers.ModelSerializer):
    """Only full_name and phone are writable; everything else is read-only."""
    phone = serializers.CharField(max_length=30, required=False)
    can_book = serializers.BooleanField(read_only=True)
    loyalty_tier = serializers.CharField(read_only=True)

    class Meta:
        model = CustomerProfile
        fields = [
            "id", "full_name", "email", "phone", "profile_photo", "preferred_language",
            "segment", "total_bookings", "rating_average", "referral_code",
            "can_book", "loyalty_tier", "created_at",
        ]
        read_only_fields = [f for f in fields if f not in ("full_name", "phone")]

    def validate_full_name(self, value):
        value = " ".join(value.split())
        if len(value) < 2:
            raise serializers.ValidationError("Enter your full name.")
        return value

    def validate_phone(self, value):
        phone = clean_phone(value)
        qs = CustomerProfile.objects.filter(phone=phone)
        if self.instance is not None:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise serializers.ValidationError("This phone number is already registered.")
        return phone


# ---------------------------------------------------------------------------
# Riders
# ---------------------------------------------------------------------------
class RiderProfileSerializer(serializers.ModelSerializer):
    email = serializers.EmailField(source="user.email", read_only=True)
    is_verified = serializers.BooleanField(read_only=True)
    profile_completion = serializers.IntegerField(read_only=True)
    documents_uploaded = serializers.BooleanField(read_only=True)

    LOCKED_AFTER_VERIFICATION = ("ghana_card_number", "license_number")

    class Meta:
        model = RiderProfile
        fields = [
            "id", "public_id", "rider_code", "email", "full_name", "phone", "profile_photo",
            "date_of_birth", "gender", "ghana_card_number", "city", "address", "preferred_language",
            "motorbike_model", "motorbike_plate_number", "motorbike_color",
            "license_number", "license_expiry_date",
            "verification_status", "is_verified", "rejection_reason", "documents_uploaded",
            "profile_completion", "is_online", "tier", "rating_average", "rating_count",
            "completed_trips", "created_at",
        ]
        read_only_fields = [
            "id", "public_id", "rider_code", "verification_status", "rejection_reason",
            "is_online", "tier", "rating_average", "rating_count", "completed_trips", "created_at",
        ]

    def validate_phone(self, value):
        phone = clean_phone(value)
        qs = RiderProfile.objects.filter(phone=phone)
        if self.instance is not None:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise serializers.ValidationError("This phone number is already registered.")
        return phone

    def _unique_or_blank(self, field, value):
        value = (value or "").strip()
        if not value:
            return value
        qs = RiderProfile.objects.filter(**{field: value})
        if self.instance is not None:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise serializers.ValidationError("This value is already registered to another rider.")
        return value

    def validate_ghana_card_number(self, value):
        return self._unique_or_blank("ghana_card_number", value.upper())

    def validate_license_number(self, value):
        return self._unique_or_blank("license_number", value.upper())

    def validate(self, attrs):
        if self.instance is not None and self.instance.is_verified:
            locked = {
                f: "Cannot be changed after verification. Contact support."
                for f in self.LOCKED_AFTER_VERIFICATION
                if f in attrs and attrs[f] != getattr(self.instance, f)
            }
            if locked:
                raise serializers.ValidationError(locked)
        return attrs

    @transaction.atomic
    def update(self, instance, validated_data):
        instance = super().update(instance, validated_data)
        if "full_name" in validated_data:
            first, last = split_name(instance.full_name)
            User.objects.filter(pk=instance.user_id).update(first_name=first, last_name=last)
        return instance


class RiderDocumentSerializer(serializers.Serializer):
    """Upload payload. The uploaded file maps to RiderDocument.file."""
    document_type = serializers.ChoiceField(choices=RiderDocument.DocumentType.choices)
    document_file = serializers.FileField(write_only=True)
    expiry_date = serializers.DateField(required=False, allow_null=True)

    def validate_document_file(self, f):
        ext = f.name.rsplit(".", 1)[-1].lower() if "." in f.name else ""
        if ext not in ALLOWED_DOCUMENT_EXTENSIONS:
            raise serializers.ValidationError(
                f"Unsupported file type. Allowed: {', '.join(ALLOWED_DOCUMENT_EXTENSIONS)}."
            )
        if f.size > MAX_DOCUMENT_SIZE_MB * 1024 * 1024:
            raise serializers.ValidationError(f"File too large. Maximum size is {MAX_DOCUMENT_SIZE_MB} MB.")
        return f

    def validate_expiry_date(self, value):
        if value and value < timezone.localdate():
            raise serializers.ValidationError("This document has already expired.")
        return value


class RiderLocationSerializer(serializers.Serializer):
    latitude = serializers.FloatField(min_value=-90, max_value=90)
    longitude = serializers.FloatField(min_value=-180, max_value=180)

    def validate(self, attrs):
        if attrs["latitude"] == 0 and attrs["longitude"] == 0:
            raise serializers.ValidationError("Invalid coordinates.")
        return attrs


class LegacyNearbyRidersQuerySerializer(serializers.Serializer):
    """Used by views.get_nearby_riders  (GET /riders/nearby/?latitude=&longitude=&service=)."""
    latitude = serializers.FloatField(min_value=-90, max_value=90)
    longitude = serializers.FloatField(min_value=-180, max_value=180)
    service = serializers.CharField(max_length=30, help_text='Service key, e.g. "okada".')
    radius_km = serializers.FloatField(required=False, default=5.0, min_value=0.5, max_value=25)
    limit = serializers.IntegerField(required=False, default=10, min_value=1, max_value=50)

    def validate_service(self, value):
        value = value.strip().lower()
        if not Service.objects.filter(pk=value, is_active=True).exists():
            raise serializers.ValidationError("Unknown or inactive service.")
        return value


class RiderPublicSerializer(serializers.ModelSerializer):
    """Shown to a customer only for their own active booking.
    JSON keys are unchanged; they are now mapped onto real RiderProfile fields."""

    name = serializers.SerializerMethodField()
    phone_number = serializers.CharField(source="phone", read_only=True)
    plate_number = serializers.CharField(source="motorbike_plate_number", read_only=True)
    rating = serializers.FloatField(source="rating_average", read_only=True)
    latitude = serializers.FloatField(source="current_lat", read_only=True)
    longitude = serializers.FloatField(source="current_lng", read_only=True)
    last_location_at = serializers.DateTimeField(source="location_updated_at", read_only=True)

    class Meta:
        model = RiderProfile
        fields = [
            "id", "name", "phone_number", "plate_number",
            "rating", "latitude", "longitude", "last_location_at",
        ]
        read_only_fields = fields

    def get_name(self, obj) -> str:
        return obj.full_name or obj.user.get_full_name() or obj.user.get_username()


class NearbyRiderSerializer(serializers.Serializer):
    """Deliberately minimal: no phone number, coarse location (~100 m). Reachable by every customer."""

    id = serializers.UUIDField()
    name = serializers.SerializerMethodField()
    service_type = serializers.SerializerMethodField()
    rating = serializers.SerializerMethodField()
    distance_km = serializers.SerializerMethodField()
    latitude = serializers.SerializerMethodField()
    longitude = serializers.SerializerMethodField()

    def get_name(self, obj) -> str:
        first = (getattr(obj, "full_name", "") or "").split(" ")[0]
        return first or getattr(obj.user, "first_name", "") or "Rider"

    def get_service_type(self, obj):
        return getattr(obj, "service_type", None)

    def get_rating(self, obj):
        return round(float(getattr(obj, "rating_average", 0) or 0), 2)

    def get_distance_km(self, obj):
        value = getattr(obj, "distance_km", None)
        return None if value is None else round(float(value), 2)

    def get_latitude(self, obj):
        return round(float(obj.current_lat), 3)

    def get_longitude(self, obj):
        return round(float(obj.current_lng), 3)


class NearbyRidersQuerySerializer(serializers.Serializer):
    """Used by booking.NearbyRidersView  (GET /dispatch/rider/nearby/?lat=&lng=&service_type=)."""
    lat = Lat()
    lng = Lng()
    radius_km = serializers.FloatField(min_value=0.1, max_value=50, default=5)
    service_type = serializers.ChoiceField(choices=ServiceType.choices, required=False)
    limit = serializers.IntegerField(min_value=1, max_value=50, default=20)


# ---------------------------------------------------------------------------
# Money
# ---------------------------------------------------------------------------
class AmountSerializer(serializers.Serializer):
    amount = serializers.DecimalField(max_digits=12, decimal_places=2, min_value=Decimal("0.01"))


class WithdrawalRequestSerializer(AmountSerializer):
    payout_account_id = serializers.IntegerField(required=False, min_value=1)


class InitializePaymentSerializer(serializers.Serializer):
    PURPOSES = ("booking", "wallet_deposit")

    purpose = serializers.ChoiceField(choices=PURPOSES, default="booking")
    booking_ref = serializers.CharField(required=False, max_length=32)
    amount = serializers.DecimalField(required=False, max_digits=12, decimal_places=2,
                                      min_value=Decimal("0.01"))

    def validate(self, attrs):
        if attrs["purpose"] == "booking" and not attrs.get("booking_ref"):
            raise serializers.ValidationError({"booking_ref": "This field is required for booking payments."})
        if attrs["purpose"] == "wallet_deposit" and attrs.get("amount") is None:
            raise serializers.ValidationError({"amount": "This field is required for wallet deposits."})
        return attrs


class VerifyPaymentSerializer(serializers.Serializer):
    reference = serializers.RegexField(REFERENCE_RE, error_messages={"invalid": "Invalid reference format."})


class TransactionSerializer(serializers.ModelSerializer):
    type_display = serializers.CharField(source="get_transaction_type_display", read_only=True)
    status_display = serializers.CharField(source="get_status_display", read_only=True)
    direction = serializers.SerializerMethodField()

    class Meta:
        model = WalletTransaction
        fields = [
            "id", "reference", "transaction_type", "type_display", "direction", "status",
            "status_display", "amount", "balance_after", "currency", "description", "created_at",
        ]
        read_only_fields = fields

    def get_direction(self, obj):
        return "credit" if obj.amount >= 0 else "debit"


class TransactionQuerySerializer(serializers.Serializer):
    ORDERING = ("-created_at", "created_at", "-amount", "amount")

    type = serializers.ChoiceField(choices=[c for c, _ in TRANSACTION_TYPES], required=False)
    status = serializers.ChoiceField(choices=WALLET_TX_STATUSES, required=False)
    direction = serializers.ChoiceField(choices=("credit", "debit"), required=False)
    date_from = serializers.DateField(required=False)
    date_to = serializers.DateField(required=False)
    min_amount = serializers.DecimalField(required=False, max_digits=12, decimal_places=2, min_value=Decimal("0"))
    max_amount = serializers.DecimalField(required=False, max_digits=12, decimal_places=2, min_value=Decimal("0"))
    search = serializers.CharField(required=False, max_length=100, allow_blank=True)
    ordering = serializers.ChoiceField(choices=ORDERING, required=False, default="-created_at")
    export = serializers.ChoiceField(choices=("csv",), required=False)

    def validate(self, attrs):
        if "date_from" in attrs and "date_to" in attrs and attrs["date_from"] > attrs["date_to"]:
            raise serializers.ValidationError({"date_to": "Must not be earlier than date_from."})
        if "min_amount" in attrs and "max_amount" in attrs and attrs["min_amount"] > attrs["max_amount"]:
            raise serializers.ValidationError({"max_amount": "Must not be less than min_amount."})
        return attrs


class WalletDepositSerializer(serializers.ModelSerializer):
    """Read-only view of a WalletDeposit (credited only through WalletDeposit.mark_completed())."""
    rider_name = serializers.CharField(source="wallet.rider.full_name", read_only=True)
    method_display = serializers.CharField(source="get_method_display", read_only=True)
    status_display = serializers.CharField(source="get_status_display", read_only=True)
    net_amount = serializers.DecimalField(max_digits=12, decimal_places=2, read_only=True)
    is_expired = serializers.BooleanField(read_only=True)

    class Meta:
        model = WalletDeposit
        fields = [
            "id", "rider_name", "amount", "fee", "net_amount", "currency",
            "method", "method_display", "status", "status_display",
            "provider", "provider_reference", "failure_reason",
            "balance_before", "balance_after",
            "created_at", "expires_at", "processed_at", "is_expired",
        ]
        read_only_fields = fields


# ---------------------------------------------------------------------------
# Pricing quote
# ---------------------------------------------------------------------------
class QuoteStopSerializer(serializers.Serializer):
    lat = Lat()
    lng = Lng()
    wait_minutes = WaitMinutes()  # expected wait; the final bill uses server timestamps


class QuoteRequestSerializer(serializers.Serializer):
    pickup_lat = Lat()
    pickup_lng = Lng()
    destination_lat = Lat()
    destination_lng = Lng()
    service_type = serializers.ChoiceField(choices=ServiceType.choices, default=ServiceType.OKADA)
    stops = QuoteStopSerializer(many=True, required=False, default=list)
    promo_code = serializers.CharField(max_length=32, required=False, allow_blank=True, default="")

    def validate(self, attrs):
        limit = RATES[attrs["service_type"]].max_stops
        if len(attrs["stops"]) > limit:
            raise serializers.ValidationError(
                {"stops": f"{attrs['service_type']} allows at most {limit} added stop(s)."}
            )
        return attrs


# ---------------------------------------------------------------------------
# Bookings
# ---------------------------------------------------------------------------
class BookingStatusHistorySerializer(serializers.ModelSerializer):
    class Meta:
        model = BookingStatusHistory
        fields = ["from_status", "to_status", "source", "note", "created_at"]
        read_only_fields = fields


class BookingStopInputSerializer(serializers.Serializer):
    lat = Lat()
    lng = Lng()
    address = serializers.CharField(max_length=255, required=False, allow_blank=True, default="")


class BookingCreateSerializer(serializers.Serializer):
    """No price field exists on purpose: the amount comes from quote_token only."""

    quote_token = serializers.CharField(max_length=4096)
    service_type = serializers.ChoiceField(choices=ServiceType.choices)
    pickup_address = serializers.CharField(max_length=255)
    pickup_latitude = Lat()
    pickup_longitude = Lng()
    dropoff_address = serializers.CharField(max_length=255)
    dropoff_latitude = Lat()
    dropoff_longitude = Lng()
    stops = BookingStopInputSerializer(many=True, required=False, default=list)
    notes = serializers.CharField(required=False, allow_blank=True, max_length=1000)

    def validate(self, attrs):
        if (
            attrs["pickup_latitude"] == attrs["dropoff_latitude"]
            and attrs["pickup_longitude"] == attrs["dropoff_longitude"]
        ):
            raise serializers.ValidationError("Pickup and drop-off cannot be the same location.")
        limit = RATES[attrs["service_type"]].max_stops
        if len(attrs["stops"]) > limit:
            raise serializers.ValidationError(
                {"stops": f"{attrs['service_type']} allows at most {limit} added stop(s)."}
            )
        return attrs


class BookingListSerializer(serializers.ModelSerializer):
    status_display = serializers.CharField(source="get_status_display", read_only=True)

    class Meta:
        model = Booking
        fields = [
            "id", "reference", "status", "status_display", "service_type",
            "pickup_address", "dropoff_address", "price", "currency",
            "payment_status", "created_at",
        ]
        read_only_fields = fields


class BookingStopSerializer(serializers.ModelSerializer):
    class Meta:
        model = BookingStop
        fields = ["sequence", "address", "latitude", "longitude", "arrived_at", "departed_at"]
        read_only_fields = fields


RECEIPT_KEYS = (
    "base_fare", "distance_fare", "surge_amount", "ride_fare", "stop_fees",
    "waiting_charge", "booking_fee", "discount", "total",
)

_DETAIL_FIELDS = [
    "id", "reference", "status", "status_display", "payment_status",
    "customer_name", "rider", "service_type", "notes",
    "pickup_address", "pickup_latitude", "pickup_longitude",
    "dropoff_address", "dropoff_latitude", "dropoff_longitude",
    "stops", "distance_km", "quoted_price", "price", "currency",
    "surge_multiplier", "promo_code", "discount_amount", "cancellation_fee", "receipt",
    "accepted_at", "arrived_at", "picked_up_at", "completed_at",
    "cancelled_at", "cancellation_reason",
    "allowed_next_statuses", "history", "created_at", "updated_at",
]


class BookingDetailSerializer(serializers.ModelSerializer):
    """Customer-safe view: no commission, no rider earnings, no signing data."""

    status_display = serializers.CharField(source="get_status_display", read_only=True)
    customer_name = serializers.SerializerMethodField()
    rider = serializers.SerializerMethodField()
    history = BookingStatusHistorySerializer(many=True, read_only=True)
    stops = BookingStopSerializer(many=True, read_only=True)
    receipt = serializers.SerializerMethodField()
    allowed_next_statuses = serializers.SerializerMethodField()

    class Meta:
        model = Booking
        fields = _DETAIL_FIELDS
        read_only_fields = _DETAIL_FIELDS

    def get_customer_name(self, obj) -> str:
        return obj.customer.get_full_name() or obj.customer.get_username()

    def get_rider(self, obj):
        if obj.rider is None:
            return None
        data = RiderPublicSerializer(obj.rider).data
        if obj.is_terminal:  # no phone number or live location once the trip is over
            for key in ("phone_number", "latitude", "longitude", "last_location_at"):
                data.pop(key, None)
        return data

    def get_receipt(self, obj):
        source = obj.final_breakdown or obj.price_breakdown or {}
        return {k: source[k] for k in RECEIPT_KEYS if k in source}

    def get_allowed_next_statuses(self, obj) -> list[str]:
        return sorted(obj.ALLOWED_TRANSITIONS.get(obj.status, set()))


class RiderBookingDetailSerializer(BookingDetailSerializer):
    class Meta(BookingDetailSerializer.Meta):
        fields = _DETAIL_FIELDS + ["rider_earnings"]
        read_only_fields = fields


class StaffBookingDetailSerializer(BookingDetailSerializer):
    class Meta(BookingDetailSerializer.Meta):
        fields = _DETAIL_FIELDS + [
            "rider_earnings", "platform_fee", "quote_id", "price_breakdown", "final_breakdown",
        ]
        read_only_fields = fields


class BookingStatusUpdateSerializer(serializers.Serializer):
    status = serializers.ChoiceField(choices=Booking.Status.choices)
    note = serializers.CharField(required=False, allow_blank=True, max_length=255)


class BookingCancelSerializer(serializers.Serializer):
    reason = serializers.CharField(required=False, allow_blank=True, max_length=255)


class BookingStopEventSerializer(serializers.Serializer):
    event = serializers.ChoiceField(choices=["arrived", "departed"])


# ---------------------------------------------------------------------------
# Webhook
# ---------------------------------------------------------------------------
class BookingWebhookSerializer(serializers.Serializer):
    event_id = serializers.CharField(max_length=100)
    event_type = serializers.ChoiceField(
        choices=["payment.succeeded", "payment.failed", "payment.refunded", "booking.status_changed"]
    )
    data = serializers.DictField()

    def validate(self, attrs):
        data = attrs["data"]
        if not data.get("booking_reference"):
            raise serializers.ValidationError({"data": "booking_reference is required."})
        if attrs["event_type"] == "payment.succeeded" and not (data.get("amount") and data.get("currency")):
            raise serializers.ValidationError({"data": "amount and currency are required."})
        if attrs["event_type"] == "booking.status_changed" and not data.get("status"):
            raise serializers.ValidationError({"data": "status is required."})
        return attrs

