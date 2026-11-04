from rest_framework import serializers
from django.contrib.auth.models import User
from django.contrib.auth.password_validation import validate_password
from django.core import validators
from .models import Rider, RiderProfile, RiderWallet
from .models import Booking, CustomerProfile


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
        validators=[validators.validate_email]
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


class RiderSerializer(serializers.ModelSerializer):
    class Meta:
        model = Rider
        fields = ['id', 'name', 'phone', 'is_online', 'verification_status',
                  'current_lat', 'current_lng']


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


class CustomerProfileSerializer(serializers.ModelSerializer):
    username = serializers.CharField(source='user.username', read_only=True)
    email = serializers.EmailField(source='user.email', read_only=True)

    class Meta:
        model = CustomerProfile
        fields = [
            'id', 'username', 'email', 'full_name', 'phone',
            'profile_photo', 'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'username', 'email', 'created_at', 'updated_at']

    def validate_phone(self, value):
        cleaned = value.replace(' ', '').strip()

        if cleaned.startswith('0') and len(cleaned) == 10 and cleaned.isdigit():
            return cleaned

        if cleaned.startswith('+233') and len(cleaned) == 13:
            return cleaned

        if len(cleaned) == 9 and cleaned.isdigit():
            return '0' + cleaned

        raise serializers.ValidationError(
            "Enter a valid Ghanaian number — e.g. 0241234567 or +233241234567"
        )

    def validate_full_name(self, value):
        if value and not value.strip():
            raise serializers.ValidationError("Full name cannot be blank.")
        return value.strip() if value else value
