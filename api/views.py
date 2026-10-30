#  STANDARD LIBRARY IMPORTS
import os
import json
import requests
from decimal import Decimal, ROUND_HALF_UP

#  DJANGO CORE IMPORTS
from django.db import transaction, IntegrityError
from django.conf import settings
from django.utils import timezone
from django.core.exceptions import ObjectDoesNotExist
from django.shortcuts import render, redirect
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.contrib.auth import authenticate

#  DJANGO REST FRAMEWORK IMPORTS
from rest_framework.decorators import api_view, permission_classes
from rest_framework.response import Response
from rest_framework import status
from rest_framework.authtoken.models import Token
from rest_framework.permissions import AllowAny, IsAuthenticated

#  LOCAL APP MODELS
from .models import (
    Booking, WalletTransaction, RiderWallet, COMMISSION_RATE,
    Rider, Transaction, WithdrawalRequest
)

@api_view(['POST'])
@permission_classes([AllowAny])
def user_login(request):
    username= request.data.get('username')
    password= request.data.get('password')

    if not username or password:
        return Response(
            {"detail": "Username and password are required."},
            status=status.HTTP_400_BAD_REQUEST
        )
    user = authentication(request, username=username, password=password)

    if user is None or not user.is_active:
        return Response(
            {"detail": "Invalid credentials."},
            status=status.HTTP_401_UNAUTHORIZED
        )
    
    token,_= Token.objects.get_or_create(user=user)

    return Response({
        "tokekn": token.key,
        "user":{
            "id":user.id,
            "username":user.username,
            "email":user.email
        }
    }, status=status.HTTP_200_OK)

def customer_register(request):
    from djanjo.contrib.auth.password_validation import validate_password
    from django.core.exceptions import ValidationError
    from django.core.validators import validate_email as django_validate_email
    from django.db import transaction, IntegrityError
    from django.contrib.auth.models import User
    from .models import CustomerProfile

    def require_str(field_name,value):
        if not isinstance(value,str):
            return None
            raw_email = request.data.get('email')
            raw_phone = request.data.get('phone')
            raw_full_name = request.data.get('full_name')
            raw_password = request.data.get('password')
    
    errors = {}

    type_err = require_str('email', raw_email)
    if type_err:
        errors['email']= type_err
    else:
        email = raw_email.strip()
    type_err = requires_str('phone' , raw_phone)
    if type_err:
        errors ['phone'] = type_err
    else:
        phone = raw_phone.strip()
    type_err = require_str('full_name', raw_full_name)
    if type_err:
        errors['full_name'] = type_err
    else:
        full_name = raw_full_name.strip()
    if not isinstance(raw_password, str):
        errors['password'] = 'Password must be a text value.'
    else: 
        password = raw_password.strip()
    if errors:
        return Response({'errors': errors}, status=status.HTTP_400_BAD_REQUEST)

    if not email:
        errors['email'] = 'Email is required.'
    if not phone:
        errors['phone'] = 'Phone number is required.'
    if not full_name:
        errors['full_name'] = 'Full name is required.'
    if not password:
        errors['password'] = 'Password is required.'

    if errors:
        return Response({'errors' : errors}, status = status.HTTP_400_BAD_REQUEST)

    try:    
        django_validate_email(email)
    except ValidationError:
        errors['email'] ='Please enter a valid email address'
        return Response({'errors': errors} , status=status.HTTP_400_BAD_REQUEST) 

    if len(email) > 150:
        errors['email'] = 'Email address is too long. Please use an email under 150 characters'
        return Response ({'errors' : errors}, status=status.HTTP_400_BAD_REQUEST)
    try:
        validate_password(password)
    except ValidateError as e:
        errors['password'] = list (e.messages)
        return Response ({'errors' : errors}  , status=status.HTTP_400_BAD_REQUEST)

    if user.objects.filter(username=email).exists():
        return Response(
            {'email': 'This  email is already registered.'},
            status=status.HTTP_409_CONFLICT
    ) 
    if customer_profile.objects.filter(phone=phone).exists():
        return Response(
            {'phone' : 'This phone number is already registered.'},
            status=status.HTTP_409_CONFLICT
    )

    try:
        with transaction.atomic():
            user = User.objects.create_user(
                username=email,
                email=email,
                password=password,
            )

        # Split full name safely
            name_parts = full_name.split() if full_else else []
            first_name = name_parts[0] if name_parts else ''
            last_name = ''.join(name_parts[1:]) if len (name_parts) > 1 else ''

            customer_profile= CustomerProfile.objects.create(
                user=user,
                full_name=full_name,
                phone=phone
            )
    except IntegrityError:
        import logging
        logger= logging.getLogger(__name)
        logger.error ('Registration: unexpected database integrity constraint violation', exc_info=True)
        return Response({'detail':'Registration could not be completed. Please try again later.'
        }, status=status.HTTP_500_INTERNAL_SERVER_ERROR )

    return Response ({
        'message': 'Registration Successful',
        'user' : {
            'id': user.id,
            'email' : user.email,
            'full_name' : customer_profile.full_name,
            'phone': customer_profile.phone
            }
    }, status=status.HTTP_201_CREATED)


