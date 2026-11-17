"""
RYDNOW api/serializers.py

Aligned field-for-field with api/models.py and pricing.py.

Conventions
  * Read serializers never expose secrets (delivery PIN only to the parcel owner, no commission
    or platform revenue to customers, no signing data).
  * Money is Decimal everywhere. Clients never send a price: bookings are created from a signed
    quote token (pricing.issue_quote) and the server stores the snapshot in Booking.details.
  * Model-level ValidationErrors raised by save()/clean() are converted to DRF 400 responses by
    CleanErrorsMixin, so they can never surface as HTTP 500.
  * Service keys: input accepts any alias (okada | ride | parcel | delivery | waste | waste-bin ...)
    and is normalised to the pricing key (okada | parcel | waste-bin), which is also the value
    stored in Booking.service_type.

Sections
  0  Helpers and base classes        7  Rider wallet, withdrawals, deposits
  1  Authentication / account        8  Payments and refunds
  2  Platform (service, zone)        9  Pricing quote and bookings
  3  Customers                      10  Dispatch offers
  4  Customer wallet and loyalty    11  Parcel
  5  Riders                          12  Growth (promo, referral, business, waste, recurring)
  6  Rider public / nearby search   13  Reviews, complaints, incidents, notifications, webhook
"""
from decimal import ROUND_HALF_UP, Decimal
from math import isfinite

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone
from rest_framework import serializers

from . import pricing
from .models import (
    Booking,
    BookingOffer,
    BookingStatusHistory,
    BookingStop,
    BusinessAccount,
    CustomerProfile,
    CustomerRiderPreference,
    CustomerWallet,
    CustomerWalletTransaction,
    DispatchOffer,
    LoyaltyAccount,
    LoyaltyTransaction,
    Notification,
    ParcelDetail,
    ParcelFeedback,
    ParcelProof,
    ParcelTrackingEvent,
    Payment,
    PromoCode,
    RecurringBooking,
    Referral,
    ReferralCode,
    Refund,
    Review,
    RiderComplaint,
    RiderDocument,
    RiderEmergencyContact,
    RiderIncident,
    RiderPayoutAccount,
    RiderProfile,
    RiderStatusHistory,
    RiderWallet,
    SavedAddress,
    Service,
    ServiceQualification,
    SystemAlert,
    WalletDeposit,
    WalletTransaction,
    WasteCollection,
    WasteSubscription,
    WithdrawalRequest,
    Zone,
)
from .pricing import COORD, MAX_WAIT_INPUT_MINUTES, RATES
from .validators import check_password_strength, clean_phone, email_is_taken, split_name

User = get_user_model()

REFERENCE_RE = r"^[A-Za-z0-9_.\-]{6,100}$"
MAX_DOCUMENT_SIZE_MB = 10
MAX_PHOTO_SIZE_MB = 5
ALLOWED_DOCUMENT_EXTENSIONS = ("jpg", "jpeg", "png", "webp", "pdf")
PAYMENT_METHOD_ALIASES = {
    "cash": "cash", "momo": "momo", "mobile_money": "momo", "mobile-money": "momo",
    "card": "card", "visa": "card", "debit": "card", "wallet": "wallet",
}


# =====================================================================
# 0. HELPERS AND BASE CLASSES
# =====================================================================
def _create_auth_user(*, email, password, full_name=""):
    """Single place where auth Users are created."""
    first, last = split_name(full_name) if full_name else ("", "")
    return User.objects.create_user(
        username=email, email=email, password=password, first_name=first, last_name=last
    )


def _django_error_payload(exc):
    if getattr(exc, "error_dict", None):
        return exc.message_dict
    return {"detail": exc.messages}


class CleanErrorsMixin:
    """Turn Django model ValidationErrors / IntegrityErrors into DRF 400s instead of 500s."""

    def create(self, validated_data):
        try:
            return super().create(validated_data)
        except DjangoValidationError as exc:
            raise serializers.ValidationError(_django_error_payload(exc)) from None
        except IntegrityError:
            raise serializers.ValidationError(
                {"detail": "This record conflicts with an existing one."}
            ) from None

    def update(self, instance, validated_data):
        try:
            return super().update(instance, validated_data)
        except DjangoValidationError as exc:
            raise serializers.ValidationError(_django_error_payload(exc)) from None
        except IntegrityError:
            raise serializers.ValidationError(
                {"detail": "This record conflicts with an existing one."}
            ) from None


class ReadOnlyModelSerializer(serializers.ModelSerializer):
    """Every declared field is read-only. Use for anything the API only displays."""

    def get_fields(self):
        fields = super().get_fields()
        for field in fields.values():
            field.read_only = True
            field.required = False
        return fields


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


class ServiceKeyField(serializers.CharField):
    """Accepts any spelling (delivery, ride, waste ...) and returns the pricing key."""

    default_error_messages = {"unknown": "Unknown service type."}

    def to_internal_value(self, data):
        value = super().to_internal_value(data)
        try:
            return pricing.normalize_service_type(value)
        except ValueError:
            self.fail("unknown")


class PaymentMethodField(serializers.CharField):
    default_error_messages = {"unknown": "Unsupported payment method."}

    def to_internal_value(self, data):
        value = super().to_internal_value(data).strip().lower()
        if value not in PAYMENT_METHOD_ALIASES:
            self.fail("unknown")
        return PAYMENT_METHOD_ALIASES[value]


def _validate_image(f, max_mb=MAX_PHOTO_SIZE_MB):
    if f.size > max_mb * 1024 * 1024:
        raise serializers.ValidationError(f"File too large. Maximum size is {max_mb} MB.")
    return f


# =====================================================================
# 1. AUTHENTICATION AND ACCOUNT
# =====================================================================
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
            username=attrs["email"], email=attrs["email"],
            first_name=split_name(attrs["full_name"])[0],
        )
        check_password_strength(attrs["password"], user=probe)
        return attrs


class RegistrationSerializer(serializers.Serializer):
    """Customer sign-up: User + CustomerProfile together. save() returns the User."""

    email = serializers.EmailField(max_length=254)
    password = serializers.CharField(write_only=True, min_length=8, trim_whitespace=False)
    full_name = serializers.CharField(max_length=150, required=False, allow_blank=True, default="")
    phone = serializers.CharField(max_length=30, required=False, allow_blank=True, default="")
    referral_code = serializers.CharField(max_length=20, required=False, allow_blank=True, default="")

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
            raise serializers.ValidationError("Enter a valid phone number.") from None
        if not phone:
            raise serializers.ValidationError("Enter a valid phone number.")
        if CustomerProfile.objects.filter(phone=phone).exists():
            raise serializers.ValidationError("This phone number is already registered.")
        return phone

    def validate_referral_code(self, value):
        value = (value or "").strip().upper()
        if value and not CustomerProfile.objects.filter(referral_code=value).exists():
            raise serializers.ValidationError("This referral code is not valid.")
        return value

    def validate(self, attrs):
        probe = User(
            username=attrs["email"], email=attrs["email"],
            first_name=split_name(attrs["full_name"])[0] if attrs.get("full_name") else "",
        )
        check_password_strength(attrs["password"], user=probe)
        return attrs

    def create(self, validated_data):
        referrer = None
        if validated_data.get("referral_code"):
            referrer = CustomerProfile.objects.filter(referral_code=validated_data["referral_code"]).first()
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
                    phone=validated_data.get("phone") or None,
                    referred_by=referrer,
                    acquisition_source=(
                        CustomerProfile.Source.REFERRAL if referrer else CustomerProfile.Source.ORGANIC
                    ),
                )
        except IntegrityError:
            raise serializers.ValidationError(
                {"detail": "An account with this email or phone already exists."}
            ) from None
        return user


