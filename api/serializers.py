from rest_framework import serializers
from django.contrib.auth.models import User
from .models import (
    CustomerProfile, RiderProfile, Rider, Booking,
    RiderWallet, Transaction, WithdrawalRequest, RiderComplaint
)




# ─── USER SERIALIZER ───
class UserSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = ['id', 'username', 'email', 'first_name', 'last_name']




# ─── CUSTOMER PROFILE ───
class CustomerProfileSerializer(serializers.ModelSerializer):
    username = serializers.CharField(source='user.username', read_only=True)
    email = serializers.EmailField(source='user.email', read_only=True)
    user_name = serializers.CharField(source='user.username', required=False)
    
    class Meta:
        model = CustomerProfile
        fields = ['id', 'username', 'email', 'full_name', 'phone', 'profile_photo', 'user_name', 'created_at', 'updated_at']
        read_only_fields = ['id', 'created_at', 'updated_at']




# ─── RIDER PROFILE ───
class RiderProfileSerializer(serializers.ModelSerializer):
    class Meta:
        model = RiderProfile
        fields = '__all__'




# ─── RIDER ───
class RiderSerializer(serializers.ModelSerializer):
    class Meta:
        model = Rider
        fields = '__all__'




# ─── BOOKING ───
class BookingSerializer(serializers.ModelSerializer):
    class Meta:
        model = Booking
        fields = '__all__'
        read_only_fields = ['id', 'created_at', 'updated_at']




# ─── RIDER WALLET ───
class RiderWalletSerializer(serializers.ModelSerializer):
    class Meta:
        model = RiderWallet
        fields = '__all__'




# ─── TRANSACTION ───
class TransactionSerializer(serializers.ModelSerializer):
    class Meta:
        model = Transaction
        fields = '__all__'




# ─── WITHDRAWAL REQUEST ───
class WithdrawalRequestSerializer(serializers.ModelSerializer):
    class Meta:
        model = WithdrawalRequest
        fields = '__all__'




# ─── RIDER COMPLAINT ───
class RiderComplaintSerializer(serializers.ModelSerializer):
    class Meta:
        model = RiderComplaint
        fields = '__all__'
