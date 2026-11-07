"""
Rydnow API views.

Sections
1. Constants, response helpers, throttles, decorators
2. Authentication & customer registration
3. Phone verification (OTP)
4. Rider onboarding
5. Customer profile
6. Rider location
7. Rider wallet
8. Payments (Paystack)

Settings used (add to settings.py):
PAYSTACK_SECRET_KEY = os.environ.get("PAYSTACK_SECRET_KEY", "")
PAYSTACK_CALLBACK_URL = ""            # optional
PAYMENT_CURRENCY = "GHS"              # optional
COMMISSION_RATE = ...                 # optional, falls back to models.COMMISSION_RATE
MINIMUM_WALLET_BALANCE = "50.00"      # optional
MIN_WITHDRAWAL_AMOUNT = "10.00"       # optional
SMS_BACKEND = "api.sms.send_sms"      # dotted path to fn(phone: str, message: str)

Model assumptions (rename below if yours differ):
    Rider.user               OneToOne -> User
    Booking.customer         FK -> User
    Booking                  booking_ref, total_fare, payment_ref, payment_status,
                             assigned_rider, confirm_payment_verified()
    RiderWallet.rider        OneToOne/FK -> Rider, .balance, .credit_earnings()
    Transaction              rider, amount, transaction_type, description,
                             created_at, reference (NEW: unique, null=True)
    WithdrawalRequest        rider, amount, status
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
from django.utils import timezone
from django.utils.module_loading import import_string
from rest_framework import status
from rest_framework.authtoken.models import Token
from rest_framework.decorators import (
    api_view,
    authentication_classes,
    parser_classes,
    permission_classes,
    throttle_classes,
)

#from .serializers import CustomerDocumentsSerializer
from rest_framework.pagination import PageNumberPagination
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import AnonRateThrottle, UserRateThrottle
#from .serializers import (
    #CustomerDocumentsSerializer
#)
from .models import Booking, CustomerProfile, RiderDocument, WalletTransaction, WithdrawalRequest
from .rider import Rider, RiderProfile
from .models import (
    COMMISSION_RATE,
    Booking,
    CustomerProfile,
    Rider,
    RiderDocument,
    RiderProfile,
    RiderWallet,
    WalletTransaction,
    WithdrawalRequest,
)
from .serializers import (
    CustomerRegisterSerializer,
    CoordinatesSerializer,
    InitializePaymentSerializer,
    LoginSerializer,
    NearbyRidersSerializer,
    PhoneField,
    PhoneSerializer,
    RiderDocumentSerializer,
    RiderRegistrationSerializer,
    VerifyCodeSerializer,
    VerifyPaymentSerializer,
)


User = get_user_model()
logger = logging.getLogger(__name__)


# ===========================================================================
# 1. CONSTANTS, HELPERS, THROTTLES, DECORATORS
# ===========================================================================
CURRENCY = getattr(settings, "PAYMENT_CURRENCY", "GHS")
MINIMUM_WALLET_BALANCE = Decimal(str(getattr(settings, "MINIMUM_WALLET_BALANCE", "50.00")))
MIN_WITHDRAWAL_AMOUNT = Decimal(str(getattr(settings, "MIN_WITHDRAWAL_AMOUNT", "10.00")))
AMOUNT_TOLERANCE = Decimal("0.01")
TWO_PLACES = Decimal("0.01")

CUSTOMER_EDITABLE_FIELDS = ("full_name", "phone")

LOCATION_STALE_AFTER = timedelta(minutes=5)
MAX_NEARBY_RESULTS = 10
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


def ok(message=None, http_status=status.HTTP_200_OK, **payload):
    """Consistent success envelope: {"success": true, "message"?, ...payload}."""
    body = {"success": True}
    if message:
        body["message"] = message
    body.update(payload)
    return Response(body, status=http_status)


def fail(error, http_status=status.HTTP_400_BAD_REQUEST, errors=None):
    """Consistent error envelope: {"success": false, "error": "...", "errors"?}."""
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


def rider_required(view):
    """Inject the authenticated user's Rider as `rider`, or return 404."""

    @wraps(view)
    def wrapper(request, *args, **kwargs):
        rider = Rider.objects.filter(user=request.user).first()
        if rider is None:
            return fail("Rider profile not found.", status.HTTP_404_NOT_FOUND)
        return view(request, *args, rider=rider, **kwargs)

    return wrapper


def _get_wallet(rider, *, lock=False):
    """Fetch (or create) the rider's wallet. lock=True requires transaction.atomic()."""
    manager = RiderWallet.objects.select_for_update() if lock else RiderWallet.objects
    wallet, _ = manager.get_or_create(rider=rider)
    return wallet


