"""
Rydnow API views.

Sections
    1. Constants, response helpers, throttles, decorators
    2. Authentication & customer registration
    3. Phone verification (OTP)
    4. Rider onboarding
    5. Customer profile
    6. Rider location & nearby-rider search
    7. Rider wallet
    8. Payments (Paystack)
"""


import hashlib
import hmac
import json
import logging
import math
import secrets
import time
import uuid
from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal
from functools import wraps
from urllib.parse import quote


import requests
from django.conf import settings
from django.contrib.auth import authenticate, get_user_model
from django.contrib.auth.models import update_last_login
from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.utils.module_loading import import_string
from django.core.paginator import Paginator, EmptyPage, PageNotAnInteger
from rest_framework import generics, status, serializers
from rest_framework.authtoken.models import Token
from rest_framework.decorators import (
    api_view,
    authentication_classes,
    parser_classes,
    permission_classes,
    throttle_classes,
)
from rest_framework.pagination import PageNumberPagination
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import AnonRateThrottle, UserRateThrottle


from .models import (
    COMMISSION_RATE, Booking, CustomerProfile, Service, ServiceQualification,
    RiderWallet, WalletTransaction, WithdrawalRequest
)
from .rider_domain import RiderDocument, RiderProfile
from .serializers import (
    AccountSerializer,
    AccountUpdateSerializer,
    ChangePasswordSerializer,
    CustomerProfileSerializer,
    CustomerSerializer,
    InitializePaymentSerializer,
    LoginSerializer,
    NearbyRidersQuerySerializer,
    PhoneSerializer,
    RiderDocumentSerializer,
    RiderLocationSerializer,
    RiderProfileSerializer,
    RiderRegistrationSerializer,
    VerifyCodeSerializer,
    VerifyPaymentSerializer,
    TransactionSerializer,
    TransactionQuerySerializer,
    AmountSerializer,
)


User = get_user_model()
logger = logging.getLogger(__name__)


# ===========================================================================
# 1. CONSTANTS, HELPERS, THROTTLES, DECORATORS
# ===========================================================================
CURRENCY = getattr(settings, "PAYMENT_CURRENCY", "GHS")
MINIMUM_WALLET_BALANCE = Decimal(getattr(settings, "MINIMUM_WALLET_BALANCE", "50.00"))
MIN_WITHDRAWAL_AMOUNT = Decimal(getattr(settings, "MIN_WITHDRAWAL_AMOUNT", "10.00"))
AMOUNT_TOLERANCE = Decimal("0.01")
TWO_PLACES = Decimal("0.01")


CUSTOMER_EDITABLE_FIELDS = ("full_name", "phone")


LOCATION_STALE_AFTER = timedelta(minutes=5)
EARTH_RADIUS_KM = 6371.0
KM_PER_DEGREE = 111.0
MAX_NEARBY_CANDIDATES = 200


PAYSTACK_BASE_URL = "https://api.paystack.co"
PAYSTACK_TIMEOUT = (5, 15)  # (connect, read) seconds
PURPOSE_BOOKING = "booking"
PURPOSE_DEPOSIT = "wallet_deposit"
PAID_STATUSES = ("held", "released")


OTP_LENGTH = 6
OTP_TTL_SECONDS = 600
OTP_COOLDOWN_SECONDS = 60
OTP_MAX_ATTEMPTS = 5
OTP_MAX_SENDS_PER_HOUR = 5


def fill_null_defaults(data):
    """Replace None values with sensible defaults in API responses."""
    if isinstance(data, dict):
        return {k: fill_null_defaults(v) for k, v in data.items()}
    elif isinstance(data, list):
        return [fill_null_defaults(item) for item in data]
    return data


def ok(message=None, http_status=status.HTTP_200_OK, **payload):
    """Success envelope: {"success": true, "message"?, ...payload}."""
    body = {"success": True}
    if message:
        body["message"] = message
    body.update(payload)
    return Response(body, status=http_status)


def fail(error, http_status=status.HTTP_400_BAD_REQUEST, errors=None):
    """Error envelope: {"success": false, "error": "...", "errors"?}."""
    body = {"success": False, "error": error}
    if errors:
        body["errors"] = errors
    return Response(body, status=http_status)


def validation_fail(serializer):
    return fail("Validation failed.", status.HTTP_400_BAD_REQUEST, serializer.errors)


def money(value):
    """Format money as a fixed 2-decimal string (never floats for currency)."""
    return str(Decimal(str(value)).quantize(TWO_PLACES, rounding=ROUND_HALF_UP))


def split_name(full_name):
    first, _, last = full_name.strip().partition(" ")
    return first, last.strip()


def haversine_km(lat1, lng1, lat2, lng2):
    lat1, lng1, lat2, lng2 = map(math.radians, (lat1, lng1, lat2, lng2))
    a = (
        math.sin((lat2 - lat1) / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin((lng2 - lng1) / 2) ** 2
    )
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(a))


class LoginThrottle(AnonRateThrottle):
    scope = "login"
    rate = "10/min"


class RegistrationThrottle(AnonRateThrottle):
    scope = "register"
    rate = "5/min"


class OTPThrottle(AnonRateThrottle):
    scope = "otp"
    rate = "5/min"


class LocationThrottle(UserRateThrottle):
    scope = "location"
    rate = "120/min"


class WalletReadThrottle(UserRateThrottle):
    scope = "wallet_read"


def rider_required(view):
    """Inject the authenticated user's RiderProfile as `rider`, or return 404."""
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        rider = RiderProfile.objects.filter(user=request.user).first()
        if rider is None:
            return fail("Rider profile not found.", status.HTTP_404_NOT_FOUND)
        return view(request, *args, rider=rider, **kwargs)
    return wrapper


def _get_wallet(user, *, lock=False):
    """Fetch (or create) a rider's wallet. lock=True requires transaction.atomic()."""
    manager = RiderWallet.objects.select_for_update() if lock else RiderWallet.objects
    wallet, _ = manager.get_or_create(user=user)
    return wallet


