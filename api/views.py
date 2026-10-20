from rest_framework.decorators import api_view, permission_classes
from rest_framework.response import Response
from rest_framework import status, viewsets
from rest_framework.permissions import AllowAny, IsAuthenticated
from django.contrib.auth import authenticate
from django.contrib.auth.models import User
from django.db import transaction
from django.views.decorators.csrf import csrf_exempt
from django.utils import timezone
from django.conf import settings
from django.http import HttpResponse
from .models import CustomerProfile, RiderProfile, Rider, Booking, RiderWallet, Transaction, WithdrawalRequest
import uuid
import json
import os
import requests


# ==========================================
#  HELPER FUNCTIONS
# ==========================================

def get_or_create_customer_profile(user):
    profile, created = CustomerProfile.objects.get_or_create(
        user=user,
        defaults={
            'full_name': user.get_full_name() or user.username,
            'email': user.email,
            'phone': ''
        }
    )
    return profile


# ==========================================
#  AUTHENTICATION — Customer & Rider Registration
# ==========================================

@csrf_exempt
@api_view(['POST'])
@permission_classes([AllowAny])
def customer_register(request):
    try:
        username = request.data.get('username')
        email = request.data.get('email')
        password = request.data.get('password')
        full_name = request.data.get('full_name')
        phone = request.data.get('phone')

        if not all([username, email, password]):
            return Response(
                {'detail': 'Username, email and password are required.'},
                status=status.HTTP_400_BAD_REQUEST
            )

        if User.objects.filter(username=username).exists():
            return Response(
                {'username': 'Username already taken.'},
                status=status.HTTP_400_BAD_REQUEST
            )

        if User.objects.filter(email=email).exists():
            return Response(
                {'email': 'Email already registered.'},
                status=status.HTTP_400_BAD_REQUEST
            )

        first_name = full_name.split()[0] if full_name else ''
        last_name = ' '.join(full_name.split()[1:]) if len(full_name.split()) > 1 else ''

        user = User.objects.create_user(
            username=username,
            email=email,
            password=password,
            first_name=first_name,
            last_name=last_name
        )

        CustomerProfile.objects.create(
            user=user,
            full_name=full_name,
            email=email,
            phone=phone
        )

        return Response({
            'message': 'Customer account created successfully!',
            'user': {
                'id': user.id,
                'username': user.username,
                'email': user.email
            }
        }, status=status.HTTP_201_CREATED)

    except Exception as e:
        return Response(
            {'detail': str(e)},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR
        )


@csrf_exempt
@api_view(['POST'])
@permission_classes([AllowAny])
def user_login(request):
    try:
        username = request.data.get('username')
        password = request.data.get('password')

        user = authenticate(username=username, password=password)

        if not user:
            return Response(
                {'detail': '❌ Username or password incorrect.'},
                status=status.HTTP_401_UNAUTHORIZED
            )

        token = str(uuid.uuid4())

        return Response({
            'token': token,
            'user': {
                'id': user.id,
                'username': user.username,
                'email': user.email,
                'full_name': user.get_full_name()
            }
        })

    except Exception as e:
        return Response(
            {'detail': str(e)},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR
        )


@csrf_exempt
@api_view(['POST'])
@permission_classes([AllowAny])
def rider_register(request):
    try:
        username = request.data.get('username')
        email = request.data.get('email')
        password = request.data.get('password')
        full_name = request.data.get('full_name')
        phone = request.data.get('phone')
        motorbike_model = request.data.get('motorbike_model')
        license_number = request.data.get('license_number')

        if not all([username, email, password, full_name, phone]):
            return Response(
                {'detail': 'Please fill in all required fields.'},
                status=status.HTTP_400_BAD_REQUEST
            )

        if User.objects.filter(username=username).exists():
            return Response(
                {'username': 'Username already taken.'},
                status=status.HTTP_400_BAD_REQUEST
            )

        if User.objects.filter(email=email).exists():
            return Response(
                {'email': 'Email already registered.'},
                status=status.HTTP_400_BAD_REQUEST
            )

        first_name = full_name.split()[0] if full_name else ''
        last_name = ' '.join(full_name.split()[1:]) if len(full_name.split()) > 1 else ''

        user = User.objects.create_user(
            username=username,
            email=email,
            password=password,
            first_name=first_name,
            last_name=last_name
        )

        rider_profile = RiderProfile.objects.create(
            user=user,
            full_name=full_name,
            email=email,
            phone=phone,
            motorbike_model=motorbike_model or '',
            license_number=license_number or '',
            status='pending'
        )

        RiderWallet.objects.create(rider=rider_profile)

        return Response({
            'message': 'Rider account created! Please upload your documents.',
            'user': {
                'id': user.id,
                'username': user.username,
                'email': user.email
            }
        }, status=status.HTTP_201_CREATED)

    except Exception as e:
        return Response(
            {'detail': str(e)},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR
        )


