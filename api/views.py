api_key=
settings.GOOGLE_MAPS_API_KEYS
from rest_framework.decorators import api_view, permission_classes
from rest_framework.response import Response
from rest_framework import status, viewsets
from rest_framework.permissions import AllowAny, IsAuthenticated
from django.contrib.auth import authenticate
from django.contrib.auth.models import User
from django.views.decorators.csrf import csrf_exempt
from django.utils import timezone
from django.conf import settings
from .models import CustomerProfile, RiderProfile, Rider, Booking, RiderWallet, Transaction, WithdrawalRequest
from .serializers import BookingSerializer, CustomerProfileSerializer, RiderSerializer
import uuid
import json
import os
from urllib import request, parse


# ─── AI & MAPS IMPORTS ───
try:
    import google.generativeai as genai
    import googlemaps
    GOOGLE_SDK_AVAILABLE = True
except ImportError:
    GOOGLE_SDK_AVAILABLE = False
    print("⚠️ google-generativeai or googlemaps not installed — using fallback pricing only")




# ═══════════════════════════════════════════════════════
# 📍 HELPER: GET DISTANCE FROM GOOGLE MAPS
# ═══════════════════════════════════════════════════════


def get_distance_and_duration(pickup_address, dest_address):
    """Call Google Maps API using Django's built-in urllib"""
    api_key = settings.GOOGLE_MAPS_API_KEY
    
    if not api_key:
        print("⚠️ GOOGLE_MAPS_API_KEY not set — using fallback estimate")
        return None, None, 1.0
    
    base_url = "https://maps.googleapis.com/maps/api/distancematrix/json"
    params = {
        "origins": pickup_address,
        "destinations": dest_address,
        "key": api_key,
        "units": "metric",
        "departure_time": "now"
    }
    full_url = f"{base_url}?{parse.urlencode(params)}"
    
    try:
        with request.urlopen(full_url, timeout=10) as resp:
            data = json.loads(resp.read().decode())
        
        if data["status"] == "OK" and data["rows"][0]["elements"][0]["status"] == "OK":
            element = data["rows"][0]["elements"][0]
            distance_km = element["distance"]["value"] / 1000
            duration_mins = round(element["duration_in_traffic"]["value"] / 60)
            duration_normal = element["duration"]["value"] / 60
            traffic_factor = duration_mins / duration_normal if duration_normal > 0 else 1.0
            return round(distance_km, 2), duration_mins, round(traffic_factor, 2)
    except Exception as e:
        print(f"⚠️ Google Maps error: {e}")
    
    return None, None, 1.0




# ═══════════════════════════════════════════════════════
# 💰 HELPER: BACKEND-ONLY PRICING ENGINE
# ═══════════════════════════════════════════════════════


def calculate_fare(service_type, distance_km):
    """Secure pricing — frontend can't change these prices"""
    PRICING = {
        "okada": {"base": 5.0, "per_km": 1.5},
        "parcel": {"base": 8.0, "per_km": 2.0},
        "delivery": {"base": 8.0, "per_km": 2.0},
        "waste-bin": {"base": 15.0, "per_km": 0.0},
        "waste": {"base": 15.0, "per_km": 0.0},
    }
    
    rules = PRICING.get(service_type, PRICING["okada"])
    base = rules["base"]
    per_km = rules["per_km"]
    
    if distance_km:
        total = base + (distance_km * per_km)
    else:
        total = base
    
    return round(total, 2)




# ═══════════════════════════════════════════════════════
# 📋 CUSTOMER PROFILE VIEWSET
# ═══════════════════════════════════════════════════════


class CustomerProfileViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = CustomerProfile.objects.all()
    serializer_class = CustomerProfileSerializer
    permission_classes = [AllowAny]




