from rest_framework import serializers
from .models import Rider, Booking, CustomerProfile


class RiderSerializer(serializers.ModelSerializer):
    class Meta:
        model = Rider
        # ✅ ONLY safe public fields — NO documents, emails, or private data
        fields = ['id', 'name', 'phone', 'is_online', 'verification_status',
                  'current_lat', 'current_lng']


class BookingSerializer(serializers.ModelSerializer):
    class Meta:
        model = Booking
        # ✅ Everything the frontend needs — but NOTHING sensitive writable
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
        # ✅ Sensitive fields — READ ONLY! Frontend can display them but CANNOT change them
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
        fields = ['id', 'username', 'email', 'phone', 'profile_photo']

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