# ===========================================================================
# 2. AUTHENTICATION & CUSTOMER REGISTRATION
# ===========================================================================
@api_view(["POST"])
@authentication_classes([])  # a stale token in the header must not break login
@permission_classes([AllowAny])
@throttle_classes([LoginThrottle])
def user_login(request):
    serializer = LoginSerializer(data=request.data)
    if not serializer.is_valid():
        return validation_fail(serializer)

    identifier = serializer.validated_data["username"].strip()
    if "@" in identifier:
        identifier = identifier.lower()  # accounts are created with lowercase email usernames

    # ModelBackend already rejects inactive users, so no separate is_active check.
    user = authenticate(
        request=request,
        username=identifier,
        password=serializer.validated_data["password"],
    )
    if user is None:
        return fail("Invalid credentials.", status.HTTP_401_UNAUTHORIZED)

    update_last_login(None, user)
    token, _ = Token.objects.get_or_create(user=user)
    rider = Rider.objects.filter(user=user).first()

    user_payload = {
        "id": user.id,
        "username": user.username,
        "email": user.email,
        "role": "rider" if rider else "customer",
    }
    if rider:
        # Pending riders may log in (they must upload documents) but the app
        # should gate going online on this value.
        user_payload["verification_status"] = rider.verification_status

    return ok(token=token.key, user=user_payload)


import logging
from django.db import IntegrityError
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes, throttle_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.throttling import AnonRateThrottle

from .serializers import CustomerRegisterSerializer

logger = logging.getLogger(__name__)


class RegisterRateThrottle(AnonRateThrottle):
    scope = 'register'


@api_view(['POST'])
@permission_classes([AllowAny])
@throttle_classes([RegisterRateThrottle])
def customer_register(request):
    """Register a new customer account."""
    serializer = CustomerRegisterSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)

    try:
        user = serializer.save()
    except IntegrityError:
        return Response(
            {'error': 'An account with these details already exists.'},
            status=status.HTTP_409_CONFLICT,
        )
    except Exception:
        logger.exception('Customer registration failed')
        return Response(
            {'error': 'Something went wrong. Please try again.'},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )

    return Response(
        {
            'message': 'Account created successfully',
            'user_id': user.id,
            'email': user.email,
        },
        status=status.HTTP_201_CREATED,
    )