def _can_go_online(rider, wallet):
    return rider.is_verified and rider.is_active and wallet.balance >= MINIMUM_WALLET_BALANCE


# ===========================================================================
# 2. AUTHENTICATION & CUSTOMER REGISTRATION
# ===========================================================================
@api_view(["POST"])
@authentication_classes([])
@permission_classes([AllowAny])
@throttle_classes([LoginThrottle])
def user_login(request):
    serializer = LoginSerializer(data=request.data)
    if not serializer.is_valid():
        return validation_fail(serializer)

    identifier = serializer.validated_data["username"].strip()
    if "@" in identifier:
        identifier = identifier.lower()

    user = authenticate(
        request=request,
        username=identifier,
        password=serializer.validated_data["password"],
    )
    if user is None:
        return fail("Invalid credentials.", status.HTTP_401_UNAUTHORIZED)

    update_last_login(None, user)
    token, _ = Token.objects.get_or_create(user=user)
    rider = RiderProfile.objects.filter(user=user).first()

    user_payload = {
        "id": user.id,
        "username": user.username,
        "email": user.email,
        "role": "rider" if rider else "customer",
    }
    if rider:
        user_payload["is_verified"] = rider.is_verified

    return ok(token=token.key, user=user_payload)


@api_view(["POST"])
@authentication_classes([])
@permission_classes([AllowAny])
@throttle_classes([RegistrationThrottle])
def customer_register(request):
    """Register a new customer account."""
    serializer = RegistrationSerializer(data=request.data)
    try:
        serializer.is_valid(raise_exception=True)
        user = serializer.save()
        return Response({
            "message": "Account created successfully",
            "user_id": user.id,
            "email": user.email,
        }, status=status.HTTP_201_CREATED)
    except serializers.ValidationError as e:
        return Response({
            "error": e.detail,
        }, status=status.HTTP_400_BAD_REQUEST)
    except IntegrityError:
        return Response({
            "error": "An account with these details already exists.",
        }, status=status.HTTP_409_CONFLICT)
    except Exception as e:
        logger.exception(f"Customer registration failed: {e}")
        return Response({
            "error": "Something went wrong. Please try again.",
        }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


# ===========================================================================
# 3. PHONE VERIFICATION (OTP)
# ===========================================================================
def _otp_hash(phone, code):
    return hmac.new(
        settings.SECRET_KEY.encode(), f"{phone}:{code}".encode(), hashlib.sha256
    ).hexdigest()


def _send_sms(phone, message):
    """Deliver an SMS through the configured backend. Returns True on success."""
    backend_path = getattr(settings, "SMS_BACKEND", None)
    if backend_path:
        try:
            import_string(backend_path)(phone, message)
            return True
        except Exception:
            logger.exception("SMS backend failed for %s", phone)
            return False
    if settings.DEBUG:
        logger.warning("SMS not sent (dev mode) to %s: %s", phone, message)
        return True
    logger.error("SMS_BACKEND is not configured; cannot send verification codes.")
    return False


@api_view(["POST"])
@authentication_classes([])
@permission_classes([AllowAny])
@throttle_classes([OTPThrottle])
def send_verification_code(request):
    serializer = PhoneSerializer(data=request.data)
    if not serializer.is_valid():
        return validation_fail(serializer)
    phone = serializer.validated_data["phone"]

    hourly_key = f"otp:hourly:{phone}"
    if cache.get(hourly_key, 0) >= OTP_MAX_SENDS_PER_HOUR:
        return fail("Too many codes requested. Try again later.", status.HTTP_429_TOO_MANY_REQUESTS)

    cooldown_key = f"otp:cooldown:{phone}"
    if not cache.add(cooldown_key, 1, OTP_COOLDOWN_SECONDS):
        return fail("Please wait before requesting another code.", status.HTTP_429_TOO_MANY_REQUESTS)

    code = f"{secrets.randbelow(10 ** OTP_LENGTH):0{OTP_LENGTH}d}"
    message = f"Your Rydnow verification code is {code}. It expires in {OTP_TTL_SECONDS // 60} minutes."

    if not _send_sms(phone, message):
        cache.delete(cooldown_key)
        return fail(
            "Verification service is unavailable. Please try again.",
            status.HTTP_503_SERVICE_UNAVAILABLE,
        )

    cache.set(
        f"otp:code:{phone}",
        {"hash": _otp_hash(phone, code), "attempts": 0, "expires_at": time.time() + OTP_TTL_SECONDS},
        OTP_TTL_SECONDS,
    )
    cache.add(hourly_key, 0, 3600)
    cache.incr(hourly_key)

    return ok("Verification code sent.", expires_in=OTP_TTL_SECONDS)


@api_view(["POST"])
@authentication_classes([])
@permission_classes([AllowAny])
@throttle_classes([OTPThrottle])
def verify_code(request):
    serializer = VerifyCodeSerializer(data=request.data)
    if not serializer.is_valid():
        return validation_fail(serializer)
    phone = serializer.validated_data["phone"]
    code = serializer.validated_data["code"]

    key = f"otp:code:{phone}"
    entry = cache.get(key)
    if not entry:
        return fail("Code expired or was never requested.", status.HTTP_400_BAD_REQUEST)

    if entry["attempts"] >= OTP_MAX_ATTEMPTS:
        cache.delete(key)
        return fail("Too many attempts. Request a new code.", status.HTTP_429_TOO_MANY_REQUESTS)

    if not hmac.compare_digest(entry["hash"], _otp_hash(phone, code)):
        entry["attempts"] += 1
        remaining = max(int(entry["expires_at"] - time.time()), 1)
        cache.set(key, entry, remaining)
        return fail("Invalid verification code.", status.HTTP_400_BAD_REQUEST)

    cache.delete(key)
    return ok("Phone number verified.", verified=True)


# ===========================================================================
# 4. RIDER ONBOARDING
# ===========================================================================
@api_view(["POST"])
@authentication_classes([])
@permission_classes([AllowAny])
@throttle_classes([RegistrationThrottle])
def rider_register(request):
    """
    Create User -> RiderProfile -> RiderWallet atomically. A failure at any step
    rolls everything back (no orphan records). Returns an auth token so the
    client can upload documents immediately (step 2 of onboarding).
    """
    serializer = RiderRegistrationSerializer(data=request.data)
    if not serializer.is_valid():
        return validation_fail(serializer)

    data = serializer.validated_data
    email = data["email"].strip().lower()
    first_name, last_name = split_name(data["full_name"])

    try:
        with transaction.atomic():
            user = User.objects.create_user(
                username=email,
                email=email,
                password=data["password"],
                first_name=first_name,
                last_name=last_name,
            )
            rider = RiderProfile.objects.create(
                user=user,
                full_name=data["full_name"],
                phone=data["phone"],
            )
            RiderWallet.objects.create(user=user)
            token, _ = Token.objects.get_or_create(user=user)
    except IntegrityError:
        return fail("An account with this email or phone already exists.", status.HTTP_409_CONFLICT)

    logger.info("Rider registered: rider_id=%s", rider.id)
    return ok(
        "Registration submitted. Upload your documents to complete verification.",
        status.HTTP_201_CREATED,
        rider_id=rider.id,
        email=user.email,
        phone=rider.phone,
        is_verified=rider.is_verified,
        requires_approval=True,
        token=token.key,
    )


class MyRiderProfileView(generics.RetrieveUpdateAPIView):
    """GET/PATCH the authenticated rider's own profile."""
    serializer_class = RiderProfileSerializer
    permission_classes = [IsAuthenticated]
    http_method_names = ["get", "patch", "head", "options"]

def get_object(self):
    return get_object_or_404(
        RiderProfile.objects.select_related("user"), user=self.request.user
    )


@api_view(["POST"])
@parser_classes([MultiPartParser, FormParser])
@permission_classes([IsAuthenticated])
@rider_required
def rider_submit_documents(request, rider):
    serializer = RiderDocumentSerializer(data=request.data)
    if not serializer.is_valid():
        return validation_fail(serializer)

    try:
        document = RiderDocument.resubmit_document(
            rider=rider,
            document_type=serializer.validated_data["document_type"],
            document_file=serializer.validated_data["document_file"],
        )
    except ValueError as exc:
        return fail(str(exc), status.HTTP_400_BAD_REQUEST)
    except IntegrityError:
        return fail("This document type has already been submitted.", status.HTTP_409_CONFLICT)

    return ok(
        "Document uploaded successfully.",
        status=HTTP_201_CREATED,
        id=document.id,
        document_type=document.document_type,
    )


# ===========================================================================
# 5. CUSTOMER PROFILE
# ===========================================================================
@api_view(["GET", "PATCH"])
@permission_classes([IsAuthenticated])
def customer_profile(request):
    """GET: return profile. PATCH: update full_name and/or phone only."""
    try:
        profile = CustomerProfile.objects.get(user=request.user)
    except CustomerProfile.DoesNotExist:
        return fail(
            "Customer profile not found. Please complete registration.",
            status.HTTP_404_NOT_FOUND,
        )

    if request.method == "GET":
        return ok(data=CustomerProfileSerializer(profile).data)

    update_data = {f: request.data[f] for f in CUSTOMER_EDITABLE_FIELDS if f in request.data}
    if not update_data:
        return fail(f"No valid fields provided. You can update: {', '.join(CUSTOMER_EDITABLE_FIELDS)}.")

    serializer = CustomerProfileSerializer(profile, data=update_data, partial=True)
    if not serializer.is_valid():
        return validation_fail(serializer)
    serializer.save()
    return ok("Profile updated successfully.", data=serializer.data)


# ===========================================================================
# 6. RIDER LOCATION & NEARBY SEARCH
# ===========================================================================
@api_view(["PATCH"])
@permission_classes([IsAuthenticated])
@throttle_classes([LocationThrottle])
@rider_required
def update_rider_location(request, rider):
    """Update the authenticated rider's current location."""
    if not rider.is_active:
        return fail("Rider account is inactive.", status.HTTP_403_FORBIDDEN)

    serializer = RiderLocationSerializer(data=request.data)
    if not serializer.is_valid():
        return validation_fail(serializer)

    rider.latitude = serializer.validated_data["latitude"]
    rider.longitude = serializer.validated_data["longitude"]
    rider.location_updated_at = timezone.now()
    rider.save(update_fields=["latitude", "longitude", "location_updated_at", "updated_at"])

    return ok(
        "Location updated successfully.",
        latitude=rider.latitude,
        longitude=rider.longitude,
        updated_at=rider.location_updated_at,
    )


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def get_nearby_riders(request):
    """Active, verified riders near the customer who are approved for the service."""
    query = NearbyRidersQuerySerializer(data=request.query_params)
    if not query.is_valid():
        return validation_fail(query)
    data = query.validated_data

    lat, lng = data["latitude"], data["longitude"]
    radius_km, limit = data["radius_km"], data["limit"]

    service_ref = str(data["service"])
    lookup = {"pk": service_ref} if service_ref.isdigit() else {"slug": service_ref}
    service = get_object_or_404(Service, is_active=True, **lookup)

    qualified_ids = ServiceQualification.objects.filter(
        service=service, is_approved=True
    ).values_list("rider_id", flat=True)

    lat_delta = radius_km / KM_PER_DEGREE
    lng_delta = radius_km / (KM_PER_DEGREE * max(math.cos(math.radians(lat)), 0.01))

    candidates = RiderProfile.objects.filter(
        pk__in=qualified_ids,
        is_active=True,
        is_verified=True,
        user__rider_wallet__balance__gte=MINIMUM_WALLET_BALANCE,
        location_updated_at__gte=timezone.now() - LOCATION_STALE_AFTER,
        latitude__range=(lat - lat_delta, lat + lat_delta),
        longitude__range=(lng - lng_delta, lng + lng_delta),
    )[:MAX_NEARBY_CANDIDATES]

    riders = []
    for rider in candidates:
        distance = haversine_km(lat, lng, float(rider.latitude), float(rider.longitude))
        if distance <= radius_km:
            riders.append(
                {
                    "id": rider.pk,
                    "full_name": rider.full_name,
                    "latitude": float(rider.latitude),
                    "longitude": float(rider.longitude),
                    "distance_km": round(distance, 2),
                    "location_updated_at": rider.location_updated_at,
                }
            )
    riders.sort(key=lambda r: r["distance_km"])
    riders = riders[:limit]

    return ok(service=service.name, count=len(riders), riders=riders)


# ===========================================================================
# 7. RIDER WALLET
# ===========================================================================
def _serialize_transaction(txn):
    return {
        "id": txn.id,
        "amount": money(txn.amount),
        "type": txn.transaction_type,
        "description": txn.description,
        "timestamp": txn.created_at,
    }


@api_view(["GET"])
@permission_classes([IsAuthenticated])
@rider_required
def get_rider_wallet(request, rider):
    wallet = _get_wallet(request.user)
    recent = WalletTransaction.objects.filter(wallet=wallet).order_by("-created_at")[:10]
    return ok(
        balance=money(wallet.balance),
        minimum_required=money(MINIMUM_WALLET_BALANCE),
        can_go_online=_can_go_online(rider, wallet),
        transactions=[_serialize_transaction(t) for t in recent],
    )


@api_view(["GET"])
@permission_classes([IsAuthenticated])
@rider_required
def rider_transactions(request, rider):
    """Paginated transaction history (?page=2)."""
    wallet = _get_wallet(request.user)
    queryset = WalletTransaction.objects.filter(wallet=wallet).order_by("-created_at")
    paginator = PageNumberPagination()
    page = paginator.paginate_queryset(queryset, request)
    body = paginator.get_paginated_response([_serialize_transaction(t) for t in page]).data
    return ok(**body)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
@rider_required
def wallet_deposit(request, rider):
    """Start a wallet deposit."""
    serializer = AmountSerializer(data=request.data)
    if not serializer.is_valid():
        return validation_fail(serializer)
    return _start_wallet_deposit(request.user, rider, serializer.validated_data["amount"])


@api_view(["POST"])
@permission_classes([IsAuthenticated])
@rider_required
def request_withdrawal(request, rider):
    serializer = AmountSerializer(data=request.data)
    if not serializer.is_valid():
        return validation_fail(serializer)
    amount = serializer.validated_data["amount"]

    if not rider.is_verified:
        return fail("Only verified riders can withdraw.", status.HTTP_403_FORBIDDEN)
    if amount < MIN_WITHDRAWAL_AMOUNT:
        return fail(f"Minimum withdrawal is {money(MIN_WITHDRAWAL_AMOUNT)}.")

    with transaction.atomic():
        wallet = _get_wallet(request.user, lock=True)
        if wallet.balance < amount:
            return fail("Insufficient wallet balance.")
        if wallet.balance - amount < MINIMUM_WALLET_BALANCE:
            return fail(
                f"You must keep at least {money(MINIMUM_WALLET_BALANCE)} in your wallet. "
                f"Maximum withdrawable: {money(max(wallet.balance - MINIMUM_WALLET_BALANCE, 0))}."
            )

        wallet.balance -= amount
        wallet.save()
        withdrawal = WithdrawalRequest.objects.create(
            rider_profile=rider, amount=amount, status="pending"
        )
        WalletTransaction.objects.create(
            wallet=wallet,
            amount=amount,
            transaction_type="withdrawal",
            description=f"Withdrawal request - GHS {money(amount)}",
        )

    logger.info("Withdrawal requested: rider_id=%s amount=%s", rider.id, amount)
    return ok(
        "Withdrawal request submitted.",
        status=HTTP_201_CREATED,
        withdrawal_id=withdrawal.id,
        new_balance=money(wallet.balance),
    )


# ===========================================================================
# 8. PAYMENTS (PAYSTACK)
# ===========================================================================
class PaymentError(Exception):
    def __init__(self, message, status_code=status.HTTP_400_BAD_REQUEST):
        super().__init__(message)
        self.message = message
        self.status_code = status_code

    @property
    def retryable(self):
        return self.status_code >= 500


def _paystack_request(method, path, **kwargs):
    secret = getattr(settings, "PAYSTACK_SECRET_KEY", "")
    if not secret:
        logger.error("PAYSTACK_SECRET_KEY is not configured.")
        raise PaymentError("Payment service is currently unavailable.", status.HTTP_503_SERVICE_UNAVAILABLE)

    try:
        response = requests.request(
            method,
            f"{PAYSTACK_BASE_URL}{path}",
            headers={"Authorization": f"Bearer {secret}"},
            timeout=PAYSTACK_TIMEOUT,
            **kwargs,
        )
    except requests.exceptions.RequestException:
        logger.exception("Paystack request failed: %s %s", method, path)
        raise PaymentError(
            "Could not reach the payment service. Please try again.",
            status.HTTP_503_SERVICE_UNAVAILABLE,
        ) from None

    try:
        body = response.json()
    except ValueError:
        logger.error("Paystack returned non-JSON (HTTP %s) for %s", response.status_code, path)
        raise PaymentError("Invalid response from payment service.", status.HTTP_502_BAD_GATEWAY) from None

    if response.status_code != 200 or not isinstance(body, dict) or not body.get("status"):
        logger.warning("Paystack rejected %s %s: HTTP %s", method, path, response.status_code)
        code = status.HTTP_502_BAD_GATEWAY if response.status_code >= 500 else status.HTTP_400_BAD_REQUEST
        raise PaymentError("Payment verification was not successful.", code)

    return body.get("data") or {}


def _paystack_initialize(*, email, amount, reference, metadata):
    payload = {
        "email": email,
        "amount": int((Decimal(amount) * 100).to_integral_value(ROUND_HALF_UP)),
        "currency": CURRENCY,
        "reference": reference,
        "metadata": metadata,
    }
    callback_url = getattr(settings, "PAYSTACK_CALLBACK_URL", "")
    if callback_url:
        payload["callback_url"] = callback_url

    data = _paystack_request("POST", "/transaction/initialize", json=payload)
    if not data.get("authorization_url"):
        raise PaymentError("Payment service returned an incomplete response.", status.HTTP_502_BAD_GATEWAY)
    return data


def _paystack_verify(reference):
    data = _paystack_request("GET", f"/transaction/verify/{quote(reference, safe='')}")

    if data.get("status") != "success":
        raise PaymentError("Payment has not been completed successfully.")
    if data.get("reference") != reference:
        raise PaymentError("Payment reference mismatch detected.", status.HTTP_409_CONFLICT)
    if data.get("currency") != CURRENCY:
        raise PaymentError(f"Unsupported payment currency. Only {CURRENCY} is accepted.")

    amount = data.get("amount")
    if not isinstance(amount, int) or isinstance(amount, bool) or amount <= 0:
        raise PaymentError("Invalid payment amount from verification service.")
    return data


def _paystack_amount(paystack_data):
    return Decimal(paystack_data["amount"]) / Decimal("100")


def _new_reference(prefix):
    return f"{prefix}-{uuid.uuid4().hex.upper()}"


def _start_wallet_deposit(user, rider, amount):
    reference = _new_reference("DEP")
    try:
        data = _paystack_initialize(
            email=user.email,
            amount=amount,
            reference=reference,
            metadata={"purpose": PURPOSE_DEPOSIT, "rider_id": rider.pk},
        )
    except PaymentError as exc:
        return fail(exc.message, exc.status_code)

    return ok(
        "Complete the payment to fund your wallet.",
        reference=reference,
        authorization_url=data["authorization_url"],
        access_code=data.get("access_code"),
    )


def _settle_reference(reference, requesting_user=None):
    paystack = _paystack_verify(reference)
    metadata = paystack.get("metadata")
    metadata = metadata if isinstance(metadata, dict) else {}

    try:
        if metadata.get("purpose") == PURPOSE_DEPOSIT:
            return _settle_wallet_deposit(reference, paystack, metadata, requesting_user)
        return _settle_booking_payment(reference, paystack, requesting_user)
    except IntegrityError:
        raise PaymentError(
            "Payment recording conflict. Reference may already exist.",
            status.HTTP_409_CONFLICT,
        ) from None


def _settle_booking_payment(reference, paystack, requesting_user):
    verified_amount = _paystack_amount(paystack)
    not_found = PaymentError("No booking found for this payment reference.", status.HTTP_404_NOT_FOUND)

    with transaction.atomic():
        try:
            booking = Booking.objects.select_for_update().get(payment_ref=reference)
        except Booking.DoesNotExist:
            raise not_found from None
        if requesting_user is not None and booking.customer_id != requesting_user.id:
            raise not_found

        booking_amount = Decimal(str(booking.total_fare))

        existing = WalletTransaction.objects.filter(reference=reference).first()
        if existing is not None and existing.related_booking_id != booking.id:
            raise PaymentError(
                "Payment reference is associated with a different record. Please contact support.",
                status.HTTP_409_CONFLICT,
            )
        if existing is not None or booking.payment_status in PAID_STATUSES:
                   return {
            "created": False,
            "message": "Payment already verified and recorded.",
            "booking_id": str(booking.id),
            "booking_ref": booking.booking_ref,
            "total_amount": money(booking_amount),
            "payment_status": booking.payment_status,
        }

        if abs(verified_amount - booking_amount) > AMOUNT_TOLERANCE:
            raise PaymentError("Payment amount does not match booking total.", status.HTTP_409_CONFLICT)

        rider = booking.assigned_rider
        if rider is None:
            raise PaymentError("This booking has no assigned rider. Cannot record payment.")

        commission_rate = Decimal(str(getattr(settings, "COMMISSION_RATE", COMMISSION_RATE)))
        commission = (booking_amount * commission_rate).quantize(TWO_PLACES, rounding=ROUND_HALF_UP)
        rider_amount = (booking_amount - commission).quantize(TWO_PLACES, rounding=ROUND_HALF_UP)

        verified_ok, message = booking.confirm_payment_verified(
            provider_ref=paystack.get("id", ""),
            payment_ref=reference,
        )
        if not verified_ok:
            raise PaymentError(message, status.HTTP_409_CONFLICT)

        wallet = _get_wallet(rider.user, lock=True)
        credited, message = wallet.credit_earnings(
            rider_amount,
            booking_ref=booking.booking_ref,
            payment_verified=True,
        )
        if credited is None:
            raise PaymentError(message, status.HTTP_409_CONFLICT)

    logger.info("Booking payment settled: booking_ref=%s reference=%s", booking.booking_ref, reference)
    return {
        "created": True,
        "message": "Payment verified successfully.",
        "booking_id": str(booking.id),
        "booking_ref": booking.booking_ref,
        "total_amount": money(booking_amount),
        "platform_commission": money(commission),
        "rider_amount": money(rider_amount),
        "payment_status": booking.payment_status,
    }


def _settle_wallet_deposit(reference, paystack, metadata, requesting_user):
    amount = _paystack_amount(paystack)
    not_found = PaymentError(
        "Deposit could not be matched to a rider account.", status.HTTP_404_NOT_FOUND
    )

    with transaction.atomic():
        try:
            rider = RiderProfile.objects.select_related("user").get(pk=metadata.get("rider_id"))
        except (RiderProfile.DoesNotExist, ValueError, TypeError):
            raise not_found from None
        if requesting_user is not None and rider.user_id != requesting_user.id:
            raise not_found

        wallet = _get_wallet(rider.user, lock=True)
        _, created = WalletTransaction.objects.get_or_create(
            reference=reference,
            defaults={
                "wallet": wallet,
                "amount": amount,
                "transaction_type": "deposit",
                "description": f"Wallet deposit - GHS {money(amount)}",
            },
        )
        if created:
            wallet.balance += amount
            wallet.save()

    if created:
        logger.info("Wallet deposit settled: rider_id=%s reference=%s", rider.id, reference)
    return {
        "created": created,
        "message": "Deposit successful." if created else "Deposit already recorded.",
        "reference": reference,
        "amount": money(amount),
        "new_balance": money(wallet.balance),
        "can_go_online": _can_go_online(rider, wallet),
    }


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def initialize_payment(request):
    """Start a Paystack checkout for a booking or a wallet deposit."""
    serializer = InitializePaymentSerializer(data=request.data)
    if not serializer.is_valid():
        return validation_fail(serializer)
    data = serializer.validated_data

    if data["purpose"] == PURPOSE_DEPOSIT:
        rider = RiderProfile.objects.filter(user=request.user).first()
        if rider is None:
            return fail("Only riders can fund a wallet.", status.HTTP_403_FORBIDDEN)
        return _start_wallet_deposit(request.user, rider, data["amount"])

    try:
        booking = Booking.objects.get(booking_ref=data["booking_ref"], customer=request.user)
    except Booking.DoesNotExist:
        return fail("Booking not found.", status.HTTP_404_NOT_FOUND)
    if booking.payment_status in PAID_STATUSES:
        return fail("This booking has already been paid.", status.HTTP_409_CONFLICT)

    reference = _new_reference("BKG")
    try:
        checkout = _paystack_initialize(
            email=request.user.email,
            amount=Decimal(str(booking.total_fare)),
            reference=reference,
            metadata={"purpose": PURPOSE_BOOKING, "booking_ref": booking.booking_ref},
        )
    except PaymentError as exc:
        return fail(exc.message, exc.status_code)

    booking.payment_ref = reference
    booking.save(update_fields=["payment_ref"])

    return ok(
        "Complete the payment to confirm your booking.",
        reference=reference,
        authorization_url=checkout["authorization_url"],
        access_code=checkout.get("access_code"),
    )


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def verify_payment(request):
    """Called by the app after checkout. Safe to call repeatedly."""
    serializer = VerifyPaymentSerializer(data=request.data)
    if not serializer.is_valid():
        return validation_fail(serializer)

    try:
        result = _settle_reference(
            serializer.validated_data["reference"], requesting_user=request.user
        )
    except PaymentError as exc:
        return fail(exc.message, exc.status_code)

    created = result.pop("created")
    message = result.pop("message")
    return ok(message, status=status.HTTP_201_CREATED if created else status.HTTP_200_OK, **result)


@api_view(["POST"])
@authentication_classes([])
@permission_classes([AllowAny])
@throttle_classes([])
def paystack_webhook(request):
    secret = getattr(settings, "PAYSTACK_SECRET_KEY", "")
    raw_body = request.body
    received = request.headers.get("X-Paystack-Signature", "").encode()
    expected = hmac.new(secret.encode(), raw_body, hashlib.sha512).hexdigest().encode()

    if not secret or not hmac.compare_digest(received, expected):
        logger.warning("Paystack webhook rejected: invalid signature.")
        return Response(status=status.HTTP_401_UNAUTHORIZED)

    try:
        event = json.loads(raw_body)
    except ValueError:
        return Response(status=status.HTTP_400_BAD_REQUEST)

    if event.get("event") == "charge.success":
        reference = (event.get("data") or {}).get("reference")
        if isinstance(reference, str) and reference:
            try:
                _settle_reference(reference)
            except PaymentError as exc:
                if exc.retryable:
                    logger.error("Webhook settlement failed (will retry) ref=%s: %s", reference, exc.message)
                    return Response(status=status.HTTP_503_SERVICE_UNAVAILABLE)
                logger.warning("Webhook payment not settled ref=%s: %s", reference, exc.message)

    return Response(status=status.HTTP_200_OK)





# ===========================================================================
# 9. TRANSACTION HISTORY & WALLET STATEMENTS
# ===========================================================================
import csv
from datetime import datetime, time, timedelta
from django.db.models import Count, DecimalField, Q, Sum, Value
from django.db.models.functions import Coalesce, TruncMonth
from django.http import StreamingHttpResponse
from rest_framework.pagination import PageNumberPagination


ZERO = Value(Decimal("0.00"), output_field=DecimalField(max_digits=14, decimal_places=2))
LOW_BALANCE_THRESHOLD = Decimal("10.00")
MAX_EXPORT_ROWS = 5000


class TransactionPagination(PageNumberPagination):
    page_size = 20
    page_size_query_param = "page_size"
    max_page_size = 100
    
    
def _apply_filters(qs, f):
    tz = timezone.get_current_timezone()
    if "type" in f:
        qs = qs.filter(transaction_type=f["type"])
    if "status" in f:
        qs = qs.filter(status=f["status"])
    if "direction" in f:
        qs = qs.filter(direction=f["direction"])
    if "date_from" in f:
        qs = qs.filter(created_at__gte=timezone.make_aware(datetime.combine(f["date_from"], time.min), tz))
    if "date_to" in f:
        end = datetime.combine(f["date_to"] + timedelta(days=1), time.min)
        qs = qs.filter(created_at__lt=timezone.make_aware(end, tz))
    if "min_amount" in f:
        qs = qs.filter(amount__gte=f["min_amount"])
    if "max_amount" in f:
        qs = qs.filter(amount__lte=f["max_amount"])
    if f.get("search"):
        term = f["search"].strip()
        qs = qs.filter(Q(reference__icontains=term) | Q(description__icontains=term))
    return qs.order_by(f["ordering"], "-id")

def _summary(qs):
    agg = qs.filter(status=WalletTransaction.Status.SUCCESS).aggregate(
        total_in=Coalesce(Sum("amount", filter=Q(transaction_type="deposit")), ZERO),
        total_out=Coalesce(Sum("amount", filter=Q(transaction_type="withdrawal")), ZERO),
        count=Count("id"),
    )
    agg["net"] = agg["total_in"] - agg["total_out"]
    return agg

def _monthly_trend(wallet, months=6):
    since = (timezone.now().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    - timedelta(days=31 * (months - 1)))
    rows = (
        wallet.transactions.filter(status=WalletTransaction.Status.SUCCESS, created_at__gte=since)
        .annotate(month=TruncMonth("created_at"))
        .values("month")
        .annotate(
            money_in=Coalesce(Sum("amount", filter=Q(transaction_type="deposit")), ZERO),
            money_out=Coalesce(Sum("amount", filter=Q(transaction_type="withdrawal")), ZERO),
        )
        .order_by("month")
    )
    return [
        {"month": r["month"].strftime("%Y-%m"), "money_in": r["money_in"], "money_out": r["money_out"]}
        for r in rows
    ]


def _top_up_nudge(wallet):
    low = wallet.balance < LOW_BALANCE_THRESHOLD
    return {
        "low_balance": low,
        "message": "Your balance is running low. Top up to keep riding without interruption." if low else None,
    }


def _csv_safe(value):
    s = "" if value is None else str(value)
    return "'" + s if s[:1] in ("=", "+", "-", "@") else s


class _Echo:
    def write(self, value):
        return value


def _csv_response(qs, currency):
    qs = qs[:MAX_EXPORT_ROWS]
    writer = csv.writer(_Echo())
    
    def rows():
        yield writer.writerow([
            "Date", "Reference", "Type", "Status", f"Amount ({currency})",
            "Description"
        ])
        for t in qs.iterator(chunk_size=1000):
            yield writer.writerow([
                timezone.localtime(t.created_at).strftime("%Y-%m-%d %H:%M:%S"),
                _csv_safe(t.reference), t.get_transaction_type_display(),
                t.get_status_display(), t.amount,
                _csv_safe(t.description),
            ])

        resp = StreamingHttpResponse(rows(), content_type="text/csv; charset=utf-8")
        stamp = timezone.now().strftime("%Y%m%d")
        resp["Content-Disposition"] = f'attachment; filename="wallet-statement-{stamp}.csv"'
        resp["Cache-Control"] = "no-store"
        return resp


@api_view(["GET"])
@permission_classes([IsAuthenticated])
@throttle_classes([WalletReadThrottle])
def get_transaction_history(request):
    params = TransactionQuerySerializer(data=request.query_params)
    if not params.is_valid():
        return Response({"error": "Invalid query parameters", "details": params.errors}, status=400)
    filters = params.validated_data

    wallet = _get_wallet(request.user)
    currency = getattr(wallet, "currency", "GHS")
    qs = _apply_filters(wallet.transactions.all(), filters)

    try:
        if filters.get("export") == "csv":
            logger.info("wallet statement export user=%s wallet=%s", request.user.pk, wallet.pk)
            return _csv_response(qs, currency)

        paginator = TransactionPagination()
        page = paginator.paginate_queryset(qs, request)
        payload = paginator.get_paginated_response(
            TransactionSerializer(page, many=True).data
        ).data

        payload.update({
            "currency": currency,
            "balance": wallet.balance,
            "summary": _summary(qs),
            "monthly_trend": _monthly_trend(wallet),
            "insights": _top_up_nudge(wallet),
        })
        return Response(payload)

    except Exception:
        logger.exception("transaction history failed user=%s", request.user.pk)
        return Response({"error": "Unable to load transactions. Please try again."}, status=500)


# ===========================================================================
# 10. DEPOSIT VERIFY & DEPOSIT INITIATE
# ===========================================================================
import re

REFERENCE_RE = re.compile(r"^[A-Za-z0-9_.\-]{6,100}$")
RATE_LIMIT = 20
RATE_WINDOW = 60
GATEWAY_TIMEOUT = 10
POLL_INTERVAL = 5
GATEWAY_URL = "https://api.paystack.co/transaction/verify/{ref}"


def error(message, status, **extra):
    return JsonResponse({"success": False, "error": message, **extra}, status=status)


def json_login_required(view):
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return error("Authentication required", 401)
        return view(request, *args, **kwargs)
    return wrapper


def is_rate_limited(user_id):
    key = f"deposit_verify:{user_id}"
    cache.add(key, 0, RATE_WINDOW)
    try:
        return cache.incr(key) > RATE_LIMIT
    except ValueError:
        cache.set(key, 1, RATE_WINDOW)
        return False


def query_gateway(txn):
    try:
        resp = requests.get(
            GATEWAY_URL.format(ref=txn.reference),
            headers={"Authorization": f"Bearer {settings.PAYSTACK_SECRET_KEY}"},
            timeout=GATEWAY_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json().get("data", {})
    except (requests.RequestException, ValueError):
        logger.warning("Gateway verify failed for %s", txn.reference, exc_info=True)
        return None

    gateway_status = data.get("status")
    if gateway_status == "success":
        expected = int(txn.amount * 100)
        if data.get("amount") != expected:
            logger.error(
                "Amount mismatch for %s: expected %s got %s",
                txn.reference, expected, data.get("amount"),
            )
            return "failed"
        return "success"
    if gateway_status in ("failed", "abandoned", "reversed"):
        return "failed"
    return None


def settle_transaction(reference, outcome):
    with transaction.atomic():
        txn = WalletTransaction.objects.select_for_update().get(reference=reference)

        if txn.status != WalletTransaction.Status.PENDING:
            return txn

        if outcome == "success":
            txn.status = WalletTransaction.Status.SUCCESS
            wallet_model = txn.wallet.__class__
            wallet_model.objects.filter(pk=txn.wallet_id).update(
                balance=F("balance") + txn.amount
            )
        else:
            txn.status = WalletTransaction.Status.FAILED

        txn.save(update_fields=["status"])
        logger.info("Settled %s as %s", reference, outcome)
        return txn


def serialize(txn):
    status = txn.status
    return {
        "reference": txn.reference,
        "status": txn.get_status_display(),
        "status_code": status,
        "transaction_type": getattr(txn, "type", None) or getattr(txn, "transaction_type", ""),
        "amount": str(txn.amount),
        "verified": status == WalletTransaction.Status.SUCCESS,
        "is_final": status != WalletTransaction.Status.PENDING,
        "created_at": txn.created_at.isoformat() if hasattr(txn, "created_at") else None,
    }


@require_GET
@never_cache
@json_login_required
def deposit_verify(request, reference):
    if not REFERENCE_RE.match(reference):
        return error("Invalid reference format", 400)

    if is_rate_limited(request.user.pk):
        return error("Too many requests. Slow down.", 429, retry_after=RATE_WINDOW)

    qs = WalletTransaction.objects.select_related("wallet")
    if not request.user.is_staff:
        qs = qs.filter(wallet__rider__user=request.user)

    try:
        txn = qs.get(reference=reference)
    except WalletTransaction.DoesNotExist:
        return error("Transaction not found", 404, reference=reference)

    message = "Transaction found"

    if txn.status == WalletTransaction.Status.PENDING:
        outcome = query_gateway(txn)
        if outcome:
            txn = settle_transaction(reference, outcome)
            message = (
                "Deposit confirmed and wallet credited"
                if txn.status == WalletTransaction.Status.SUCCESS
                else "Deposit failed"
            )
        else:
            message = "Payment still pending"

    payload = {"success": True, "message": message, **serialize(txn)}
    if not payload["is_final"]:
        payload["retry_after"] = POLL_INTERVAL

    return JsonResponse(payload)


MIN_DEPOSIT = Decimal(getattr(settings, "MIN_DEPOSIT", "100"))
MAX_DEPOSIT = Decimal(getattr(settings, "MAX_DEPOSIT", "1000000"))
INIT_LIMIT = 5
INIT_WINDOW = 60
IDEMPOTENCY_TTL = 60 * 30


def throttled(scope, user_id, limit, window):
    key = f"{scope}:{user_id}"
    cache.add(key, 0, window)
    try:
        return cache.incr(key) > limit
    except ValueError:
        cache.set(key, 1, window)
        return False


def parse_amount(raw):
    if raw is None or isinstance(raw, bool):
        raise ValueError("Amount is required")
    try:
        amount = Decimal(str(raw).strip())
    except (InvalidOperation, ValueError):
        raise ValueError("Amount must be a number")
    if not amount.is_finite():
        raise ValueError("Amount must be a number")
    if amount.as_tuple().exponent < -2:
        raise ValueError("Amount cannot have more than 2 decimal places")
    if amount < MIN_DEPOSIT:
        raise ValueError(f"Minimum deposit is {MIN_DEPOSIT}")
    if amount > MAX_DEPOSIT:
        raise ValueError(f"Maximum deposit is {MAX_DEPOSIT}")
    return amount.quantize(Decimal("0.01"))


def generate_reference():
    return f"DEP-{uuid.uuid4().hex[:20].upper()}"


def init_gateway_payment(user, txn, callback_url):
    resp = requests.post(
        GATEWAY_INIT_URL,
        headers={"Authorization": f"Bearer {settings.PAYSTACK_SECRET_KEY}"},
        json={
            "email": user.email,
            "amount": int(txn.amount * 100),
            "reference": txn.reference,
            "callback_url": callback_url,
            "metadata": {"user_id": user.pk, "purpose": "wallet_deposit"},
        },
        timeout=GATEWAY_TIMEOUT,
    )
    resp.raise_for_status()
    body = resp.json()
    if not body.get("status"):
        raise ValueError(body.get("message", "Gateway rejected request"))
    return body["data"]


@require_POST
@never_cache
@json_login_required
def deposit_initiate(request):
    user = request.user

    if not user.email:
        return error("Add an email to your account before depositing", 400)

    if throttled("deposit_init", user.pk, INIT_LIMIT, INIT_WINDOW):
        return error("Too many deposit attempts. Try again shortly.", 429, retry_after=INIT_WINDOW)

    try:
        body = json.loads(request.body or b"{}")
        if not isinstance(body, dict):
            raise ValueError
    except ValueError:
        return error("Invalid JSON body", 400)

    try:
        amount = parse_amount(body.get("amount"))
    except ValueError as exc:
        return error(str(exc), 400)

    idem_header = request.headers.get("Idempotency-Key", "").strip()
    idem_key = f"deposit_idem:{user.pk}:{idem_header[:100]}" if idem_header else None
    if idem_key:
        cached = cache.get(idem_key)
        if cached:
            return JsonResponse({**cached, "idempotent_replay": True})

    try:
        wallet = RiderWallet.objects.get(rider__user=user)
    except RiderWallet.DoesNotExist:
        return error("Wallet not found", 404)

    txn = WalletTransaction.objects.create(
        wallet=wallet,
        transaction_type="deposit",
        amount=amount,
        status=WalletTransaction.Status.PENDING,
        reference=generate_reference(),
    )

    callback_url = getattr(
        settings, "DEPOSIT_CALLBACK_URL", None
    ) or request.build_absolute_uri("/deposit/callback/")

    try:
        data = init_gateway_payment(user, txn, callback_url)
    except (requests.RequestException, ValueError, KeyError):
        logger.exception("Gateway init failed for %s", txn.reference)
        txn.status = WalletTransaction.Status.FAILED
        txn.save(update_fields=["status"])
        return error("Could not start payment. Please try again.", 502)

    payload = {
        "success": True,
        "message": "Deposit initiated",
        "reference": txn.reference,
        "amount": str(txn.amount),
        "status": txn.get_status_display(),
        "authorization_url": data["authorization_url"],
        "access_code": data.get("access_code"),
    }

    if idem_key:
        cache.set(idem_key, payload, IDEMPOTENCY_TTL)

    logger.info("Deposit initiated %s user=%s amount=%s", txn.reference, user.pk, amount)
    return JsonResponse(payload, status=201)


@json_login_required
def deposit_history(request):
    deposits = WalletTransaction.objects.filter(
        wallet__rider__user=request.user,
        transaction_type="deposit"
    )

    status_filter = request.GET.get("status")
    if status_filter:
        deposits = deposits.filter(status=status_filter.lower())

    deposits = deposits.order_by("-created_at")

    page_num = request.GET.get("page", 1)
    paginator = Paginator(deposits, 20)

    try:
        page = paginator.page(page_num)
    except PageNotAnInteger:
        page = paginator.page(1)