def record_job_payment(request):
    """
    ✅ Secure payment verification & recording — Paystack-first flow.
    Idempotent: safe to call multiple times. No duplicate credits.
    Client supplies ONLY the reference — all other data verified server-side.
    """
    reference = request.data.get('reference')


    # ─── 1. Input validation ───
    if not reference or not isinstance(reference, str) or len(reference.strip()) < 8:
        return Response(
            {'detail': 'A valid payment reference is required.'},
            status=status.HTTP_400_BAD_REQUEST
        )
    reference = reference.strip()


    # ─── 2. Look up booking by payment_ref ───
    try:
        booking = Booking.objects.select_related('assigned_rider').get(
            payment_ref=reference
        )
    except Booking.DoesNotExist:
        return Response(
            {'detail': 'No booking found for this payment reference.'},
            status=status.HTTP_404_NOT_FOUND
        )


    # ─── 3. Idempotency: Check existing transaction FIRST ───
    existing_txn = None
    try:
        existing_txn = WalletTransaction.objects.get(reference=reference)
    except WalletTransaction.DoesNotExist:
        pass


    if existing_txn is not None:
        if existing_txn.related_booking_id != booking.id:
            return Response(
                {'detail': 'Payment reference is associated with a different record. Please contact support.'},
                status=status.HTTP_409_CONFLICT
            )
        return Response({
            'message': 'Payment already verified and recorded.',
            'booking_id': str(booking.id),
            'status': 'already_recorded',
            'total_amount': float(existing_txn.amount),
        }, status=status.HTTP_200_OK)


    # ─── 4. Check booking-level payment status ───
    if hasattr(booking, 'payment_status') and booking.payment_status in ['held', 'released']:
        return Response({
            'message': 'Payment already verified and recorded.',
            'booking_id': str(booking.id),
            'status': 'already_recorded'
        }, status=status.HTTP_200_OK)


    # ─── 5. Verify payment with Paystack ───
    PAYSTACK_SECRET_KEY = os.environ.get('PAYSTACK_SECRET_KEY')
    if not PAYSTACK_SECRET_KEY:
        return Response(
            {'detail': 'Payment verification service is currently unavailable.'},
            status=status.HTTP_503_SERVICE_UNAVAILABLE
        )


    verify_url = f'https://api.paystack.co/transaction/verify/{reference}'
    headers = {
        'Authorization': f'Bearer {PAYSTACK_SECRET_KEY}',
        'Content-Type': 'application/json'
    }


    try:
        resp = requests.get(verify_url, headers=headers, timeout=30)
        if resp.status_code != 200:
            return Response(
                {'detail': 'Payment verification failed. Please retry shortly.'},
                status=status.HTTP_502_BAD_GATEWAY
            )
        data = resp.json()
    except requests.exceptions.RequestException:
        return Response(
            {'detail': 'Could not reach payment verification service. Please try again.'},
            status=status.HTTP_503_SERVICE_UNAVAILABLE
        )
    except json.JSONDecodeError:
        return Response(
            {'detail': 'Invalid response from payment service.'},
            status=status.HTTP_502_BAD_GATEWAY
        )


    # ─── 6. Validate Paystack response ───
    if not isinstance(data, dict) or not data.get('status'):
        return Response(
            {'detail': 'Payment verification was not successful.'},
            status=status.HTTP_400_BAD_REQUEST
        )


    paystack_data = data.get('data', {})


    if paystack_data.get('status') != 'success':
        return Response(
            {'detail': 'Payment has not been completed successfully.'},
            status=status.HTTP_400_BAD_REQUEST
        )


    if paystack_data.get('reference') != reference:
        return Response(
            {'detail': 'Payment reference mismatch detected.'},
            status=status.HTTP_409_CONFLICT
        )


    if paystack_data.get('currency') != 'GHS':
        return Response(
            {'detail': 'Unsupported payment currency. Only GHS is accepted.'},
            status=status.HTTP_400_BAD_REQUEST
        )


    # ─── 7. Amount verification ───
    verified_amount_pesewas = paystack_data.get('amount')
    if not isinstance(verified_amount_pesewas, int) or verified_amount_pesewas <= 0:
        return Response(
            {'detail': 'Invalid payment amount from verification service.'},
            status=status.HTTP_400_BAD_REQUEST
        )


    verified_amount = Decimal(verified_amount_pesewas) / Decimal('100')
    booking_amount = Decimal(booking.total_fare)


    if abs(verified_amount - booking_amount) > Decimal('0.01'):
        return Response(
            {'detail': 'Payment amount does not match booking total.'},
            status=status.HTTP_409_CONFLICT
        )


    # ─── 8. Atomic transaction ───
    if hasattr(booking_locked, 'payment_status') and booking_locked.payment_status in ['held', 'released']:
        return Response({
            'message': 'Payment already verified and recorded.',
            'booking_id': str(booking_locked.id),
            'status': 'already_recorded'
        }, status=status.HTTP_200_OK)


    rider = getattr(booking_locked, 'assigned_rider', None)
    if rider is None:
        return Response(
            {'detail': 'This booking has no assigned rider. Cannot record payment.'},
            status=status.HTTP_400_BAD_REQUEST
        )


    commission_rate = getattr(settings, 'COMMISSION_RATE', COMMISSION_RATE)
    commission = (booking_amount * Decimal(str(commission_rate))).quantize(
        Decimal('0.01'), rounding=ROUND_HALF_UP
    )
    rider_amount = (booking_amount - commission).quantize(
        Decimal('0.01'), rounding=ROUND_HALF_UP
    )
    #Verify payment referebce and protect against mismatches
    verified_ok, verify_msg = booking_locked.confirm_payment_verified(
        provider_ref=paystack_data.get('id',''),
        payment_ref= reference,
    )
    #Stop immediatelly if reference validation failed
    if not verified_ok:
        return Response(
            {'detail': verify_msg},
            status=status.HTTP_409_CONFLICT
        )

    try:
        wallet, _ = RiderWallet.objects.get_or_create(rider=rider)
        if hasattr(wallet, 'credit_earnings'):
            wallet_result, wallet_msg = wallet.credit_earnings(
                rider_amount,
                booking_ref=booking_locked.booking_ref,
                payment_verified=True,
        )
        if wallet_result is None:
            return Response({'detail': wallet_msg}, status=status.HTTP_409_CONFLICT)

    except Booking.DoesNotExist:
        return Response(
            {'detail': 'Booking was removed during processing.'},
            status=status.HTTP_404_NOT_FOUND
     )
    except IntegrityError:
        return Response(
            {'detail': 'Payment recording conflict. Reference may already exist.'},
            status=status.HTTP_409_CONFLICT
     )
            
    # ─── 9. Success response ───
    return Response({
        'message': '✅ Payment verified successfully.',
        'booking_id': str(booking_locked.id),
        'booking_ref': booking_locked.booking_ref,
        'total_amount': float(booking_amount),
        'platform_commission': float(commission),
        'rider_amount': float(rider_amount),
        'payment_status': getattr(booking_locked, 'payment_status', 'confirmed')
    }, status=status.HTTP_201_CREATED)