class RiderRegistrationSerializer(_SignupBase):
    """Rider sign-up: User -> RiderProfile -> RiderWallet. save() returns the RiderProfile."""

    referral_code = serializers.CharField(max_length=16, required=False, allow_blank=True, default="")

    def validate_phone(self, value):
        phone = clean_phone(value)
        if RiderProfile.objects.filter(phone=phone).exists():
            raise serializers.ValidationError("This phone number is already registered.")
        return phone

    def validate_referral_code(self, value):
        value = (value or "").strip().upper()
        if value and not RiderProfile.objects.filter(rider_code=value).exists():
            raise serializers.ValidationError("This rider code is not valid.")
        return value

    def create(self, validated_data):
        referrer = None
        if validated_data.get("referral_code"):
            referrer = RiderProfile.objects.filter(rider_code=validated_data["referral_code"]).first()
        try:
            with transaction.atomic():
                user = _create_auth_user(
                    email=validated_data["email"],
                    password=validated_data["password"],
                    full_name=validated_data["full_name"],
                )
                rider = RiderProfile.objects.create(
                    user=user,
                    full_name=validated_data["full_name"],
                    phone=validated_data["phone"],
                    referred_by=referrer,
                )
                RiderWallet.objects.create(rider=rider)
        except ValueError as exc:  # normalize_gh_phone
            raise serializers.ValidationError({"phone": str(exc)}) from None
        except IntegrityError:
            raise serializers.ValidationError(
                {"detail": "An account with this email or phone already exists."}
            ) from None
        return rider


class AccountSerializer(ReadOnlyModelSerializer):
    class Meta:
        model = User
        fields = ["id", "username", "email", "first_name", "last_name", "is_active", "is_staff"]


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


class PhoneSerializer(serializers.Serializer):
    phone = serializers.CharField(max_length=30)

    def validate_phone(self, value):
        return clean_phone(value)


class VerifyCodeSerializer(PhoneSerializer):
    code = serializers.RegexField(r"^\d{6}$", error_messages={"invalid": "Enter the 6-digit code."})


# =====================================================================
# 2. PLATFORM
# =====================================================================
class ServiceSerializer(ReadOnlyModelSerializer):
    class Meta:
        model = Service
        fields = ["key", "name", "description", "is_active"]


class ZoneSerializer(ReadOnlyModelSerializer):
    services = serializers.SlugRelatedField(many=True, read_only=True, slug_field="key")

    class Meta:
        model = Zone
        fields = ["id", "name", "city", "center_lat", "center_lng", "radius_km", "services", "is_active"]


class SystemAlertSerializer(ReadOnlyModelSerializer):
    class Meta:
        model = SystemAlert
        fields = ["id", "title", "message", "level", "is_dismissible", "starts_at", "expires_at"]


# =====================================================================
# 3. CUSTOMERS
# =====================================================================
class CustomerSerializer(ReadOnlyModelSerializer):
    """Compact public view of a customer (shown to riders)."""

    class Meta:
        model = CustomerProfile
        fields = ["id", "full_name", "rating_average"]


class CustomerProfileSerializer(CleanErrorsMixin, serializers.ModelSerializer):
    """Customers may edit phone and preferences only. The name is staff-controlled."""

    phone = serializers.CharField(max_length=30, required=False)
    can_book = serializers.BooleanField(read_only=True)
    loyalty_tier = serializers.CharField(read_only=True)
    phone_verified = serializers.SerializerMethodField()

    WRITABLE = ("phone", "preferred_language", "marketing_opt_in", "profile_photo")

    class Meta:
        model = CustomerProfile
        fields = [
            "id", "full_name", "email", "phone", "phone_verified", "profile_photo",
            "preferred_language", "marketing_opt_in", "segment", "total_bookings",
            "rating_average", "outstanding_fees", "referral_code", "can_book",
            "loyalty_tier", "created_at",
        ]
        read_only_fields = [
            f for f in fields
            if f not in ("phone", "preferred_language", "marketing_opt_in", "profile_photo")
        ]

    def get_phone_verified(self, obj) -> bool:
        return bool(getattr(obj, "phone_verified", False))

    def validate_phone(self, value):
        phone = clean_phone(value)
        qs = CustomerProfile.objects.filter(phone=phone)
        if self.instance is not None:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise serializers.ValidationError("This phone number is already registered.")
        return phone

    def validate_profile_photo(self, f):
        return _validate_image(f)


class CustomerPreferenceSerializer(CleanErrorsMixin, serializers.ModelSerializer):
    class Meta:
        model = CustomerRiderPreference
        fields = [
            "preferred_service", "preferred_language", "dark_mode",
            "notification_enabled", "location_sharing", "updated_at",
        ]
        read_only_fields = ["updated_at"]


class SavedAddressSerializer(CleanErrorsMixin, serializers.ModelSerializer):
    class Meta:
        model = SavedAddress
        fields = [
            "id", "label", "name", "address", "lat", "lng",
            "landmark_note", "is_default", "created_at",
        ]
        read_only_fields = ["id", "created_at"]


# =====================================================================
# 4. CUSTOMER WALLET AND LOYALTY
# =====================================================================
class CustomerWalletSerializer(ReadOnlyModelSerializer):
    class Meta:
        model = CustomerWallet
        fields = ["id", "balance", "withdrawable_balance", "bonus_credit", "currency", "updated_at"]


class CustomerWalletTransactionSerializer(ReadOnlyModelSerializer):
    type_display = serializers.CharField(source="get_transaction_type_display")
    direction = serializers.SerializerMethodField()
    booking_reference = serializers.CharField(source="related_booking.reference", default="")

    class Meta:
        model = CustomerWalletTransaction
        fields = [
            "id", "reference", "transaction_type", "type_display", "direction", "status",
            "amount", "balance_after", "currency", "description", "booking_reference", "created_at",
        ]

    def get_direction(self, obj) -> str:
        return "credit" if obj.amount >= 0 else "debit"


class LoyaltyAccountSerializer(ReadOnlyModelSerializer):
    multiplier = serializers.DecimalField(max_digits=4, decimal_places=2, source="multiplier")

    class Meta:
        model = LoyaltyAccount
        fields = ["points_balance", "lifetime_points", "tier", "multiplier", "updated_at"]


class LoyaltyTransactionSerializer(ReadOnlyModelSerializer):
    class Meta:
        model = LoyaltyTransaction
        fields = ["id", "points", "reason", "reference", "created_at"]


class LoyaltyRedeemSerializer(serializers.Serializer):
    points = serializers.IntegerField(min_value=1)


# =====================================================================
# 5. RIDERS
# =====================================================================
class RiderProfileSerializer(CleanErrorsMixin, serializers.ModelSerializer):
    email = serializers.EmailField(source="user.email", read_only=True)
    is_verified = serializers.BooleanField(read_only=True)
    profile_completion = serializers.IntegerField(read_only=True)
    documents_uploaded = serializers.BooleanField(read_only=True)
    performance_grade = serializers.CharField(read_only=True)

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
            "completed_trips", "performance_grade", "created_at",
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

    def validate_profile_photo(self, f):
        return _validate_image(f)

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
        try:
            instance = super().update(instance, validated_data)
        except ValueError as exc:  # normalize_gh_phone
            raise serializers.ValidationError({"phone": str(exc)}) from None
        if "full_name" in validated_data:
            first, last = split_name(instance.full_name)
            User.objects.filter(pk=instance.user_id).update(first_name=first, last_name=last)
        return instance


