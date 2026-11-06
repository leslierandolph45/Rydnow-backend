import math
import os
import re
from decimal import Decimal

from rest_framework import serializers
from django.contrib.auth.models import User
from django.contrib.auth.password_validation import validate_password
from django.contrib.auth import get_user_model
from django.db import transaction

from .models import Rider, RiderProfile, RiderWallet
from .models import Booking, CustomerProfile

from django.core.validators import RegexValidator, validate_email


User = get_user_model()


class RiderRegistrationSerializer(serializers.Serializer):
    """
    Serializer for rider registration - Phase 1
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
        allow_blank=True,
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


SERVICE_TYPES = ("okada", "parcel", "waste-bin")

PHONE_PATTERN = re.compile(r"^\+?[0-9]{9,15}$")
REFERENCE_PATTERN = r"^[A-Za-z0-9._=-]{8,100}$"

MAX_DOCUMENT_SIZE = 5 * 1024 * 1024  # 5 MB
ALLOWED_DOCUMENT_EXTENSIONS = {".jpg", ".jpeg", ".png", ".pdf"}
ALLOWED_DOCUMENT_CONTENT_TYPES = {"image/jpeg", "image/png", "application/pdf"}


class PhoneField(serializers.CharField):
    """Accepts '+233 24-123 4567' style input; stores digits with optional leading +."""

    def to_internal_value(self, data):
        value = re.sub(r"[\s\-()]", "", super().to_internal_value(data))
        if not PHONE_PATTERN.fullmatch(value):
            raise serializers.ValidationError("Enter a valid phone number.")
        return value


def _finite(value):
    if not math.isfinite(value):  # FloatField accepts "nan"/"inf" and min/max don't catch them
        raise serializers.ValidationError("Enter a valid number.")
    return value


# ───────────────────────
# Auth
# ───────────────────────
class LoginSerializer(serializers.Serializer):
    username = serializers.CharField(max_length=150)
    password = serializers.CharField(write_only=True, trim_whitespace=False)


class CustomerRegisterSerializer(serializers.Serializer):
    full_name = serializers.CharField(max_length=150)
    email = serializers.EmailField()
    phone = PhoneField()
    password = serializers.CharField(write_only=True, trim_whitespace=False)

    def validate_email(self, value):
        value = value.strip().lower()
        if User.objects.filter(email__iexact=value).exists() or User.objects.filter(username__iexact=value).exists():
            raise serializers.ValidationError("This email is already registered.")
        return value

    def validate_phone(self, value):
        if CustomerProfile.objects.filter(phone=value).exists():
            raise serializers.ValidationError("This phone number is already registered.")
        return value

    def validate_password(self, value):
        validate_password(value)  # uses AUTH_PASSWORD_VALIDATORS from settings
        return value


class PhoneSerializer(serializers.Serializer):
    phone = PhoneField()


class VerifyCodeSerializer(serializers.Serializer):
    phone = PhoneField()
    code = serializers.RegexField(r"^\d{6}$", error_messages={"invalid": "Enter the 6-digit code."})


# ───────────────────────
# Location
# ───────────────────────
class CoordinatesSerializer(serializers.Serializer):
    latitude = serializers.FloatField(min_value=-90, max_value=90)
    longitude = serializers.FloatField(min_value=-180, max_value=180)

    def validate_latitude(self, value):
        return _finite(value)

    def validate_longitude(self, value):
        return _finite(value)


class NearbyRidersSerializer(CoordinatesSerializer):
    service_type = serializers.ChoiceField(choices=SERVICE_TYPES)
    radius_km = serializers.FloatField(required=False, default=10.0, min_value=0.5, max_value=25)


# ───────────────────────
# Wallet & payments
# ───────────────────────
class AmountSerializer(serializers.Serializer):
    amount = serializers.DecimalField(max_digits=10, decimal_places=2, min_value=Decimal("1.00"))


class InitializePaymentSerializer(serializers.Serializer):
    purpose = serializers.ChoiceField(choices=("booking", "wallet_deposit"))
    booking_ref = serializers.CharField(required=False, max_length=64)
    amount = serializers.DecimalField(
        required=False, max_digits=10, decimal_places=2, min_value=Decimal("1.00")
    )

    def validate(self, attrs):
        if attrs["purpose"] == "booking" and not attrs.get("booking_ref"):
            raise serializers.ValidationError({"booking_ref": "This field is required for booking payments."})
        if attrs["purpose"] == "wallet_deposit" and attrs.get("amount") is None:
            raise serializers.ValidationError({"amount": "This field is required for wallet deposits."})
        return attrs


class VerifyPaymentSerializer(serializers.Serializer):
    reference = serializers.RegexField(REFERENCE_PATTERN)


# ───────────────────────
# Rider documents
# ───────────────────────
class RiderDocumentSerializer(serializers.Serializer):
    document_type = serializers.CharField(max_length=50)
    document_file = serializers.FileField()

    def validate_document_file(self, file):
        if file.size > MAX_DOCUMENT_SIZE:
            raise serializers.ValidationError("File too large (max 5 MB).")
        extension = os.path.splitext(file.name)[1].lower()
        if extension not in ALLOWED_DOCUMENT_EXTENSIONS:
            raise serializers.ValidationError("Allowed file types: JPG, PNG, PDF.")
        content_type = getattr(file, "content_type", "")
        if content_type not in ALLOWED_DOCUMENT_CONTENT_TYPES:
            raise serializers.ValidationError("Unsupported file content type.")
        return file