@api_view(['POST'])
@permission_classes([AllowAny])
def send_verification_code(request):
    return Response(
        {"detail": "Feature coming soon"},
        status=status.HTTP_200_OK
    )



from rest_framework.decorators import api_view, permission_classes, throttle_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework import status
from rest_framework.throttling import AnonRateThrottle
from django.db import transaction
from django.contrib.auth.models import User
from .models import Rider, RiderProfile, RiderWallet
from .serializers import RiderRegistrationSerializer


class RegistrationThrottle(AnonRateThrottle):
    """Limit registration attempts: max 5 per minute per IP"""
    rate = '5/min'


@api_view(['POST'])
@permission_classes([AllowAny])
@throttle_classes([RegistrationThrottle])
@transaction.atomic
def rider_register(request):
    """
    Rider Registration — Phase 1 Implementation
    Creates: Django User → Rider → RiderProfile → RiderWallet
    All-or-nothing transaction — no orphan records on failure.
    """
    serializer = RiderRegistrationSerializer(data=request.data)
    if not serializer.is_valid():
        return Response(
            {"error": "Validation failed", "details": serializer.errors},
            status=status.HTTP_400_BAD_REQUEST
        )

    data = serializer.validated_data

    try:
        # Step 1: Create Django User
        user = User.objects.create_user(
            username=data['email'],
            email=data['email'],
            password=data['password'],
            first_name=data['full_name'].split()[0],
            last_name=' '.join(data['full_name'].split()[1:]) or ''
        )

        # Step 2: Create Rider
        rider = Rider.objects.create(
            phone=data['phone'],
            location=data.get('location', ''),
            verification_status='pending',
            is_restricted=False,
        )

         # Step 3: Create RiderProfile
        RiderProfile.objects.create(
            rider=rider,
            full_name=data['full_name'],
            email=data['email'],
            phone=data['phone'],
            motorbike_model=data.get('motorbike_model', ''),
            license_number=data.get('license_number', ''),
            is_online=False,
        )

        # Step 4: Create RiderWallet
        RiderWallet.objects.create(rider=rider)
        return Response({
            "success": True,
            "message": "Registration submitted successfully. Please wait for verification approval.",
            "rider_id": rider.id,
            "email": user.email,
            "phone": rider.phone,
            "verification_status": rider.verification_status,
            "requires_approval": True,
            "auth_note": "You will be able to log in once your account is approved."
            }, status=status.HTTP_201_CREATED)
    except Exception:
        return Response(
            {"error": "Registration failed. Please try again later."},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR
        )