class RiderDocumentUploadSerializer(serializers.Serializer):
    """Upload payload. `document_file` maps to RiderDocument.file."""

    document_type = serializers.ChoiceField(choices=RiderDocument.DocumentType.choices)
    document_file = serializers.FileField(write_only=True)
    expiry_date = serializers.DateField(required=False, allow_null=True)

    def validate_document_file(self, f):
        ext = f.name.rsplit(".", 1)[-1].lower() if "." in f.name else ""
        if ext not in ALLOWED_DOCUMENT_EXTENSIONS:
            raise serializers.ValidationError(
                f"Unsupported file type. Allowed: {', '.join(ALLOWED_DOCUMENT_EXTENSIONS)}."
            )
        return _validate_image(f, MAX_DOCUMENT_SIZE_MB)

    def validate_expiry_date(self, value):
        if value and value < timezone.localdate():
            raise serializers.ValidationError("This document has already expired.")
        return value


# Name used by views.py
RiderDocumentSerializer = RiderDocumentUploadSerializer


class RiderDocumentReadSerializer(ReadOnlyModelSerializer):
    type_display = serializers.CharField(source="get_document_type_display")
    is_expired = serializers.BooleanField()

    class Meta:
        model = RiderDocument
        fields = [
            "id", "document_type", "type_display", "file", "status", "expiry_date",
            "is_expired", "uploaded_at", "reviewed_at", "notes",
        ]


class RiderDocumentReviewSerializer(serializers.Serializer):
    """Staff decision on one document."""

    approve = serializers.BooleanField()
    notes = serializers.CharField(required=False, allow_blank=True, max_length=1000, default="")

    def validate(self, attrs):
        if not attrs["approve"] and not attrs["notes"].strip():
            raise serializers.ValidationError({"notes": "Add a short reason before rejecting a document."})
        return attrs


class RiderRejectSerializer(serializers.Serializer):
    reason = serializers.CharField(max_length=1000)


class RiderSuspendSerializer(serializers.Serializer):
    reason = serializers.CharField(max_length=1000)
    until = serializers.DateTimeField(required=False, allow_null=True)

    def validate_until(self, value):
        if value and value <= timezone.now():
            raise serializers.ValidationError("Must be in the future.")
        return value


class RiderStatusHistorySerializer(ReadOnlyModelSerializer):
    class Meta:
        model = RiderStatusHistory
        fields = ["id", "from_status", "to_status", "reason", "created_at"]


class ServiceQualificationSerializer(ReadOnlyModelSerializer):
    service_name = serializers.CharField(source="service.name")

    class Meta:
        model = ServiceQualification
        fields = ["id", "service", "service_name", "status", "approved_at", "status_reason"]


class RiderPayoutAccountSerializer(CleanErrorsMixin, serializers.ModelSerializer):
    masked_number = serializers.CharField(read_only=True)
    network_display = serializers.CharField(source="get_network_display", read_only=True)

    class Meta:
        model = RiderPayoutAccount
        fields = [
            "id", "network", "network_display", "momo_number", "masked_number",
            "account_name", "is_verified", "is_primary", "created_at",
        ]
        read_only_fields = ["id", "is_verified", "created_at"]
        extra_kwargs = {"momo_number": {"write_only": True}}

    def validate_momo_number(self, value):
        try:
            return clean_phone(value)
        except Exception:
            raise serializers.ValidationError("Enter a valid Mobile Money number.") from None


class RiderEmergencyContactSerializer(CleanErrorsMixin, serializers.ModelSerializer):
    masked_phone = serializers.CharField(read_only=True)

    class Meta:
        model = RiderEmergencyContact
        fields = [
            "id", "full_name", "relationship", "relationship_note", "phone_number", "masked_phone",
            "alternative_phone", "address", "preferred_language", "is_primary",
            "notify_on_emergency", "is_verified", "created_at",
        ]
        read_only_fields = ["id", "is_verified", "created_at"]

    def validate(self, attrs):
        relationship = attrs.get("relationship", getattr(self.instance, "relationship", None))
        note = attrs.get("relationship_note", getattr(self.instance, "relationship_note", ""))
        if relationship == RiderEmergencyContact.Relationship.OTHER and not note:
            raise serializers.ValidationError({"relationship_note": "Please describe the relationship."})
        return attrs

    def validate_phone_number(self, value):
        try:
            return RiderEmergencyContact.normalize_phone(value)
        except ValueError as exc:
            raise serializers.ValidationError(str(exc)) from None


class RiderLocationSerializer(serializers.Serializer):
    latitude = serializers.FloatField(min_value=-90, max_value=90)
    longitude = serializers.FloatField(min_value=-180, max_value=180)
    speed_kmh = serializers.FloatField(required=False, allow_null=True, min_value=0, max_value=250)
    heading = serializers.FloatField(required=False, allow_null=True, min_value=0, max_value=360)

    def validate(self, attrs):
        if attrs["latitude"] == 0 and attrs["longitude"] == 0:
            raise serializers.ValidationError("Invalid coordinates.")
        return attrs


class RiderPerformanceSerializer(ReadOnlyModelSerializer):
    performance_score = serializers.FloatField()
    performance_grade = serializers.CharField()
    acceptance_rate = serializers.FloatField()
    cancellation_rate = serializers.FloatField()
    completion_rate = serializers.FloatField()
    avg_response_seconds = serializers.FloatField()
    avg_arrival_minutes = serializers.FloatField()
    total_online_hours = serializers.FloatField()
    needs_attention = serializers.BooleanField()
    attention_reasons = serializers.ListField(child=serializers.CharField())

    class Meta:
        model = RiderProfile
        fields = [
            "tier", "rating_average", "rating_count", "completed_trips", "cancelled_trips",
            "strike_count", "performance_score", "performance_grade", "acceptance_rate",
            "cancellation_rate", "completion_rate", "avg_response_seconds",
            "avg_arrival_minutes", "total_online_hours", "needs_attention", "attention_reasons",
        ]


# =====================================================================
# 6. RIDER PUBLIC / NEARBY SEARCH
# =====================================================================
class RiderPublicSerializer(ReadOnlyModelSerializer):
    """Shown to a customer only for their own active booking."""

    name = serializers.SerializerMethodField()
    phone_number = serializers.CharField(source="phone")
    plate_number = serializers.CharField(source="motorbike_plate_number")
    rating = serializers.FloatField(source="rating_average")
    latitude = serializers.FloatField(source="current_lat")
    longitude = serializers.FloatField(source="current_lng")
    last_location_at = serializers.DateTimeField(source="location_updated_at")

    class Meta:
        model = RiderProfile
        fields = [
            "id", "name", "phone_number", "plate_number", "motorbike_model", "motorbike_color",
            "profile_photo", "rating", "latitude", "longitude", "last_location_at",
        ]

    def get_name(self, obj) -> str:
        return obj.full_name or obj.user.get_full_name() or obj.user.get_username()


