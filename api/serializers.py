from rest_framework import serializers
from .models import Rider, Booking, CustomerProfile


class RiderSerializer(serializers.ModelSerializer):
    class Meta:
        model = Rider
        fields = '__all__'


class BookingSerializer(serializers.ModelSerializer):
    class Meta:
        model = Booking
        fields = '__all__'
        read_only_fields = ('id', 'created_at', 'updated_at')


class CustomerProfileSerializer(serializers.ModelSerializer):
    username = serializers.CharField(source='user.username', read_only=True)
    email = serializers.EmailField(source='user.email', read_only=True)

    class Meta:
        model = CustomerProfile
        fields = ['id', 'username', 'email', 'phone', 'profile_photo']

    def validate_phone(self, value):
        # Remove ALL spaces first — user can type however they want!
        cleaned = value.replace(' ', '').strip()

        # ✅ Format 1: 0244203327 (starts with 0, 10 digits total)
        if cleaned.startswith('0') and len(cleaned) == 10 and cleaned.isdigit():
            return cleaned

        # ✅ Format 2: +233244203327 (starts with +233, 13 chars total)
        if cleaned.startswith('+233') and len(cleaned) == 13:
            return cleaned

        # ✅ Format 3: 244203327 (no leading zero → add it automatically)
        if len(cleaned) == 9 and cleaned.isdigit():
            return '0' + cleaned

        # ❌ If none of the above
        raise serializers.ValidationError(
            "Enter a valid Ghanaian number — e.g. 0241234567 or +233241234567"
        )
