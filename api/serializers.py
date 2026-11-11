"""
Rydnow serializers — written against the consolidated models.py.


Model facts these serializers rely on:
    RiderProfile        user (OneToOne), full_name, phone, current_lat/current_lng,
                        verification_status, `is_verified` is a PROPERTY
    RiderWallet         rider (OneToOne -> RiderProfile)
    WalletTransaction   rider_wallet / customer_wallet, transaction_type, signed `amount`,
                        status in {pending, completed, failed, reversed}
    RiderDocument       rider_profile (FK), document_type, `file`, status
    Service             primary key is `key` ("okada", "parcel", "waste-bin")
"""
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils import timezone
from rest_framework import serializers

from .models import (
    TRANSACTION_TYPES,
    CustomerProfile,
    RiderDocument,
    RiderProfile,
    RiderWallet,
    Service,
    WalletTransaction,
)
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
        probe = User(username=attrs["email"], email=attrs["email"],
                     first_name=split_name(attrs["full_name"])[0])
        check_password_strength(attrs["password"], user=probe)
        return attrs

    def _create_user(self, data):
        first, last = split_name(data["full_name"])
        return User.objects.create_user(
            username=data["email"], email=data["email"], password=data["password"],
            first_name=first, last_name=last,
        )


from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import IntegrityError, transaction
from rest_framework import serializers

from .models import CustomerProfile
from .utils import clean_phone  # adjust import path if needed

User = get_user_model()


class RegistrationSerializer(serializers.Serializer):
    """Customer sign-up: creates the User and its CustomerProfile together."""

    email = serializers.EmailField(max_length=254)
    password = serializers.CharField(write_only=True, min_length=8, trim_whitespace=False)
    full_name = serializers.CharField(max_length=150, required=False, allow_blank=True, default="")
    phone = serializers.CharField(max_length=20, required=False, allow_blank=True, default="")

    def validate_email(self, value):
        email = value.strip().lower()
        if User.objects.filter(email__iexact=email).exists():
            raise serializers.ValidationError("This email is already registered.")
        return email

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
        try:
            validate_password(attrs["password"])
        except DjangoValidationError as e:
            raise serializers.ValidationError({"password": list(e.messages)})
        return attrs

    def _create_user(self, data):
        full_name = (data.get("full_name") or "").strip()
        first, _, last = full_name.partition(" ")
        fields = {"email": data["email"], "first_name": first, "last_name": last}
        # Only set username if your User model has one
        if "username" in [f.name for f in User._meta.get_fields()]:
            fields["username"] = data["email"]
        return User.objects.create_user(password=data["password"], **fields)

    @transaction.atomic
    def create(self, validated_data):
        try:
            # ✅ SAVE values FIRST — before _create_user modifies validated_data
            full_name = validated_data.get("full_name", "")
            phone = validated_data.get("phone", "")
            
            user = self._create_user(validated_data)
            
            CustomerProfile.objects.create(
                user=user,
                full_name=full_name,
                phone=phone,
            )
        except IntegrityError:
            # Race condition: duplicate slipped past validation
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

    @transaction.atomic
    def create(self, validated_data):
        user = self._create_user(validated_data)
        rider = RiderProfile.objects.create(
            user=user, full_name=validated_data["full_name"], phone=validated_data["phone"],
        )
        RiderWallet.objects.create(rider=rider)
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


class NearbyRidersQuerySerializer(serializers.Serializer):
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