@csrf_exempt
@api_view(['POST'])
@permission_classes([AllowAny])
def rider_submit_documents(request):
    try:
        username = request.data.get('username')
        documents = request.data.get('documents', {})

        if not username:
            return Response(
                {'detail': 'Username is required.'},
                status=status.HTTP_400_BAD_REQUEST
            )

        try:
            rider_profile = RiderProfile.objects.get(user__username=username)
        except RiderProfile.DoesNotExist:
            return Response(
                {'detail': 'Rider profile not found. Please register first.'},
                status=status.HTTP_404_NOT_FOUND
            )

        rider_profile.ghana_card_doc = documents.get('ghanaCard', '')
        rider_profile.passport_photo = documents.get('passportPhoto', '')
        rider_profile.license_doc = documents.get('licenseDoc', '')
        rider_profile.status = 'pending_review'
        rider_profile.save()

        return Response({
            'message': '✅ Documents submitted successfully!',
            'status': 'pending_review'
        })

    except Exception as e:
        return Response(
            {'detail': str(e)},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR
        )


# ==========================================
#  VIEWSETS — Riders, Bookings
# ==========================================

class RiderViewSet(viewsets.ReadOnlyModelViewSet):
    """List and view all registered riders"""
    queryset = Rider.objects.all()


class BookingViewSet(viewsets.ModelViewSet):
    """Full CRUD for bookings"""
    queryset = Booking.objects.all()


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def customer_profile(request):
    """Return the logged-in customer's profile info"""
    profile, created = CustomerProfile.objects.get_or_create(
        user=request.user,
        defaults={'full_name': request.user.get_full_name()}
    )
    return Response({
        'full_name': profile.full_name or request.user.get_full_name(),
        'email': profile.email or request.user.email,
        'phone': profile.phone,
    })


# ==========================================
#  LIVE RIDER LOCATION
# ==========================================

