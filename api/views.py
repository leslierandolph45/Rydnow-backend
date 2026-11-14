"""
Rydnow API views (audited + hardened).

Sections
    1. Constants, response helpers, throttles, decorators
    2. Authentication & customer registration
    3. Phone verification (OTP)
    4. Rider onboarding
    5. Customer profile & customer wallet
    6. Rider location & nearby-rider search
    7. Rider wallet, withdrawals, transaction history
    8. Payments (Paystack)
    9. "Who am I" profile endpoint (formerly profile_views.py)

Money rules
    * A wallet is NEVER credited by a client-supplied amount; only after Paystack verifies it.
    * Booking payments only mark the booking paid (escrow "held"). The rider is credited
      by Booking.complete_booking(), never here.
"""
import csv
import hashlib
import hmac
import json
import logging
import math
import os
import re
import secrets
import time
import uuid
from datetime import datetime, timedelta
from datetime import time as dtime
from decimal import ROUND_HALF_UP, Decimal
from functools import wraps
from urllib.parse import quote

import requests
from django.conf import settings
from django.contrib.auth import authenticate, get_user_model
from django.contrib.auth.models import update_last_login
from django.core.cache import cache
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import IntegrityError, transaction
from django.db.models import Count, DecimalField, Q, Sum, Value
from django.db.models.functions import Abs, Coalesce, TruncMonth
from django.http import StreamingHttpResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.utils.module_loading import import_string
from rest_framework import generics, serializers, status
from rest_framework.authtoken.models import Token
from rest_framework.decorators import (
    api_view,
    authentication_classes,
    parser_classes,
    permission_classes,
    throttle_classes,
)
from rest_framework.pagination import PageNumberPagination
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import AnonRateThrottle, UserRateThrottle

from .models import (
    Booking,
    BookingOffer,
    CustomerProfile,
    CustomerWallet,
    Payment,
    RiderDocument,
    RiderPayoutAccount,
    RiderProfile,
    RiderWallet,
    Service,
    ServiceQualification,
    SystemAlert,
    SystemConfig,
    VerificationStatus,
    WalletTransaction,
    WebhookEvent,
    WithdrawalRequest,
    haversine_km,
)
from .serializers import (
    REFERENCE_RE,
    AmountSerializer,
    CustomerProfileSerializer,
    InitializePaymentSerializer,
    LoginSerializer,
    NearbyRidersQuerySerializer,
    PhoneSerializer,
    RegistrationSerializer,
    RiderDocumentSerializer,
    RiderLocationSerializer,
    RiderProfileSerializer,
    RiderRegistrationSerializer,
    TransactionQuerySerializer,
    TransactionSerializer,
    VerifyCodeSerializer,
    VerifyPaymentSerializer,
    WithdrawalRequestSerializer,
)
from .validators import clean_phone, email_is_taken, split_name


User = get_user_model()
logger = logging.getLogger(__name__)


CUSTOMER_EDITABLE_FIELDS = ('full_name', 'phone')


# ===========================================================================
# 1. CONSTANTS, HELPERS, THROTTLES, DECORATORS
# ===========================================================================
CURRENCY = getattr(settings, "PAYMENT_CURRENCY", "GHS")
MIN_DEPOSIT = Decimal(str(getattr(settings, "MIN_DEPOSIT", "10.00")))
MAX_DEPOSIT = Decimal(str(getattr(settings, "MAX_DEPOSIT", "100000.00")))
AMOUNT_TOLERANCE = Decimal("0.01")
TWO_PLACES = Decimal("0.01")

CUSTOMER_EDITABLE_FIELDS = ("full_name", "phone")

KM_PER_DEGREE = 111.0
MAX_NEARBY_CANDIDATES = 200

PAYSTACK_BASE_URL = "https://api.paystack.co"
PAYSTACK_TIMEOUT = (5, 15)  # (connect, read) seconds
PURPOSE_BOOKING = "booking"
PURPOSE_DEPOSIT = "wallet_deposit"
PAID_STATUSES = ("held", "released")
GATEWAY_PENDING_STATES = ("ongoing", "pending", "processing", "queued")
CHANNEL_TO_METHOD = {"mobile_money": "momo", "card": "visa", "bank": "debit"}
POLL_INTERVAL = 5  # seconds; hint for clients polling a pending payment
REFERENCE_PATTERN = re.compile(REFERENCE_RE)

OTP_LENGTH = 6
OTP_TTL_SECONDS = 600
OTP_COOLDOWN_SECONDS = 60
OTP_MAX_ATTEMPTS = 5
OTP_MAX_SENDS_PER_HOUR = 5
PHONE_VERIFIED_TTL_SECONDS = 1800

LOW_BALANCE_FALLBACK = Decimal("50.00")


def ok(message=None, status_code=status.HTTP_200_OK, **kwargs):
    data = {"success": True}
    if message:
        data["message"] = message
    data.update(kwargs)
    return Response(data, status=status_code)


def fail(message, status_code=status.HTTP_400_BAD_REQUEST):
    return Response({"success": False, "error": message}, status=status_code)


def _flatten_errors(errors):
    """Yield plain strings from any nesting of DRF error dicts/lists."""
    if isinstance(errors, dict):
        for value in errors.values():
            yield from _flatten_errors(value)
    elif isinstance(errors, (list, tuple)):
        for item in errors:
            yield from _flatten_errors(item)
    else:
        yield str(errors)


def validation_fail(serializer):
    """First message per field. Safe for nested/non-field errors (never raises)."""
    messages = []
    for value in serializer.errors.values():
        first = next(_flatten_errors(value), None)
        if first:
            messages.append(first)
    return fail(" ".join(messages) or "Invalid request.")


def money(value):
    """Format money as a fixed 2-decimal string (never floats for currency)."""
    return str(Decimal(str(value)).quantize(TWO_PLACES, rounding=ROUND_HALF_UP))


def min_wallet_balance():
    """Single source of truth, shared with RiderProfile.has_minimum_deposit."""
    return SystemConfig.get_decimal("MIN_RIDER_BOOKING_BALANCE", str(LOW_BALANCE_FALLBACK))


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
    rate = "60/min"