class NearbyRiderSerializer(serializers.Serializer):
    """Deliberately minimal: no phone number, coarse location (~100 m)."""

    id = serializers.UUIDField()
    name = serializers.SerializerMethodField()
    service_type = serializers.SerializerMethodField()
    rating = serializers.SerializerMethodField()
    distance_km = serializers.SerializerMethodField()
    latitude = serializers.SerializerMethodField()
    longitude = serializers.SerializerMethodField()

    def get_name(self, obj) -> str:
        first = (getattr(obj, "full_name", "") or "").split(" ")[0]
        return first or "Rider"

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
    """GET /dispatch/rider/nearby/?lat=&lng=&service_type="""

    lat = Lat()
    lng = Lng()
    radius_km = serializers.FloatField(min_value=0.1, max_value=50, default=5)
    service_type = ServiceKeyField(required=False)
    limit = serializers.IntegerField(min_value=1, max_value=50, default=20)


class LegacyNearbyRidersQuerySerializer(serializers.Serializer):
    """GET /riders/nearby/?latitude=&longitude=&service="""

    latitude = serializers.FloatField(min_value=-90, max_value=90)
    longitude = serializers.FloatField(min_value=-180, max_value=180)
    service = serializers.CharField(max_length=30, help_text='Service key, e.g. "okada".')
    radius_km = serializers.FloatField(required=False, default=5.0, min_value=0.5, max_value=25)
    limit = serializers.IntegerField(required=False, default=10, min_value=1, max_value=50)

    def validate_service(self, value):
        try:
            key = pricing.normalize_service_type(value)
        except ValueError:
            raise serializers.ValidationError("Unknown or inactive service.") from None
        if not Service.objects.filter(pk=key, is_active=True).exists():
            raise serializers.ValidationError("Unknown or inactive service.")
        return key


# =====================================================================
# 7. RIDER WALLET, WITHDRAWALS, DEPOSITS
# =====================================================================
class AmountSerializer(serializers.Serializer):
    amount = serializers.DecimalField(max_digits=12, decimal_places=2, min_value=Decimal("0.01"))


class RiderWalletSerializer(ReadOnlyModelSerializer):
    total_funds = serializers.DecimalField(max_digits=12, decimal_places=2)

    class Meta:
        model = RiderWallet
        fields = [
            "balance", "held_balance", "total_funds", "currency", "total_earned",
            "total_withdrawn", "min_withdrawal", "daily_withdrawal_limit",
            "is_active", "is_frozen", "frozen_reason", "updated_at",
        ]


class TransactionSerializer(ReadOnlyModelSerializer):
    """Rider ledger line (WalletTransaction). Keeps the keys the app already uses."""

    transaction_type = serializers.CharField(source="tx_type")
    type_display = serializers.CharField(source="get_tx_type_display")
    signed_amount = serializers.SerializerMethodField()
    status = serializers.SerializerMethodField()
    status_display = serializers.SerializerMethodField()
    currency = serializers.CharField(source="wallet.currency")

    class Meta:
        model = WalletTransaction
        fields = [
            "id", "reference", "transaction_type", "type_display", "direction", "status",
            "status_display", "amount", "signed_amount", "balance_before", "balance_after",
            "currency", "description", "booking_ref", "created_at",
        ]

    def get_signed_amount(self, obj):
        return -obj.amount if obj.direction == WalletTransaction.Direction.DEBIT else obj.amount

    def get_status(self, obj) -> str:
        return "completed"  # ledger entries are only written once the movement happened

    def get_status_display(self, obj) -> str:
        return "Completed"


class TransactionQuerySerializer(serializers.Serializer):
    ORDERING = ("-created_at", "created_at", "-amount", "amount")

    type = serializers.ChoiceField(choices=WalletTransaction.Type.choices, required=False)
    direction = serializers.ChoiceField(choices=WalletTransaction.Direction.choices, required=False)
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


class WithdrawalRequestSerializer(AmountSerializer):
    """Input. The payout account is resolved server-side and must belong to the rider."""

    payout_account_id = serializers.IntegerField(required=False, min_value=1)


class WithdrawalSerializer(ReadOnlyModelSerializer):
    net_amount = serializers.DecimalField(max_digits=12, decimal_places=2)
    masked_account = serializers.CharField()
    status_display = serializers.CharField(source="get_status_display")

    class Meta:
        model = WithdrawalRequest
        fields = [
            "id", "reference", "amount", "fee", "net_amount", "status", "status_display",
            "method", "provider", "account_name", "masked_account", "failure_reason",
            "requested_at", "processed_at",
        ]


class WalletDepositSerializer(ReadOnlyModelSerializer):
    rider_name = serializers.CharField(source="wallet.rider.full_name")
    currency = serializers.CharField(source="wallet.currency")
    source_display = serializers.CharField(source="get_source_display")

    class Meta:
        model = WalletDeposit
        fields = [
            "id", "reference", "rider_name", "amount", "currency", "source",
            "source_display", "source_reference", "notes", "created_at",
        ]


class StaffDepositSerializer(serializers.Serializer):
    """Staff records a cash/bonus/bank deposit for a rider."""

    amount = serializers.DecimalField(max_digits=12, decimal_places=2, min_value=Decimal("0.01"))
    source = serializers.ChoiceField(choices=WalletDeposit.SourceType.choices)
    source_reference = serializers.CharField(required=False, allow_blank=True, max_length=255, default="")
    notes = serializers.CharField(required=False, allow_blank=True, default="")

    def validate(self, attrs):
        source = attrs["source"]
        if source in WalletDeposit.REFERENCE_REQUIRED_SOURCES and not attrs["source_reference"].strip():
            raise serializers.ValidationError(
                {"source_reference": "A provider transaction reference is required for this deposit type."}
            )
        return attrs


# =====================================================================
# 8. PAYMENTS AND REFUNDS
# =====================================================================
class InitializePaymentSerializer(serializers.Serializer):
    PURPOSES = ("booking", "wallet_deposit")

    purpose = serializers.ChoiceField(choices=PURPOSES, default="booking")
    booking_ref = serializers.CharField(required=False, max_length=32)
    amount = serializers.DecimalField(required=False, max_digits=12, decimal_places=2, min_value=Decimal("0.01"))

    def validate(self, attrs):
        if attrs["purpose"] == "booking" and not attrs.get("booking_ref"):
            raise serializers.ValidationError({"booking_ref": "This field is required for booking payments."})
        if attrs["purpose"] == "wallet_deposit" and attrs.get("amount") is None:
            raise serializers.ValidationError({"amount": "This field is required for wallet deposits."})
        return attrs


class VerifyPaymentSerializer(serializers.Serializer):
    reference = serializers.RegexField(REFERENCE_RE, error_messages={"invalid": "Invalid reference format."})


class PaymentSerializer(ReadOnlyModelSerializer):
    method_display = serializers.CharField(source="get_method_display")
    status_display = serializers.CharField(source="get_status_display")
    refundable_amount = serializers.DecimalField(max_digits=10, decimal_places=2)

    class Meta:
        model = Payment
        fields = [
            "id", "reference", "booking_ref", "amount", "currency", "refunded_amount",
            "refundable_amount", "method", "method_display", "status", "status_display",
            "failure_reason", "recorded_at", "created_at",
        ]


class RefundSerializer(ReadOnlyModelSerializer):
    status_display = serializers.CharField(source="get_status_display")
    reason_display = serializers.CharField(source="get_reason_display")
    currency = serializers.CharField()
    payment_reference = serializers.CharField(source="payment.reference")

    class Meta:
        model = Refund
        fields = [
            "id", "reference", "payment_reference", "amount", "currency", "reason",
            "reason_display", "reason_details", "status", "status_display",
            "rejection_reason", "failure_reason", "reviewed_at", "processed_at", "created_at",
        ]