# ===========================================================================
# 3. PHONE VERIFICATION (OTP)
# Requires a shared cache (Redis) in production: LocMemCache is per-process.
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

    # cache.add is atomic: only one request per phone passes the cooldown gate.
    cooldown_key = f"otp:cooldown:{phone}"
    if not cache.add(cooldown_key, 1, OTP_COOLDOWN_SECONDS):
        return fail("Please wait before requesting another code.", status.HTTP_429_TOO_MANY_REQUESTS)

    code = f"{secrets.randbelow(10 ** OTP_LENGTH):0{OTP_LENGTH}d}"
    message = f"Your Rydnow verification code is {code}. It expires in {OTP_TTL_SECONDS // 60} minutes."

    if not _send_sms(phone, message):
        cache.delete(cooldown_key)
        return fail("Verification service is unavailable. Please try again.", status.HTTP_503_SERVICE_UNAVAILABLE)

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
        cache.set(key, entry, remaining)  # keep the original expiry
        return fail("Invalid verification code.", status.HTTP_400_BAD_REQUEST)

    cache.delete(key)
    # Persist the verified state here (e.g. profile.phone_verified = True)
    # once that field exists on your models.
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
    Creates User -> Rider -> RiderProfile -> RiderWallet atomically.
    A failure at any step rolls everything back (no orphan records).
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
            rider = Rider.objects.create(
                user=user,
                phone=data["phone"],
                location=data.get("location", ""),
                verification_status="pending",
                is_restricted=False,
            )
            RiderProfile.objects.create(
                rider=rider,
                full_name=data["full_name"],
                email=email,
                phone=data["phone"],
                motorbike_model=data.get("motorbike_model", ""),
                license_number=data.get("license_number", ""),
                is_online=False,
            )
            RiderWallet.objects.create(rider=rider)
    except IntegrityError:
        return fail("An account with this email or phone already exists.", status.HTTP_409_CONFLICT)

    logger.info("Rider registered: rider_id=%s", rider.id)
    return ok(
        "Registration submitted. Upload your documents to complete verification.",
        status.HTTP_201_CREATED,
        rider_id=rider.id,
        email=user.email,
        phone=rider.phone,
        verification_status=rider.verification_status,
        requires_approval=True,
    )

    # ✅ CREATE AUTH TOKEN for document uploads
    token, _ = Token.objects.get_or_create(user=user)  # ← ADD THIS LINE

    logger.info("Rider registered: rider_id=%s", rider.id)
    return ok(
        "Registration submitted. Upload your documents to complete verification.",
        status=status.HTTP_201_CREATED,
        rider_id=rider.id,
        email=user.email,
        phone=rider.phone,
        verification_status=rider.verification_status,
        requires_approval=True,
        token=token.key,  # ← ADD THIS — send token to frontend
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
    except ValueError as exc:  # raised by the model for business-rule violations
        return fail(str(exc), status.HTTP_400_BAD_REQUEST)
    except IntegrityError:
        return fail("This document type has already been submitted.", status.HTTP_409_CONFLICT)

    return ok(
        "Document uploaded successfully.",
        status.HTTP_201_CREATED,
        id=document.id,
        document_type=document.document_type,
        status=document.status,
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
        return fail("Customer profile not found. Please complete registration.", status.HTTP_404_NOT_FOUND)

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
# 6. RIDER LOCATION
# ===========================================================================
@api_view(["PATCH"])
@permission_classes([IsAuthenticated])
@throttle_classes([LocationThrottle])
@rider_required
def update_rider_location(request, rider):
    serializer = CoordinatesSerializer(data=request.data)
    if not serializer.is_valid():
        return validation_fail(serializer)

    rider.current_lat = serializer.validated_data["latitude"]
    rider.current_lng = serializer.validated_data["longitude"]
    rider.last_location_update = timezone.now()
    rider.save(update_fields=["current_lat", "current_lng", "last_location_update"])

    return ok(
        "Location updated successfully.",
        data={
            "latitude": rider.current_lat,
            "longitude": rider.current_lng,
            "updated_at": rider.last_location_update,
        },
    )


@api_view(["POST"])
@permission_classes([IsAuthenticated])  # rider locations are private: no anonymous access
def get_nearby_riders(request):
    serializer = NearbyRidersSerializer(data=request.data)
    if not serializer.is_valid():
        return validation_fail(serializer)

    lat = serializer.validated_data["latitude"]
    lng = serializer.validated_data["longitude"]
    service_type = serializer.validated_data["service_type"]
    radius_km = serializer.validated_data["radius_km"]

    # Cheap bounding-box prefilter in SQL; exact distance is computed only for
    # the few riders inside the box (instead of every online rider).
    lat_delta = radius_km / 111.0
    lng_delta = radius_km / (111.0 * max(math.cos(math.radians(lat)), 0.01))
    fresh_after = timezone.now() - LOCATION_STALE_AFTER

    candidates = (
        Rider.objects.filter(
            is_online=True,
            verification_status="approved",
            is_restricted=False,
            current_lat__range=(lat - lat_delta, lat + lat_delta),
            current_lng__range=(lng - lng_delta, lng + lng_delta),
            last_location_update__gte=fresh_after,
            services__key=service_type,
            services__is_active=True,
        )
        .distinct()[:MAX_NEARBY_CANDIDATES]
    )

    results = []
    for rider in candidates:
        distance_km = rider.get_distance_km(lat, lng)
        if distance_km is None or distance_km > radius_km:
            continue
        results.append(
            {
                "id": rider.id,
                "distance_km": distance_km,
                "latitude": rider.current_lat,
                "longitude": rider.current_lng,
                "last_updated": rider.last_location_update,
            }
        )

    results.sort(key=lambda r: r["distance_km"])
    results = results[:MAX_NEARBY_RESULTS]
    return ok(count=len(results), service_type=service_type, riders=results)


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
    wallet = _get_wallet(rider)
    recent = Transaction.objects.filter(rider=rider).order_by("-created_at")[:10]
    return ok(
        balance=money(wallet.balance),
        minimum_required=money(MINIMUM_WALLET_BALANCE),
        can_go_online=rider.can_go_online,
        transactions=[_serialize_transaction(t) for t in recent],
    )


@api_view(["GET"])
@permission_classes([IsAuthenticated])
@rider_required
def rider_transactions(request, rider):
    """Paginated JSON history (?page=2). Replaces the old HTML template view."""
    queryset = Transaction.objects.filter(rider=rider).order_by("-created_at")
    paginator = PageNumberPagination()
    page = paginator.paginate_queryset(queryset, request)
    body = paginator.get_paginated_response([_serialize_transaction(t) for t in page]).data
    return ok(**body)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
@rider_required
def wallet_deposit(request, rider):
    """
    Starts a wallet top-up. The wallet is NEVER credited here: funds are added
    only after Paystack confirms payment (verify_payment or the webhook).
    """
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

    if rider.verification_status != "approved":
        return fail("Only verified riders can withdraw.", status.HTTP_403_FORBIDDEN)
    if amount < MIN_WITHDRAWAL_AMOUNT:
        return fail(f"Minimum withdrawal is {money(MIN_WITHDRAWAL_AMOUNT)}.")

    with transaction.atomic():
        wallet = _get_wallet(rider, lock=True)  # row lock prevents double-spend
        if wallet.balance < amount:
            return fail("Insufficient wallet balance.")

        # Funds are held immediately; refund the wallet if payout is rejected.
        wallet.balance -= amount
        wallet.save()
        withdrawal = WithdrawalRequest.objects.create(rider=rider, amount=amount, status="pending")
        Transaction.objects.create(
            rider=rider,
            amount=amount,
            transaction_type="withdrawal",
            description=f"Withdrawal request — ₵{money(amount)}",
        )

    logger.info("Withdrawal requested: rider_id=%s amount=%s", rider.id, amount)
    return ok(
        "Withdrawal request submitted.",
        status.HTTP_201_CREATED,
        withdrawal_id=withdrawal.id,
        new_balance=money(wallet.balance),
    )


# ===========================================================================
# 8. PAYMENTS (PAYSTACK)
# Flow: initialize_payment/wallet_deposit -> user pays on Paystack ->
#       verify_payment (client) and/or paystack_webhook (server) -> settle once.
# Settlement is idempotent, so both paths can safely fire for one payment.
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
            "Could not reach the payment service. Please try again.", status.HTTP_503_SERVICE_UNAVAILABLE
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
        "amount": int((Decimal(amount) * 100).to_integral_value(ROUND_HALF_UP)),  # pesewas
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
    """Server-to-server verification. Never trust client-supplied amounts."""
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
    """
    Verify a Paystack reference and apply it exactly once.
    requesting_user=None means a trusted server caller (the webhook); otherwise
    the reference must belong to that user.
    """
    paystack = _paystack_verify(reference)
    metadata = paystack.get("metadata")
    metadata = metadata if isinstance(metadata, dict) else {}

    try:
        if metadata.get("purpose") == PURPOSE_DEPOSIT:
            return _settle_wallet_deposit(reference, paystack, metadata, requesting_user)
        return _settle_booking_payment(reference, paystack, requesting_user)
    except IntegrityError:
        raise PaymentError(
            "Payment recording conflict. Reference may already exist.", status.HTTP_409_CONFLICT
        ) from None


def _settle_booking_payment(reference, paystack, requesting_user):
    verified_amount = _paystack_amount(paystack)
    not_found = PaymentError("No booking found for this payment reference.", status.HTTP_404_NOT_FOUND)

    with transaction.atomic():  # any PaymentError raised inside rolls everything back
        try:
            booking = Booking.objects.select_for_update().get(payment_ref=reference)
        except Booking.DoesNotExist:
            raise not_found from None
        if requesting_user is not None and booking.customer_id != requesting_user.id:
            raise not_found

        booking_amount = Decimal(str(booking.total_fare))

        # Idempotency: already settled?
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

        wallet = _get_wallet(rider, lock=True)
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
    not_found = PaymentError("Deposit could not be matched to a rider account.", status.HTTP_404_NOT_FOUND)

    with transaction.atomic():
        try:
            rider = Rider.objects.get(pk=metadata.get("rider_id"))
        except (Rider.DoesNotExist, ValueError, TypeError):
            raise not_found from None
        if requesting_user is not None and rider.user_id != requesting_user.id:
            raise not_found

        wallet = _get_wallet(rider, lock=True)  # serialises concurrent settlements
        _, created = Transaction.objects.get_or_create(
            reference=reference,  # unique: a payment can only ever credit once
            defaults={
                "rider": rider,
                "amount": amount,
                "transaction_type": "deposit",
                "description": f"Wallet deposit — ₵{money(amount)}",
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
        "can_go_online": rider.can_go_online,
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
        rider = Rider.objects.filter(user=request.user).first()
        if rider is None:
            return fail("Only riders can fund a wallet.", status.HTTP_403_FORBIDDEN)
        return _start_wallet_deposit(request.user, rider, data["amount"])

    # Booking payment: the amount always comes from the server, never the client.
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
        result = _settle_reference(serializer.validated_data["reference"], requesting_user=request.user)
    except PaymentError as exc:
        return fail(exc.message, exc.status_code)

    created = result.pop("created")
    message = result.pop("message")
    return ok(message, status.HTTP_201_CREATED if created else status.HTTP_200_OK, **result)


@api_view(["POST"])
@authentication_classes([])
@permission_classes([AllowAny])
@throttle_classes([])
def paystack_webhook(request):
    """
    Server-to-server notification from Paystack. Settles payments even when the
    user closes the app before verify_payment is called.
    """
    secret = getattr(settings, "PAYSTACK_SECRET_KEY", "")
    raw_body = request.body  # read BEFORE touching request.data
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
                # Non-retryable (unknown reference, no rider yet, ...): acknowledge so
                # Paystack stops retrying, and log it for reconciliation.
                logger.warning("Webhook payment not settled ref=%s: %s", reference, exc.message)

    return Response(status=status.HTTP_200_OK)