class PaymentThrottle(UserRateThrottle):
    scope = "payment"
    rate = "20/min"


class ProfileThrottle(UserRateThrottle):
    scope = "profile"
    rate = "60/min"


def rider_required(view):
    """Inject the authenticated user's RiderProfile as `rider`, or return 404.

    The wrapped view MUST accept a `rider` argument.
    """
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        rider = RiderProfile.objects.filter(user=request.user, deleted_at__isnull=True).first()
        if rider is None:
            return fail("Rider profile not found.", status.HTTP_404_NOT_FOUND)
        return view(request, *args, rider=rider, **kwargs)
    return wrapper


def _get_wallet(rider, *, lock=False):
    """Fetch (or create) a rider's wallet. lock=True must run inside transaction.atomic()."""
    wallet, _ = RiderWallet.objects.get_or_create(rider=rider)
    if lock:
        wallet = RiderWallet.objects.select_for_update().get(pk=wallet.pk)
    return wallet


def _can_go_online(rider, wallet):
    return rider.can_accept_jobs and wallet.balance >= min_wallet_balance()


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

    user = authenticate(
        request=request,
        username=identifier,
        password=serializer.validated_data["password"],
    )
    if user is None:
        return fail("Invalid credentials.", status.HTTP_401_UNAUTHORIZED)

    rider = RiderProfile.objects.filter(user=user, deleted_at__isnull=True).first()
    customer = None if rider else CustomerProfile.objects.filter(user=user).first()
    if customer is not None and customer.is_blocked:
        return fail("This account has been blocked. Contact support.", status.HTTP_403_FORBIDDEN)

    update_last_login(None, user)
    token, _ = Token.objects.get_or_create(user=user)

    user_payload = {
        "id": user.id,
        "username": user.username,
        "email": user.email,
        "role": "rider" if rider else "customer",
    }
    if rider:
        user_payload["is_verified"] = rider.is_verified
        user_payload["verification_status"] = rider.verification_status

    return ok(token=token.key, user=user_payload)


@api_view(["POST"])
@authentication_classes([])
@permission_classes([AllowAny])
@throttle_classes([RegistrationThrottle])
def customer_register(request):
    """Register a customer: User + CustomerProfile, atomically."""
    serializer = RegistrationSerializer(data=request.data)
    if not serializer.is_valid():
        return validation_fail(serializer)

    phone = serializer.validated_data["phone"]
    if not _phone_verification_ok(phone):
        return fail("Verify your phone number first.", status.HTTP_403_FORBIDDEN)

    try:
        user = serializer.save()
    except IntegrityError:
        return fail("An account with these details already exists.", status.HTTP_409_CONFLICT)

    _consume_phone_verification(phone)
    logger.info("Customer registered: user_id=%s", user.id)
    return ok("Account created successfully.", status.HTTP_201_CREATED, user_id=user.id, email=user.email)


# ===========================================================================
# 3. PHONE VERIFICATION (OTP)
# Requires a shared cache (Redis) in production: LocMemCache is per-process.
# ===========================================================================
def _otp_hash(phone, code):
    return hmac.new(
        settings.SECRET_KEY.encode(), f"{phone}:{code}".encode(), hashlib.sha256
    ).hexdigest()


def _verified_key(phone):
    return f"otp:verified:{phone}"


def _phone_verification_ok(phone):
    """True when verification is not enforced, or this phone passed an OTP check recently."""
    if not getattr(settings, "REQUIRE_PHONE_VERIFICATION", False):
        return True
    return bool(cache.get(_verified_key(phone)))