@api_view(['POST'])
@permission_classes([IsAuthenticated])
def rider_submit_documents(request):
    """
    Handle document uploads from authenticated riders.
    POST: Upload a new document
    """
    # Get the rider profile for current user
    try:
        rider = getattr(request.user, 'rider', None)
        if not rider:
            return Response(
                {'error': 'Rider profile not found'},
                status=status.HTTP_404_NOT_FOUND
            )
    except Exception as e:
        return Response(
            {'error': str(e)},
            status=status.HTTP_400_BAD_REQUEST
        )
    
    # Get form data
    document_type = request.data.get('document_type')
    document_file = request.FILES.get('document_file')
    
    # Validate inputs
    if not document_type or not document_file:
        return Response(
            {'error': 'document_type and document_file are required'},
            status=status.HTTP_400_BAD_REQUEST
        )
    
    try:
        # Use our atomic upload method
        new_doc = RiderDocument.resubmit_document(
            rider=rider,
            document_type=document_type,
            document_file=document_file
        )
        
        return Response({
            'success': True,
            'id': new_doc.id,
            'document_type': new_doc.document_type,
            'status': new_doc.status,
            'message': 'Document uploaded successfully'
        }, status=status.HTTP_201_CREATED)
    
    except ValueError as e:
        return Response(
            {'error': str(e)},
            status=status.HTTP_400_BAD_REQUEST
        )
    
    except IntegrityError:
        return Response(
            {'error': 'This document type has already been submitted'},
            status=status.HTTP_409_CONFLICT
        )
    
    except Exception as e:
        logger.error(f"Upload error: {str(e)}")
        return Response(
            {'error': 'Upload failed. Please try again.'},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR
        )
@api_view(['GET', 'PATCH'])
@permission_classes([IsAuthenticated])
def customer_profile(request):
    """
    GET  : Return the authenticated customer's profile
    PATCH: Update full_name and/or phone number
    """
    # 🔍 Get the customer's profile — safely from the logged-in user ONLY
    try:
        profile = request.user.customer_profile
    except CustomerProfile.DoesNotExist:
        return Response(
            {
                "success": False,
                "error": "Customer profile not found. Please complete registration."
            },
            status=status.HTTP_404_NOT_FOUND
        )

    if request.method == 'GET':
        serializer = CustomerProfileSerializer(profile)
        return Response({
            "success": True,
            "data": serializer.data
        })

    if request.method == 'PATCH':
        # 🛡️ Only allow THESE fields to be updated — never email/username
        allowed_fields = ['full_name', 'phone']
        update_data = {
            key: value for key, value in request.data.items()
            if key in allowed_fields
        }

        # 🚫 Empty payload
        if not update_data:
            return Response({
                "success": False,
                "error": "No valid fields provided. You can update: full_name, phone"
            }, status=status.HTTP_400_BAD_REQUEST)

        # ✅ Validate and save
        serializer = CustomerProfileSerializer(
            profile,
            data=update_data,
            partial=True  # ← This is what allows updating ONE field at a time
        )
        if serializer.is_valid():
            serializer.save()
            return Response({
                "success": True,
                "message": "Profile updated successfully",
                "data": serializer.data
            })
        else:
            return Response({
                "success": False,
                "error": serializer.errors
            }, status=status.HTTP_400_BAD_REQUEST)