class RefundRequestSerializer(serializers.Serializer):
    payment_reference = serializers.CharField(max_length=30)
    reason = serializers.ChoiceField(choices=Refund.Reason.choices)
    amount = serializers.DecimalField(required=False, max_digits=10, decimal_places=2, min_value=Decimal("0.01"))
    reason_details = serializers.CharField(required=False, allow_blank=True, default="")
    idempotency_key = serializers.CharField(required=False, allow_blank=True, max_length=100, default="")

    def validate(self, attrs):
        if attrs["reason"] == Refund.Reason.OTHER and not attrs["reason_details"].strip():
            raise serializers.ValidationError({"reason_details": "Please describe the reason."})
        return attrs


class RefundDecisionSerializer(serializers.Serializer):
    approve = serializers.BooleanField()
    reason = serializers.CharField(required=False, allow_blank=True, max_length=255, default="")

    def validate(self, attrs):
        if not attrs["approve"] and not attrs["reason"].strip():
            raise serializers.ValidationError({"reason": "A reason is required to reject a refund."})
        return attrs


# =====================================================================
# 9. PRICING QUOTE AND BOOKINGS
# =====================================================================
class QuoteStopSerializer(serializers.Serializer):
    lat = Lat()
    lng = Lng()
    wait_minutes = WaitMinutes()  # expected wait; the final bill uses server timestamps


class QuoteRequestSerializer(serializers.Serializer):
    pickup_lat = Lat()
    pickup_lng = Lng()
    destination_lat = Lat()
    destination_lng = Lng()
    service_type = ServiceKeyField(default="okada")
    stops = QuoteStopSerializer(many=True, required=False, default=list)
    promo_code = serializers.CharField(max_length=32, required=False, allow_blank=True, allow_null=True, default="")

    def validate_promo_code(self, value):
        return (value or "").strip()

    def validate(self, attrs):
        limit = RATES[attrs["service_type"]].max_stops
        if len(attrs["stops"]) > limit:
            raise serializers.ValidationError(
                {"stops": f"{attrs['service_type']} allows at most {limit} added stop(s)."}
            )
        return attrs


class QuoteResponseSerializer(serializers.Serializer):
    """Shape the quote endpoint returns (documentation and response validation)."""

    quote_token = serializers.CharField()
    quote_id = serializers.CharField()
    service_type = serializers.CharField()
    service_name = serializers.CharField()
    original_price = serializers.DecimalField(max_digits=10, decimal_places=2)
    discount_amount = serializers.DecimalField(max_digits=10, decimal_places=2)
    price = serializers.DecimalField(max_digits=10, decimal_places=2)
    total_price = serializers.DecimalField(max_digits=10, decimal_places=2)
    currency = serializers.CharField()
    distance_km = serializers.DecimalField(max_digits=8, decimal_places=2)
    duration_min = serializers.IntegerField()
    surge_multiplier = serializers.DecimalField(max_digits=3, decimal_places=1)
    expires_at = serializers.DateTimeField()


class BookingStopInputSerializer(serializers.Serializer):
    lat = Lat()
    lng = Lng()
    address = serializers.CharField(max_length=255, required=False, allow_blank=True, default="")


class BookingCreateSerializer(serializers.Serializer):
    """
    No price field exists on purpose: the amount comes from quote_token only.

    save(customer=request.user) creates a DRAFT booking priced from the signed quote and stores
    the full fare snapshot in Booking.details["fare_snapshot"]. The view then calls
    booking.submit() (cash/wallet) or starts the online payment first.
    """

    quote_token = serializers.CharField(max_length=4096)
    service_type = ServiceKeyField()
    pickup_address = serializers.CharField(max_length=255)
    pickup_lat = Lat()
    pickup_lng = Lng()
    destination_address = serializers.CharField(max_length=255, required=False, allow_blank=True, default="")
    destination_lat = Lat()
    destination_lng = Lng()
    stops = BookingStopInputSerializer(many=True, required=False, default=list)
    notes = serializers.CharField(required=False, allow_blank=True, max_length=1000, default="")
    scheduled_for = serializers.DateTimeField(required=False, allow_null=True)
    payment_method = PaymentMethodField(default="cash")
    details = serializers.DictField(required=False, default=dict)

    def validate_scheduled_for(self, value):
        if value and value < timezone.now():
            raise serializers.ValidationError("Scheduled time cannot be in the past.")
        return value

    def validate(self, attrs):
        service = attrs["service_type"]
        waste = service == "waste-bin"
        if not waste and not attrs["destination_address"].strip():
            raise serializers.ValidationError({"destination_address": "A destination is required."})
        if (
            not waste
            and attrs["pickup_lat"] == attrs["destination_lat"]
            and attrs["pickup_lng"] == attrs["destination_lng"]
        ):
            raise serializers.ValidationError("Pickup and destination cannot be the same location.")
        limit = RATES[service].max_stops
        if len(attrs["stops"]) > limit:
            raise serializers.ValidationError({"stops": f"{service} allows at most {limit} added stop(s)."})

        customer = self.context.get("customer") or getattr(self.context.get("request"), "user", None)
        profile = CustomerProfile.objects.filter(user=customer).first() if customer else None
        if profile is not None and not profile.can_book:
            raise serializers.ValidationError(
                "This account cannot place bookings right now. Clear outstanding fees or contact support."
            )
        return attrs

    def _read_quote(self, attrs, user):
        try:
            data = pricing.read_quote(attrs["quote_token"], user_id=user.pk)
        except pricing.QuoteError as exc:
            raise serializers.ValidationError({"quote_token": exc.message}) from None
        route = pricing.build_route(
            (attrs["pickup_lat"], attrs["pickup_lng"]),
            [{"lat": s["lat"], "lng": s["lng"]} for s in attrs["stops"]],
            (attrs["destination_lat"], attrs["destination_lng"]),
        )
        if not pricing.quote_matches_trip(data, service_type=attrs["service_type"], route=route):
            raise serializers.ValidationError(
                {"quote_token": "This price was issued for a different trip. Please request a new quote."}
            )
        return data

    def create(self, validated_data):
        user = validated_data.pop("customer", None) or self.context.get("customer") or self.context["request"].user
        quote = self._read_quote(validated_data, user)
        fare = quote["fare"]
        total = Decimal(fare["total"])
        discount = Decimal(fare["discount"])
        stops = validated_data["stops"]

        details = dict(validated_data.get("details") or {})
        details["quote_id"] = quote["qid"]
        details["fare_snapshot"] = fare          # includes commission and rider earnings (staff/rider only)
        details["waits"] = quote.get("waits", [])

        with transaction.atomic():
            booking = Booking.objects.create(
                customer=user,
                service_type=validated_data["service_type"],
                pickup_address=validated_data["pickup_address"],
                pickup_lat=float(validated_data["pickup_lat"]),
                pickup_lng=float(validated_data["pickup_lng"]),
                destination_address=validated_data["destination_address"],
                destination_lat=float(validated_data["destination_lat"]),
                destination_lng=float(validated_data["destination_lng"]),
                notes=validated_data["notes"],
                scheduled_for=validated_data.get("scheduled_for"),
                details=details,
                subtotal=total + discount,       # gross; Booking._apply_pricing() subtracts the discount
                discount_amount=discount,
                currency=pricing.CURRENCY,
                payment_method=validated_data["payment_method"],
                estimated_distance=float(Decimal(quote.get("dist", "0"))),
            )
            for index, stop in enumerate(stops, start=1):
                BookingStop.objects.create(
                    booking=booking, order=index, address=stop["address"],
                    latitude=float(stop["lat"]), longitude=float(stop["lng"]),
                )
            code = fare.get("promo_code")
            if code and discount > 0:
                promo = PromoCode.find(code)
                if promo is None:
                    raise serializers.ValidationError({"promo_code": "This promo code is no longer valid."})
                try:
                    promo.redeem(
                        user, booking.reference, total + discount, validated_data["service_type"],
                        currency=pricing.CURRENCY,
                    )
                except DjangoValidationError as exc:
                    raise serializers.ValidationError({"promo_code": exc.messages[0]}) from None
            if booking.total_fare != total:
                raise serializers.ValidationError("Price mismatch. Please request a new quote.")
        return booking