def _consume_phone_verification(phone):
    cache.delete(_verified_key(phone))


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
        cache.set(key, entry, remaining)  # keep the original expiry
        return fail("Invalid verification code.", status.HTTP_400_BAD_REQUEST)

    cache.delete(key)
    cache.set(_verified_key(phone), True, PHONE_VERIFIED_TTL_SECONDS)
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
    Create User -> RiderProfile -> RiderWallet atomically (inside the serializer).
    Returns an auth token so the client can upload documents immediately.
    """
    serializer = RiderRegistrationSerializer(data=request.data)
    if not serializer.is_valid():
        return validation_fail(serializer)

    phone = serializer.validated_data["phone"]
    if not _phone_verification_ok(phone):
        return fail("Verify your phone number first.", status.HTTP_403_FORBIDDEN)

    try:
        with transaction.atomic():
            rider = serializer.save()
            token, _ = Token.objects.get_or_create(user=rider.user)
    except IntegrityError:
        return fail("An account with this email or phone already exists.", status.HTTP_409_CONFLICT)

    _consume_phone_verification(phone)
    logger.info("Rider registered: rider_id=%s", rider.id)
    return ok(
        "Registration submitted. Upload your documents to complete verification.",
        status.HTTP_201_CREATED,
        rider_id=str(rider.id),
        email=rider.user.email,
        phone=rider.phone,
        is_verified=rider.is_verified,
        verification_status=rider.verification_status,
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
            RiderProfile.objects.select_related("user"),
            user=self.request.user, deleted_at__isnull=True,
        )


@api_view(["POST"])
@parser_classes([MultiPartParser, FormParser])
@permission_classes([IsAuthenticated])
@rider_required
def rider_submit_documents(request, rider):
    """
    Upload (or re-upload) one document. When every required document is present the
    rider is moved to 'under review' automatically.
    """
    serializer = RiderDocumentSerializer(data=request.data)
    if not serializer.is_valid():
        return validation_fail(serializer)
    data = serializer.validated_data
    doc_type = data["document_type"]

    if rider.verification_status in (VerificationStatus.SUSPENDED, VerificationStatus.UNDER_REVIEW):
        return fail(
            "Documents cannot be changed while your account is under review or suspended.",
            status.HTTP_409_CONFLICT,
        )

    try:
        with transaction.atomic():
            approved = rider.documents.filter(
                document_type=doc_type, status=RiderDocument.DocumentStatus.APPROVED
            ).exists()
            if approved:
                return fail("This document has already been approved.", status.HTTP_409_CONFLICT)

            # A fresh upload replaces any earlier unapproved copy of the same type.
            rider.documents.filter(document_type=doc_type).exclude(
                status=RiderDocument.DocumentStatus.APPROVED
            ).delete()
            document = RiderDocument.objects.create(
                rider_profile=rider,
                document_type=doc_type,
                file=data["document_file"],
                expiry_date=data.get("expiry_date"),
            )
    except IntegrityError:
        return fail("This document type has already been submitted.", status.HTTP_409_CONFLICT)
    except Exception:
        # Storage errors (MEDIA_ROOT, S3/Cloudinary credentials, disk) land here.
        logger.exception("Document save failed rider_id=%s type=%s", rider.id, doc_type)
        return fail(
            "We couldn't save your file. Please try again in a moment.",
            status.HTTP_500_INTERNAL_SERVER_ERROR,
        )

    submitted = False
    if rider.documents_uploaded and rider.verification_status in (
        VerificationStatus.PENDING, VerificationStatus.REJECTED
    ):
        try:
            rider.submit_for_review()
            submitted = True
        except DjangoValidationError as exc:
            logger.warning("Auto-submit failed rider_id=%s: %s", rider.id, exc.messages)
        except Exception:
            logger.exception("Auto-submit crashed rider_id=%s", rider.id)

    return ok(
        "Document uploaded successfully.",
        status.HTTP_201_CREATED,
        id=str(document.id),
        document_type=document.document_type,
        document_status=document.status,
        documents_uploaded=rider.documents_uploaded,
        submitted_for_review=submitted,
    )


def _stage(rider):
    """
    One value the frontend can trust.
    Returns: suspended | approved | under_review | rejected | not_started
    """
    if rider.verification_status == VerificationStatus.SUSPENDED:
        return "suspended"
    if rider.is_verified:
        return "approved"
    if rider.verification_status == VerificationStatus.UNDER_REVIEW:
        return "under_review"
    if rider.verification_status == VerificationStatus.REJECTED:
        return "rejected"
    return "not_started"


def _doc_state(document):
    """Frontend-friendly status for one document."""
    if document is None:
        return "missing"
    if document.status == RiderDocument.DocumentStatus.APPROVED:
        return "approved"
    if document.status == RiderDocument.DocumentStatus.REJECTED:
        return "rejected"
    return "uploaded"


def _file_url(field_file):
    """URL of a stored file, or None. A storage problem must never break the endpoint."""
    try:
        return field_file.url if field_file and field_file.name else None
    except Exception:  # noqa: BLE001
        logger.warning("File URL unavailable", exc_info=True)
        return None


@api_view(["GET"])
@permission_classes([IsAuthenticated])
@throttle_classes([WalletReadThrottle])
@rider_required
def rider_verification_status(request, rider):
    """Current review state + per-document state, for the rider-verify page."""
    latest = {}
    for doc in rider.documents.all().order_by("uploaded_at"):
        latest[doc.document_type] = doc  # newest wins

    documents = []
    for value, label in RiderDocument.DocumentType.choices:
        doc = latest.get(value)
        documents.append({
            "type": value,
            "label": label,
            # RiderDocument has no `name` field; derive it from the stored file.
            "name": os.path.basename(doc.file.name) if doc and doc.file else None,
            "status": _doc_state(doc),
            "url": _file_url(doc.file) if doc else None,
            "notes": (doc.notes or "") if doc and doc.status == RiderDocument.DocumentStatus.REJECTED else "",
        })

    return ok(
        stage=_stage(rider),
        documents=documents,
        rejection_reason=rider.rejection_reason or "",
    )


# ===========================================================================
# 5. CUSTOMER PROFILE & CUSTOMER WALLET
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
        return ok(data=CustomerProfileSerializer(profile, context={"request": request}).data)

    update_data = {f: request.data[f] for f in CUSTOMER_EDITABLE_FIELDS if f in request.data}
    if not update_data:
        return fail(f"No valid fields provided. You can update: {', '.join(CUSTOMER_EDITABLE_FIELDS)}.")

    serializer = CustomerProfileSerializer(
        profile, data=update_data, partial=True, context={"request": request}
    )
    if not serializer.is_valid():
        return validation_fail(serializer)
    serializer.save()
    return ok("Profile updated successfully.", data=serializer.data)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
@throttle_classes([WalletReadThrottle])
def customer_wallet(request):
    """Customer wallet balance + last 20 ledger lines."""
    if not CustomerProfile.objects.filter(user=request.user).exists():
        return fail("Customer profile not found.", status.HTTP_404_NOT_FOUND)

    wallet, _ = CustomerWallet.objects.get_or_create(user=request.user)
    transactions = (
        WalletTransaction.objects.filter(customer_wallet=wallet).order_by("-created_at")[:20]
    )
    return ok(
        balance=money(wallet.balance),
        withdrawable_balance=money(wallet.withdrawable_balance),
        bonus_credit=money(wallet.bonus_credit),
        currency=wallet.currency,
        transactions=TransactionSerializer(transactions, many=True).data,
    )


# ===========================================================================
# 6. RIDER LOCATION & NEARBY SEARCH
# ===========================================================================
@api_view(["PATCH"])
@permission_classes([IsAuthenticated])
@throttle_classes([LocationThrottle])
@rider_required
def update_rider_location(request, rider):
    """Update the authenticated rider's current location (hot path: one UPDATE)."""
    if not rider.is_active:
        return fail("Rider account is inactive.", status.HTTP_403_FORBIDDEN)

    serializer = RiderLocationSerializer(data=request.data)
    if not serializer.is_valid():
        return validation_fail(serializer)

    rider.update_location(serializer.validated_data["latitude"], serializer.validated_data["longitude"])
    return ok(
        "Location updated successfully.",
        latitude=rider.current_lat,
        longitude=rider.current_lng,
        updated_at=rider.location_updated_at,
    )