# ═══════════════════════════════════════════════════════
# 🆕 AUTHENTICATION ENDPOINTS
# ═══════════════════════════════════════════════════════


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
        last_name = ' '.join(full_name.split()[1:]) if full_name and len(full_name.split()) > 1 else ''
        
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
        last_name = ' '.join(full_name.split()[1:]) if full_name and len(full_name.split()) > 1 else ''
        
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


        rider = Rider.objects.create(
            user=user,
            name=full_name,
            email=email,
            phone=phone,
            verification_status='pending'
        )


        RiderWallet.objects.create(rider=rider)


        return Response({
            'message': 'Rider account created! Please upload your documents.',
            'user': {
                'id': user.id,
                'username': user.username,
                'email': email
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
            rider = Rider.objects.get(user__username=username)
        except RiderProfile.DoesNotExist:
            return Response(
                {'detail': 'Rider profile not found. Please register first.'},
                status=status.HTTP_404_NOT_FOUND
            )
        except Rider.DoesNotExist:
            return Response(
                {'detail': 'Rider record not found. Please register first.'},
                status=status.HTTP_404_NOT_FOUND
            )


        rider_profile.ghana_card_doc = documents.get('ghanaCard', '')
        rider_profile.passport_photo = documents.get('passportPhoto', '')
        rider_profile.license_doc = documents.get('licenseDoc', '')
        rider_profile.status = 'pending_review'
        rider_profile.save()


        rider.verification_status = 'submitted'
        rider.save()


        return Response({
            'message': '✅ Documents submitted successfully!',
            'status': 'pending_review'
        })


    except Exception as e:
        return Response(
            {'detail': str(e)},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR
        )




# ═══════════════════════════════════════════════════════
# 🆕 CREATE BOOKING
# ═══════════════════════════════════════════════════════


@csrf_exempt
@api_view(['POST'])
@permission_classes([AllowAny])
def create_booking(request):
    try:
        pickup = request.data.get('pickup')
        destination = request.data.get('destination')
        service_type = request.data.get('service_type', 'okada')
        delivery_category = request.data.get('delivery_category', '')
        parcel_details = request.data.get('parcel_details', '')
        customer_name = request.data.get('customer_name', 'Customer')
        customer_phone = request.data.get('customer_phone', '')
        payment_method = request.data.get('payment_method', 'cash')


        if not pickup or not destination:
            return Response(
                {'error': 'Pickup and destination are required'},
                status=status.HTTP_400_BAD_REQUEST
            )


        distance_km, duration_mins, _ = get_distance_and_duration(pickup, destination)
        final_price = calculate_fare(service_type, distance_km)


        customer = None
        if customer_phone:
            customer, _ = CustomerProfile.objects.get_or_create(
                phone=customer_phone,
                defaults={
                    'full_name': customer_name,
                    'email': ''
                }
            )


        booking_data = {
            'service_type': service_type,
            'pickup': pickup,
            'destination': destination,
            'delivery_category': delivery_category,
            'parcel_details': parcel_details,
            'distance_km': distance_km,
            'duration_mins': duration_mins,
            'price': final_price,
            'customer': customer.id if customer else None,
            'customer_name': customer_name,
            'customer_phone': customer_phone,
            'payment_method': payment_method,
            'status': 'searching_rider'
        }


        serializer = BookingSerializer(data=booking_data)
        if serializer.is_valid():
            booking = serializer.save()
            return Response({
                'success': True,
                'id': booking.id,
                'service_type': service_type,
                'pickup': pickup,
                'destination': destination,
                'distance_km': distance_km,
                'duration_mins': duration_mins,
                'price': final_price,
                'status': booking.status,
                'message': '✅ Booking created successfully!'
            }, status=status.HTTP_201_CREATED)
        else:
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


    except Exception as e:
        return Response({'detail': str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)




@csrf_exempt
@api_view(['GET'])
@permission_classes([AllowAny])
def list_bookings(request):
    phone = request.GET.get('phone')
    if phone:
        bookings = Booking.objects.filter(customer_phone=phone).order_by('-created_at')
    else:
        bookings = Booking.objects.all().order_by('-created_at')
    
    serializer = BookingSerializer(bookings, many=True)
    return Response({'bookings': serializer.data})




# ═══════════════════════════════════════════════════════
# 💰 ESTIMATE FARE
# ═══════════════════════════════════════════════════════


@csrf_exempt
@api_view(['POST'])
@permission_classes([AllowAny])
def estimate_fare(request):
    """Secure fare estimation — calculated on backend only"""
    try:
        pickup = request.data.get('pickup')
        destination = request.data.get('destination')
        service_type = request.data.get('service_type', 'okada')
        
        if not pickup or not destination:
            return Response({'error': 'Pickup and destination required'}, status=400)
        
        PRICING = {
            'okada': {'basePrice': 5.0, 'pricePerKm': 1.5, 'name': 'Okada Ride'},
            'delivery': {'basePrice': 8.0, 'pricePerKm': 2.0, 'name': 'Delivery'},
            'waste': {'basePrice': 15.0, 'pricePerKm': 0.0, 'name': 'Waste Collection'},
        }
        
        pricing = PRICING.get(service_type, PRICING['okada'])
        distance_km, duration_mins, _ = get_distance_and_duration(pickup, destination)
        
        if not distance_km:
            distance_km = 5.0
        
        fare = pricing['basePrice'] + (distance_km * pricing['pricePerKm'])
        
        return Response({
            'service_name': pricing['name'],
            'distance_km': round(distance_km, 2),
            'estimated_fare': round(fare, 2),
            'currency': 'GH₵'
        })
    except Exception as e:
        return Response({'error': str(e)}, status=500)




# ═══════════════════════════════════════════════════════
# 📋 VIEWSETS
# ═══════════════════════════════════════════════════════


class RiderViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = Rider.objects.all()
    serializer_class = RiderSerializer
    permission_classes = [AllowAny]




class BookingViewSet(viewsets.ModelViewSet):
    queryset = Booking.objects.all()
    serializer_class = BookingSerializer
    permission_classes = [AllowAny]




@api_view(['GET'])
@permission_classes([IsAuthenticated])
def customer_profile(request):
    profile, created = CustomerProfile.objects.get_or_create(
        user=request.user,
        defaults={'full_name': request.user.get_full_name()}
    )
    return Response({
        'full_name': profile.full_name or request.user.get_full_name(),
        'email': profile.email or request.user.email,
        'phone': profile.phone,
    })




# ═══════════════════════════════════════════════════════
# 📍 LIVE RIDER LOCATION
# ═══════════════════════════════════════════════════════


@csrf_exempt
@api_view(['POST'])
@permission_classes([AllowAny])
def update_rider_location(request):
    try:
        rider_id = request.data.get('rider_id')
        lat = request.data.get('latitude')
        lng = request.data.get('longitude')


        if not all([rider_id, lat, lng]):
            return Response({'detail': 'Missing required fields'}, status=status.HTTP_400_BAD_REQUEST)


        try:
            rider = Rider.objects.get(id=rider_id)
            rider.current_lat = lat
            rider.current_lng = lng
            rider.last_location_update = timezone.now()
            rider.save()
        except Rider.DoesNotExist:
            try:
                rider_profile = RiderProfile.objects.get(id=rider_id)
                rider_profile.current_lat = lat
                rider_profile.current_lng = lng
                rider_profile.last_location_update = timezone.now()
                rider_profile.save()
            except RiderProfile.DoesNotExist:
                return Response({'detail': 'Rider not found'}, status=status.HTTP_404_NOT_FOUND)


        return Response({
            'message': 'Location updated',
            'rider_id': rider_id,
            'latitude': lat,
            'longitude': lng,
            'updated_at': timezone.now()
        })
    except Exception as e:
        return Response({'detail': str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)




@api_view(['GET'])
@permission_classes([AllowAny])
def get_nearby_riders(request):
    try:
        riders = list(Rider.objects.filter(
            is_online=True,
            verification_status='approved'
        ).values(
            'id', 'name', 'phone', 'current_lat', 'current_lng', 'is_online'
        ))


        return Response({'riders': riders})
    except Exception as e:
        return Response({'detail': str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)




# ═══════════════════════════════════════════════════════
# 💰 WALLET & PAYMENT SYSTEM
# ═══════════════════════════════════════════════════════


COMMISSION_RATE = 0.15


@csrf_exempt
@api_view(['GET'])
@permission_classes([AllowAny])
def get_rider_wallet(request):
    rider_id = request.GET.get('rider_id')
    
    if not rider_id:
        return Response({'detail': 'rider_id is required'}, status=status.HTTP_400_BAD_REQUEST)
    
    try:
        wallet = RiderWallet.objects.get(rider__id=rider_id)
        return Response({
            'balance': float(wallet.balance),
            'total_earned': float(wallet.total_earned),
            'total_withdrawn': float(wallet.total_withdrawn),
            'commission_rate': f"{int(COMMISSION_RATE * 100)}%"
        })
    except RiderWallet.DoesNotExist:
        return Response({
            'balance': 0.00,
            'total_earned': 0.00,
            'total_withdrawn': 0.00,
            'commission_rate': f"{int(COMMISSION_RATE * 100)}%"
        })
    except Exception as e:
        return Response({'detail': str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)




@csrf_exempt
@api_view(['GET'])
@permission_classes([AllowAny])
def rider_transactions(request):
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




def get_rider_object(rider_id):
    try:
        return Rider.objects.get(id=rider_id)
    except Rider.DoesNotExist:
        try:
            profile = RiderProfile.objects.get(id=rider_id)
            return Rider.objects.get(user=profile.user)
        except (RiderProfile.DoesNotExist, Rider.DoesNotExist):
            return None




@csrf_exempt
@api_view(['POST'])
@permission_classes([AllowAny])
def record_job_payment(request):
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


        rider = get_rider_object(rider_id)
        if not rider:
            return Response({'detail': 'Rider not found'}, status=status.HTTP_404_NOT_FOUND)


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
    except Exception as e:
        return Response({'detail': str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)




@csrf_exempt
@api_view(['POST'])
@permission_classes([AllowAny])
def request_withdrawal(request):
    try:
        rider_id = request.data.get('rider_id')
        amount = float(request.data.get('amount', 0))
        mobile_money_number = request.data.get('mobile_money_number', '')
        network = request.data.get('network', '')


        if not all([rider_id, amount, mobile_money_number]):
            return Response({
                'detail': 'Amount and mobile money number required'
            }, status=status.HTTP_400_BAD_REQUEST)


        if amount <= 0:
            return Response({
                'detail': 'Invalid amount'
            }, status=status.HTTP_400_BAD_REQUEST)


        rider = get_rider_object(rider_id)
        if not rider:
            return Response({'detail': 'Rider not found'}, status=status.HTTP_404_NOT_FOUND)


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


    except Exception as e:
        return Response({'detail': str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)




# ═══════════════════════════════════════════════════════
# ✅ BOOKING FLOW — ACCEPT / START / COMPLETE
# ═══════════════════════════════════════════════════════


@csrf_exempt
@api_view(['POST'])
@permission_classes([AllowAny])
def accept_booking(request):
    try:
        booking_id = request.data.get('booking_id')
        rider_id = request.data.get('rider_id')


        if not booking_id or not rider_id:
            return Response(
                {'detail': 'Booking ID and Rider ID are required.'},
                status=status.HTTP_400_BAD_REQUEST
            )


        booking = Booking.objects.get(id=booking_id)


        if booking.status != 'searching_rider':
            return Response(
                {'detail': 'Booking already taken or no longer available.'},
                status=status.HTTP_409_CONFLICT
            )


        rider = get_rider_object(rider_id)
        if not rider:
            return Response({'detail': 'Rider not found.'}, status=status.HTTP_404_NOT_FOUND)


        booking.rider = rider
        booking.status = 'assigned'
        booking.assigned_at = timezone.now()
        booking.save()


        return Response({
            'message': '✅ Booking accepted!',
            'booking_id': str(booking.id),
            'status': 'assigned',
            'rider_name': rider.name
        }, status=status.HTTP_200_OK)


    except Booking.DoesNotExist:
        return Response({'detail': 'Booking not found.'}, status=status.HTTP_404_NOT_FOUND)
    except Exception as e:
        return Response({'detail': str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)




@csrf_exempt
@api_view(['POST'])
@permission_classes([AllowAny])
def start_ride(request):
    try:
        booking_id = request.data.get('booking_id')


        booking = Booking.objects.get(id=booking_id)


        if booking.status != 'assigned':
            return Response(
                {'detail': 'Booking must be accepted first.'},
                status=status.HTTP_409_CONFLICT
            )


        booking.status = 'in_progress'
        booking.started_at = timezone.now()
        booking.save()


        return Response({
            'message': '🛵 Ride started',
            'booking_id': str(booking.id),
            'status': 'in_progress',
            'started_at': booking.started_at
        }, status=status.HTTP_200_OK)


    except Booking.DoesNotExist:
        return Response({'detail': 'Booking not found.'}, status=status.HTTP_404_NOT_FOUND)
    except Exception as e:
        return Response({'detail': str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)




@csrf_exempt
@api_view(['POST'])
@permission_classes([AllowAny])
def complete_ride(request):
    try:
        booking_id = request.data.get('booking_id')


        booking = Booking.objects.get(id=booking_id)


        if booking.status != 'in_progress':
            return Response(
                {'detail': 'Ride must be in progress first.'},
                status=status.HTTP_409_CONFLICT
            )


        booking.status = 'completed'
        booking.completed_at = timezone.now()
        booking.save()


        return Response({
            'message': '✅ Ride completed!',
            'booking_id': str(booking.id),
            'status': 'completed',
            'completed_at': booking.completed_at
        }, status=status.HTTP_200_OK)


    except Booking.DoesNotExist:
        return Response({'detail': 'Booking not found.'}, status=status.HTTP_404_NOT_FOUND)
    except Exception as e:
        return Response({'detail': str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)




# ═══════════════════════════════════════════════════════
# 🧠 AI-POWERED FARE CALCULATOR — NEW ENDPOINT
# ═══════════════════════════════════════════════════════


if GOOGLE_SDK_AVAILABLE and hasattr(settings, 'GEMINI_API_KEY') and settings.GEMINI_API_KEY:
    gmaps = googlemaps.Client(key=settings.GOOGLE_MAPS_API_KEY)
    genai.configure(api_key=settings.GEMINI_API_KEY)


@csrf_exempt
@api_view(['GET'])
@permission_classes([AllowAny])
def calculate_fare_with_ai(request):
    """Calculate smart fare using Google Maps + Gemini AI"""
    pickup = request.GET.get('pickup')
    destination = request.GET.get('destination')
    service_type = request.GET.get('service_type', 'okada')
    
    if not pickup or not destination:
        return Response({"error": "Pickup and destination required"}, status=400)
    
    try:
        distance_km, duration_mins, traffic_factor = get_distance_and_duration(pickup, destination)
        
        if not distance_km:
            raise Exception("Could not get distance data")
        
        traffic_level = "High" if traffic_factor > 1.3 else "Normal" if traffic_factor > 1.1 else "Low"
        
        ai_fare = None
        explanation = "Standard rate"
        
        if GOOGLE_SDK_AVAILABLE and hasattr(settings, 'GEMINI_API_KEY') and settings.GEMINI_API_KEY:
            try:
                prompt = f"""
                Calculate a fair price in Ghana Cedis (GH₵).
                Service: {service_type}
                Distance: {distance_km:.2f} km
                Travel time with traffic: {duration_mins:.0f} mins
                Traffic level: {traffic_level} ({round((traffic_factor - 1) * 100)}% busier than normal)
                
                Pricing rules:
                - Okada Ride: GH₵5 base + GH₵1.50 per km
                - Delivery: GH₵8 base + GH₵2.00 per km
                - Waste Collection: GH₵15 flat rate
                
                Return ONLY valid JSON like this:
                {{
                  "base_price": 5.00,
                  "distance_cost": 7.50,
                  "traffic_adjustment": 0.00,
                  "total_fare": 12.50,
                  "explanation": "Clear short explanation"
                }}
                """
                model = genai.GenerativeModel("gemini-2.0-flash")
                response = model.generate_content(prompt)
                result_text = response.text.replace("```json", "").replace("```", "").strip()
                fare_data = json.loads(result_text)
                ai_fare = fare_data
                explanation = fare_data.get("explanation", "AI-calculated fare")
            except Exception as ai_err:
                print(f"⚠️ AI unavailable, using standard pricing: {ai_err}")
        
        if not ai_fare:
            rates = {
                'okada': {'base': 5.00, 'per_km': 1.50},
                'delivery': {'base': 8.00, 'per_km': 2.00},
                'waste': {'base': 15.00, 'per_km': 0},
                'parcel': {'base': 8.00, 'per_km': 2.00},
                'waste-bin': {'base': 15.00, 'per_km': 0},
            }
            rate = rates.get(service_type, rates['okada'])
            base_price = rate['base']
            distance_cost = round(distance_km * rate['per_km'], 2)
            traffic_adjustment = round((traffic_factor - 1.0) * 2, 2) if traffic_factor > 1.1 else 0.00
            total_fare = round(base_price + distance_cost + traffic_adjustment, 2)
            
            ai_fare = {
                "base_price": base_price,
                "distance_cost": distance_cost,
                "traffic_adjustment": traffic_adjustment,
                "total_fare": total_fare,
                "explanation": explanation
            }
        
        return Response({
            "success": True,
            "pickup": pickup,
            "destination": destination,
            "distance_km": distance_km,
            "duration_mins": duration_mins,
            "traffic_level": traffic_level,
            "fare": ai_fare
        })
        
    except Exception as e:
        print(f"⚠️ Fare calculation error: {e}")
        base_fare = calculate_fare(service_type, 5.0)
        return Response({
            "success": True,
            "distance_km": 5.0,
            "duration_mins": 15,
            "traffic_level": "Normal",
            "fare": {
                "base_price": base_fare,
                "distance_cost": 0,
                "traffic_adjustment": 0,
                "total_fare": base_fare,
                "explanation": "Estimate — using standard pricing"
            }
        })