from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework import status
from django.utils import timezone
from .models import Rider


@api_view(['PATCH'])
@permission_classes([IsAuthenticated])
def update_rider_location(request):
    try:
        rider = request.user.rider
    except Rider.DoesNotExist:
        return Response(
            {"success": False, "error": "Rider profile not found"},
            status=status.HTTP_404_NOT_FOUND
        )

    latitude = request.data.get('latitude')
    longitude = request.data.get('longitude')

    if latitude is None or longitude is None:
        return Response(
            {"success": False, "error": "Both latitude and longitude are required"},
            status=status.HTTP_400_BAD_REQUEST
        )

    try:
        lat_float = float(latitude)
        lng_float = float(longitude)
    except (TypeError, ValueError):
        return Response(
            {"success": False, "error": "Invalid coordinate format"},
            status=status.HTTP_400_BAD_REQUEST
        )

    if not (-90 <= lat_float <= 90):
        return Response(
            {"success": False, "error": "Latitude must be between -90 and 90"},
            status=status.HTTP_400_BAD_REQUEST
        )
    if not (-180 <= lng_float <= 180):
        return Response(
            {"success": False, "error": "Longitude must be between -180 and 180"},
            status=status.HTTP_400_BAD_REQUEST
        )

    rider.current_lat = lat_float
    rider.current_lng = lng_float
    rider.last_location_update = timezone.now()
    rider.save(update_fields=['current_lat', 'current_lng', 'last_location_update'])

    return Response({
        "success": True,
        "message": "Location updated successfully",
        "data": {
            "latitude": lat_float,
            "longitude": lng_float,
            "updated_at": rider.last_location_update
        }
    })

@api_view(['GET', 'PATCH'])
@permission_classes([IsAuthenticated])
def customer_profile(request):
    """Get or update the current user's profile"""
    profile = getattr(request.user, 'customerprofile', None)
    if not profile:
        return Response({"detail": "Profile not found"}, status=404)
    
    if request.method == 'GET':
        return Response({
            "full_name": profile.full_name,
            "phone": profile.phone,
            "email": request.user.email
        })
    
    # PATCH — update fields
    if request.method == 'PATCH':
        for field in ['full_name', 'phone']:
            if field in request.data:
                setattr(profile, field, request.data[field])
        profile.save()
        return Response({"detail": "Profile updated successfully"})