@api_view(["GET"])
@permission_classes([IsAuthenticated])
@throttle_classes([LocationThrottle])
def get_nearby_riders(request):
    """
    Riders who can take a job right now, near the given point, for the given service.

    NOTE: this used to be wrapped in @rider_required but did not accept the `rider`
    argument it injects, so EVERY call crashed with a TypeError (HTTP 500). It is also
    the customer-facing "riders near me" search, so any authenticated user may call it.
    """
    query = NearbyRidersQuerySerializer(data=request.query_params)
    if not query.is_valid():
        return validation_fail(query)
    data = query.validated_data

    lat, lng = data["latitude"], data["longitude"]
    radius_km, limit = data["radius_km"], data["limit"]
    service = get_object_or_404(Service, pk=data["service"], is_active=True)

    # Cheap bounding box in SQL; exact distance only for the survivors.
    lat_delta = radius_km / KM_PER_DEGREE
    lng_delta = radius_km / (KM_PER_DEGREE * max(math.cos(math.radians(lat)), 0.01))

    candidates = (
        RiderProfile.objects.dispatchable(service=service)
        .filter(
            wallet__balance__gte=min_wallet_balance(),
            current_lat__range=(lat - lat_delta, lat + lat_delta),
            current_lng__range=(lng - lng_delta, lng + lng_delta),
        )
        .exclude(user=request.user)[:MAX_NEARBY_CANDIDATES]
    )

    riders = []
    for rider in candidates:
        distance = haversine_km(lat, lng, rider.current_lat, rider.current_lng)
        if distance is not None and distance <= radius_km:
            riders.append({
                "id": str(rider.pk),
                "full_name": rider.full_name,
                "latitude": rider.current_lat,
                "longitude": rider.current_lng,
                "distance_km": distance,
                "rating_average": rider.rating_average,
                "location_updated_at": rider.location_updated_at,
            })
    riders.sort(key=lambda r: r["distance_km"])
    riders = riders[:limit]

    return ok(service=service.name, count=len(riders), riders=riders)


# ===========================================================================
# 7. RIDER WALLET, WITHDRAWALS, TRANSACTION HISTORY
# ===========================================================================
ZERO = Value(Decimal("0.00"), output_field=DecimalField(max_digits=14, decimal_places=2))
MAX_EXPORT_ROWS = 5000


class TransactionPagination(PageNumberPagination):
    page_size = 20
    page_size_query_param = "page_size"
    max_page_size = 100


def _sum(condition):
    return Coalesce(
        Sum("amount", filter=condition), ZERO,
        output_field=DecimalField(max_digits=14, decimal_places=2),
    )


@api_view(["GET"])
@permission_classes([IsAuthenticated])
@rider_required
def get_rider_wallet(request, rider):
    wallet = _get_wallet(rider)
    recent = WalletTransaction.objects.filter(rider_wallet=wallet).order_by("-created_at")[:10]

    minimum = min_wallet_balance()
    can_go_online = _can_go_online(rider, wallet)

    return ok(
        balance=money(wallet.balance),
        currency=wallet.currency,
        minimum_required=money(minimum),
        can_go_online=can_go_online,
        transactions=TransactionSerializer(recent, many=True).data,
        message=None if can_go_online else f"Please deposit at least GH₵{money(minimum)} to start accepting bookings",
    )


@api_view(["POST"])
@permission_classes([IsAuthenticated])
@rider_required
def accept_offer(request, rider, offer_id):
    minimum = min_wallet_balance()
    wallet = _get_wallet(rider)

    if wallet.balance < minimum:
        return fail(
            f"Please deposit at least GH₵{money(minimum)} to start accepting bookings",
            status.HTTP_403_FORBIDDEN,
        )

    offer = get_object_or_404(BookingOffer, pk=offer_id, rider=rider)
    done, msg = offer.accept()
    return ok(msg) if done else fail(msg, status.HTTP_409_CONFLICT)


def _apply_filters(qs, f):
    tz = timezone.get_current_timezone()
    qs = qs.annotate(abs_amount=Abs("amount"))
    if "type" in f:
        qs = qs.filter(transaction_type=f["type"])
    if "status" in f:
        qs = qs.filter(status=f["status"])
    if f.get("direction") == "credit":
        qs = qs.filter(amount__gt=0)
    elif f.get("direction") == "debit":
        qs = qs.filter(amount__lt=0)
    if "date_from" in f:
        qs = qs.filter(created_at__gte=timezone.make_aware(datetime.combine(f["date_from"], dtime.min), tz))
    if "date_to" in f:
        end = datetime.combine(f["date_to"] + timedelta(days=1), dtime.min)
        qs = qs.filter(created_at__lt=timezone.make_aware(end, tz))
    if "min_amount" in f:
        qs = qs.filter(abs_amount__gte=f["min_amount"])
    if "max_amount" in f:
        qs = qs.filter(abs_amount__lte=f["max_amount"])
    if f.get("search"):
        term = f["search"].strip()
        qs = qs.filter(Q(reference__icontains=term) | Q(description__icontains=term))
    # Amount ordering is by size, regardless of credit/debit sign.
    return qs.order_by(f["ordering"].replace("amount", "abs_amount"), "-id")


def _summary(qs):
    agg = qs.filter(status="completed").aggregate(
        total_in=_sum(Q(amount__gt=0)),
        total_out=_sum(Q(amount__lt=0)),
        count=Count("id"),
    )
    total_in, total_out = agg["total_in"], abs(agg["total_out"])
    return {
        "total_in": money(total_in),
        "total_out": money(total_out),
        "net": money(total_in - total_out),
        "count": agg["count"],
    }


def _months_ago_start(months):
    now = timezone.localtime()
    year, month = now.year, now.month - (months - 1)
    while month <= 0:
        month += 12
        year -= 1
    return now.replace(year=year, month=month, day=1, hour=0, minute=0, second=0, microsecond=0)


def _monthly_trend(wallet, months=6):
    rows = (
        wallet.transactions.filter(status="completed", created_at__gte=_months_ago_start(months))
        .annotate(month=TruncMonth("created_at"))
        .values("month")
        .annotate(money_in=_sum(Q(amount__gt=0)), money_out=_sum(Q(amount__lt=0)))
        .order_by("month")
    )
    return [
        {"month": r["month"].strftime("%Y-%m"), "money_in": money(r["money_in"]),
         "money_out": money(abs(r["money_out"]))}
        for r in rows
    ]