@csrf_exempt
@api_view(['POST'])
@permission_classes([AllowAny])
def update_rider_location(request):
    """Rider app sends this to update their current location"""
    try:
        rider_id = request.data.get('rider_id')
        lat = request.data.get('latitude')
        lng = request.data.get('longitude')

        if not all([rider_id, lat, lng]):
            return Response({'detail': 'Missing required fields'}, status=status.HTTP_400_BAD_REQUEST)

        try:
            rider = RiderProfile.objects.get(id=rider_id)
            rider.current_lat = lat
            rider.current_lng = lng
            rider.last_location_update = timezone.now()
            rider.save()
        except RiderProfile.DoesNotExist:
            rider = Rider.objects.get(id=rider_id)
            rider.current_lat = lat
            rider.current_lng = lng
            rider.last_location_update = timezone.now()
            rider.save()

        return Response({
            'message': 'Location updated',
            'rider_id': rider_id,
            'latitude': lat,
            'longitude': lng,
            'updated_at': timezone.now()
        })
    except (RiderProfile.DoesNotExist, Rider.DoesNotExist):
        return Response({'detail': 'Rider not found'}, status=status.HTTP_404_NOT_FOUND)
    except Exception as e:
        return Response({'detail': str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


@api_view(['GET'])
@permission_classes([AllowAny])
def get_nearby_riders(request):
    """Customer dashboard & Admin panel get all online riders with live locations"""
    try:
        profile_riders = list(RiderProfile.objects.filter(
            is_online=True,
            status='approved'
        ).values(
            'id', 'full_name', 'phone', 'current_lat', 'current_lng', 'is_online'
        ))

        profile_riders = [
            {**r, 'name': r.pop('full_name')} for r in profile_riders
        ]

        legacy_riders = list(Rider.objects.filter(
            is_online=True,
            verification_status='approved'
        ).values(
            'id', 'name', 'phone', 'current_lat', 'current_lng', 'is_online'
        ))

        return Response({
            'riders': profile_riders + legacy_riders
        })
    except Exception as e:
        return Response({'detail': str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


# ==========================================
#  WALLET, COMMISSION & WITHDRAWAL SYSTEM
# ==========================================

COMMISSION_RATE = 0.15  # 15% Platform Commission


@csrf_exempt
@api_view(['GET'])
@permission_classes([AllowAny])
def get_rider_wallet(request):
    """Rider checks their balance from dashboard"""
    rider_id = request.GET.get('rider_id')

    if not rider_id:
        return Response({'detail': 'rider_id is required'}, status=status.HTTP_400_BAD_REQUEST)

    try:
        try:
            wallet = RiderWallet.objects.get(rider__id=rider_id)
        except RiderWallet.DoesNotExist:
            return Response({
                'balance': 0.00,
                'total_earned': 0.00,
                'total_withdrawn': 0.00,
                'commission_rate': f"{int(COMMISSION_RATE * 100)}%"
            })

        return Response({
            'balance': float(wallet.balance),
            'total_earned': float(wallet.total_earned),
            'total_withdrawn': float(wallet.total_withdrawn),
            'commission_rate': f"{int(COMMISSION_RATE * 100)}%"
        })
    except Exception as e:
        return Response({'detail': str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


@csrf_exempt
@api_view(['GET'])
@permission_classes([AllowAny])
def rider_transactions(request):
    """Rider sees payment history & withdrawals"""
    rider_id = request.GET.get('rider_id')

    if not rider_id:
        return Response({'detail': 'rider_id is required'}, status=status.HTTP_400_BAD_REQUEST)

    try:
        transactions = Transaction.objects.filter(rider__id=rider_id).order_by('-created_at')[:50]
        data = []
        for t in transactions:
            data.append({
                'id': str(t.id),
                'type': t.transaction_type,
                'amount': float(t.amount),
                'commission': float(t.commission),
                'rider_amount': float(t.rider_amount),
                'status': t.status,
                'description': t.description,
                'created_at': t.created_at
            })
        return Response({'transactions': data})
    except Exception as e:
        return Response({'detail': str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


@csrf_exempt
@api_view(['POST'])
@permission_classes([AllowAny])
def record_job_payment(request):
    """Customer pays → AUTO SPLIT: 15% Platform, 85% Rider"""
    try:
        booking_id = request.data.get('booking_id')
        rider_id = request.data.get('rider_id')
        total_amount = float(request.data.get('amount', 0))
        payment_method = request.data.get('payment_method', 'mobile_money')
        reference = request.data.get('reference', '')

        if not all([booking_id, rider_id, total_amount]):
            return Response({'detail': 'Missing required fields'}, status=status.HTTP_400_BAD_REQUEST)

        commission = round(total_amount * COMMISSION_RATE, 2)
        rider_amount = round(total_amount - commission, 2)

        booking = Booking.objects.get(id=booking_id)
        booking.total_amount = total_amount
        booking.platform_commission = commission
        booking.rider_amount = rider_amount
        booking.payment_method = payment_method
        booking.transaction_reference = reference
        booking.payment_status = 'released'
        booking.paid_at = timezone.now()
        booking.save()

        rider = RiderProfile.objects.get(id=rider_id)
        wallet, _ = RiderWallet.objects.get_or_create(rider=rider)
        wallet.deposit(rider_amount)

        Transaction.objects.create(
            rider=rider,
            booking=booking,
            transaction_type='job_payment',
            amount=total_amount,
            commission=commission,
            rider_amount=rider_amount,
            status='completed',
            description=f"Payment — {booking.pickup} → {booking.destination}"
        )

        return Response({
            'message': '✅ Payment recorded & split',
            'total_amount': total_amount,
            'platform_commission': commission,
            'rider_amount': rider_amount,
            'wallet_balance': float(wallet.balance)
        }, status=status.HTTP_201_CREATED)

    except Booking.DoesNotExist:
        return Response({'detail': 'Booking not found'}, status=status.HTTP_404_NOT_FOUND)
    except RiderProfile.DoesNotExist:
        return Response({'detail': 'Rider not found'}, status=status.HTTP_404_NOT_FOUND)
    except Exception as e:
        return Response({'detail': str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


@csrf_exempt
@api_view(['POST'])
@permission_classes([AllowAny])
def request_withdrawal(request):
    """Rider taps 'Withdraw' → money moves from wallet to their mobile money"""
    try:
        rider_id = request.data.get('rider_id')
        amount = float(request.data.get('amount', 0))
        mobile_money_number = request.data.get('mobile_money_number', '')
        network = request.data.get('network', '')

        if not all([rider_id, amount, mobile_money_number]):
            return Response({'detail': 'Amount and mobile money number required'}, status=status.HTTP_400_BAD_REQUEST)

        if amount <= 0:
            return Response({'detail': 'Invalid amount'}, status=status.HTTP_400_BAD_REQUEST)

        rider = RiderProfile.objects.get(id=rider_id)
        wallet, _ = RiderWallet.objects.get_or_create(rider=rider)

        if float(wallet.balance) < amount:
            return Response({
                'detail': 'Insufficient balance',
                'balance': float(wallet.balance)
            }, status=status.HTTP_400_BAD_REQUEST)

        success = wallet.withdraw(amount)
        if not success:
            return Response({'detail': 'Withdrawal failed'}, status=status.HTTP_400_BAD_REQUEST)

        withdrawal = WithdrawalRequest.objects.create(
            rider=rider,
            amount=amount,
            mobile_money_number=mobile_money_number,
            network=network,
            status='pending'
        )

        Transaction.objects.create(
            rider=rider,
            transaction_type='withdrawal',
            amount=amount,
            commission=0.00,
            rider_amount=amount,
            status='pending',
            description=f"Withdrawal to {mobile_money_number}"
        )

        return Response({
            'message': '✅ Withdrawal request submitted!',
            'withdrawal_id': str(withdrawal.id),
            'amount': amount,
            'new_balance': float(wallet.balance),
            'status': 'pending'
        }, status=status.HTTP_201_CREATED)

    except RiderProfile.DoesNotExist:
        return Response({'detail': 'Rider not found'}, status=status.HTTP_404_NOT_FOUND)
    except Exception as e:
        return Response({'detail': str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


# ==========================================
#  💳 PAYSTACK PAYMENT INTEGRATION — NEW!
# ==========================================

PAYSTACK_SECRET_KEY = os.environ.get('PAYSTACK_SECRET_KEY', '')


@csrf_exempt
@api_view(['POST'])
@permission_classes([IsAuthenticated])
def initialize_payment(request):
    """Initialize Paystack payment for a booking"""
    try:
        booking_id = request.data.get('booking_id')
        email = request.data.get('email')
        amount = request.data.get('amount')  # in GH₵ — convert to kobo: × 100

        if not all([booking_id, email, amount]):
            return Response(
                {'detail': 'booking_id, email and amount are required.'},
                status=status.HTTP_400_BAD_REQUEST
            )

        amount_kobo = int(float(amount) * 100)

        # Verify booking exists
        try:
            booking = Booking.objects.get(id=booking_id)
        except Booking.DoesNotExist:
            return Response({'detail': 'Booking not found.'}, status=status.HTTP_404_NOT_FOUND)

        # Initialize with Paystack
        headers = {
            'Authorization': f'Bearer {PAYSTACK_SECRET_KEY}',
            'Content-Type': 'application/json'
        }
        payload = {
            'email': email,
            'amount': amount_kobo,
            'reference': f"RYDNOW-{uuid.uuid4().hex[:12].upper()}",
            'metadata': {
                'booking_id': str(booking_id),
                'customer_id': str(request.user.id)
            }
        }

        resp = requests.post(
            'https://api.paystack.co/transaction/initialize',
            headers=headers,
            data=json.dumps(payload),
            timeout=30
        )
        data = resp.json()

        if data.get('status'):
            booking.transaction_reference = data['data']['reference']
            booking.save()
            return Response({
                'status': True,
                'authorization_url': data['data']['authorization_url'],
                'reference': data['data']['reference']
            })
        else:
            return Response(
                {'detail': data.get('message', 'Payment initialization failed.')},
                status=status.HTTP_400_BAD_REQUEST
            )

    except requests.exceptions.RequestException:
        return Response(
            {'detail': 'Could not connect to payment gateway. Try again.'},
            status=status.HTTP_503_SERVICE_UNAVAILABLE
        )
    except Exception as e:
        return Response({'detail': str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


@csrf_exempt
@api_view(['GET'])
@permission_classes([AllowAny])
def verify_payment(request):
    """Verify payment status from Paystack"""
    reference = request.GET.get('reference')

    if not reference:
        return Response({'detail': 'Reference required.'}, status=status.HTTP_400_BAD_REQUEST)

    try:
        headers = {
            'Authorization': f'Bearer {PAYSTACK_SECRET_KEY}',
        }
        resp = requests.get(
            f'https://api.paystack.co/transaction/verify/{reference}',
            headers=headers,
            timeout=30
        )
        data = resp.json()

        if data.get('status') and data['data']['status'] == 'success':
            # Update booking payment status
            Booking.objects.filter(transaction_reference=reference).update(
                payment_status='released',
                paid_at=timezone.now()
            )
            return Response({
                'status': 'success',
                'message': 'Payment verified successfully',
                'amount': data['data']['amount'] / 100
            })
        else:
            Booking.objects.filter(transaction_reference=reference).update(
                payment_status='cancelled'
            )
            return Response({
                'status': 'failed',
                'message': 'Payment not successful'
            })

    except Exception as e:
        return Response({'detail': str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


@csrf_exempt
def paystack_webhook(request):
    """Receive Paystack payment events — called by Paystack servers"""
    signature = request.headers.get('x-paystack-signature', '')
    if not PAYSTACK_SECRET_KEY or not signature:
        return HttpResponse(status=200)

    try:
        payload = json.loads(request.body.decode('utf-8'))
        event = payload.get('event')
        data = payload.get('data', {})

        if event == 'charge.success':
            reference = data.get('reference')
            amount = data.get('amount', 0) / 100
            # Update booking if exists
            Booking.objects.filter(transaction_reference=reference).update(
                payment_status='released',
                paid_at=timezone.now()
            )

        return HttpResponse(status=200)
    except Exception:
        return HttpResponse(status=200)