class BookingStatusHistorySerializer(ReadOnlyModelSerializer):
    changed_by_name = serializers.SerializerMethodField()

    class Meta:
        model = BookingStatusHistory
        fields = ["id", "status", "notes", "changed_by_name", "created_at"]

    def get_changed_by_name(self, obj):
        user = obj.changed_by
        return (user.get_full_name() or user.get_username()) if user else None


class BookingStopSerializer(ReadOnlyModelSerializer):
    class Meta:
        model = BookingStop
        fields = ["id", "order", "address", "latitude", "longitude"]


class BookingListSerializer(ReadOnlyModelSerializer):
    """List view. Also exposes the short keys the customer app reads (pickup, destination, price)."""

    status_display = serializers.CharField(source="get_status_display")
    pickup = serializers.CharField(source="pickup_address")
    destination = serializers.CharField(source="destination_address")
    price = serializers.DecimalField(source="total_fare", max_digits=10, decimal_places=2)
    rider_name = serializers.SerializerMethodField()
    rider_phone = serializers.SerializerMethodField()
    rider_lat = serializers.SerializerMethodField()
    rider_lon = serializers.SerializerMethodField()

    class Meta:
        model = Booking
        fields = [
            "id", "reference", "status", "status_display", "service_type",
            "pickup_address", "destination_address", "pickup", "destination",
            "total_fare", "price", "currency", "payment_method", "payment_status",
            "scheduled_for", "created_at", "rider_name", "rider_phone", "rider_lat", "rider_lon",
        ]

    def _profile(self, obj):
        return getattr(obj.rider, "rider_profile", None) if obj.rider_id else None

    def _live(self, obj):
        return obj.status in (Booking.Status.ACCEPTED, Booking.Status.EN_ROUTE, Booking.Status.ARRIVED)

    def get_rider_name(self, obj):
        p = self._profile(obj)
        return p.full_name if p else None

    def get_rider_phone(self, obj):
        p = self._profile(obj)
        return p.phone if p and self._live(obj) else None

    def get_rider_lat(self, obj):
        p = self._profile(obj)
        return p.current_lat if p and self._live(obj) else None

    def get_rider_lon(self, obj):
        p = self._profile(obj)
        return p.current_lng if p and self._live(obj) else None


RECEIPT_KEYS = (
    "base_fare", "distance_fare", "surge_amount", "ride_fare", "stop_fees",
    "waiting_charge", "booking_fee", "discount", "total",
)

_BOOKING_DETAIL_FIELDS = [
    "id", "reference", "status", "status_display", "service_type", "payment_method",
    "payment_status", "customer_name", "rider", "notes", "scheduled_for",
    "pickup_address", "pickup_lat", "pickup_lng",
    "destination_address", "destination_lat", "destination_lng", "stops",
    "estimated_distance", "estimated_duration", "subtotal", "discount_amount", "total_fare",
    "currency", "receipt", "requested_at", "accepted_at", "started_at", "arrived_at",
    "completed_at", "cancelled_at", "cancellation_reason", "allowed_next_statuses",
    "history", "created_at", "updated_at",
]


class BookingDetailSerializer(ReadOnlyModelSerializer):
    """Customer-safe view: no commission, no rider earnings, no signing data."""

    status_display = serializers.CharField(source="get_status_display")
    customer_name = serializers.SerializerMethodField()
    rider = serializers.SerializerMethodField()
    history = BookingStatusHistorySerializer(source="status_history", many=True)
    stops = BookingStopSerializer(many=True)
    receipt = serializers.SerializerMethodField()
    allowed_next_statuses = serializers.SerializerMethodField()

    class Meta:
        model = Booking
        fields = _BOOKING_DETAIL_FIELDS

    def get_customer_name(self, obj) -> str:
        return obj.customer.get_full_name() or obj.customer.get_username()

    def get_rider(self, obj):
        profile = getattr(obj.rider, "rider_profile", None) if obj.rider_id else None
        if profile is None:
            return None
        data = RiderPublicSerializer(profile).data
        if obj.is_terminal:  # no phone number or live location once the trip is over
            for key in ("phone_number", "latitude", "longitude", "last_location_at"):
                data.pop(key, None)
        return data

    def get_receipt(self, obj):
        snapshot = (obj.details or {}).get("fare_snapshot", {})
        return {k: snapshot[k] for k in RECEIPT_KEYS if k in snapshot}

    def get_allowed_next_statuses(self, obj) -> list:
        return sorted(str(s) for s in obj.TRANSITIONS.get(obj.status, set()))


class RiderBookingDetailSerializer(BookingDetailSerializer):
    rider_earnings = serializers.SerializerMethodField()

    class Meta(BookingDetailSerializer.Meta):
        fields = _BOOKING_DETAIL_FIELDS + ["rider_earnings"]

    def get_rider_earnings(self, obj):
        return (obj.details or {}).get("fare_snapshot", {}).get("rider_earnings")


class StaffBookingDetailSerializer(RiderBookingDetailSerializer):
    platform_commission = serializers.SerializerMethodField()
    platform_revenue = serializers.SerializerMethodField()
    fare_snapshot = serializers.SerializerMethodField()

    class Meta(RiderBookingDetailSerializer.Meta):
        fields = RiderBookingDetailSerializer.Meta.fields + [
            "platform_commission", "platform_revenue", "fare_snapshot", "details",
            "cancelled_by", "failure_reason", "failed_at",
        ]

    def get_platform_commission(self, obj):
        return (obj.details or {}).get("fare_snapshot", {}).get("platform_commission")

    def get_platform_revenue(self, obj):
        return (obj.details or {}).get("fare_snapshot", {}).get("platform_revenue")

    def get_fare_snapshot(self, obj):
        return (obj.details or {}).get("fare_snapshot", {})


class BookingStatusUpdateSerializer(serializers.Serializer):
    status = serializers.ChoiceField(choices=Booking.Status.choices)
    note = serializers.CharField(required=False, allow_blank=True, max_length=255, default="")


class BookingCancelSerializer(serializers.Serializer):
    reason = serializers.CharField(required=False, allow_blank=True, max_length=255, default="")