@api_view(['POST'])
@permission_classes([AllowAny])
def get_nearby_riders(request):
    """Find nearest eligible riders for a service"""
    from .models import Rider, Service
    
    latitude = request.data.get('latitude')
    longitude = request.data.get('longitude')
    service_type = request.data.get('service_type')

    if not all([latitude, longitude, service_type]):
        return Response(
            {"success": False, "error": "latitude, longitude, and service_type required"},
            status=status.HTTP_400_BAD_REQUEST
        )

    valid_services = ['okada', 'parcel', 'waste-bin']
    if service_type not in valid_services:
        return Response(
            {"success": False, "error": f"service_type must be one of: {', '.join(valid_services)}"},
            status=status.HTTP_400_BAD_REQUEST
        )

    try:
        lat_float = float(latitude)
        lng_float = float(longitude)
    except (TypeError, ValueError):
        return Response(
            {"success": False, "error": "Invalid coordinate format"},
            status=status.HTTP_400_BAD_REQUEST
        )

    if not (-90 <= lat_float <= 90):
        return Response({"success": False, "error": "Latitude must be between -90 and 90"}, status=400)
    if not (-180 <= lng_float <= 180):
        return Response({"success": False, "error": "Longitude must be between -180 and 180"}, status=400)

    riders = Rider.objects.filter(
        is_online=True,
        verification_status='approved',
        is_restricted=False,
        current_lat__isnull=False,
        current_lng__isnull=False,
        services__key=service_type,
        services__is_active=True
    ).distinct()

    results = []
    for rider in riders:
        distance_km = rider.get_distance_km(lat_float, lng_float)
        if distance_km is not None:
            results.append({
                "id": rider.id,
                "distance_km": distance_km,
                "latitude": rider.current_lat,
                "longitude": rider.current_lng,
                "last_updated": rider.last_location_update
            })

    results.sort(key=lambda x: x['distance_km'])
    results = results[:10]

    return Response({
        "success": True,
        "count": len(results),
        "service_type": service_type,
        "riders": results
    })


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def get_rider_wallet(request):
    """Get rider's wallet balance and recent transactions"""
    try:
        rider = request.user.rider_profile
        wallet = rider.wallet
        transactions = Transaction.objects.filter(rider=rider).order_by('-created_at')[:10]
        
        return Response({
            "success": True,
            "balance": wallet.balance,
            "minimum_required": "50.00",
            "can_go_online": rider.can_go_online,
            "transactions": [
                {
                    "id": t.id,
                    "amount": t.amount,
                    "type": t.transaction_type,
                    "description": t.description,
                    "timestamp": t.created_at
                }
                for t in transactions
            ]
        })
    except Rider.DoesNotExist:
        return Response(
            {"success": False, "error": "Rider profile not found"},
            status=status.HTTP_404_NOT_FOUND
        )


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def wallet_deposit(request):
    """Add funds to wallet — minimum deposit applies"""
    try:
        rider = request.user.rider_profile
        amount = request.data.get('amount')
        
        if not amount:
            return Response(
                {"success": False, "error": "Amount is required"},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        try:
            deposit_amount = Decimal(str(amount))
        except:
            return Response(
                {"success": False, "error": "Invalid amount"},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        if deposit_amount <= Decimal('0'):
            return Response(
                {"success": False, "error": "Amount must be greater than zero"},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        wallet = rider.wallet
        wallet.balance += deposit_amount
        wallet.save()
        
        Transaction.objects.create(
            rider=rider,
            amount=deposit_amount,
            transaction_type='deposit',
            description=f'Wallet deposit — ₵{deposit_amount}'
        )
        
        return Response({
            "success": True,
            "new_balance": wallet.balance,
            "can_go_online": rider.can_go_online,
            "message": "Deposit successful"
        })
        
    except Rider.DoesNotExist:
        return Response(
            {"success": False, "error": "Rider profile not found"},
            status=status.HTTP_404_NOT_FOUND
        )

@api_view(['GET'])
@permission_classes([IsAuthenticated])
def get_rider_wallet(request):
    try:
        rider = getattr(request.user, 'rider', None)
        if not rider:
            return Response({"success": False, "error": "Rider not found"}, status=404)
        
        wallet, _ = RiderWallet.objects.get_or_create(rider=rider)
        transactions = Transaction.objects.filter(rider=rider).order_by('-created_at')[:10]
        
        return Response({
            "success": True,
            "balance": wallet.balance,
            "minimum_required": "50.00",
            "can_go_online": rider.can_go_online,
            "transactions": [
                {
                    "id": t.id,
                    "amount": t.amount,
                    "type": t.transaction_type,
                    "description": t.description,
                    "timestamp": t.created_at
                }
                for t in transactions
            ]
        })
    except Exception as e:
        return Response({"success": False, "error": str(e)}, status=500)

@login_required
def rider_transactions(request):
    transactions = Transaction.objects.filter(
        rider=request.user.riderprofile
    ).order_by('-created_at')
    return render(request, 'rider_transactions.html', {
        'transactions': transactions
    })

@login_required
def request_withdrawal(request):
    if request.method == 'POST':
        amount = request.POST.get('amount')
        # Basic validation here
        return redirect('rider_wallet')
    return render(request, 'request_withdrawal.html')


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def initialize_payment(request):
    amount = request.data.get('amount')
    if not amount:
        return Response(
            {"detail": "Amount is required."},
            status=status.HTTP_400_BAD_REQUEST
        )
    return Response({
        "message": "Payment endpoint ready",
        "amount": amount
    }, status=status.HTTP_200_OK)


@api_view(['POST'])
@permission_classes([AllowAny])
def verify_payment(request):
    reference = request.data.get('reference')
    if not reference:
        return Response(
            {"detail": "Reference is required."},
            status=status.HTTP_400_BAD_REQUEST
        )
    return Response({
        "message": "Verification endpoint ready",
        "reference": reference
    })