def _top_up_nudge(wallet):
    minimum = min_wallet_balance()
    low = wallet.balance < minimum
    return {
        "low_balance": low,
        "message": (f"Your balance is below the GH₵{money(minimum)} needed to receive jobs. "
                    "Top up to go online.") if low else None,
    }


def _csv_safe(value):
    """Neutralise spreadsheet formula injection in free-text columns."""
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
            "Date", "Reference", "Type", "Direction", "Status",
            f"Amount ({currency})", f"Balance After ({currency})", "Description",
        ])
        for t in qs.iterator(chunk_size=1000):
            yield writer.writerow([
                timezone.localtime(t.created_at).strftime("%Y-%m-%d %H:%M:%S"),
                _csv_safe(t.reference),
                t.get_transaction_type_display(),
                "credit" if t.amount >= 0 else "debit",
                t.get_status_display(),
                t.amount,
                t.balance_after,
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
@rider_required
def get_transaction_history(request, rider):
    """Filterable, paginated wallet history with summary, trend and CSV export (?export=csv)."""
    params = TransactionQuerySerializer(data=request.query_params)
    if not params.is_valid():
        return validation_fail(params)
    filters = params.validated_data

    wallet = _get_wallet(rider)
    qs = _apply_filters(WalletTransaction.objects.filter(rider_wallet=wallet), filters)

    if filters.get("export") == "csv":
        logger.info("wallet statement export user=%s wallet=%s", request.user.pk, wallet.pk)
        return _csv_response(qs, wallet.currency)

    paginator = TransactionPagination()
    page = paginator.paginate_queryset(qs, request)
    payload = paginator.get_paginated_response(TransactionSerializer(page, many=True).data).data
    return ok(
        currency=wallet.currency,
        balance=money(wallet.balance),
        summary=_summary(qs),
        monthly_trend=_monthly_trend(wallet),
        insights=_top_up_nudge(wallet),
        **payload,
    )


rider_transactions = get_transaction_history  # kept so existing URL names keep working


@api_view(["POST"])
@permission_classes([IsAuthenticated])
@throttle_classes([PaymentThrottle])
@rider_required
def request_withdrawal(request, rider):
    serializer = WithdrawalRequestSerializer(data=request.data)
    if not serializer.is_valid():
        return validation_fail(serializer)
    amount = serializer.validated_data["amount"]

    if not rider.is_verified:
        return fail("Only verified riders can withdraw.", status.HTTP_403_FORBIDDEN)

    accounts = RiderPayoutAccount.objects.filter(rider_profile=rider, is_deleted=False)
    account_id = serializer.validated_data.get("payout_account_id")
    account = (accounts.filter(pk=account_id) if account_id else accounts.filter(is_primary=True)).first()
    if account is None:
        return fail("Add a payout account before withdrawing.")

    fee = SystemConfig.get_decimal("WITHDRAWAL_FEE", "0.00")
    minimum = min_wallet_balance()

    with transaction.atomic():
        wallet = _get_wallet(rider, lock=True)  # row lock prevents double-spend
        if wallet.balance < amount + fee:
            return fail("Insufficient wallet balance.")
        if wallet.balance - amount - fee < minimum:
            return fail(
                f"You must keep at least {money(minimum)} in your wallet. "
                f"Maximum withdrawable: {money(max(wallet.balance - fee - minimum, 0))}."
            )
        withdrawal, message = WithdrawalRequest.create_request(wallet, account, amount)
        if withdrawal is None:
            return fail(message)

    wallet.refresh_from_db()
    logger.info("Withdrawal requested: rider_id=%s reference=%s", rider.id, withdrawal.reference)
    return ok(
        "Withdrawal request submitted.",
        status.HTTP_201_CREATED,
        withdrawal_id=str(withdrawal.id),
        reference=withdrawal.reference,
        amount=money(withdrawal.amount),
        fee=money(withdrawal.fee),
        new_balance=money(wallet.balance),
    )


# ===========================================================================
# 8. PAYMENTS (PAYSTACK)
# Flow: initialize_payment / wallet_deposit -> user pays on Paystack ->
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


class PaymentPending(PaymentError):
    """The gateway has not finished processing; the client should poll again."""
    def __init__(self):
        super().__init__("Payment is still being processed.", status.HTTP_503_SERVICE_UNAVAILABLE)


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
        if response.status_code == 404:
            raise PaymentError("Transaction not found.", status.HTTP_404_NOT_FOUND)
        code = status.HTTP_502_BAD_GATEWAY if response.status_code >= 500 else status.HTTP_400_BAD_REQUEST
        raise PaymentError("The payment service rejected the request.", code)

    return body.get("data") or {}


def _paystack_initialize(*, email, amount, reference, metadata):
    payload = {
        "email": email,
        "amount": int((Decimal(str(amount)) * 100).to_integral_value(ROUND_HALF_UP)),  # pesewas
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

    gateway_status = data.get("status")
    if gateway_status in GATEWAY_PENDING_STATES:
        raise PaymentPending()
    if gateway_status != "success":
        raise PaymentError("Payment was not completed.")
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
    return f"{prefix}-{uuid.uuid4().hex.upper()}"  # 36 chars: fits Payment.reference (40)


def _start_wallet_deposit(user, rider, amount):
    if not user.email:
        return fail("Add an email to your account before depositing.")
    if amount < MIN_DEPOSIT:
        return fail(f"Minimum deposit is {money(MIN_DEPOSIT)}.")
    if amount > MAX_DEPOSIT:
        return fail(f"Maximum deposit is {money(MAX_DEPOSIT)}.")

    reference = _new_reference("DEP")
    try:
        data = _paystack_initialize(
            email=user.email,
            amount=amount,
            reference=reference,
            metadata={"purpose": PURPOSE_DEPOSIT, "rider_id": str(rider.pk)},
        )
    except PaymentError as exc:
        return fail(exc.message, exc.status_code)

    return ok(
        "Complete the payment to fund your wallet.",
        status.HTTP_201_CREATED,
        reference=reference,
        amount=money(amount),
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
            "Payment recording conflict. Reference may already exist.",
            status.HTTP_409_CONFLICT,
        ) from None


def _settle_booking_payment(reference, paystack, requesting_user):
    """Mark the booking paid (escrow). The rider is credited later by complete_booking()."""
    verified_amount = _paystack_amount(paystack)
    not_found = PaymentError("No booking found for this payment reference.", status.HTTP_404_NOT_FOUND)

    with transaction.atomic():  # any PaymentError raised inside rolls everything back
        payment = Payment.objects.select_for_update().filter(reference=reference).first()
        # of=("self",): Booking.customer is nullable, and PostgreSQL refuses
        # FOR UPDATE on the nullable side of the outer join select_related creates
        # (that was a 500 on every booking payment).
        booking_qs = Booking.objects.select_for_update(of=("self",)).select_related("customer")
        if payment is not None:
            booking = booking_qs.get(pk=payment.booking_id)
        else:  # references issued before Payment rows existed
            try:
                booking = booking_qs.get(payment_ref=reference)
            except Booking.DoesNotExist:
                raise not_found from None

        if requesting_user is not None and (
            booking.customer is None or booking.customer.user_id != requesting_user.id
        ):
            raise not_found

        booking_amount = Decimal(str(booking.total_fare))
        summary = {
            "booking_id": str(booking.id),
            "booking_ref": booking.booking_ref,
            "total_amount": money(booking_amount),
        }

        if booking.payment_status in PAID_STATUSES:
            if booking.payment_ref != reference:
                logger.error("DUPLICATE PAYMENT booking_ref=%s extra_reference=%s",
                             booking.booking_ref, reference)
                SystemAlert.create_alert(
                    "error", "Duplicate booking payment",
                    f"Booking {booking.booking_ref} was paid twice. Extra reference: {reference}.",
                    booking_ref=booking.booking_ref,
                )
            return {"created": False, "message": "Payment already verified and recorded.",
                    "payment_status": booking.payment_status, **summary}

        if booking.status == "cancelled":
            logger.error("Payment received for cancelled booking %s ref=%s", booking.booking_ref, reference)
            SystemAlert.create_alert(
                "error", "Payment on cancelled booking",
                f"Reference {reference} paid for cancelled booking {booking.booking_ref}. Refund required.",
                booking_ref=booking.booking_ref,
            )
            return {"created": False,
                    "message": "This booking was cancelled. Support will refund your payment.",
                    "payment_status": booking.payment_status, **summary}

        if abs(verified_amount - booking_amount) > AMOUNT_TOLERANCE:
            logger.error("Amount mismatch booking_ref=%s paid=%s expected=%s",
                         booking.booking_ref, verified_amount, booking_amount)
            raise PaymentError("Payment amount does not match booking total.", status.HTTP_409_CONFLICT)

        method = CHANNEL_TO_METHOD.get(paystack.get("channel"), "debit")
        Booking.objects.filter(pk=booking.pk).update(payment_ref=reference, payment_method=method)

        if payment is None:
            payment = Payment.objects.create(
                booking=booking, provider=Payment.Provider.PAYSTACK, method=method,
                reference=reference, amount=booking_amount,
            )
        raw = {k: paystack.get(k) for k in ("id", "status", "reference", "amount", "currency", "channel", "paid_at")}
        confirmed, message = payment.mark_success(provider_ref=str(paystack.get("id", "")), raw=raw)
        if not confirmed:
            raise PaymentError(message, status.HTTP_409_CONFLICT)
        booking.refresh_from_db(fields=["payment_status", "status"])

    logger.info("Booking payment settled: booking_ref=%s reference=%s", booking.booking_ref, reference)
    return {"created": True, "message": "Payment verified successfully.",
            "payment_status": booking.payment_status, "booking_status": booking.status, **summary}


def _settle_wallet_deposit(reference, paystack, metadata, requesting_user):
    amount = _paystack_amount(paystack)
    not_found = PaymentError(
        "Deposit could not be matched to a rider account.", status.HTTP_404_NOT_FOUND
    )

    with transaction.atomic():
        try:
            rider = RiderProfile.objects.select_related("user").get(pk=metadata.get("rider_id"))
        except (RiderProfile.DoesNotExist, ValueError, TypeError, DjangoValidationError):
            raise not_found from None
        if requesting_user is not None and rider.user_id != requesting_user.id:
            raise not_found

        wallet = _get_wallet(rider, lock=True)  # serialises concurrent settlements
        already_recorded = WalletTransaction.objects.filter(reference=reference).exists()
        if not already_recorded:
            # Needs RiderWallet.deposit() (added in models fixes). The unique
            # reference makes this idempotent.
            deposited, message = wallet.deposit(
                amount, reference=reference, description=f"Wallet deposit - GHS {money(amount)}"
            )
            if not deposited:
                raise PaymentError(message, status.HTTP_409_CONFLICT)
        wallet.refresh_from_db()

    created = not already_recorded
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


def _settle_response(reference, user):
    try:
        result = _settle_reference(reference, requesting_user=user)
    except PaymentPending:
        return ok("Payment is still being processed.", status.HTTP_202_ACCEPTED,
                  payment_status="pending", retry_after=POLL_INTERVAL)
    except PaymentError as exc:
        return fail(exc.message, exc.status_code)

    created = result.pop("created")
    message = result.pop("message")
    return ok(message, status.HTTP_201_CREATED if created else status.HTTP_200_OK, **result)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
@throttle_classes([PaymentThrottle])
@rider_required
def wallet_deposit(request, rider):
    """
    Start a wallet top-up. The wallet is NEVER credited here: funds are added only
    after Paystack confirms payment (deposit_verify or the webhook).
    """
    serializer = AmountSerializer(data=request.data)
    if not serializer.is_valid():
        return validation_fail(serializer)
    return _start_wallet_deposit(request.user, rider, serializer.validated_data["amount"])


deposit_initiate = wallet_deposit  # one deposit pipeline; old name kept for URL compatibility


@api_view(["POST"])
@permission_classes([IsAuthenticated])
@throttle_classes([PaymentThrottle])
def initialize_payment(request):
    """Start a Paystack checkout for a booking or a wallet deposit."""
    serializer = InitializePaymentSerializer(data=request.data)
    if not serializer.is_valid():
        return validation_fail(serializer)
    data = serializer.validated_data

    if data["purpose"] == PURPOSE_DEPOSIT:
        rider = RiderProfile.objects.filter(user=request.user, deleted_at__isnull=True).first()
        if rider is None:
            return fail("Only riders can fund a wallet.", status.HTTP_403_FORBIDDEN)
        return _start_wallet_deposit(request.user, rider, data["amount"])

    # Booking payment: the amount always comes from the server, never the client.
    try:
        booking = Booking.objects.get(booking_ref=data["booking_ref"], customer__user=request.user)
    except Booking.DoesNotExist:
        return fail("Booking not found.", status.HTTP_404_NOT_FOUND)
    if booking.payment_status in PAID_STATUSES:
        return fail("This booking has already been paid.", status.HTTP_409_CONFLICT)
    if booking.status in ("completed", "cancelled"):
        return fail("This booking can no longer be paid.", status.HTTP_409_CONFLICT)
    if booking.payment_method in ("cash", "invoice", "wallet"):
        return fail("This booking is not payable online.", status.HTTP_409_CONFLICT)
    if booking.total_fare <= 0:
        return fail("This booking has nothing to pay.")
    if not request.user.email:
        return fail("Add an email to your account before paying.")

    amount = Decimal(str(booking.total_fare))
    reference = _new_reference("BKG")
    try:
        checkout = _paystack_initialize(
            email=request.user.email,
            amount=amount,
            reference=reference,
            metadata={"purpose": PURPOSE_BOOKING, "booking_ref": booking.booking_ref},
        )
    except PaymentError as exc:
        return fail(exc.message, exc.status_code)

    with transaction.atomic():
        Payment.objects.create(
            booking=booking, provider=Payment.Provider.PAYSTACK, reference=reference, amount=amount,
        )
        booking.payment_ref = reference
        booking.save(update_fields=["payment_ref"])

    return ok(
        "Complete the payment to confirm your booking.",
        status.HTTP_201_CREATED,
        reference=reference,
        amount=money(amount),
        authorization_url=checkout["authorization_url"],
        access_code=checkout.get("access_code"),
    )


@api_view(["POST"])
@permission_classes([IsAuthenticated])
@throttle_classes([PaymentThrottle])
def verify_payment(request):
    """Called by the app after checkout. Safe to call repeatedly."""
    serializer = VerifyPaymentSerializer(data=request.data)
    if not serializer.is_valid():
        return validation_fail(serializer)
    return _settle_response(serializer.validated_data["reference"], request.user)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
@throttle_classes([PaymentThrottle])
def deposit_verify(request, reference):
    """Polling endpoint. Returns 202 while pending."""
    if not REFERENCE_PATTERN.match(reference):
        return fail("Invalid reference format.")
    return _settle_response(reference, request.user)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
@throttle_classes([WalletReadThrottle])
@rider_required
def deposit_history(request, rider):
    """Completed deposits for the current rider, newest first (?status=, ?page=)."""
    wallet = _get_wallet(rider)
    qs = WalletTransaction.objects.filter(rider_wallet=wallet, transaction_type="deposit")
    status_filter = request.query_params.get("status")
    if status_filter:
        qs = qs.filter(status=status_filter.lower())
    qs = qs.order_by("-created_at")

    paginator = TransactionPagination()
    page = paginator.paginate_queryset(qs, request)
    payload = paginator.get_paginated_response(TransactionSerializer(page, many=True).data).data
    return ok("Deposit history retrieved.", **payload)


@api_view(["POST"])
@authentication_classes([])
@permission_classes([AllowAny])
@throttle_classes([])
def paystack_webhook(request):
    """
    Server-to-server notification from Paystack. Settles payments even when the user
    closes the app before verify_payment is called. The payload is never trusted for
    amounts: every settlement re-verifies the reference with Paystack.
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
    if not isinstance(event, dict):
        return Response(status=status.HTTP_400_BAD_REQUEST)

    if event.get("event") == "charge.success":
        event_data = event.get("data") or {}
        reference = event_data.get("reference")
        if isinstance(reference, str) and reference:
            event_id = str(event_data.get("id") or reference)[:120]
            record, created = WebhookEvent.register("paystack", event_id, event, event_type="charge.success")
            if not created and record.processed:
                return Response(status=status.HTTP_200_OK)  # duplicate delivery

            try:
                _settle_reference(reference)
                record.mark_processed()
            except PaymentError as exc:
                record.mark_processed(error=exc.message)
                if exc.retryable:
                    logger.error("Webhook settlement failed (will retry) ref=%s: %s", reference, exc.message)
                    return Response(status=status.HTTP_503_SERVICE_UNAVAILABLE)
                logger.warning("Webhook payment not settled ref=%s: %s", reference, exc.message)

    return Response(status=status.HTTP_200_OK)


# ===========================================================================
# 9. "WHO AM I" PROFILE ENDPOINT  (merged from profile_views.py)
#     GET   /auth/me/   the logged-in user's own profile
#     PATCH /auth/me/   update full_name / phone / email / profile_photo
# No id in the URL: the profile is always looked up from request.user.
# ===========================================================================
MAX_PHOTO_MB = 5

RIDER_STATUS = {
    "pending": "pending",
    "under_review": "under_review",
    "verified": "approved",
    "rejected": "rejected",
    "suspended": "suspended",
}


def _photo_url(request, field):
    """Absolute URL of an ImageField, or None. A storage problem must never break the profile."""
    try:
        if field and field.name:
            return request.build_absolute_uri(field.url)
    except Exception:  # noqa: BLE001
        logger.warning("Profile photo URL unavailable", exc_info=True)
    return None


def _rider_payload(request, rider):
    user = rider.user
    qualified = {q.service_id: q.status for q in rider.qualifications.all()}
    services = []
    for service in Service.objects.filter(is_active=True):
        approved = qualified.get(service.pk) == ServiceQualification.Status.APPROVED
        services.append({"key": service.pk, "approved": approved, "active": approved})

    status_value = RIDER_STATUS.get(str(rider.verification_status), "pending")
    return {
        "id": str(rider.id),
        "role": "rider",
        "rider_id": str(rider.id),
        "rider_code": rider.rider_code,
        "full_name": rider.full_name,
        "email": user.email or "",
        "phone_number": rider.phone,
        "profile_photo": _photo_url(request, rider.profile_photo),
        "status": status_value,
        "rejection_reason": (rider.rejection_reason or "") if status_value == "rejected" else "",
        "created_at": rider.created_at.isoformat(),
        "rating": rider.rating_average,
        "completed_jobs": rider.completed_trips,
        "vehicle_plate": rider.motorbike_plate_number,
        "acceptance_rate": rider.acceptance_rate,
        "completion_rate": rider.completion_rate,
        "is_online": rider.is_online,
        "services": services,
    }


def _customer_payload(request, customer):
    user = customer.user
    return {
        "id": customer.pk,
        "role": "customer",
        "full_name": customer.full_name or user.get_full_name() or "",
        "email": user.email or customer.email or "",
        "phone_number": customer.phone or "",
        "profile_photo": _photo_url(request, customer.profile_photo),
        "status": "blocked" if customer.is_blocked else "active",
        "created_at": customer.created_at.isoformat(),
    }


INPUT_ALIASES = {
    "full_name": ("full_name", "name"),
    "phone": ("phone", "phone_number"),
    "email": ("email",),
}


class MeUpdateSerializer(serializers.Serializer):
    """context: user, profile, role ("rider" | "customer")."""

    full_name = serializers.CharField(max_length=150, required=False)
    phone = serializers.CharField(max_length=30, required=False, allow_blank=True)
    email = serializers.EmailField(required=False)
    profile_photo = serializers.ImageField(required=False)  # Pillow checks it is a real image

    def validate_full_name(self, value):
        value = " ".join(value.split())
        if len(value) < 2:
            raise serializers.ValidationError("Enter your full name.")
        return value

    def validate_email(self, value):
        value = value.strip().lower()
        if email_is_taken(value, exclude_user_id=self.context["user"].pk):
            raise serializers.ValidationError("An account with this email already exists.")
        return value

    def validate_phone(self, value):
        role, profile = self.context["role"], self.context["profile"]
        if not value.strip():
            if role == "rider":
                raise serializers.ValidationError("A phone number is required.")
            return ""
        try:
            phone = clean_phone(value)
        except Exception:  # noqa: BLE001
            raise serializers.ValidationError("Enter a valid phone number.") from None
        if not phone:
            raise serializers.ValidationError("Enter a valid phone number.")
        model = RiderProfile if role == "rider" else CustomerProfile
        if model.objects.filter(phone=phone).exclude(pk=profile.pk).exists():
            raise serializers.ValidationError("This phone number is already registered.")
        return phone

    def validate_profile_photo(self, f):
        if f.size > MAX_PHOTO_MB * 1024 * 1024:
            raise serializers.ValidationError(f"Photo too large. Maximum size is {MAX_PHOTO_MB} MB.")
        return f


def _collect_input(request):
    data = {}
    for field, keys in INPUT_ALIASES.items():
        for key in keys:
            if key in request.data:
                data[field] = request.data[key]
                break
    if "profile_photo" in request.FILES:
        data["profile_photo"] = request.FILES["profile_photo"]
    return data


@api_view(["GET", "PATCH"])
@parser_classes([JSONParser, MultiPartParser, FormParser])
@permission_classes([IsAuthenticated])
@throttle_classes([ProfileThrottle])
def my_profile(request):
    rider = (
        RiderProfile.objects.select_related("user")
        .filter(user=request.user, deleted_at__isnull=True)
        .first()
    )
    customer = None
    if rider is None:
        customer = CustomerProfile.objects.select_related("user").filter(user=request.user).first()
    if rider is None and customer is None:
        return fail("Profile not found.", status.HTTP_404_NOT_FOUND)
    if customer is not None and customer.is_blocked:
        return fail("This account has been blocked. Contact support.", status.HTTP_403_FORBIDDEN)

    role = "rider" if rider else "customer"
    profile = rider or customer
    user_payload["full_name"] =(gatattr(profile, "full_name", "") or
    user.get_full_name() or "")

    user_payload["phone_number"] =(Rider.phone if rider else
    gatattr(customer, "phone", "")) or ""
    

    if request.method == "GET":
        return ok(**build(request, profile))

    serializer = MeUpdateSerializer(
        data=_collect_input(request),
        context={"user": profile.user, "profile": profile, "role": role},
        partial=True,
    )
    if not serializer.is_valid():
        return validation_fail(serializer)
    values = serializer.validated_data
    if not values:
        return fail("Nothing to update.")

    user = profile.user
    try:
        with transaction.atomic():
            user_fields = []
            if "full_name" in values:
                profile.full_name = values["full_name"]
                user.first_name, user.last_name = split_name(values["full_name"])
                user_fields += ["first_name", "last_name"]
            if "email" in values and values["email"] != (user.email or "").lower():
                old_email = (user.email or "").lower()
                user.email = values["email"]
                user_fields.append("email")
                # Accounts are created with username = email and login looks up the
                # username, so keep them in step.
                if (user.username or "").lower() == old_email:
                    user.username = values["email"]
                    user_fields.append("username")
                if customer is not None:
                    customer.email = values["email"]
            if "phone" in values:
                profile.phone = values["phone"] or None
            if "profile_photo" in values:
                profile.profile_photo = values["profile_photo"]
            if user_fields:
                user.save(update_fields=user_fields)
            profile.save()
    except IntegrityError:
        return fail("That phone number or email is already in use.", status.HTTP_409_CONFLICT)

    return ok("Profile updated.", **build(request, profile))