# =====================================================================
# 10. DISPATCH OFFERS
# =====================================================================
class DispatchOfferSerializer(ReadOnlyModelSerializer):
    booking_reference = serializers.CharField(source="booking.reference")
    service_type = serializers.CharField(source="booking.service_type")
    pickup_address = serializers.CharField(source="booking.pickup_address")
    destination_address = serializers.CharField(source="booking.destination_address")
    estimated_distance = serializers.FloatField(source="booking.estimated_distance")
    rider_earnings = serializers.SerializerMethodField()
    seconds_remaining = serializers.SerializerMethodField()

    class Meta:
        model = DispatchOffer
        fields = [
            "id", "booking_reference", "service_type", "pickup_address", "destination_address",
            "estimated_distance", "rider_earnings", "status", "round_no", "distance_km",
            "offered_at", "expires_at", "seconds_remaining",
        ]

    def get_rider_earnings(self, obj):
        return (obj.booking.details or {}).get("fare_snapshot", {}).get("rider_earnings")

    def get_seconds_remaining(self, obj) -> int:
        return max(int((obj.expires_at - timezone.now()).total_seconds()), 0)


class DeclineOfferSerializer(serializers.Serializer):
    reason = serializers.CharField(required=False, allow_blank=True, max_length=60, default="")


class BookingOfferSerializer(ReadOnlyModelSerializer):
    """Legacy price-offer model (BookingOffer)."""

    is_actionable = serializers.BooleanField()

    class Meta:
        model = BookingOffer
        fields = [
            "id", "booking_ref", "rider", "offered_price", "message", "status",
            "expires_at", "responded_at", "is_actionable", "created_at",
        ]


# =====================================================================
# 11. PARCEL
# =====================================================================
class ParcelTrackingEventSerializer(ReadOnlyModelSerializer):
    status_display = serializers.CharField(source="get_status_display")

    class Meta:
        model = ParcelTrackingEvent
        fields = [
            "id", "status", "status_display", "note", "location_label",
            "latitude", "longitude", "created_at",
        ]


class ParcelProofSerializer(ReadOnlyModelSerializer):
    class Meta:
        model = ParcelProof
        fields = [
            "id", "proof_type", "image", "signed_by", "notes",
            "latitude", "longitude", "created_at",
        ]


class ParcelProofCreateSerializer(serializers.ModelSerializer):
    class Meta:
        model = ParcelProof
        fields = ["proof_type", "image", "signed_by", "notes", "latitude", "longitude"]

    def validate_image(self, f):
        return _validate_image(f) if f else f

    def validate(self, attrs):
        if attrs["proof_type"] != ParcelProof.ProofType.PIN_VERIFIED and not attrs.get("image"):
            raise serializers.ValidationError({"image": "An image is required for this proof type."})
        return attrs


class ParcelDetailSerializer(ReadOnlyModelSerializer):
    """Customer/rider view. The delivery PIN is NOT included."""

    status_display = serializers.CharField(source="get_status_display")
    events = ParcelTrackingEventSerializer(many=True)
    proofs = ParcelProofSerializer(many=True)
    is_final = serializers.BooleanField()

    class Meta:
        model = ParcelDetail
        fields = [
            "id", "booking_ref", "tracking_code", "status", "status_display", "recipient_name",
            "recipient_phone", "item_description", "item_value", "package_size", "weight_kg",
            "is_fragile", "requires_signature", "special_instructions", "delivery_attempts",
            "received_by", "picked_up_at", "delivered_at", "is_final", "events", "proofs",
            "created_at",
        ]


class ParcelOwnerSerializer(ParcelDetailSerializer):
    """Sender only: includes the handover PIN to give the recipient."""

    class Meta(ParcelDetailSerializer.Meta):
        fields = ParcelDetailSerializer.Meta.fields + ["delivery_pin"]


class ParcelPublicTrackingSerializer(ReadOnlyModelSerializer):
    """Recipient tracking page: no phone numbers, no PIN."""

    status_display = serializers.CharField(source="get_status_display")
    events = ParcelTrackingEventSerializer(many=True)

    class Meta:
        model = ParcelDetail
        fields = ["tracking_code", "status", "status_display", "delivered_at", "events"]


class ParcelCreateSerializer(CleanErrorsMixin, serializers.ModelSerializer):
    recipient_phone = serializers.CharField(max_length=20)

    class Meta:
        model = ParcelDetail
        fields = [
            "booking_ref", "recipient_name", "recipient_phone", "recipient_email", "notify_recipient",
            "item_description", "item_value", "package_size", "weight_kg", "is_fragile",
            "requires_signature", "special_instructions",
        ]

    def validate_recipient_phone(self, value):
        return clean_phone(value)

    def validate_booking_ref(self, value):
        user = self.context["request"].user
        if not Booking.objects.filter(reference=value, customer=user).exists():
            raise serializers.ValidationError("Booking not found.")
        return value


class ParcelStatusUpdateSerializer(serializers.Serializer):
    status = serializers.ChoiceField(choices=ParcelDetail.Status.choices)
    note = serializers.CharField(required=False, allow_blank=True, max_length=255, default="")
    latitude = Lat(required=False, allow_null=True)
    longitude = Lng(required=False, allow_null=True)
    location_label = serializers.CharField(required=False, allow_blank=True, max_length=150, default="")
    delivery_pin = serializers.RegexField(r"^\d{4}$", required=False)

    def validate(self, attrs):
        if attrs["status"] == ParcelDetail.Status.DELIVERED and "delivery_pin" not in attrs:
            raise serializers.ValidationError({"delivery_pin": "The recipient's 4-digit PIN is required."})
        return attrs


class ParcelFeedbackSerializer(CleanErrorsMixin, serializers.ModelSerializer):
    class Meta:
        model = ParcelFeedback
        fields = ["id", "rating", "comment", "created_at"]
        read_only_fields = ["id", "created_at"]


# =====================================================================
# 12. GROWTH: PROMO, REFERRAL, BUSINESS, WASTE, RECURRING
# =====================================================================
class PromoCodePublicSerializer(ReadOnlyModelSerializer):
    discount_display = serializers.CharField(source="get_discount_display")
    weekday_label = serializers.CharField()

    class Meta:
        model = PromoCode
        fields = [
            "code", "name", "description", "discount_type", "discount_value", "discount_display",
            "currency", "minimum_order_value", "maximum_discount", "valid_until",
            "weekday_label", "valid_from_time", "valid_until_time", "for_service_types",
            "first_time_customers_only",
        ]


class PromoValidateSerializer(serializers.Serializer):
    """Server-side promo check. The discount is recalculated inside the next quote."""

    code = serializers.CharField(max_length=50)
    amount = serializers.DecimalField(max_digits=10, decimal_places=2, min_value=Decimal("0.01"))
    service_type = ServiceKeyField()

    def validate_code(self, value):
        return value.strip()


class ReferralSerializer(ReadOnlyModelSerializer):
    status_display = serializers.CharField(source="get_status_display")

    class Meta:
        model = Referral
        fields = [
            "id", "code", "status", "status_display", "currency", "reward_amount",
            "referee_reward_amount", "signed_up_at", "completed_at", "rewarded_at",
            "expires_at", "created_at",
        ]


class ReferralCodeSerializer(ReadOnlyModelSerializer):
    class Meta:
        model = ReferralCode
        fields = [
            "code", "status", "referrer_reward_value", "referee_reward_value", "currency",
            "max_uses", "times_used", "valid_until",
        ]


class RedeemReferralSerializer(serializers.Serializer):
    code = serializers.CharField(max_length=20)

    def validate_code(self, value):
        return Referral.normalize_code(value)


class BusinessAccountApplicationSerializer(CleanErrorsMixin, serializers.ModelSerializer):
    status_display = serializers.CharField(source="get_status_display", read_only=True)

    class Meta:
        model = BusinessAccount
        fields = [
            "id", "business_name", "contact_person", "email", "phone_number", "address", "industry",
            "registration_number", "service_type", "preferred_frequency", "preferred_day",
            "preferred_time", "estimated_volume", "discount_requested", "discount_approved",
            "status", "status_display", "review_notes", "created_at",
        ]
        read_only_fields = ["id", "discount_approved", "status", "review_notes", "created_at"]

    def validate_email(self, value):
        value = value.strip().lower()
        qs = BusinessAccount.objects.filter(email__iexact=value)
        if self.instance is not None:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise serializers.ValidationError("A business with this email already exists.")
        return value


class BusinessReviewSerializer(serializers.Serializer):
    """Staff review: approve (optional discount) or reject (reason required)."""

    approve = serializers.BooleanField()
    discount_approved = serializers.DecimalField(
        required=False, max_digits=5, decimal_places=2, min_value=Decimal("0"), max_value=Decimal("100")
    )
    notes = serializers.CharField(required=False, allow_blank=True, default="")

    def validate(self, attrs):
        if not attrs["approve"] and not attrs["notes"].strip():
            raise serializers.ValidationError({"notes": "A reason is required to reject an application."})
        return attrs


class RecurringBookingSerializer(CleanErrorsMixin, serializers.ModelSerializer):
    status_display = serializers.CharField(source="get_status_display", read_only=True)

    class Meta:
        model = RecurringBooking
        fields = [
            "id", "reference", "business", "service_type", "pickup_address", "dropoff_address",
            "notes", "frequency", "preferred_day", "preferred_time", "start_date", "end_date",
            "next_booking_date", "status", "status_display", "created_at",
        ]
        read_only_fields = ["id", "reference", "status", "created_at"]

    def validate(self, attrs):
        get = lambda k: attrs.get(k, getattr(self.instance, k, None))  # noqa: E731
        nxt, day, start, end = get("next_booking_date"), get("preferred_day"), get("start_date"), get("end_date")
        errors = {}
        if nxt and day is not None and nxt.weekday() != day:
            errors["next_booking_date"] = "Next booking must fall on the preferred weekday."
        if start and nxt and nxt < start:
            errors["next_booking_date"] = "First booking cannot be before the start date."
        if start and end and end < start:
            errors["end_date"] = "End date cannot be before the start date."
        if get("service_type") != "waste-bin" and not (get("dropoff_address") or "").strip():
            errors["dropoff_address"] = "A drop-off address is required for this service."
        business = get("business")
        if business is not None and not business.is_active:
            errors["business"] = "Only active businesses can schedule recurring bookings."
        if errors:
            raise serializers.ValidationError(errors)
        return attrs


class WasteCollectionSerializer(ReadOnlyModelSerializer):
    status_display = serializers.CharField(source="get_status_display")

    class Meta:
        model = WasteCollection
        fields = [
            "id", "reference", "subscription", "scheduled_date", "scheduled_time", "status",
            "status_display", "assigned_rider", "price", "bins_collected", "weight_kg",
            "proof_photo", "notes", "completed_at",
        ]


class WasteCollectionCompleteSerializer(serializers.Serializer):
    bins_collected = serializers.IntegerField(required=False, min_value=0)
    weight_kg = serializers.DecimalField(required=False, max_digits=7, decimal_places=2, min_value=Decimal("0"))
    proof_photo = serializers.ImageField(required=False)
    notes = serializers.CharField(required=False, allow_blank=True, max_length=255, default="")

    def validate_proof_photo(self, f):
        return _validate_image(f)


class WasteSubscriptionSerializer(ReadOnlyModelSerializer):
    status_display = serializers.CharField(source="get_status_display")
    estimated_monthly_cost = serializers.DecimalField(max_digits=10, decimal_places=2)

    class Meta:
        model = WasteSubscription
        fields = [
            "id", "reference", "waste_type", "quantity", "pickup_address", "latitude", "longitude",
            "contact_phone", "special_instructions", "frequency", "preferred_day", "preferred_time",
            "start_date", "end_date", "next_collection_date", "price_per_collection",
            "discount_percent", "total_price", "estimated_monthly_cost", "status",
            "status_display", "business", "created_at",
        ]


class WasteSubscriptionCreateSerializer(CleanErrorsMixin, serializers.ModelSerializer):
    """
    The price is NEVER accepted from the client: it is fixed server-side from pricing.RATES
    when the subscription is created. save(customer=request.user) sets the owner.
    """

    class Meta:
        model = WasteSubscription
        fields = [
            "waste_type", "quantity", "pickup_address", "latitude", "longitude", "contact_phone",
            "special_instructions", "frequency", "preferred_day", "preferred_time", "start_date",
            "end_date", "next_collection_date", "business",
        ]

    def validate(self, attrs):
        errors = {}
        day, nxt = attrs.get("preferred_day"), attrs.get("next_collection_date")
        start, end = attrs.get("start_date") or timezone.localdate(), attrs.get("end_date")
        if nxt and day is not None and nxt.weekday() != day:
            errors["next_collection_date"] = "Next collection must fall on the preferred weekday."
        if nxt and nxt < start:
            errors["next_collection_date"] = "First collection cannot be before the start date."
        if end and end < start:
            errors["end_date"] = "End date cannot be before the start date."
        if (
            attrs.get("waste_type") == WasteSubscription.WasteType.HAZARDOUS
            and not (attrs.get("special_instructions") or "").strip()
        ):
            errors["special_instructions"] = "Describe the hazardous waste and how it is stored."
        business = attrs.get("business")
        if business is not None and not business.is_active:
            errors["business"] = "Subscriptions can only be created for active businesses."
        if errors:
            raise serializers.ValidationError(errors)
        return attrs

    def create(self, validated_data):
        validated_data.setdefault("price_per_collection", RATES["waste-bin"].minimum)
        return super().create(validated_data)


# =====================================================================
# 13. REVIEWS, COMPLAINTS, INCIDENTS, NOTIFICATIONS, WEBHOOK
# =====================================================================
class ReviewSerializer(ReadOnlyModelSerializer):
    class Meta:
        model = Review
        fields = ["id", "booking_ref", "subject_type", "rating", "comment", "created_at"]


class ReviewCreateSerializer(serializers.Serializer):
    booking_ref = serializers.CharField(max_length=50)
    rating = serializers.IntegerField(min_value=1, max_value=5)
    comment = serializers.CharField(required=False, allow_blank=True, max_length=1000, default="")


class RiderComplaintSerializer(CleanErrorsMixin, serializers.ModelSerializer):
    status_display = serializers.CharField(source="get_status_display", read_only=True)

    class Meta:
        model = RiderComplaint
        fields = [
            "id", "reference", "complaint_type", "subject", "description", "rider",
            "booking_ref", "status", "status_display", "created_at", "resolved_at",
        ]
        read_only_fields = ["id", "reference", "status", "created_at", "resolved_at"]


class RiderIncidentSerializer(CleanErrorsMixin, serializers.ModelSerializer):
    class Meta:
        model = RiderIncident
        fields = [
            "id", "booking_ref", "title", "description", "severity", "status",
            "created_at", "resolved_at",
        ]
        read_only_fields = ["id", "status", "created_at", "resolved_at"]


class NotificationSerializer(ReadOnlyModelSerializer):
    class Meta:
        model = Notification
        fields = ["id", "title", "message", "category", "data", "is_read", "read_at", "created_at"]


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

