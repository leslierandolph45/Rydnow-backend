"""
RYDNOW api/views.py  (rewritten to match api/models.py, api/serializers.py and pricing.py)

Conventions
  * Function views return {"success": bool, ...}. Generic CRUD views return plain DRF payloads.
  * Every money value is Decimal. Clients never send a price: bookings are priced from the signed
    quote token (pricing.issue_quote / BookingCreateSerializer).
  * Booking lifecycle goes only through Booking methods (submit / accept / transition_to / cancel).
  * Rider wallet goes only through RiderWallet.post / request_withdrawal / WalletDeposit.record.
  * Customer wallet goes only through CustomerWallet.deposit / pay_for_booking / refund_to_wallet.
  * Review.subject_profile_id stores the SUBJECT'S USER ID (RiderProfile.pk is a UUID and cannot
    fit that integer column).

Suggested routes (add to api/urls.py; names kept from the old file are marked *)
  AUTH      user_login* user_logout change_password account_me customer_register* rider_register*
            send_verification_code* verify_code* phone_send_otp phone_verify_otp my_profile*
  PLATFORM  services_list zones_list system_alerts system_alert_dismiss
  CUSTOMER  customer_profile* customer_preferences SavedAddressList/Detail customer_wallet*
            customer_wallet_transactions customer_wallet_deposit loyalty_account loyalty_transactions
            loyalty_redeem
  RIDER     MyRiderProfileView* rider_submit_documents* rider_documents rider_verification_status*
            rider_go_online rider_go_offline update_rider_location* rider_performance
            rider_status_history rider_qualifications PayoutAccountList/Detail
            EmergencyContactList/Detail get_nearby_riders* nearby_riders
  WALLET    get_rider_wallet* get_transaction_history* rider_transactions* request_withdrawal*
            rider_withdrawals cancel_withdrawal wallet_deposit* deposit_initiate* deposit_history*
  OFFERS    rider_offers accept_offer* decline_offer* booking_offers
  PAYMENTS  initialize_payment* verify_payment* deposit_verify* my_payments refunds
            paystack_webhook* booking_webhook
  BOOKINGS  booking_quote booking_list_create booking_detail booking_status_update booking_cancel
            booking_dispatch
  PARCEL    parcel_create parcel_detail parcel_public_tracking parcel_status_update
            parcel_proof_create parcel_feedback
  GROWTH    promos_public promo_validate referral_summary referral_redeem
            BusinessApplicationList BusinessApplicationDetail RecurringBookingList/Detail
            recurring_action WasteSubscriptionList waste_subscription_action waste_collections
            waste_collections_available waste_collection_claim waste_collection_complete
  OTHER     reviews review_create ComplaintList IncidentList notifications_list
            notification_mark_read notifications_mark_all_read
  STAFF     staff_rider_action staff_document_review staff_qualification_action
            staff_wallet_deposit staff_withdrawal_action staff_refund_decision staff_refund_complete
            staff_business_review staff_business_action
"""
import csv
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import time
import uuid
from datetime import datetime, timedelta
from datetime import time as dtime
from decimal import ROUND_HALF_UP, Decimal
from functools import wraps
from types import SimpleNamespace
from urllib.parse import quote

import requests
from django.conf import settings
from django.contrib.auth import authenticate, get_user_model
from django.contrib.auth.models import update_last_login
from django.core.cache import cache
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import IntegrityError, transaction
from django.db.models import Count, DecimalField, F, Q, Sum, Value
from django.db.models.functions import Coalesce, TruncMonth
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
from rest_framework.exceptions import PermissionDenied
from rest_framework.pagination import PageNumberPagination
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.permissions import AllowAny, BasePermission, IsAdminUser, IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import AnonRateThrottle, UserRateThrottle

from decimal import InvalidOperation (or extend your existing decimal import)
from django.http import Http404, StreamingHttpResponse (Http404 is new)


from . import pricing
from .models import (
    DISPATCH_LEAD,
    LOCATION_STALE_SECONDS,
    MAX_CODE_ATTEMPTS,
    Booking,
    BookingOffer,
    BookingStatusHistory,
    BusinessAccount,
    CustomerProfile,
    CustomerRiderPreference,
    CustomerWallet,
    CustomerWalletTransaction,
    DispatchOffer,
    LoyaltyAccount,
    Notification,
    ParcelDetail,
    ParcelProof,
    Payment,
    PromoCode,
    PromoRedemption,
    RecurringBooking,
    Referral,
    ReferralCode,
    Refund,
    Review,
    RiderComplaint,
    RiderDocument,
    RiderEmergencyContact,
    RiderIncident,
    RiderPayoutAccount,
    RiderProfile,
    RiderWallet,
    SavedAddress,
    Service,
    ServiceQualification,
    SystemAlert,
    SystemConfig,
    VerificationStatus,
    WalletDeposit,
    WalletTransaction,
    WasteCollection,
    WasteSubscription,
    WebhookEvent,
    WithdrawalRequest,
    Zone,
    audit,
    generate_payment_reference,
    verify_hmac_signature,
)
from .serializers import (
    RECEIPT_KEYS,
    REFERENCE_RE,
    AccountSerializer,
    AccountUpdateSerializer,
    AmountSerializer,
    BookingCancelSerializer,
    BookingCreateSerializer,
    BookingDetailSerializer,
    BookingListSerializer,
    BookingOfferSerializer,
    BookingStatusUpdateSerializer,
    BookingWebhookSerializer,
    BusinessAccountApplicationSerializer,
    BusinessReviewSerializer,
    ChangePasswordSerializer,
    CustomerPreferenceSerializer,
    CustomerProfileSerializer,
    CustomerWalletSerializer,
    CustomerWalletTransactionSerializer,
    DeclineOfferSerializer,
    DispatchOfferSerializer,
    InitializePaymentSerializer,
    LegacyNearbyRidersQuerySerializer,
    LoginSerializer,
    LoyaltyAccountSerializer,
    LoyaltyRedeemSerializer,
    LoyaltyTransactionSerializer,
    NearbyRiderSerializer,
    NearbyRidersQuerySerializer,
    NotificationSerializer,
    ParcelCreateSerializer,
    ParcelDetailSerializer,
    ParcelFeedbackSerializer,
    ParcelOwnerSerializer,
    ParcelProofCreateSerializer,
    ParcelProofSerializer,
    ParcelPublicTrackingSerializer,
    ParcelStatusUpdateSerializer,
    PaymentSerializer,
    PhoneSerializer,
    PromoCodePublicSerializer,
    PromoValidateSerializer,
    QuoteRequestSerializer,
    QuoteResponseSerializer,
    RecurringBookingSerializer,
    RedeemReferralSerializer,
    ReferralCodeSerializer,
    ReferralSerializer,
    RefundDecisionSerializer,
    RefundRequestSerializer,
    RefundSerializer,
    RegistrationSerializer,
    ReviewCreateSerializer,
    ReviewSerializer,
    RiderBookingDetailSerializer,
    RiderComplaintSerializer,
    RiderDocumentReadSerializer,
    RiderDocumentReviewSerializer,
    RiderDocumentSerializer,
    RiderEmergencyContactSerializer,
    RiderIncidentSerializer,
    RiderLocationSerializer,
    RiderPayoutAccountSerializer,
    RiderPerformanceSerializer,
    RiderProfileSerializer,
    RiderRegistrationSerializer,
    RiderRejectSerializer,
    RiderStatusHistorySerializer,
    RiderSuspendSerializer,
    RiderWalletSerializer,
    SavedAddressSerializer,
    ServiceQualificationSerializer,
    ServiceSerializer,
    StaffBookingDetailSerializer,
    StaffDepositSerializer,
    SystemAlertSerializer,
    TransactionQuerySerializer,
    TransactionSerializer,
    VerifyCodeSerializer,
    VerifyPaymentSerializer,
    WalletDepositSerializer,
    WasteCollectionCompleteSerializer,
    WasteCollectionSerializer,
    WasteSubscriptionCreateSerializer,
    WasteSubscriptionSerializer,
    WithdrawalRequestSerializer,
    WithdrawalSerializer,
    ZoneSerializer,
)
from .validators import clean_phone, email_is_taken, split_name

User = get_user_model()
logger = logging.getLogger(__name__)


# ===========================================================================
# 0. CONSTANTS, HELPERS, THROTTLES, PERMISSIONS, DECORATORS
# ===========================================================================
CURRENCY = getattr(settings, "PAYMENT_CURRENCY", pricing.CURRENCY)
MIN_DEPOSIT = Decimal(str(getattr(settings, "MIN_DEPOSIT", "10.00")))
MAX_DEPOSIT = Decimal(str(getattr(settings, "MAX_DEPOSIT", "100000.00")))
AMOUNT_TOLERANCE = Decimal("0.01")
TWO_PLACES = Decimal("0.01")
ZERO_D = Decimal("0.00")
ZERO_EXPR = Value(Decimal("0.00"), output_field=DecimalField(max_digits=14, decimal_places=2))

PAYSTACK_BASE_URL = "https://api.paystack.co"
PAYSTACK_TIMEOUT = (5, 15)
PURPOSE_BOOKING = "booking"
PURPOSE_DEPOSIT = "wallet_deposit"
GATEWAY_PENDING_STATES = ("ongoing", "pending", "processing", "queued")
CHANNEL_TO_METHOD = {"mobile_money": "momo", "card": "card", "bank": "bank_transfer"}
POLL_INTERVAL = 5
REFERENCE_PATTERN = re.compile(REFERENCE_RE)
PAYMENT_WINDOW = timedelta(minutes=30)

OTP_LENGTH = 6
OTP_TTL_SECONDS = 600
OTP_COOLDOWN_SECONDS = 60
OTP_MAX_ATTEMPTS = 5
OTP_MAX_SENDS_PER_HOUR = 5
PHONE_VERIFIED_TTL_SECONDS = 1800

LOW_BALANCE_FALLBACK = Decimal("50.00")
MAX_EXPORT_ROWS = 5000
MAX_PHOTO_MB = 5
PRIVATE_COORD_DP = 3  # ~100 m, used for anything shown to people who are not on the trip


def ok(message=None, status_code=status.HTTP_200_OK, **kwargs):
    data = {"success": True}
    if message:
        data["message"] = message
    data.update(kwargs)
    return Response(data, status=status_code)


def fail(message, status_code=status.HTTP_400_BAD_REQUEST, **extra):
    return Response({"success": False, "error": message, **extra}, status=status_code)


def dj_fail(exc, status_code=status.HTTP_400_BAD_REQUEST):
    """Django ValidationError -> our error envelope."""
    return fail(" ".join(exc.messages) or "Invalid request.", status_code)


def _flatten_errors(errors):
    if isinstance(errors, dict):
        for value in errors.values():
            yield from _flatten_errors(value)
    elif isinstance(errors, (list, tuple)):
        for item in errors:
            yield from _flatten_errors(item)
    else:
        yield str(errors)


def validation_fail(serializer):
    messages = []
    for value in serializer.errors.values():
        first = next(_flatten_errors(value), None)
        if first:
            messages.append(first)
    return fail(" ".join(messages) or "Invalid request.")


def drf_fail(exc, status_code=status.HTTP_400_BAD_REQUEST):
    """serializers.ValidationError raised inside serializer.save() -> our envelope."""
    return fail(" ".join(_flatten_errors(exc.detail)) or "Invalid request.", status_code)


def money(value):
    return str(Decimal(str(value)).quantize(TWO_PLACES, rounding=ROUND_HALF_UP))


def min_wallet_balance():
    """Single source of truth, shared with RiderProfile.has_minimum_deposit and dispatch."""
    return SystemConfig.get_decimal("MIN_RIDER_BOOKING_BALANCE", str(LOW_BALANCE_FALLBACK))


def _try(label, fn, *args, **kwargs):
    """Run a NON-critical side effect in a savepoint; log and swallow any failure."""
    try:
        with transaction.atomic():
            return fn(*args, **kwargs)
    except Exception:
        logger.exception("Non-critical step failed: %s", label)
        return None


def _client_ip(request):
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
    return (forwarded.split(",")[0].strip() or request.META.get("REMOTE_ADDR")) or None


def _has_field(model, name):
    return any(f.name == name for f in model._meta.get_fields())


class StandardPagination(PageNumberPagination):
    page_size = 20
    page_size_query_param = "page_size"
    max_page_size = 100


TransactionPagination = StandardPagination


def paginated(request, queryset, serializer_class, **extra):
    paginator = StandardPagination()
    page = paginator.paginate_queryset(queryset, request)
    data = serializer_class(page, many=True, context={"request": request}).data
    response = paginator.get_paginated_response(data)
    response.data.update(extra)
    return response


# ---- throttles -------------------------------------------------------------
class LoginThrottle(AnonRateThrottle):
    scope = "login"
    rate = "10/min"


class RegistrationThrottle(AnonRateThrottle):
    scope = "register"
    rate = "5/min"


class OTPThrottle(AnonRateThrottle):
    scope = "otp"
    rate = "5/min"


class PublicLookupThrottle(AnonRateThrottle):
    scope = "public_lookup"
    rate = "30/min"


class AuthOTPThrottle(UserRateThrottle):
    scope = "auth_otp"
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


class QuoteThrottle(UserRateThrottle):
    scope = "quote"
    rate = "60/min"


class BookingThrottle(UserRateThrottle):
    scope = "booking"
    rate = "30/min"


class UploadThrottle(UserRateThrottle):
    scope = "upload"
    rate = "20/min"


# ---- permissions + role helpers -------------------------------------------------
def _rider_of(user):
    return RiderProfile.objects.filter(user=user, deleted_at__isnull=True).select_related("user").first()


def _customer_of(user):
    return CustomerProfile.objects.filter(user=user).first()


class IsRider(BasePermission):
    message = "A rider account is required."

    def has_permission(self, request, view):
        u = request.user
        return bool(u and u.is_authenticated and RiderProfile.objects.filter(
            user=u, deleted_at__isnull=True).exists())


class IsCustomer(BasePermission):
    message = "A customer account is required."

    def has_permission(self, request, view):
        u = request.user
        return bool(u and u.is_authenticated and CustomerProfile.objects.filter(user=u).exists())


def rider_required(view):
    """Inject the authenticated user's RiderProfile as `rider`, or return 404."""
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        rider = _rider_of(request.user)
        if rider is None:
            return fail("Rider profile not found.", status.HTTP_404_NOT_FOUND)
        return view(request, *args, rider=rider, **kwargs)
    return wrapper


def customer_required(view):
    """Inject the authenticated user's CustomerProfile as `customer`, or return 404/403."""
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        customer = _customer_of(request.user)
        if customer is None:
            return fail("Customer profile not found.", status.HTTP_404_NOT_FOUND)
        if customer.is_blocked:
            return fail("This account has been blocked. Contact support.", status.HTTP_403_FORBIDDEN)
        return view(request, *args, customer=customer, **kwargs)
    return wrapper


def _get_wallet(rider, *, lock=False):
    """Fetch (or create) a rider's wallet. lock=True must run inside transaction.atomic()."""
    wallet, _ = RiderWallet.objects.get_or_create(rider=rider)
    if lock:
        wallet = RiderWallet.objects.select_for_update().get(pk=wallet.pk)
    return wallet


def _can_go_online(rider, wallet):
    return rider.can_accept_jobs and wallet.balance >= min_wallet_balance()


def _notify(user, title, message="", category=Notification.Category.SYSTEM, data=None):
    _try("notify", Notification.notify, user, title, message, category, data)


# ===========================================================================
# 1. AUTHENTICATION, ACCOUNT, PHONE VERIFICATION
# ===========================================================================
def _role_of(user):
    if _rider_of(user):
        return "rider"
    if user.is_staff and not _customer_of(user):
        return "staff"
    return "customer"


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
        identifier = identifier.lower()

    user = authenticate(request=request, username=identifier, password=serializer.validated_data["password"])
    if user is None:
        return fail("Invalid credentials.", status.HTTP_401_UNAUTHORIZED)

    rider = _rider_of(user)
    customer = None if rider else _customer_of(user)
    if customer is not None and customer.is_blocked:
        return fail("This account has been blocked. Contact support.", status.HTTP_403_FORBIDDEN)

    update_last_login(None, user)
    token, _ = Token.objects.get_or_create(user=user)

    profile = rider or customer
    phone = getattr(profile, "phone", "") if profile else ""
    user_payload = {
        "id": user.id,
        "username": user.username,
        "email": user.email,
        "role": "rider" if rider else ("staff" if user.is_staff and not customer else "customer"),
        "full_name": (getattr(profile, "full_name", "") if profile else "") or user.get_full_name() or "",
        "phone": str(phone) if phone else "",
        "referral_code": getattr(customer, "referral_code", None) or "",
    }
    if rider:
        user_payload["is_verified"] = rider.is_verified
        user_payload["verification_status"] = rider.verification_status
    if customer is not None:
        user_payload["phone_verified"] = bool(getattr(customer, "phone_verified", False))
    return ok(token=token.key, user=user_payload)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def user_logout(request):
    Token.objects.filter(user=request.user).delete()
    return ok("Logged out.")


@api_view(["POST"])
@permission_classes([IsAuthenticated])
@throttle_classes([ProfileThrottle])
def change_password(request):
    serializer = ChangePasswordSerializer(data=request.data, context={"request": request})
    if not serializer.is_valid():
        return validation_fail(serializer)
    user = request.user
    user.set_password(serializer.validated_data["new_password"])
    user.save(update_fields=["password"])
    Token.objects.filter(user=user).delete()  # sign out every other device
    token = Token.objects.create(user=user)
    return ok("Password changed.", token=token.key)


@api_view(["GET", "PATCH"])
@permission_classes([IsAuthenticated])
@throttle_classes([ProfileThrottle])
def account_me(request):
    """Raw auth-User view (names / email). Use my_profile for the role-aware profile."""
    user = request.user
    if request.method == "GET":
        return ok(account=AccountSerializer(user).data)
    serializer = AccountUpdateSerializer(user, data=request.data, partial=True)
    if not serializer.is_valid():
        return validation_fail(serializer)
    old_email = (user.email or "").lower()
    with transaction.atomic():
        user = serializer.save()
        if user.email and (user.username or "").lower() == old_email and user.email.lower() != old_email:
            user.username = user.email
            user.save(update_fields=["username"])
    return ok("Account updated.", account=AccountSerializer(user).data)


def _apply_signup_referral(user, code):
    """New customer typed a customer referral code (CustomerProfile.referral_code)."""
    if not code:
        return
    referrer = CustomerProfile.objects.filter(referral_code=code).select_related("user").first()
    if referrer is None or referrer.user_id == user.id:
        return
    reward, referee_reward = _referral_rewards()
    _create_active_referral(referrer.user, user, reward, referee_reward)


@api_view(["POST"])
@authentication_classes([])
@permission_classes([AllowAny])
@throttle_classes([RegistrationThrottle])
def customer_register(request):
    """Register a customer: User + CustomerProfile, atomically."""
    serializer = RegistrationSerializer(data=request.data)
    if not serializer.is_valid():
        return validation_fail(serializer)

    phone = serializer.validated_data.get("phone") or ""
    if phone and not _phone_verification_ok(phone):
        return fail("Verify your phone number first.", status.HTTP_403_FORBIDDEN)

    try:
        user = serializer.save()
    except serializers.ValidationError as exc:
        return drf_fail(exc, status.HTTP_409_CONFLICT)
    except IntegrityError:
        return fail("An account with these details already exists.", status.HTTP_409_CONFLICT)

    if phone:
        _consume_phone_verification(phone)
    _try("signup referral", _apply_signup_referral, user, serializer.validated_data.get("referral_code"))
    logger.info("Customer registered: user_id=%s", user.id)

    token, _ = Token.objects.get_or_create(user=user)
    profile = _customer_of(user)
    return ok(
        "Account created successfully.", status.HTTP_201_CREATED,
        user_id=user.id, email=user.email, token=token.key,
        user={
            "id": user.id, "username": user.username, "email": user.email, "role": "customer",
            "full_name": (profile.full_name if profile else "") or "",
            "phone": str(profile.phone) if profile and profile.phone else "",
            "referral_code": (profile.referral_code if profile else "") or "",
        },
    )


@api_view(["POST"])
@authentication_classes([])
@permission_classes([AllowAny])
@throttle_classes([RegistrationThrottle])
def rider_register(request):
    """Create User -> RiderProfile -> RiderWallet atomically (inside the serializer)."""
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
    except serializers.ValidationError as exc:
        return drf_fail(exc, status.HTTP_409_CONFLICT)
    except IntegrityError:
        return fail("An account with this email or phone already exists.", status.HTTP_409_CONFLICT)

    _consume_phone_verification(phone)
    logger.info("Rider registered: rider_id=%s", rider.id)
    return ok(
        "Registration submitted. Upload your documents to complete verification.",
        status.HTTP_201_CREATED,
        rider_id=str(rider.id), rider_code=rider.rider_code, email=rider.user.email, phone=rider.phone,
        is_verified=rider.is_verified, verification_status=rider.verification_status,
        requires_approval=True, token=token.key,
    )


# ---- OTP (requires a shared cache such as Redis in production) --------------------
def _otp_hash(phone, code):
    return hmac.new(settings.SECRET_KEY.encode(), f"{phone}:{code}".encode(), hashlib.sha256).hexdigest()


def _verified_key(phone):
    return f"otp:verified:{phone}"


def _phone_verification_ok(phone):
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
    logger.error("SMS_BACKEND is not configured; cannot send SMS.")
    return False


def _issue_otp(phone):
    """Rate-limit, generate, send and store a code. Returns a Response."""
    hourly_key = f"otp:hourly:{phone}"
    if cache.get(hourly_key, 0) >= OTP_MAX_SENDS_PER_HOUR:
        return fail("Too many codes requested. Try again later.", status.HTTP_429_TOO_MANY_REQUESTS)

    cooldown_key = f"otp:cooldown:{phone}"
    if not cache.add(cooldown_key, 1, OTP_COOLDOWN_SECONDS):  # atomic gate
        return fail("Please wait before requesting another code.", status.HTTP_429_TOO_MANY_REQUESTS)

    code = f"{secrets.randbelow(10 ** OTP_LENGTH):0{OTP_LENGTH}d}"
    message = f"Your Rydnow verification code is {code}. It expires in {OTP_TTL_SECONDS // 60} minutes."
    if not _send_sms(phone, message):
        cache.delete(cooldown_key)
        return fail("Verification service is unavailable. Please try again.",
                    status.HTTP_503_SERVICE_UNAVAILABLE)

    cache.set(
        f"otp:code:{phone}",
        {"hash": _otp_hash(phone, code), "attempts": 0, "expires_at": time.time() + OTP_TTL_SECONDS},
        OTP_TTL_SECONDS,
    )
    cache.add(hourly_key, 0, 3600)
    cache.incr(hourly_key)
    return ok("Verification code sent.", expires_in=OTP_TTL_SECONDS)


def _check_otp(phone, code):
    """Returns (True, None) when the code is right, else (False, error Response)."""
    key = f"otp:code:{phone}"
    entry = cache.get(key)
    if not entry:
        return False, fail("Code expired or was never requested.")
    if entry["attempts"] >= OTP_MAX_ATTEMPTS:
        cache.delete(key)
        return False, fail("Too many attempts. Request a new code.", status.HTTP_429_TOO_MANY_REQUESTS)
    if not hmac.compare_digest(entry["hash"], _otp_hash(phone, code)):
        entry["attempts"] += 1
        cache.set(key, entry, max(int(entry["expires_at"] - time.time()), 1))
        return False, fail("Invalid verification code.")
    cache.delete(key)
    return True, None


@api_view(["POST"])
@authentication_classes([])
@permission_classes([AllowAny])
@throttle_classes([OTPThrottle])
def send_verification_code(request):
    """Pre-registration OTP (public)."""
    serializer = PhoneSerializer(data=request.data)
    if not serializer.is_valid():
        return validation_fail(serializer)
    return _issue_otp(serializer.validated_data["phone"])


@api_view(["POST"])
@authentication_classes([])
@permission_classes([AllowAny])
@throttle_classes([OTPThrottle])
def verify_code(request):
    serializer = VerifyCodeSerializer(data=request.data)
    if not serializer.is_valid():
        return validation_fail(serializer)
    phone = serializer.validated_data["phone"]
    verified, error = _check_otp(phone, serializer.validated_data["code"])
    if not verified:
        return error
    cache.set(_verified_key(phone), True, PHONE_VERIFIED_TTL_SECONDS)
    return ok("Phone number verified.", verified=True)


def _phone_in_use(phone, user):
    own_rider = RiderProfile.objects.filter(user=user).values_list("pk", flat=True).first()
    own_customer = CustomerProfile.objects.filter(user=user).values_list("pk", flat=True).first()
    return (
        RiderProfile.objects.filter(phone=phone).exclude(pk=own_rider).exists()
        or CustomerProfile.objects.filter(phone=phone).exclude(pk=own_customer).exists()
    )


@api_view(["POST"])
@permission_classes([IsAuthenticated])
@throttle_classes([AuthOTPThrottle])
def phone_send_otp(request):
    """Signed-in user adds/changes/verifies their phone: sends a code to the NEW number."""
    serializer = PhoneSerializer(data=request.data)
    if not serializer.is_valid():
        return validation_fail(serializer)
    phone = serializer.validated_data["phone"]
    if _phone_in_use(phone, request.user):
        return fail("This phone number is already registered.", status.HTTP_409_CONFLICT)
    return _issue_otp(phone)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
@throttle_classes([AuthOTPThrottle])
def phone_verify_otp(request):
    """The phone is saved on the profile ONLY here, and only when the code is correct."""
    serializer = VerifyCodeSerializer(data=request.data)
    if not serializer.is_valid():
        return validation_fail(serializer)
    phone = serializer.validated_data["phone"]
    if _phone_in_use(phone, request.user):
        return fail("This phone number is already registered.", status.HTTP_409_CONFLICT)
    verified, error = _check_otp(phone, serializer.validated_data["code"])
    if not verified:
        return error

    rider = _rider_of(request.user)
    profile = rider or _customer_of(request.user)
    if profile is None:
        return fail("Profile not found.", status.HTTP_404_NOT_FOUND)
    try:
        with transaction.atomic():
            profile.phone = phone
            fields = ["phone", "updated_at"]
            if not rider and _has_field(CustomerProfile, "phone_verified"):
                profile.phone_verified = True
                fields.append("phone_verified")
            profile.save(update_fields=fields)
    except ValueError as exc:  # normalize_gh_phone (riders)
        return fail(str(exc))
    except IntegrityError:
        return fail("This phone number is already registered.", status.HTTP_409_CONFLICT)
    return ok("Phone number verified.", phone=str(profile.phone), phone_verified=True)


# ---- "who am I" ------------------------------------------------------------------
RIDER_STATUS = {
    "pending": "pending", "under_review": "under_review", "verified": "approved",
    "rejected": "rejected", "suspended": "suspended",
}
INPUT_ALIASES = {
    "full_name": ("full_name", "name"),
    "phone": ("phone", "phone_number"),
    "email": ("email",),
}


def _photo_url(request, field):
    try:
        if field and field.name:
            return request.build_absolute_uri(field.url)
    except Exception:  # noqa: BLE001
        logger.warning("Profile photo URL unavailable", exc_info=True)
    return None


def _rider_payload(request, rider):
    user = rider.user
    qualified = {q.service_id: q.status for q in rider.qualifications.all()}
    service_rows = []
    for service in Service.objects.filter(is_active=True):
        approved = qualified.get(service.pk) == ServiceQualification.Status.APPROVED
        service_rows.append({"key": service.pk, "approved": approved, "active": approved})
    status_value = RIDER_STATUS.get(str(rider.verification_status), "pending")
    return {
        "id": str(rider.id), "role": "rider", "rider_id": str(rider.id), "rider_code": rider.rider_code,
        "full_name": rider.full_name, "email": user.email or "", "phone_number": rider.phone,
        "profile_photo": _photo_url(request, rider.profile_photo), "status": status_value,
        "rejection_reason": (rider.rejection_reason or "") if status_value == "rejected" else "",
        "created_at": rider.created_at.isoformat(), "rating": rider.rating_average,
        "completed_jobs": rider.completed_trips, "vehicle_plate": rider.motorbike_plate_number,
        "acceptance_rate": rider.acceptance_rate, "completion_rate": rider.completion_rate,
        "is_online": rider.is_online, "services": service_rows,
    }


def _customer_payload(request, customer):
    user = customer.user
    return {
        "id": customer.pk, "role": "customer",
        "full_name": customer.full_name or user.get_full_name() or "",
        "email": user.email or customer.email or "", "phone_number": customer.phone or "",
        "phone_verified": bool(getattr(customer, "phone_verified", False)),
        "profile_photo": _photo_url(request, customer.profile_photo),
        "status": "blocked" if customer.is_blocked else "active",
        "referral_code": customer.referral_code or "",
        "created_at": customer.created_at.isoformat(),
    }


class MeUpdateSerializer(serializers.Serializer):
    """context: user, profile, role ("rider" | "customer")."""

    full_name = serializers.CharField(max_length=150, required=False)
    email = serializers.EmailField(required=False)
    profile_photo = serializers.ImageField(required=False)

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
    """GET/PATCH /auth/me/. Phone changes go through phone_send_otp / phone_verify_otp only,
    and customers cannot change their name (staff-controlled)."""
    rider = _rider_of(request.user)
    customer = None if rider else CustomerProfile.objects.select_related("user").filter(user=request.user).first()
    if rider is None and customer is None:
        return fail("Profile not found.", status.HTTP_404_NOT_FOUND)
    if customer is not None and customer.is_blocked:
        return fail("This account has been blocked. Contact support.", status.HTTP_403_FORBIDDEN)

    role = "rider" if rider else "customer"
    profile = rider or customer
    build = _rider_payload if rider else _customer_payload

    if request.method == "GET":
        return ok(**build(request, profile))

    raw = _collect_input(request)
    if "phone" in raw:
        return fail("To change your phone number, verify it with a code (phone verification).")
    if role == "customer" and "full_name" in raw:
        return fail("Your name is locked. Contact support to change it.", status.HTTP_403_FORBIDDEN)

    serializer = MeUpdateSerializer(data=raw, context={"user": profile.user}, partial=True)
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
                if (user.username or "").lower() == old_email:
                    user.username = values["email"]
                    user_fields.append("username")
                if customer is not None:
                    customer.email = values["email"]
            if "profile_photo" in values:
                profile.profile_photo = values["profile_photo"]
            if user_fields:
                user.save(update_fields=user_fields)
            profile.save()
    except IntegrityError:
        return fail("That email is already in use.", status.HTTP_409_CONFLICT)
    return ok("Profile updated.", **build(request, profile))


# ===========================================================================
# 2. PLATFORM
# ===========================================================================
@api_view(["GET"])
@authentication_classes([])
@permission_classes([AllowAny])
def services_list(request):
    qs = Service.objects.filter(is_active=True)
    return ok(services=ServiceSerializer(qs, many=True).data)


@api_view(["GET"])
@authentication_classes([])
@permission_classes([AllowAny])
def zones_list(request):
    qs = Zone.objects.filter(is_active=True).prefetch_related("services")
    return ok(zones=ZoneSerializer(qs, many=True).data)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def system_alerts(request):
    qs = SystemAlert.objects.for_user(request.user)
    return ok(alerts=SystemAlertSerializer(qs, many=True).data)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def system_alert_dismiss(request, alert_id):
    alert = get_object_or_404(SystemAlert, pk=alert_id)
    try:
        alert.dismiss(request.user)
    except DjangoValidationError as exc:
        return dj_fail(exc)
    return ok("Alert dismissed.")


# ===========================================================================
# 3. CUSTOMER: PROFILE, PREFERENCES, ADDRESSES, WALLET, LOYALTY
# ===========================================================================
@api_view(["GET", "PATCH"])
@parser_classes([JSONParser, MultiPartParser, FormParser])
@permission_classes([IsAuthenticated])
@throttle_classes([ProfileThrottle])
@customer_required
def customer_profile(request, customer):
    """GET the profile. PATCH preferred_language / marketing_opt_in / profile_photo only:
    the name is staff-controlled and the phone changes only through OTP verification."""
    if request.method == "GET":
        return ok(data=CustomerProfileSerializer(customer, context={"request": request}).data)

    blocked = [f for f in ("phone", "full_name") if f in request.data]
    if blocked:
        return fail("Name is locked and the phone number must be changed with phone verification.",
                    status.HTTP_403_FORBIDDEN)
    editable = ("preferred_language", "marketing_opt_in", "profile_photo")
    data = {f: request.data[f] for f in editable if f in request.data}
    if not data:
        return fail(f"No valid fields provided. You can update: {', '.join(editable)}.")

    serializer = CustomerProfileSerializer(customer, data=data, partial=True, context={"request": request})
    if not serializer.is_valid():
        return validation_fail(serializer)
    try:
        serializer.save()
    except serializers.ValidationError as exc:
        return drf_fail(exc)
    return ok("Profile updated successfully.", data=serializer.data)


class CustomerPreferenceView(generics.RetrieveUpdateAPIView):
    serializer_class = CustomerPreferenceSerializer
    permission_classes = [IsAuthenticated]
    http_method_names = ["get", "patch", "put", "head", "options"]

    def get_object(self):
        obj, _ = CustomerRiderPreference.objects.get_or_create(account=self.request.user)
        return obj


customer_preferences = CustomerPreferenceView.as_view()


class SavedAddressList(generics.ListCreateAPIView):
    serializer_class = SavedAddressSerializer
    permission_classes = [IsAuthenticated, IsCustomer]
    pagination_class = None

    def get_queryset(self):
        return SavedAddress.objects.filter(customer__user=self.request.user)

    def perform_create(self, serializer):
        serializer.save(customer=CustomerProfile.objects.get(user=self.request.user))


class SavedAddressDetail(generics.RetrieveUpdateDestroyAPIView):
    serializer_class = SavedAddressSerializer
    permission_classes = [IsAuthenticated, IsCustomer]

    def get_queryset(self):
        return SavedAddress.objects.filter(customer__user=self.request.user)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
@throttle_classes([WalletReadThrottle])
@customer_required
def customer_wallet(request, customer):
    """Customer wallet balances + last 20 ledger lines."""
    wallet = CustomerWallet.for_user(request.user)
    recent = wallet.transactions.select_related("related_booking")[:20]
    return ok(
        wallet=CustomerWalletSerializer(wallet).data,
        balance=money(wallet.balance), withdrawable_balance=money(wallet.withdrawable_balance),
        bonus_credit=money(wallet.bonus_credit), currency=wallet.currency,
        transactions=CustomerWalletTransactionSerializer(recent, many=True).data,
    )


@api_view(["GET"])
@permission_classes([IsAuthenticated])
@throttle_classes([WalletReadThrottle])
@customer_required
def customer_wallet_transactions(request, customer):
    wallet = CustomerWallet.for_user(request.user)
    qs = wallet.transactions.select_related("related_booking")
    kind = request.query_params.get("type")
    if kind:
        qs = qs.filter(transaction_type=kind)
    return paginated(request, qs, CustomerWalletTransactionSerializer)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
@customer_required
def loyalty_account(request, customer):
    account, _ = LoyaltyAccount.objects.get_or_create(customer=customer)
    return ok(loyalty=LoyaltyAccountSerializer(account).data)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
@customer_required
def loyalty_transactions(request, customer):
    account, _ = LoyaltyAccount.objects.get_or_create(customer=customer)
    return paginated(request, account.transactions.all(), LoyaltyTransactionSerializer)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
@throttle_classes([PaymentThrottle])
@customer_required
def loyalty_redeem(request, customer):
    serializer = LoyaltyRedeemSerializer(data=request.data)
    if not serializer.is_valid():
        return validation_fail(serializer)
    account, _ = LoyaltyAccount.objects.get_or_create(customer=customer)
    done, message = account.redeem(serializer.validated_data["points"])
    if not done:
        return fail(message)
    return ok(message, points_balance=account.points_balance)

# ===========================================================================
# 4. RIDER ONBOARDING AND PROFILE
# ===========================================================================
class MyRiderProfileView(generics.RetrieveUpdateAPIView):
    """GET/PATCH the authenticated rider's own profile."""

    serializer_class = RiderProfileSerializer
    permission_classes = [IsAuthenticated, IsRider]
    parser_classes = [JSONParser, MultiPartParser, FormParser]
    http_method_names = ["get", "patch", "head", "options"]

    def get_object(self):
        return get_object_or_404(
            RiderProfile.objects.select_related("user"), user=self.request.user, deleted_at__isnull=True)


@api_view(["POST"])
@parser_classes([MultiPartParser, FormParser])
@permission_classes([IsAuthenticated])
@throttle_classes([UploadThrottle])
@rider_required
def rider_submit_documents(request, rider):
    """Upload (or re-upload) one document. When every required document is present the rider
    moves to 'under review' automatically."""
    serializer = RiderDocumentSerializer(data=request.data)
    if not serializer.is_valid():
        return validation_fail(serializer)
    data = serializer.validated_data
    doc_type = data["document_type"]

    if rider.verification_status in (VerificationStatus.SUSPENDED, VerificationStatus.UNDER_REVIEW):
        return fail("Documents cannot be changed while your account is under review or suspended.",
                    status.HTTP_409_CONFLICT)

    try:
        with transaction.atomic():
            approved = rider.documents.filter(
                document_type=doc_type, status=RiderDocument.DocumentStatus.APPROVED
            ).filter(Q(expiry_date__isnull=True) | Q(expiry_date__gte=timezone.localdate())).exists()
            if approved:
                return fail("This document has already been approved.", status.HTTP_409_CONFLICT)
            # A fresh upload replaces any earlier unapproved (or expired) copy of the same type.
            rider.documents.filter(document_type=doc_type).delete()
            document = RiderDocument.objects.create(
                rider_profile=rider, document_type=doc_type,
                file=data["document_file"], expiry_date=data.get("expiry_date"))
    except IntegrityError:
        return fail("This document type has already been submitted.", status.HTTP_409_CONFLICT)
    except Exception:
        logger.exception("Document save failed rider_id=%s type=%s", rider.id, doc_type)
        return fail("We couldn't save your file. Please try again in a moment.",
                    status.HTTP_500_INTERNAL_SERVER_ERROR)

    submitted = False
    if rider.documents_uploaded and rider.verification_status in (
            VerificationStatus.PENDING, VerificationStatus.REJECTED):
        try:
            rider.submit_for_review()
            submitted = True
        except DjangoValidationError as exc:
            logger.warning("Auto-submit failed rider_id=%s: %s", rider.id, exc.messages)
        except Exception:
            logger.exception("Auto-submit crashed rider_id=%s", rider.id)

    return ok(
        "Document uploaded successfully.", status.HTTP_201_CREATED,
        id=str(document.id), document_type=document.document_type, document_status=document.status,
        documents_uploaded=rider.documents_uploaded, submitted_for_review=submitted)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
@rider_required
def rider_documents(request, rider):
    docs = rider.documents.all()
    return ok(documents=RiderDocumentReadSerializer(docs, many=True, context={"request": request}).data,
              missing=rider.missing_required_documents())


def _stage(rider):
    """suspended | approved | under_review | rejected | not_started"""
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
    if document is None:
        return "missing"
    if document.status == RiderDocument.DocumentStatus.APPROVED:
        return "approved"
    if document.status == RiderDocument.DocumentStatus.REJECTED:
        return "rejected"
    return "uploaded"


def _file_url(field_file):
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
    latest = {}
    for doc in rider.documents.all().order_by("uploaded_at"):
        latest[doc.document_type] = doc  # newest wins
    documents = []
    for value, label in RiderDocument.DocumentType.choices:
        doc = latest.get(value)
        documents.append({
            "type": value, "label": label,
            "name": os.path.basename(doc.file.name) if doc and doc.file else None,
            "status": _doc_state(doc), "url": _file_url(doc.file) if doc else None,
            "notes": (doc.notes or "") if doc and doc.status == RiderDocument.DocumentStatus.REJECTED else "",
        })
    return ok(stage=_stage(rider), documents=documents, rejection_reason=rider.rejection_reason or "",
              missing=rider.missing_required_documents())


@api_view(["GET"])
@permission_classes([IsAuthenticated])
@rider_required
def rider_status_history(request, rider):
    return paginated(request, rider.status_history.all(), RiderStatusHistorySerializer)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
@rider_required
def rider_performance(request, rider):
    return ok(performance=RiderPerformanceSerializer(rider).data)


@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated])
@rider_required
def rider_qualifications(request, rider):
    """GET: the rider's service qualifications. POST {service}: apply for one (staff approves)."""
    if request.method == "POST":
        if not rider.is_verified:
            return fail("Get verified before applying for services.", status.HTTP_403_FORBIDDEN)
        try:
            key = pricing.normalize_service_type(request.data.get("service"))
        except ValueError:
            return fail("Unknown service type.")
        service = get_object_or_404(Service, pk=key, is_active=True)
        qualification, created = ServiceQualification.objects.get_or_create(rider=rider, service=service)
        return ok("Application submitted." if created else "Already applied.",
                  status.HTTP_201_CREATED if created else status.HTTP_200_OK,
                  qualification=ServiceQualificationSerializer(qualification).data)
    qs = rider.qualifications.select_related("service")
    return ok(qualifications=ServiceQualificationSerializer(qs, many=True).data)


class PayoutAccountList(generics.ListCreateAPIView):
    serializer_class = RiderPayoutAccountSerializer
    permission_classes = [IsAuthenticated, IsRider]
    pagination_class = None

    def get_queryset(self):
        return RiderPayoutAccount.objects.filter(rider_profile__user=self.request.user, is_deleted=False)

    def perform_create(self, serializer):
        serializer.save(rider_profile=_rider_of(self.request.user))


class PayoutAccountDetail(generics.RetrieveUpdateDestroyAPIView):
    serializer_class = RiderPayoutAccountSerializer
    permission_classes = [IsAuthenticated, IsRider]

    def get_queryset(self):
        return RiderPayoutAccount.objects.filter(rider_profile__user=self.request.user, is_deleted=False)

    def perform_destroy(self, instance):
        instance.soft_delete()  # keeps withdrawal history intact


class EmergencyContactList(generics.ListCreateAPIView):
    serializer_class = RiderEmergencyContactSerializer
    permission_classes = [IsAuthenticated, IsRider]
    pagination_class = None

    def get_queryset(self):
        return RiderEmergencyContact.objects.active().filter(rider_profile__user=self.request.user)

    def perform_create(self, serializer):
        serializer.save(rider_profile=_rider_of(self.request.user))


class EmergencyContactDetail(generics.RetrieveUpdateDestroyAPIView):
    serializer_class = RiderEmergencyContactSerializer
    permission_classes = [IsAuthenticated, IsRider]

    def get_queryset(self):
        return RiderEmergencyContact.objects.active().filter(rider_profile__user=self.request.user)

    def perform_destroy(self, instance):
        instance.soft_delete()


# ===========================================================================
# 5. RIDER ONLINE STATE, LOCATION, NEARBY SEARCH
# ===========================================================================
@api_view(["POST"])
@permission_classes([IsAuthenticated])
@rider_required
def rider_go_online(request, rider):
    wallet = _get_wallet(rider)
    if not rider.can_accept_jobs:
        return fail("Your account is not allowed to take jobs right now.", status.HTTP_403_FORBIDDEN,
                    stage=_stage(rider))
    if wallet.balance < min_wallet_balance():
        return fail(f"Please deposit at least GH₵{money(min_wallet_balance())} to start accepting bookings.",
                    status.HTTP_402_PAYMENT_REQUIRED)
    lat = lng = None
    if "latitude" in request.data or "longitude" in request.data:
        loc = RiderLocationSerializer(data=request.data)
        if not loc.is_valid():
            return validation_fail(loc)
        lat, lng = loc.validated_data["latitude"], loc.validated_data["longitude"]
    try:
        rider.go_online(lat, lng)
    except DjangoValidationError as exc:
        return dj_fail(exc, status.HTTP_403_FORBIDDEN)
    return ok("You are online.", is_online=True)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
@rider_required
def rider_go_offline(request, rider):
    if Booking.objects.filter(rider=request.user, status__in=[
            Booking.Status.ACCEPTED, Booking.Status.EN_ROUTE, Booking.Status.ARRIVED]).exists():
        return fail("Finish or cancel your current job before going offline.", status.HTTP_409_CONFLICT)
    rider.go_offline()
    return ok("You are offline.", is_online=False)


@api_view(["PATCH"])
@permission_classes([IsAuthenticated])
@throttle_classes([LocationThrottle])
@rider_required
def update_rider_location(request, rider):
    """Hot path: one UPDATE (plus one ping row only while a trip is active)."""
    if not rider.is_active:
        return fail("Rider account is inactive.", status.HTTP_403_FORBIDDEN)
    serializer = RiderLocationSerializer(data=request.data)
    if not serializer.is_valid():
        return validation_fail(serializer)
    d = serializer.validated_data
    active = Booking.objects.filter(rider=request.user, status__in=[
        Booking.Status.ACCEPTED, Booking.Status.EN_ROUTE, Booking.Status.ARRIVED]).only("id").first()
    rider.update_location(d["latitude"], d["longitude"], booking=active,
                          speed_kmh=d.get("speed_kmh"), heading=d.get("heading"),
                          store_ping=active is not None)
    return ok("Location updated successfully.", latitude=rider.current_lat,
              longitude=rider.current_lng, updated_at=rider.location_updated_at)


def _available_riders(service=None):
    """Riders who may receive a job right now. (Equivalent to RiderProfile.objects.dispatchable(),
    but excludes busy riders by USER id: Booking.rider is a User, RiderProfile.pk is a UUID.)"""
    now = timezone.now()
    busy = Booking.objects.filter(status__in=list(Booking.ACTIVE_STATUSES), rider__isnull=False).values("rider_id")
    qs = (
        RiderProfile.objects.active().verified().not_suspended().not_restricted()
        .filter(is_online=True, current_lat__isnull=False, current_lng__isnull=False,
                location_updated_at__gte=now - timedelta(seconds=LOCATION_STALE_SECONDS))
        .filter(Q(license_expiry_date__isnull=True) | Q(license_expiry_date__gte=timezone.localdate()))
        .filter(wallet__balance__gte=min_wallet_balance())
        .exclude(user_id__in=busy)
    )
    if service is not None:
        qs = qs.filter(qualifications__service=service,
                       qualifications__status=ServiceQualification.Status.APPROVED)
    return qs.distinct()


def _coarse(value):
    return round(float(value), PRIVATE_COORD_DP)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
@throttle_classes([LocationThrottle])
def get_nearby_riders(request):
    """Legacy search used by the customer app: ?lat=&lon=[&service=okada&radius_km=5&limit=10].
    Also accepts latitude/longitude/lng as aliases. Locations are coarse (~100 m)."""
    q = request.query_params
    params = {
        "latitude": q.get("latitude", q.get("lat")),
        "longitude": q.get("longitude", q.get("lon", q.get("lng"))),
        "service": q.get("service", "okada"),
    }
    for key in ("radius_km", "limit"):
        if q.get(key) is not None:
            params[key] = q.get(key)
    query = LegacyNearbyRidersQuerySerializer(data=params)
    if not query.is_valid():
        return validation_fail(query)
    data = query.validated_data
    service = get_object_or_404(Service, pk=data["service"], is_active=True)
    found = (_available_riders(service).exclude(user=request.user)
             .nearby(data["latitude"], data["longitude"], data["radius_km"], data["limit"]))
    riders = [{
        "id": str(r.pk), "full_name": (r.full_name or "Rider").split(" ")[0],
        "lat": _coarse(r.current_lat), "lon": _coarse(r.current_lng),
        "latitude": _coarse(r.current_lat), "longitude": _coarse(r.current_lng),
        "distance_km": km, "rating_average": r.rating_average,
        "location_updated_at": r.location_updated_at,
    } for r, km in found]
    return ok(service=service.name, count=len(riders), riders=riders, results=riders)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
@throttle_classes([LocationThrottle])
def nearby_riders(request):
    """GET /dispatch/rider/nearby/?lat=&lng=&service_type=&radius_km=&limit="""
    query = NearbyRidersQuerySerializer(data=request.query_params)
    if not query.is_valid():
        return validation_fail(query)
    d = query.validated_data
    service = None
    if d.get("service_type"):
        service = get_object_or_404(Service, pk=d["service_type"], is_active=True)
    found = (_available_riders(service).exclude(user=request.user)
             .nearby(d["lat"], d["lng"], d["radius_km"], d["limit"]))
    items = [SimpleNamespace(
        id=r.pk, full_name=r.full_name, service_type=d.get("service_type"),
        rating_average=r.rating_average, distance_km=km, current_lat=r.current_lat,
        current_lng=r.current_lng) for r, km in found]
    return ok(count=len(items), riders=NearbyRiderSerializer(items, many=True).data)


# ===========================================================================
# 6. RIDER WALLET, TRANSACTIONS, WITHDRAWALS
# ===========================================================================
@api_view(["GET"])
@permission_classes([IsAuthenticated])
@rider_required
def get_rider_wallet(request, rider):
    wallet = _get_wallet(rider)
    recent = wallet.transactions.all()[:10]
    minimum = min_wallet_balance()
    can_go_online = _can_go_online(rider, wallet)
    return ok(
        wallet=RiderWalletSerializer(wallet).data,
        balance=money(wallet.balance), currency=wallet.currency, minimum_required=money(minimum),
        can_go_online=can_go_online,
        transactions=TransactionSerializer(recent, many=True).data,
        message=None if can_go_online else
        f"Please deposit at least GH₵{money(minimum)} to start accepting bookings")


def _sum(condition):
    return Coalesce(Sum("amount", filter=condition), ZERO_EXPR,
                    output_field=DecimalField(max_digits=14, decimal_places=2))


def _apply_filters(qs, f):
    tz = timezone.get_current_timezone()
    if "type" in f:
        qs = qs.filter(tx_type=f["type"])
    if "direction" in f:
        qs = qs.filter(direction=f["direction"])
    if "date_from" in f:
        qs = qs.filter(created_at__gte=timezone.make_aware(datetime.combine(f["date_from"], dtime.min), tz))
    if "date_to" in f:
        end = datetime.combine(f["date_to"] + timedelta(days=1), dtime.min)
        qs = qs.filter(created_at__lt=timezone.make_aware(end, tz))
    if "min_amount" in f:
        qs = qs.filter(amount__gte=f["min_amount"])
    if "max_amount" in f:
        qs = qs.filter(amount__lte=f["max_amount"])
    if f.get("search"):
        term = f["search"].strip()
        qs = qs.filter(Q(reference__icontains=term) | Q(description__icontains=term)
                       | Q(booking_ref__icontains=term))
    return qs.order_by(f["ordering"], "-id")


def _summary(qs):
    credit, debit = Q(direction=WalletTransaction.Direction.CREDIT), Q(direction=WalletTransaction.Direction.DEBIT)
    agg = qs.aggregate(total_in=_sum(credit), total_out=_sum(debit), count=Count("id"))
    return {"total_in": money(agg["total_in"]), "total_out": money(agg["total_out"]),
            "net": money(agg["total_in"] - agg["total_out"]), "count": agg["count"]}


def _months_ago_start(months):
    now = timezone.localtime()
    year, month = now.year, now.month - (months - 1)
    while month <= 0:
        month += 12
        year -= 1
    return now.replace(year=year, month=month, day=1, hour=0, minute=0, second=0, microsecond=0)


def _monthly_trend(wallet, months=6):
    credit, debit = Q(direction=WalletTransaction.Direction.CREDIT), Q(direction=WalletTransaction.Direction.DEBIT)
    rows = (wallet.transactions.filter(created_at__gte=_months_ago_start(months))
            .annotate(month=TruncMonth("created_at")).values("month")
            .annotate(money_in=_sum(credit), money_out=_sum(debit)).order_by("month"))
    return [{"month": r["month"].strftime("%Y-%m"), "money_in": money(r["money_in"]),
             "money_out": money(r["money_out"])} for r in rows]


def _top_up_nudge(wallet):
    minimum = min_wallet_balance()
    low = wallet.balance < minimum
    return {"low_balance": low,
            "message": (f"Your balance is below the GH₵{money(minimum)} needed to receive jobs. "
                        "Top up to go online.") if low else None}


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
        yield writer.writerow(["Date", "Reference", "Type", "Direction", f"Amount ({currency})",
                               f"Balance Before ({currency})", f"Balance After ({currency})",
                               "Booking", "Description"])
        for t in qs.iterator(chunk_size=1000):
            yield writer.writerow([
                timezone.localtime(t.created_at).strftime("%Y-%m-%d %H:%M:%S"), _csv_safe(t.reference),
                t.get_tx_type_display(), t.direction, t.amount, t.balance_before, t.balance_after,
                _csv_safe(t.booking_ref), _csv_safe(t.description)])

    resp = StreamingHttpResponse(rows(), content_type="text/csv; charset=utf-8")
    resp["Content-Disposition"] = f'attachment; filename="wallet-statement-{timezone.now():%Y%m%d}.csv"'
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
    qs = _apply_filters(WalletTransaction.objects.filter(wallet=wallet), filters)
    if filters.get("export") == "csv":
        logger.info("wallet statement export user=%s wallet=%s", request.user.pk, wallet.pk)
        return _csv_response(qs, wallet.currency)
    return paginated(request, qs, TransactionSerializer, success=True, currency=wallet.currency,
                     balance=money(wallet.balance), summary=_summary(qs),
                     monthly_trend=_monthly_trend(wallet), insights=_top_up_nudge(wallet))


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

    fee = SystemConfig.get_decimal("WITHDRAWAL_FEE", "0.00")  # taken out of the amount (net = amount - fee)
    minimum = min_wallet_balance()
    try:
        with transaction.atomic():
            wallet = _get_wallet(rider, lock=True)  # row lock prevents double-spend
            if wallet.balance < amount:
                return fail("Insufficient wallet balance.")
            if wallet.balance - amount < minimum:
                return fail(f"You must keep at least {money(minimum)} in your wallet. "
                            f"Maximum withdrawable: {money(max(wallet.balance - minimum, 0))}.")
            withdrawal = wallet.request_withdrawal(
                amount, method=WithdrawalRequest.Method.MOBILE_MONEY, account_name=account.account_name,
                account_number=re.sub(r"\D", "", account.momo_number), provider=account.network,
                fee=fee, idempotency_key=(request.headers.get("Idempotency-Key") or "")[:100],
                user=request.user)
    except DjangoValidationError as exc:
        return dj_fail(exc)

    wallet.refresh_from_db()
    logger.info("Withdrawal requested: rider_id=%s reference=%s", rider.id, withdrawal.reference)
    return ok("Withdrawal request submitted.", status.HTTP_201_CREATED,
              withdrawal=WithdrawalSerializer(withdrawal).data, withdrawal_id=str(withdrawal.id),
              reference=withdrawal.reference, amount=money(withdrawal.amount), fee=money(withdrawal.fee),
              new_balance=money(wallet.balance))


@api_view(["GET"])
@permission_classes([IsAuthenticated])
@throttle_classes([WalletReadThrottle])
@rider_required
def rider_withdrawals(request, rider):
    wallet = _get_wallet(rider)
    qs = wallet.withdrawals.all()
    if request.query_params.get("status"):
        qs = qs.filter(status=request.query_params["status"].lower())
    return paginated(request, qs, WithdrawalSerializer)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
@rider_required
def cancel_withdrawal(request, rider, withdrawal_id):
    withdrawal = get_object_or_404(WithdrawalRequest, pk=withdrawal_id, wallet__rider=rider)
    try:
        withdrawal.cancel(user=request.user)
    except DjangoValidationError as exc:
        return dj_fail(exc, status.HTTP_409_CONFLICT)
    return ok("Withdrawal cancelled.", withdrawal=WithdrawalSerializer(withdrawal).data)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
@throttle_classes([WalletReadThrottle])
@rider_required
def deposit_history(request, rider):
    """Deposits credited to the rider's wallet, newest first."""
    wallet = _get_wallet(rider)
    qs = wallet.transactions.filter(tx_type=WalletTransaction.Type.DEPOSIT)
    return paginated(request, qs, TransactionSerializer, success=True,
                     message="Deposit history retrieved.")


# ===========================================================================
# 7. DISPATCH OFFERS
# ===========================================================================
@api_view(["GET"])
@permission_classes([IsAuthenticated])
@rider_required
def rider_offers(request, rider):
    """The rider's live (pending, not yet expired) job offers."""
    qs = (DispatchOffer.objects.filter(rider=rider, status=DispatchOffer.Status.PENDING,
                                       expires_at__gt=timezone.now()).select_related("booking"))
    return ok(offers=DispatchOfferSerializer(qs, many=True).data)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
@rider_required
def accept_offer(request, rider, offer_id):
    """Accept a dispatch offer. All locking and double-assignment protection lives in dispatch.py."""
    from . import dispatch  # lazy: a problem in dispatch must not stop the whole URLconf loading
    try:
        dispatch.accept_offer(offer_id, rider)
    except dispatch.OfferError as exc:
        return fail(exc.message, exc.status_code)
    except (ValueError, DjangoValidationError):
        return fail("Offer not found.", status.HTTP_404_NOT_FOUND)
    return ok("Offer accepted. Head to the pickup point.")


@api_view(["POST"])
@permission_classes([IsAuthenticated])
@rider_required
def decline_offer(request, rider, offer_id):
    """Decline a dispatch offer; the engine offers the booking to the next rider."""
    from . import dispatch
    serializer = DeclineOfferSerializer(data=request.data)
    if not serializer.is_valid():
        return validation_fail(serializer)
    try:
        dispatch.decline_offer(offer_id, rider, serializer.validated_data["reason"])
    except (ValueError, DjangoValidationError):
        return fail("Offer not found.", status.HTTP_404_NOT_FOUND)
    return ok("Offer declined.")


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def booking_offers(request, reference):
    """Legacy price offers (BookingOffer) on one of the customer's bookings."""
    booking = _get_booking(request.user, reference)
    qs = BookingOffer.objects.filter(booking_ref=booking.reference).select_related("rider")
    return ok(offers=BookingOfferSerializer(qs, many=True).data)


# ===========================================================================
# 8. PAYMENTS (PAYSTACK)
# Flow: booking create / initialize_payment -> user pays on Paystack ->
#       verify_payment (client) and/or paystack_webhook (server) -> settle once.
# Settlement is idempotent (Payment.mark_as_paid), so both paths can fire safely.
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
    def __init__(self):
        super().__init__("Payment is still being processed.", status.HTTP_503_SERVICE_UNAVAILABLE)


def _paystack_request(method, path, **kwargs):
    secret = getattr(settings, "PAYSTACK_SECRET_KEY", "")
    if not secret:
        logger.error("PAYSTACK_SECRET_KEY is not configured.")
        raise PaymentError("Payment service is currently unavailable.", status.HTTP_503_SERVICE_UNAVAILABLE)
    try:
        response = requests.request(method, f"{PAYSTACK_BASE_URL}{path}",
                                    headers={"Authorization": f"Bearer {secret}"},
                                    timeout=PAYSTACK_TIMEOUT, **kwargs)
    except requests.exceptions.RequestException:
        logger.exception("Paystack request failed: %s %s", method, path)
        raise PaymentError("Could not reach the payment service. Please try again.",
                           status.HTTP_503_SERVICE_UNAVAILABLE) from None
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


def _paystack_initialize(*, email, amount, reference, metadata, channels=None):
    payload = {
        "email": email,
        "amount": int((Decimal(str(amount)) * 100).to_integral_value(ROUND_HALF_UP)),  # pesewas
        "currency": CURRENCY, "reference": reference, "metadata": metadata,
    }
    if channels:
        payload["channels"] = channels
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
    return f"{prefix}-{uuid.uuid4().hex.upper()}"


def _start_wallet_deposit(user, amount, *, role, owner_id):
    if not user.email:
        return fail("Add an email to your account before depositing.")
    if amount < MIN_DEPOSIT:
        return fail(f"Minimum deposit is {money(MIN_DEPOSIT)}.")
    if amount > MAX_DEPOSIT:
        return fail(f"Maximum deposit is {money(MAX_DEPOSIT)}.")
    reference = _new_reference("DEP")
    metadata = {"purpose": PURPOSE_DEPOSIT, "role": role}
    metadata["rider_id" if role == "rider" else "user_id"] = str(owner_id)
    try:
        data = _paystack_initialize(email=user.email, amount=amount, reference=reference, metadata=metadata)
    except PaymentError as exc:
        return fail(exc.message, exc.status_code)
    return ok("Complete the payment to fund your wallet.", status.HTTP_201_CREATED,
              reference=reference, amount=money(amount), authorization_url=data["authorization_url"],
              access_code=data.get("access_code"))


def _start_booking_payment(user, booking):
    """Create a pending Payment + Paystack checkout for a DRAFT/PENDING booking. Raises PaymentError."""
    if not user.email:
        raise PaymentError("Add an email to your account before paying.")
    method = booking.payment_method
    if method not in (Booking.PaymentMethod.MOBILE_MONEY, Booking.PaymentMethod.CARD):
        raise PaymentError("This booking is not payable online.", status.HTTP_409_CONFLICT)
    amount = Decimal(str(booking.total_fare)).quantize(TWO_PLACES)
    if amount <= 0:
        raise PaymentError("This booking has nothing to pay.")
    reference = generate_payment_reference()
    checkout = _paystack_initialize(
        email=user.email, amount=amount, reference=reference,
        metadata={"purpose": PURPOSE_BOOKING, "booking_ref": booking.reference},
        channels=["mobile_money"] if method == Booking.PaymentMethod.MOBILE_MONEY else ["card"])
    Payment.objects.create(
        booking_ref=booking.reference, reference=reference, amount=amount, currency=booking.currency,
        method=method, payer_profile_id=CustomerProfile.objects.filter(user=user).values_list("pk", flat=True).first(),
        expires_at=timezone.now() + PAYMENT_WINDOW, metadata={"provider": "paystack"})
    return {"required": True, "reference": reference, "amount": money(amount),
            "payment_url": checkout["authorization_url"], "access_code": checkout.get("access_code")}


def _settle_reference(reference, requesting_user=None):
    """Verify a Paystack reference and apply it exactly once. requesting_user=None means a trusted
    server caller (the webhook); otherwise the reference must belong to that user."""
    paystack = _paystack_verify(reference)
    metadata = paystack.get("metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    try:
        if metadata.get("purpose") == PURPOSE_DEPOSIT:
            return _settle_wallet_deposit(reference, paystack, metadata, requesting_user)
        return _settle_booking_payment(reference, paystack, requesting_user)
    except DjangoValidationError as exc:
        raise PaymentError(" ".join(exc.messages), status.HTTP_409_CONFLICT) from None
    except IntegrityError:
        raise PaymentError("Payment recording conflict. Reference may already exist.",
                           status.HTTP_409_CONFLICT) from None


def _settle_booking_payment(reference, paystack, requesting_user):
    """Mark the Payment paid (Payment.mark_as_paid), the Booking paid, and release it to dispatch.
    The rider is credited later, when the trip completes (_on_booking_completed)."""
    verified_amount = _paystack_amount(paystack)
    not_found = PaymentError("No booking found for this payment reference.", status.HTTP_404_NOT_FOUND)

    with transaction.atomic():  # any PaymentError raised inside rolls everything back
        payment = Payment.objects.select_for_update().filter(reference=reference).first()
        if payment is None:
            raise not_found
        booking = Booking.objects.select_for_update().filter(reference=payment.booking_ref).first()
        if booking is None:
            raise not_found
        if requesting_user is not None and booking.customer_id != requesting_user.id:
            raise not_found

        summary = {"booking_id": str(booking.id), "booking_ref": booking.reference,
                   "total_amount": money(payment.amount)}
        if payment.is_successful or payment.status == Payment.Status.REFUNDED:
            return {"created": False, "message": "Payment already verified and recorded.",
                    "payment_status": "paid", "status": "paid", **summary}
        if abs(verified_amount - payment.amount) > AMOUNT_TOLERANCE:
            logger.error("Amount mismatch booking_ref=%s paid=%s expected=%s",
                         booking.reference, verified_amount, payment.amount)
            raise PaymentError("Payment amount does not match booking total.", status.HTTP_409_CONFLICT)

        method = CHANNEL_TO_METHOD.get(paystack.get("channel"), payment.method)
        Payment.objects.filter(pk=payment.pk).update(method=method)
        try:
            payment.mark_as_paid(transaction_id=str(paystack.get("id", "")))
        except DjangoValidationError as exc:
            if getattr(exc, "code", "") == "duplicate_payment":
                logger.error("DUPLICATE PAYMENT booking_ref=%s extra_reference=%s", booking.reference, reference)
            raise PaymentError(" ".join(exc.messages), status.HTTP_409_CONFLICT) from None

        if booking.status in (Booking.Status.CANCELLED, Booking.Status.FAILED):
            Refund.request(payment, Refund.Reason.SERVICE_ISSUE,
                           reason_details="Paid after the booking was cancelled.",
                           idempotency_key=f"late:{booking.reference}")
            logger.error("Payment received for %s booking %s ref=%s", booking.status, booking.reference, reference)
            return {"created": False, "payment_status": "paid", "status": "paid",
                    "message": "This booking was cancelled. Your payment will be refunded.", **summary}

        Booking.objects.filter(pk=booking.pk).update(
            payment_status=Booking.PaymentStatus.PAID, payment_method=method)
        booking.refresh_from_db()
        if booking.status == Booking.Status.DRAFT:
            booking.submit()
            _log_status(booking, Booking.Status.PENDING, "Payment confirmed", booking.customer)
            transaction.on_commit(lambda: _try("dispatch", _start_dispatch, booking))

    logger.info("Booking payment settled: booking_ref=%s reference=%s", booking.reference, reference)
    return {"created": True, "message": "Payment verified successfully.", "payment_status": "paid",
            "status": "paid", "booking_status": booking.status, **summary}


def _settle_wallet_deposit(reference, paystack, metadata, requesting_user):
    amount = _paystack_amount(paystack).quantize(TWO_PLACES, rounding=ROUND_HALF_UP)
    not_found = PaymentError("Deposit could not be matched to an account.", status.HTTP_404_NOT_FOUND)
    role = metadata.get("role", "rider")

    with transaction.atomic():
        if role == "customer":
            try:
                user = User.objects.get(pk=metadata.get("user_id"))
            except (User.DoesNotExist, ValueError, TypeError):
                raise not_found from None
            if requesting_user is not None and user.pk != requesting_user.pk:
                raise not_found
            wallet = CustomerWallet.for_user(user)
            already = CustomerWalletTransaction.objects.filter(reference=reference).exists()
            if not already:
                done, message = wallet.deposit(amount, reference=reference,
                                               description=f"Wallet deposit - GHS {money(amount)}")
                if not done:
                    raise PaymentError(message, status.HTTP_409_CONFLICT)
            wallet.refresh_from_db()
            extra = {}
        else:
            try:
                rider = RiderProfile.objects.select_related("user").get(pk=metadata.get("rider_id"))
            except (RiderProfile.DoesNotExist, ValueError, TypeError, DjangoValidationError):
                raise not_found from None
            if requesting_user is not None and rider.user_id != requesting_user.id:
                raise not_found
            wallet = _get_wallet(rider, lock=True)  # serialises concurrent settlements
            key = f"paystack:{reference}"
            already = wallet.transactions.filter(idempotency_key=key).exists()
            if not already:
                wallet.post(amount, WalletTransaction.Type.DEPOSIT, description=f"Wallet deposit - GHS {money(amount)}",
                            related_reference=reference[:50], idempotency_key=key)
            wallet.refresh_from_db()
            extra = {"can_go_online": _can_go_online(rider, wallet)}

    created = not already
    if created:
        logger.info("Wallet deposit settled: role=%s reference=%s", role, reference)
    return {"created": created, "message": "Deposit successful." if created else "Deposit already recorded.",
            "reference": reference, "amount": money(amount), "new_balance": money(wallet.balance),
            "status": "paid", **extra}


def _settle_response(reference, user):
    try:
        result = _settle_reference(reference, requesting_user=user)
    except PaymentPending:
        return ok("Payment is still being processed.", status.HTTP_202_ACCEPTED,
                  payment_status="pending", status="pending", retry_after=POLL_INTERVAL)
    except PaymentError as exc:
        return fail(exc.message, exc.status_code, status="failed" if exc.status_code == 400 else "error")
    created = result.pop("created")
    message = result.pop("message")
    return ok(message, status.HTTP_201_CREATED if created else status.HTTP_200_OK, **result)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
@throttle_classes([PaymentThrottle])
@rider_required
def wallet_deposit(request, rider):
    """Start a rider wallet top-up. The wallet is NEVER credited here: only after Paystack
    confirms payment (deposit_verify or the webhook)."""
    serializer = AmountSerializer(data=request.data)
    if not serializer.is_valid():
        return validation_fail(serializer)
    return _start_wallet_deposit(request.user, serializer.validated_data["amount"],
                                 role="rider", owner_id=rider.pk)


deposit_initiate = wallet_deposit  # old URL name kept


@api_view(["POST"])
@permission_classes([IsAuthenticated])
@throttle_classes([PaymentThrottle])
@customer_required
def customer_wallet_deposit(request, customer):
    serializer = AmountSerializer(data=request.data)
    if not serializer.is_valid():
        return validation_fail(serializer)
    return _start_wallet_deposit(request.user, serializer.validated_data["amount"],
                                 role="customer", owner_id=request.user.pk)


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
        rider = _rider_of(request.user)
        if rider is not None:
            return _start_wallet_deposit(request.user, data["amount"], role="rider", owner_id=rider.pk)
        if _customer_of(request.user) is not None:
            return _start_wallet_deposit(request.user, data["amount"], role="customer", owner_id=request.user.pk)
        return fail("Only riders and customers can fund a wallet.", status.HTTP_403_FORBIDDEN)

    # Booking payment: the amount always comes from the server, never the client.
    booking = Booking.objects.filter(reference=data["booking_ref"], customer=request.user).first()
    if booking is None:
        return fail("Booking not found.", status.HTTP_404_NOT_FOUND)
    if booking.payment_status == Booking.PaymentStatus.PAID:
        return fail("This booking has already been paid.", status.HTTP_409_CONFLICT)
    if booking.status not in (Booking.Status.DRAFT, Booking.Status.PENDING):
        return fail("This booking can no longer be paid.", status.HTTP_409_CONFLICT)
    try:
        info = _start_booking_payment(request.user, booking)
    except PaymentError as exc:
        return fail(exc.message, exc.status_code)
    return ok("Complete the payment to confirm your booking.", status.HTTP_201_CREATED,
              authorization_url=info["payment_url"], **info)


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
@permission_classes([IsAuthenticated, IsCustomer])
def my_payments(request):
    refs = Booking.objects.filter(customer=request.user).values("reference")
    return paginated(request, Payment.objects.filter(booking_ref__in=refs), PaymentSerializer)


@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated, IsCustomer])
@throttle_classes([PaymentThrottle])
def refunds(request):
    """GET: my refunds. POST: request a refund on one of my paid payments."""
    refs = Booking.objects.filter(customer=request.user).values("reference")
    if request.method == "GET":
        qs = Refund.objects.filter(payment__booking_ref__in=refs).select_related("payment")
        return paginated(request, qs, RefundSerializer)

    serializer = RefundRequestSerializer(data=request.data)
    if not serializer.is_valid():
        return validation_fail(serializer)
    d = serializer.validated_data
    payment = Payment.objects.filter(reference=d["payment_reference"], booking_ref__in=refs).first()
    if payment is None:
        return fail("Payment not found.", status.HTTP_404_NOT_FOUND)
    try:
        refund = Refund.request(payment, d["reason"], amount=d.get("amount"), reason_details=d["reason_details"],
                                requested_by=request.user, idempotency_key=d["idempotency_key"])
    except DjangoValidationError as exc:
        return dj_fail(exc)
    return ok("Refund requested.", status.HTTP_201_CREATED, refund=RefundSerializer(refund).data)


def _client_ip_or_none(request):
    return _client_ip(request)


@api_view(["POST"])
@authentication_classes([])
@permission_classes([AllowAny])
@throttle_classes([])
def paystack_webhook(request):
    """Server-to-server notification from Paystack. Settles payments even when the user closes the
    app before verify_payment is called. The payload is never trusted for amounts: every settlement
    re-verifies the reference with Paystack."""
    secret = getattr(settings, "PAYSTACK_SECRET_KEY", "")
    raw_body = request.body  # read BEFORE touching request.data
    if not verify_hmac_signature(raw_body, request.headers.get("X-Paystack-Signature", ""), secret,
                                 algorithm="sha512"):
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
            record, _created = WebhookEvent.record(
                "charge.success", event, source="paystack", headers=request.headers,
                external_id=str(event_data.get("id") or reference)[:120],
                raw_body=raw_body.decode("utf-8", "replace"), ip_address=_client_ip(request),
                signature_valid=True)
            if record.is_finished:
                return Response(status=status.HTTP_200_OK)  # duplicate delivery

            def handler(_event):
                try:
                    return _settle_reference(reference).get("message", "")
                except PaymentPending:
                    raise
                except PaymentError as exc:
                    if exc.retryable:
                        raise  # processing marks it failed; Paystack's retry runs it again
                    logger.warning("Webhook payment not settled ref=%s: %s", reference, exc.message)
                    SystemAlert.objects.create(
                        title="Payment needs attention", level=SystemAlert.Level.ERROR,
                        message=f"Reference {reference} could not be settled: {exc.message}",
                        is_dismissible=False, expires_at=timezone.now() + timedelta(days=7))
                    return f"Not settled: {exc.message}"

            record.process(handler)
            if record.status == WebhookEvent.Status.FAILED and record.can_retry:
                return Response(status=status.HTTP_503_SERVICE_UNAVAILABLE)  # ask Paystack to retry
    return Response(status=status.HTTP_200_OK)



# 9. BOOKING HELPERS
# ===========================================================================
JOB_STATUSES = (Booking.Status.ACCEPTED, Booking.Status.EN_ROUTE, Booking.Status.ARRIVED)
RIDER_ALLOWED_MOVES = (Booking.Status.EN_ROUTE, Booking.Status.ARRIVED, Booking.Status.COMPLETED)
AVG_SPEED_KMH_DEFAULT = "25"


def _q(value):
    return Decimal(str(value)).quantize(TWO_PLACES, rounding=ROUND_HALF_UP)


def _get_booking(user, reference, *, lock=False):
    """The booking if `user` is its customer, its assigned rider, or staff. Raises Http404."""
    qs = Booking.objects.select_related("customer", "rider").filter(
        reference=(reference or "").strip().upper())
    if lock:
        qs = qs.select_for_update(of=("self",))  # rider is a nullable join; lock only the booking row
    if not user.is_staff:
        qs = qs.filter(Q(customer=user) | Q(rider=user))
    booking = qs.first()
    if booking is None:
        raise Http404("Booking not found.")
    return booking


def _log_status(booking, new_status, note="", user=None):
    _try("booking status history", BookingStatusHistory.objects.create, booking=booking,
         status=str(getattr(new_status, "value", new_status)), notes=(note or "")[:255], changed_by=user)


def _start_dispatch(booking):
    """Hand a PENDING booking to the dispatch engine (dispatch.py). Scheduled jobs further out
    than DISPATCH_LEAD are picked up later by the scheduler instead."""
    if booking.scheduled_for and booking.scheduled_for - timezone.now() > DISPATCH_LEAD:
        return
    try:
        from . import dispatch
    except ImportError:
        logger.error("dispatch.py is missing; booking %s was not dispatched.", booking.reference)
        return
    starter = getattr(dispatch, "start_dispatch", None) or getattr(dispatch, "dispatch_booking", None)
    if starter is None:
        logger.error("dispatch.start_dispatch is not defined; booking %s not dispatched.", booking.reference)
        return
    starter(booking)


def _submit_and_dispatch(booking, note, user):
    if booking.status == Booking.Status.DRAFT:
        booking.submit()
        _log_status(booking, Booking.Status.PENDING, note, user)
    transaction.on_commit(lambda: _try("dispatch", _start_dispatch, booking))


def _detail_serializer_for(user, booking):
    if user.is_staff:
        return StaffBookingDetailSerializer
    if booking.rider_id == user.pk:
        return RiderBookingDetailSerializer
    return BookingDetailSerializer


# ---- fare split + trip settlement ------------------------------------------------
def _fare_split(booking, rider):
    """Rider earnings / platform revenue for a completed booking.

    Reads the quote snapshot the booking was created from (booking.details["fare"], written by
    BookingCreateSerializer from pricing.fare_snapshot). Falls back to the service's rate card if the
    snapshot is missing. Invariant: total == rider_earnings + platform_revenue."""
    total = _q(booking.total_fare or ZERO_D)
    snap = (booking.details or {}).get("fare") or {}
    try:
        trip_earnings = Decimal(snap["ride_fare"]) + Decimal(snap["stop_fees"]) + Decimal(snap["waiting_charge"])
        base_rate = Decimal(snap["commission_rate"])
        booking_fee = Decimal(snap["booking_fee"])
        discount = Decimal(snap["discount"])
    except (KeyError, ArithmeticError, TypeError):
        rate = pricing.RATES[booking.service_type]
        booking_fee = rate.booking_fee
        discount = _q(booking.discount_amount or ZERO_D)
        trip_earnings = max(total + discount - booking_fee, ZERO_D)
        base_rate = rate.commission_rate
    rate_now = max(base_rate - rider.tier_commission_discount, ZERO_D)  # better tiers keep more
    commission = _q(trip_earnings * rate_now)
    revenue = min(max(_q(commission + booking_fee - discount), ZERO_D), total)
    return {"total": total, "commission": commission, "platform_revenue": revenue,
            "rider_earnings": _q(total - revenue)}


def _record_cash_payment(booking, collector):
    """The rider collected cash: record a settled Payment so reports and refunds stay consistent."""
    if Payment.objects.filter(booking_ref=booking.reference, status__in=Payment.SUCCESS_STATUSES).exists():
        return
    payment = Payment.objects.filter(booking_ref=booking.reference, method=Payment.Method.CASH,
                                     status=Payment.Status.PENDING).first()
    if payment is None:
        payment = Payment.objects.create(
            booking_ref=booking.reference, amount=_q(booking.total_fare), currency=booking.currency,
            method=Payment.Method.CASH,
            payer_profile_id=CustomerProfile.objects.filter(user_id=booking.customer_id)
            .values_list("pk", flat=True).first())
    payment.mark_as_paid(user=collector)
    Booking.objects.filter(pk=booking.pk).update(payment_status=Booking.PaymentStatus.PAID)


def _settle_trip_money(booking):
    """Move the money for a completed trip exactly once (idempotent ledger keys).
      * paid online / by wallet -> credit the rider's earnings
      * cash                    -> the rider holds the cash, so debit the platform's share"""
    booking.refresh_from_db()
    if (booking.details or {}).get("settlement"):
        return
    rider = _rider_of(booking.rider) if booking.rider_id else None
    if rider is None:
        return
    split = _fare_split(booking, rider)
    wallet = _get_wallet(rider)
    method = "digital" if booking.payment_status == Booking.PaymentStatus.PAID else "cash"
    settled = True
    try:
        if method == "digital":
            if split["rider_earnings"] > 0:
                wallet.post(split["rider_earnings"], WalletTransaction.Type.EARNING,
                            description=f"Trip earnings {booking.reference}", booking_ref=booking.reference,
                            idempotency_key=f"trip-earn:{booking.reference}")
        else:
            _record_cash_payment(booking, booking.rider)
            if split["platform_revenue"] > 0:
                wallet.post(split["platform_revenue"], WalletTransaction.Type.COMMISSION,
                            description=f"Platform fee {booking.reference}", booking_ref=booking.reference,
                            idempotency_key=f"trip-fee:{booking.reference}")
    except DjangoValidationError as exc:
        settled = False
        logger.error("Trip settlement failed booking=%s: %s", booking.reference, exc.messages)
        audit(None, "trip.settlement_failed", booking, reason=" ".join(exc.messages), **{
            k: v for k, v in split.items()})
    details = dict(booking.details or {})
    details["settlement"] = {**{k: str(v) for k, v in split.items()}, "method": method, "settled": settled}
    Booking.objects.filter(pk=booking.pk).update(details=details)  # update(): never re-prices the fare
    if not settled:
        _notify(booking.rider, "Platform fee outstanding",
                f"Top up your wallet: GH₵{money(split['platform_revenue'])} is due for {booking.reference}.",
                Notification.Category.PAYMENT)


def _record_customer_completion(customer, booking):
    CustomerProfile.objects.filter(pk=customer.pk).update(
        total_bookings=F("total_bookings") + 1,
        total_spent=F("total_spent") + (booking.total_fare or ZERO_D),
        last_booking_at=timezone.now())
    customer.refresh_from_db(fields=["total_bookings", "total_spent", "last_booking_at"])
    customer.recompute_segment()


def _pay_referral_reward(user, amount, reference, description):
    """Pay to the right wallet: riders earn into the rider wallet, everyone else gets bonus credit."""
    rider = _rider_of(user)
    if rider is not None:
        _get_wallet(rider).post(amount, WalletTransaction.Type.BONUS, description=description,
                                idempotency_key=reference)
        return True
    done, _ = CustomerWallet.for_user(user).add_bonus(amount, reference=reference, description=description,
                                                      tx_type="referral_bonus")
    return done


def _complete_customer_referral(user):
    referral = Referral.objects.filter(referred_user=user, status=Referral.Status.ACTIVE) \
        .select_related("referrer").first()
    if referral is None:
        return
    referral.complete()
    if referral.reward_amount > 0:
        ref = f"REFR-{referral.code}"
        if _pay_referral_reward(referral.referrer, referral.reward_amount, ref, "Referral reward"):
            referral.mark_reward_paid(ref)
            _notify(referral.referrer, "Referral reward", f"You earned GH₵{money(referral.reward_amount)}.",
                    Notification.Category.PROMO)
    if referral.referee_reward_amount > 0:
        _pay_referral_reward(user, referral.referee_reward_amount, f"REFE-{referral.code}", "Welcome reward")


def _on_booking_completed(booking):
    """Everything that follows a completed trip. Money first; the rest is non-critical."""
    _try("trip money", _settle_trip_money, booking)
    booking.refresh_from_db()
    customer = _customer_of(booking.customer)
    if customer is not None:
        _try("customer stats", _record_customer_completion, customer, booking)
        account, _ = LoyaltyAccount.objects.get_or_create(customer=customer)
        _try("loyalty", account.earn, booking.total_fare or ZERO_D, booking)
        _try("customer referral", _complete_customer_referral, booking.customer)
    rider = _rider_of(booking.rider) if booking.rider_id else None
    if rider is not None:
        _try("rider stats", rider.record_trip_completed)
    _notify(booking.customer, "Trip completed", f"Booking {booking.reference} is complete. Rate your rider.",
            Notification.Category.BOOKING, {"booking_ref": booking.reference})


def _unwind_booking(booking, *, fee=ZERO_D, user=None, reason=""):
    """After a cancel/fail: free the promo, close open offers, refund what was prepaid (less any
    cancellation fee, which is kept and shared with the rider), and bill unpaid fees to the customer."""
    for redemption in PromoRedemption.objects.filter(booking_ref=booking.reference,
                                                     status=PromoRedemption.Status.REDEEMED):
        _try("promo release", redemption.release, reason or "Booking ended")
    DispatchOffer.objects.filter(booking=booking, status=DispatchOffer.Status.PENDING).update(
        status=DispatchOffer.Status.CANCELLED, responded_at=timezone.now())

    payment = Payment.objects.filter(booking_ref=booking.reference,
                                     status__in=[Payment.Status.PAID, Payment.Status.PARTIALLY_REFUNDED]).first()
    kept = ZERO_D
    if payment is not None:
        kept = min(fee, payment.refundable_amount)
        refundable = _q(payment.refundable_amount - kept)
        if refundable > 0:
            if payment.method == Payment.Method.WALLET:
                CustomerWallet.for_user(booking.customer).refund_to_wallet(
                    refundable, booking, reference=f"RFD-{booking.reference}",
                    description=f"Refund {booking.reference}")
                payment.refund(refundable, reason=reason or "Booking cancelled")
            else:
                Refund.request(payment, Refund.Reason.CUSTOMER_REQUEST, amount=refundable,
                               reason_details=reason, requested_by=None if (user and user.is_staff) else user,
                               idempotency_key=f"cancel:{booking.reference}")
        if booking.status == Booking.Status.CANCELLED and booking.payment_status == Booking.PaymentStatus.PAID:
            Booking.objects.filter(pk=booking.pk).update(payment_status=Booking.PaymentStatus.REFUNDED)

    owed = _q(fee - kept)
    if owed > 0:  # nothing prepaid to keep it from: block new bookings until it is paid
        CustomerProfile.objects.filter(user_id=booking.customer_id).update(
            outstanding_fees=F("outstanding_fees") + owed)
    if kept > 0 and booking.rider_id:
        rider = _rider_of(booking.rider)
        if rider is not None:
            share = _q(kept * (1 - pricing.RATES[booking.service_type].commission_rate))
            if share > 0:
                _get_wallet(rider).post(share, WalletTransaction.Type.EARNING,
                                        description=f"Cancellation fee {booking.reference}",
                                        booking_ref=booking.reference,
                                        idempotency_key=f"cancel-fee:{booking.reference}")
    return {"fee": str(fee), "fee_kept_from_payment": str(kept), "fee_billed_to_account": str(owed)}


# ===========================================================================
# 10. PRICE QUOTES
# ===========================================================================
def _point(data, prefix):
    """Read a coordinate pair written as prefix_lat/prefix_lng, prefix_latitude/..., or a nested
    {prefix: {lat, lng}} object. Returns (lat, lng) or (None, None)."""
    nested = data.get(prefix)
    if isinstance(nested, dict):
        return (nested.get("lat", nested.get("latitude")),
                nested.get("lng", nested.get("lon", nested.get("longitude"))))
    lat = next((data[k] for k in (f"{prefix}_lat", f"{prefix}_latitude") if data.get(k) is not None), None)
    lng = next((data[k] for k in (f"{prefix}_lng", f"{prefix}_lon", f"{prefix}_longitude")
                if data.get(k) is not None), None)
    return lat, lng


def _as_coord(value, name, low, high):
    try:
        number = Decimal(str(value))
    except (ArithmeticError, TypeError, ValueError):
        raise serializers.ValidationError(f"{name} must be a number.") from None
    if not number.is_finite() or not (low <= number <= high):
        raise serializers.ValidationError(f"{name} is out of range.")
    return number.quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)


class QuoteInputSerializer(serializers.Serializer):
    """Accepts the field spellings the web and mobile apps use; see _point()."""

    service_type = serializers.CharField()
    promo_code = serializers.CharField(required=False, allow_blank=True)
    wait_minutes = serializers.ListField(child=serializers.DecimalField(
        max_digits=6, decimal_places=1, min_value=0), required=False)
    stops = serializers.ListField(child=serializers.DictField(), required=False)

    def validate(self, attrs):
        raw = self.initial_data
        raw = raw if isinstance(raw, dict) else dict(raw.items())
        try:
            attrs["service_key"] = pricing.normalize_service_type(attrs["service_type"])
        except ValueError:
            raise serializers.ValidationError({"service_type": "Unknown service type."}) from None

        lat, lng = _point(raw, "pickup")
        if lat is None or lng is None:
            raise serializers.ValidationError({"pickup": "Pickup latitude and longitude are required."})
        attrs["pickup"] = (_as_coord(lat, "Pickup latitude", Decimal(-90), Decimal(90)),
                           _as_coord(lng, "Pickup longitude", Decimal(-180), Decimal(180)))

        d_lat, d_lng = _point(raw, "destination")
        if d_lat is None or d_lng is None:
            if attrs["service_key"] != "waste-bin":  # waste is collected at the pickup point
                raise serializers.ValidationError(
                    {"destination": "Destination latitude and longitude are required."})
            attrs["destination"] = attrs["pickup"]
        else:
            attrs["destination"] = (_as_coord(d_lat, "Destination latitude", Decimal(-90), Decimal(90)),
                                    _as_coord(d_lng, "Destination longitude", Decimal(-180), Decimal(180)))

        stops, waits = [], []
        per_stop_waits = [Decimal(str(w)) for w in attrs.get("wait_minutes", [])]
        for index, stop in enumerate(attrs.get("stops", [])):
            s_lat, s_lng = _point({"s": stop}, "s")
            if s_lat is None or s_lng is None:
                raise serializers.ValidationError({"stops": f"Stop {index + 1} needs latitude and longitude."})
            stops.append({"lat": _as_coord(s_lat, "Stop latitude", Decimal(-90), Decimal(90)),
                          "lng": _as_coord(s_lng, "Stop longitude", Decimal(-180), Decimal(180))})
            wait = stop.get("wait_minutes", per_stop_waits[index] if index < len(per_stop_waits) else 0)
            waits.append(min(max(pricing._dec(wait), ZERO_D), Decimal(pricing.MAX_WAIT_INPUT_MINUTES)))
        attrs["stops_clean"], attrs["waits"] = stops, waits
        return attrs


def _promo_terms(promo):
    if promo.discount_type == PromoCode.DiscountType.PERCENTAGE:
        return pricing.PromoTerms(code=promo.code, percent_off=promo.discount_value,
                                  max_discount=promo.maximum_discount)
    return pricing.PromoTerms(code=promo.code, flat_off=promo.discount_value)


def _quote_for(user, customer, data):
    """Build a priced, signed quote. Raises serializers.ValidationError / PaymentError-free; the
    caller turns errors into the response envelope."""
    key = data["service_key"]
    rate = pricing.RATES[key]
    if not Service.objects.filter(pk=key, is_active=True).exists():
        raise serializers.ValidationError({"service_type": "This service is currently unavailable."})
    if len(data["stops_clean"]) > rate.max_stops:
        raise serializers.ValidationError({"stops": f"At most {rate.max_stops} stops are allowed."})

    pickup, destination = data["pickup"], data["destination"]
    if SystemConfig.is_enabled("ENFORCE_SERVICE_ZONES"):
        zone = Zone.for_point(float(pickup[0]), float(pickup[1]))
        if zone is None or (zone.services.exists() and not zone.services.filter(pk=key).exists()):
            raise serializers.ValidationError({"pickup": "We don't offer this service in that area yet."})

    route = pricing.build_route(pickup, data["stops_clean"], destination)
    distance = pricing.route_distance_km(route)
    if distance > pricing.MAX_DISTANCE_KM:
        raise serializers.ValidationError({"destination": f"Trips are limited to {pricing.MAX_DISTANCE_KM} km."})
    if key != "waste-bin" and distance < Decimal("0.10"):
        raise serializers.ValidationError({"destination": "Pickup and destination are too close together."})

    surge = pricing.resolve_surge(key)
    waits = data["waits"]
    fare = pricing.calculate_fare(key, distance, surge, waits)

    promo_message = ""
    raw_code = (data.get("promo_code") or "").strip()
    if raw_code:
        promo = PromoCode.find(raw_code)
        if promo is None:
            raise serializers.ValidationError({"promo_code": "This promo code does not exist."})
        try:  # eligibility (dates, limits, service, first-time...) against the un-discounted ride fare
            promo.quote(user, fare["ride_fare"], key, currency=pricing.CURRENCY,
                        is_first_time_customer=(customer.total_bookings == 0))
        except DjangoValidationError as exc:
            raise serializers.ValidationError({"promo_code": " ".join(exc.messages)}) from None
        fare = pricing.calculate_fare(key, distance, surge, waits, promo=_promo_terms(promo))
        promo_message = f"{promo.code} applied"

    token, quote_id = pricing.issue_quote(user_id=user.pk, service_type=key, route=route, wait_minutes=waits,
                                          distance_km=distance, surge=surge, fare=fare)
    speed = max(SystemConfig.get_decimal("AVG_SPEED_KMH", AVG_SPEED_KMH_DEFAULT), Decimal("5"))
    return {
        "quote_token": token, "quote_id": quote_id,
        "expires_in": int(pricing.QUOTE_VALIDITY.total_seconds()),
        "currency": pricing.CURRENCY, "service_type": key,
        "distance_km": str(distance),
        "estimated_duration_minutes": max(5, int((distance / speed * 60).to_integral_value(ROUND_HALF_UP))),
        "surge": {"active": surge.active, "multiplier": str(surge.multiplier), "reason": surge.reason},
        "fare": pricing.fare_snapshot(fare, surge),
        "waiting": [{"stop": s["stop"], "wait_minutes": str(s["wait_minutes"]),
                     "billable_minutes": s["billable_minutes"], "charge": str(s["charge"])}
                    for s in fare["waiting"]["stops"]],
        "total": money(fare["total"]), "promo": promo_message,
    }


@api_view(["POST"])
@permission_classes([IsAuthenticated])
@throttle_classes([QuoteThrottle])
@customer_required
def booking_quote(request, customer):
    """POST /booking/calculate-price/ and /bookings/quote/. Returns a signed, expiring quote.
    The client never sends an amount; BookingCreateSerializer trusts only `quote_token`."""
    serializer = QuoteInputSerializer(data=request.data)
    if not serializer.is_valid():
        return validation_fail(serializer)
    try:
        quote = _quote_for(request.user, customer, serializer.validated_data)
    except serializers.ValidationError as exc:
        return drf_fail(exc)
    except Exception:
        # Anything unexpected lands in the Render log with a full traceback.
        logger.exception("Quote failed for user=%s payload=%s", request.user.pk, dict(request.data))
        return fail("We couldn't get a price for this trip. Please try again.",
                    status.HTTP_500_INTERNAL_SERVER_ERROR, code="quote_failed")
    return ok(**quote)


# ===========================================================================
# 11. BOOKINGS
# ===========================================================================
def _pay_with_wallet(user, booking):
    total = _q(booking.total_fare)
    done, message = CustomerWallet.for_user(user).pay_for_booking(total, booking)
    if not done:
        raise PaymentError(message, status.HTTP_402_PAYMENT_REQUIRED)
    payment = Payment.objects.create(
        booking_ref=booking.reference, amount=total, currency=booking.currency, method=Payment.Method.WALLET,
        payer_profile_id=CustomerProfile.objects.filter(user=user).values_list("pk", flat=True).first())
    payment.mark_as_paid(user=user)
    Booking.objects.filter(pk=booking.pk).update(payment_status=Booking.PaymentStatus.PAID)
    booking.payment_status = Booking.PaymentStatus.PAID


def _finalize_new_booking(user, booking):
    """Take the booking from DRAFT to the point where the customer's chosen payment method allows."""
    method, total = booking.payment_method, _q(booking.total_fare or ZERO_D)
    if method == Booking.PaymentMethod.WALLET and total > 0:
        try:
            with transaction.atomic():
                _pay_with_wallet(user, booking)
        except PaymentError:
            booking.cancel(by_user=user, reason="Wallet payment failed")
            _unwind_booking(booking, user=user, reason="Wallet payment failed")
            raise
        _submit_and_dispatch(booking, "Booking placed (wallet)", user)
        return {"required": False}
    if method in (Booking.PaymentMethod.MOBILE_MONEY, Booking.PaymentMethod.CARD) and total > 0:
        try:
            return _start_booking_payment(user, booking)  # stays DRAFT until Paystack confirms
        except PaymentError as exc:
            logger.warning("Checkout init failed booking=%s: %s", booking.reference, exc.message)
            return {"required": True, "error": exc.message}  # app can retry via initialize_payment
    _submit_and_dispatch(booking, "Booking placed (cash)", user)
    return {"required": False}


@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated])
@throttle_classes([BookingThrottle])
def booking_list_create(request):
    if request.method == "GET":
        qs = Booking.objects.select_related("customer", "rider")
        if request.user.is_staff:
            if request.query_params.get("customer"):
                qs = qs.filter(customer_id=request.query_params["customer"])
        elif _rider_of(request.user):
            qs = qs.filter(rider=request.user)
        else:
            qs = qs.filter(customer=request.user)
        q = request.query_params
        if q.get("status"):
            qs = qs.filter(status=q["status"])
        if q.get("service_type"):
            qs = qs.filter(service_type=q["service_type"])
        if q.get("active") in ("1", "true"):
            qs = qs.filter(status__in=list(Booking.ACTIVE_STATUSES))
        return paginated(request, qs, BookingListSerializer)

    customer = _customer_of(request.user)
    if customer is None:
        return fail("Only customers can create bookings.", status.HTTP_403_FORBIDDEN)
    if customer.is_blocked:
        return fail("This account has been blocked. Contact support.", status.HTTP_403_FORBIDDEN)
    if not customer.can_book:
        return fail(f"Clear your outstanding fees of GH₵{money(customer.outstanding_fees)} to book again.",
                    status.HTTP_402_PAYMENT_REQUIRED, outstanding_fees=money(customer.outstanding_fees))

    serializer = BookingCreateSerializer(data=request.data, context={"request": request, "customer": customer})
    if not serializer.is_valid():
        return validation_fail(serializer)
    try:
        with transaction.atomic():
            booking = serializer.save()
    except pricing.QuoteError as exc:
        return fail(exc.message, status.HTTP_400_BAD_REQUEST, code=exc.code)
    except serializers.ValidationError as exc:
        return drf_fail(exc)
    except DjangoValidationError as exc:
        return dj_fail(exc)

    try:
        payment = _finalize_new_booking(request.user, booking)
    except PaymentError as exc:
        return fail(exc.message, exc.status_code)
    booking.refresh_from_db()
    logger.info("Booking created: %s service=%s method=%s", booking.reference, booking.service_type,
                booking.payment_method)
    return ok("Booking created.", status.HTTP_201_CREATED, payment=payment,
              booking=BookingDetailSerializer(booking, context={"request": request}).data)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def booking_detail(request, reference):
    booking = _get_booking(request.user, reference)
    serializer_class = _detail_serializer_for(request.user, booking)
    return ok(booking=serializer_class(booking, context={"request": request}).data,
              history=[{"status": h.status, "notes": h.notes, "at": h.created_at}
                       for h in booking.status_history.all()[:50]])


@api_view(["PATCH"])
@permission_classes([IsAuthenticated])
@throttle_classes([BookingThrottle])
def booking_status_update(request, reference):
    """The assigned rider moves the trip forward (en-route -> arrived -> completed); staff can also
    cancel or fail it. Customers use booking_cancel."""
    serializer = BookingStatusUpdateSerializer(data=request.data)
    if not serializer.is_valid():
        return validation_fail(serializer)
    new_status = serializer.validated_data["status"]
    notes = (serializer.validated_data.get("notes") or "")[:255]

    try:
        with transaction.atomic():
            booking = _get_booking(request.user, reference, lock=True)
            is_assigned_rider = booking.rider_id == request.user.pk
            if not (request.user.is_staff or is_assigned_rider):
                return fail("Only the assigned rider can update this trip.", status.HTTP_403_FORBIDDEN)
            if not request.user.is_staff and new_status not in RIDER_ALLOWED_MOVES:
                return fail("You cannot set that status.", status.HTTP_403_FORBIDDEN)
            if not booking.can_transition_to(new_status):
                return fail(f"A {booking.get_status_display().lower()} booking cannot move to "
                            f"{str(new_status).replace('-', ' ')}.", status.HTTP_409_CONFLICT)

            if new_status == Booking.Status.COMPLETED and booking.service_type == Booking.ServiceType.PARCEL:
                parcel = ParcelDetail.objects.filter(booking_ref=booking.reference).first()
                if parcel is not None and parcel.status != ParcelDetail.Status.DELIVERED:
                    return fail("Confirm the parcel delivery (PIN) before completing the trip.",
                                status.HTTP_409_CONFLICT)

            if new_status in (Booking.Status.CANCELLED, Booking.Status.FAILED):
                booking.transition_to(new_status, reason=notes, by_user=request.user)
                _unwind_booking(booking, user=request.user, reason=notes)
            else:
                booking.transition_to(new_status)
            _log_status(booking, new_status, notes, request.user)
            if new_status == Booking.Status.ARRIVED and booking.accepted_at:
                rider = _rider_of(request.user)
                if rider is not None:
                    _try("arrival time", rider.record_arrival,
                         (timezone.now() - booking.accepted_at).total_seconds())
    except DjangoValidationError as exc:
        return dj_fail(exc, status.HTTP_409_CONFLICT)

    if new_status == Booking.Status.COMPLETED:
        _on_booking_completed(booking)
    elif new_status in (Booking.Status.EN_ROUTE, Booking.Status.ARRIVED):
        label = "is on the way" if new_status == Booking.Status.EN_ROUTE else "has arrived"
        _notify(booking.customer, "Rider update", f"Your rider {label}.", Notification.Category.BOOKING,
                {"booking_ref": booking.reference, "status": str(new_status)})
    return ok("Status updated.", booking_status=booking.status, booking_ref=booking.reference)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
@throttle_classes([BookingThrottle])
def booking_cancel(request, reference):
    serializer = BookingCancelSerializer(data=request.data)
    if not serializer.is_valid():
        return validation_fail(serializer)
    reason = (serializer.validated_data.get("reason") or "")[:255]

    try:
        with transaction.atomic():
            booking = _get_booking(request.user, reference, lock=True)
            by_customer = booking.customer_id == request.user.pk
            if not (by_customer or request.user.is_staff):
                return fail("Riders cannot cancel a booking. Decline or release the job instead.",
                            status.HTTP_403_FORBIDDEN)
            if not booking.can_transition_to(Booking.Status.CANCELLED):
                return fail(f"A {booking.get_status_display().lower()} booking cannot be cancelled.",
                            status.HTTP_409_CONFLICT)
            fee = ZERO_D
            if by_customer and not request.user.is_staff:
                fee = pricing.cancellation_fee(booking.service_type, status=booking.status,
                                               accepted_at=booking.accepted_at, now=timezone.now())
            rider_user = booking.rider
            booking.cancel(by_user=request.user, reason=reason)
            _log_status(booking, Booking.Status.CANCELLED, reason, request.user)
            summary = _unwind_booking(booking, fee=fee, user=request.user, reason=reason)
    except DjangoValidationError as exc:
        return dj_fail(exc, status.HTTP_409_CONFLICT)

    if rider_user is not None:
        _notify(rider_user, "Booking cancelled", f"{booking.reference} was cancelled.",
                Notification.Category.BOOKING, {"booking_ref": booking.reference})
    return ok("Booking cancelled.", booking_status=booking.status, **summary)


@api_view(["POST"])
@permission_classes([IsAdminUser])
def booking_dispatch(request, reference):
    """Staff: (re)start dispatch for a pending booking."""
    booking = _get_booking(request.user, reference)
    if booking.status != Booking.Status.PENDING:
        return fail("Only pending bookings can be dispatched.", status.HTTP_409_CONFLICT)
    _start_dispatch(booking)
    audit(request.user, "booking.redispatched", booking)
    return ok("Dispatch started.")


# ===========================================================================
# 12. PARCEL DELIVERY
# ===========================================================================
def _parcel_job(request, booking_ref):
    """(booking, parcel) for the assigned rider or staff. Raises Http404."""
    booking = _get_booking(request.user, booking_ref)
    if not request.user.is_staff and booking.rider_id != request.user.pk:
        raise Http404("Booking not found.")
    return booking, get_object_or_404(ParcelDetail, booking_ref=booking.reference)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
@throttle_classes([BookingThrottle])
@customer_required
def parcel_create(request, customer):
    """Attach parcel details to the customer's parcel booking."""
    serializer = ParcelCreateSerializer(data=request.data, context={"request": request})
    if not serializer.is_valid():
        return validation_fail(serializer)
    ref = (serializer.validated_data.get("booking_ref") or "").strip().upper()
    booking = Booking.objects.filter(reference=ref, customer=request.user,
                                     service_type=Booking.ServiceType.PARCEL).first()
    if booking is None:
        return fail("Parcel booking not found.", status.HTTP_404_NOT_FOUND)
    if booking.is_terminal:
        return fail("This booking has already ended.", status.HTTP_409_CONFLICT)
    if ParcelDetail.objects.filter(booking_ref=booking.reference).exists():
        return fail("Parcel details were already added.", status.HTTP_409_CONFLICT)
    try:
        parcel = serializer.save(booking_ref=booking.reference)
    except serializers.ValidationError as exc:
        return drf_fail(exc)
    except IntegrityError:
        return fail("Parcel details were already added.", status.HTTP_409_CONFLICT)
    return ok("Parcel details saved.", status.HTTP_201_CREATED,
              parcel=ParcelOwnerSerializer(parcel, context={"request": request}).data)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def parcel_detail(request, booking_ref):
    booking = _get_booking(request.user, booking_ref)
    parcel = get_object_or_404(ParcelDetail, booking_ref=booking.reference)
    owner = request.user.is_staff or booking.customer_id == request.user.pk
    serializer_class = ParcelOwnerSerializer if owner else ParcelDetailSerializer  # only owners see the PIN
    return ok(parcel=serializer_class(parcel, context={"request": request}).data)


@api_view(["GET"])
@authentication_classes([])
@permission_classes([AllowAny])
@throttle_classes([PublicLookupThrottle])
def parcel_public_tracking(request, tracking_code):
    """Recipients track a parcel with the code alone: no login, no PIN, no phone numbers."""
    parcel = ParcelDetail.objects.filter(tracking_code=(tracking_code or "").strip().upper()).first()
    if parcel is None:
        return fail("Tracking code not found.", status.HTTP_404_NOT_FOUND)
    return ok(parcel=ParcelPublicTrackingSerializer(parcel).data)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
@throttle_classes([BookingThrottle])
def parcel_status_update(request, booking_ref):
    """Rider moves the parcel along. Delivery needs the recipient's 4-digit PIN (attempt-limited)."""
    serializer = ParcelStatusUpdateSerializer(data=request.data)
    if not serializer.is_valid():
        return validation_fail(serializer)
    d = serializer.validated_data
    booking, parcel = _parcel_job(request, booking_ref)
    new_status = d["status"]

    attempts_key = f"parcel-pin:{parcel.pk}"
    if new_status == ParcelDetail.Status.DELIVERED:
        if cache.get(attempts_key, 0) >= MAX_CODE_ATTEMPTS:
            return fail("Too many wrong PIN attempts. Contact support.", status.HTTP_429_TOO_MANY_REQUESTS)
        if not parcel.verify_delivery_pin(d.get("pin") or ""):
            cache.add(attempts_key, 0, 3600)
            cache.incr(attempts_key)
            return fail("Incorrect delivery PIN.")
        cache.delete(attempts_key)

    try:
        with transaction.atomic():
            if new_status == ParcelDetail.Status.DELIVERED:
                ParcelProof.objects.create(parcel=parcel, proof_type=ParcelProof.ProofType.PIN_VERIFIED,
                                           captured_by=request.user, latitude=d.get("latitude"),
                                           longitude=d.get("longitude"))
            parcel.update_status(new_status, user=request.user, note=d.get("note", ""),
                                 latitude=d.get("latitude"), longitude=d.get("longitude"),
                                 location_label=d.get("location_label", ""))
            if new_status == ParcelDetail.Status.DELIVERED and d.get("received_by"):
                ParcelDetail.objects.filter(pk=parcel.pk).update(received_by=d["received_by"][:100])
    except DjangoValidationError as exc:
        return dj_fail(exc, status.HTTP_409_CONFLICT)

    parcel.refresh_from_db()
    _notify(booking.customer, "Parcel update", f"Parcel {parcel.tracking_code}: {parcel.get_status_display()}.",
            Notification.Category.BOOKING, {"booking_ref": booking.reference, "tracking_code": parcel.tracking_code})
    if parcel.notify_recipient and new_status in (ParcelDetail.Status.PICKED_UP, ParcelDetail.Status.DELIVERED):
        base = getattr(settings, "TRACKING_BASE_URL", "https://rydnow.app/track")
        _try("recipient sms", _send_sms, parcel.recipient_phone,
             f"Rydnow: your parcel is {parcel.get_status_display().lower()}. Track: {base}/{parcel.tracking_code}")
    return ok("Parcel status updated.", parcel_status=parcel.status)


@api_view(["POST"])
@parser_classes([MultiPartParser, FormParser])
@permission_classes([IsAuthenticated])
@throttle_classes([UploadThrottle])
def parcel_proof_create(request, booking_ref):
    _, parcel = _parcel_job(request, booking_ref)
    serializer = ParcelProofCreateSerializer(data=request.data)
    if not serializer.is_valid():
        return validation_fail(serializer)
    try:
        proof = serializer.save(parcel=parcel, captured_by=request.user)
    except DjangoValidationError as exc:
        return dj_fail(exc)
    return ok("Proof saved.", status.HTTP_201_CREATED,
              proof=ParcelProofSerializer(proof, context={"request": request}).data)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
@throttle_classes([BookingThrottle])
@customer_required
def parcel_feedback(request, customer, booking_ref):
    booking = get_object_or_404(Booking, reference=booking_ref.strip().upper(), customer=request.user)
    parcel = get_object_or_404(ParcelDetail, booking_ref=booking.reference)
    serializer = ParcelFeedbackSerializer(data=request.data)
    if not serializer.is_valid():
        return validation_fail(serializer)
    try:
        feedback = serializer.save(parcel=parcel, submitted_by=request.user)
    except DjangoValidationError as exc:
        return dj_fail(exc)
    except IntegrityError:
        return fail("You have already left feedback for this parcel.", status.HTTP_409_CONFLICT)
    return ok("Thanks for your feedback.", status.HTTP_201_CREATED, feedback=ParcelFeedbackSerializer(feedback).data)


# ===========================================================================
# 13. GROWTH: PROMOS, REFERRALS, BUSINESS, RECURRING, WASTE
# ===========================================================================
@api_view(["GET"])
@authentication_classes([])
@permission_classes([AllowAny])
def promos_public(request):
    """Public codes usable right now (dates, weekday and hours all checked)."""
    now = timezone.now()
    live = (PromoCode.objects.filter(is_active=True, is_public=True, valid_from__lte=now)
            .filter(Q(valid_until__isnull=True) | Q(valid_until__gt=now)))
    promos = [p for p in live if p.is_valid_at(now)]
    return ok(promos=PromoCodePublicSerializer(promos, many=True).data)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
@throttle_classes([QuoteThrottle])
@customer_required
def promo_validate(request, customer):
    serializer = PromoValidateSerializer(data=request.data)
    if not serializer.is_valid():
        return validation_fail(serializer)
    d = serializer.validated_data
    promo = PromoCode.find(d["code"])
    if promo is None:
        return fail("This promo code does not exist.", status.HTTP_404_NOT_FOUND)
    try:
        result = promo.quote(request.user, d["order_amount"], d["service_type"], currency=CURRENCY,
                             is_first_time_customer=(customer.total_bookings == 0))
    except DjangoValidationError as exc:
        return dj_fail(exc)
    return ok(code=promo.code, discount=money(result.discount), final_amount=money(result.final_amount),
              description=promo.get_discount_display())


def _referral_rewards():
    return (SystemConfig.get_decimal("REFERRAL_REWARD", "5.00"),
            SystemConfig.get_decimal("REFERRAL_REFEREE_REWARD", "5.00"))


def _create_active_referral(referrer, referred, reward, referee_reward):
    """The invited user is already signed up, so the referral starts ACTIVE (rewards are paid when
    their first trip completes, see _complete_customer_referral). One referral per account."""
    if referrer.pk == referred.pk:
        raise DjangoValidationError("You cannot use your own referral code.")
    return Referral.objects.create(referrer=referrer, referred_user=referred, status=Referral.Status.ACTIVE,
                                   signed_up_at=timezone.now(), expires_at=None,
                                   reward_amount=reward, referee_reward_amount=referee_reward)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
@customer_required
def referral_summary(request, customer):
    code, _ = ReferralCode.objects.get_or_create(referrer=request.user)
    mine = Referral.objects.filter(referrer=request.user)
    stats = mine.aggregate(
        invites=Count("id"),
        signups=Count("id", filter=Q(signed_up_at__isnull=False)),
        completed=Count("id", filter=Q(status=Referral.Status.COMPLETED)),
        rewards_earned=Coalesce(Sum("reward_amount", filter=Q(rewarded_at__isnull=False)), ZERO_EXPR))
    stats["rewards_earned"] = money(stats["rewards_earned"])
    return ok(code=ReferralCodeSerializer(code).data, customer_code=customer.referral_code, stats=stats,
              recent=ReferralSerializer(mine.select_related("referred_user")[:20], many=True).data)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
@throttle_classes([PaymentThrottle])
@customer_required
def referral_redeem(request, customer):
    """A new customer enters someone's code (a customer's personal code or an invite ReferralCode)."""
    serializer = RedeemReferralSerializer(data=request.data)
    if not serializer.is_valid():
        return validation_fail(serializer)
    code = Referral.normalize_code(serializer.validated_data["code"])
    if customer.total_bookings > 0:
        return fail("Referral codes are for new customers who haven't booked yet.")
    if Referral.objects.filter(referred_user=request.user).exists():
        return fail("You have already used a referral code.", status.HTTP_409_CONFLICT)

    reward, referee_reward = _referral_rewards()
    try:
        with transaction.atomic():
            owner = CustomerProfile.objects.filter(referral_code=code).select_related("user").first()
            invite = None
            if owner is not None:
                referrer = owner.user
            else:
                invite = ReferralCode.objects.select_for_update().filter(code=code).select_related("referrer").first()
                if invite is None:
                    return fail("This referral code is not valid.", status.HTTP_404_NOT_FOUND)
                if invite.status != ReferralCode.Status.ACTIVE or (
                        invite.valid_until and invite.valid_until < timezone.localdate()):
                    return fail("This referral code is no longer active.")
                if invite.max_uses and invite.times_used >= invite.max_uses:
                    return fail("This referral code has reached its limit.")
                referrer = invite.referrer
                reward, referee_reward = invite.referrer_reward_value, invite.referee_reward_value
                ReferralCode.objects.filter(pk=invite.pk).update(times_used=F("times_used") + 1)
            referral = _create_active_referral(referrer, request.user, reward, referee_reward)
    except DjangoValidationError as exc:
        return dj_fail(exc)
    except IntegrityError:
        return fail("You have already used a referral code.", status.HTTP_409_CONFLICT)
    return ok("Referral code applied. Your reward arrives after your first completed trip.",
              status.HTTP_201_CREATED, referral=ReferralSerializer(referral).data)


# ---- business accounts + recurring bookings ----------------------------------------
def _my_business_or_403(user, business):
    if business.created_by_id != user.pk:
        raise PermissionDenied("This business account is not yours.")
    if not business.is_active:
        raise PermissionDenied("This business account is not active yet.")


class BusinessApplicationList(generics.ListCreateAPIView):
    serializer_class = BusinessAccountApplicationSerializer
    permission_classes = [IsAuthenticated, IsCustomer]
    pagination_class = None

    def get_queryset(self):
        return BusinessAccount.objects.filter(created_by=self.request.user)

    def create(self, request, *args, **kwargs):
        try:
            return super().create(request, *args, **kwargs)
        except IntegrityError:
            return fail("A business with this email or registration number already exists.",
                        status.HTTP_409_CONFLICT)

    def perform_create(self, serializer):
        serializer.save(created_by=self.request.user)


class BusinessApplicationDetail(generics.RetrieveAPIView):
    serializer_class = BusinessAccountApplicationSerializer
    permission_classes = [IsAuthenticated, IsCustomer]

    def get_queryset(self):
        return BusinessAccount.objects.filter(created_by=self.request.user)


class RecurringBookingList(generics.ListCreateAPIView):
    serializer_class = RecurringBookingSerializer
    permission_classes = [IsAuthenticated, IsCustomer]

    def get_queryset(self):
        return RecurringBooking.objects.filter(business__created_by=self.request.user)

    def perform_create(self, serializer):
        _my_business_or_403(self.request.user, serializer.validated_data["business"])
        serializer.save(created_by=self.request.user)


class RecurringBookingDetail(generics.RetrieveAPIView):
    serializer_class = RecurringBookingSerializer
    permission_classes = [IsAuthenticated, IsCustomer]

    def get_queryset(self):
        return RecurringBooking.objects.filter(business__created_by=self.request.user)


@api_view(["POST"])
@permission_classes([IsAuthenticated, IsCustomer])
def recurring_action(request, pk):
    """POST {"action": "pause" | "resume" | "cancel", "reason": "..."}"""
    schedule = get_object_or_404(RecurringBooking, pk=pk, business__created_by=request.user)
    action = str(request.data.get("action", "")).lower()
    try:
        if action == "pause":
            schedule.pause()
        elif action == "resume":
            schedule.resume()
        elif action == "cancel":
            schedule.cancel(str(request.data.get("reason", "")))
        else:
            return fail("Action must be pause, resume or cancel.")
    except DjangoValidationError as exc:
        return dj_fail(exc, status.HTTP_409_CONFLICT)
    return ok(f"Schedule {action}d." if action != "cancel" else "Schedule cancelled.",
              schedule=RecurringBookingSerializer(schedule).data)


# ---- waste collection -----------------------------------------------------------------
class WasteSubscriptionList(generics.ListCreateAPIView):
    permission_classes = [IsAuthenticated, IsCustomer]

    def get_serializer_class(self):
        return WasteSubscriptionCreateSerializer if self.request.method == "POST" else WasteSubscriptionSerializer

    def get_queryset(self):
        return WasteSubscription.objects.filter(customer=self.request.user)

    def perform_create(self, serializer):
        business = serializer.validated_data.get("business")
        if business is not None:
            _my_business_or_403(self.request.user, business)
        try:
            serializer.save(customer=self.request.user)
        except DjangoValidationError as exc:
            raise serializers.ValidationError(exc.messages) from None


@api_view(["POST"])
@permission_classes([IsAuthenticated, IsCustomer])
def waste_subscription_action(request, pk):
    """POST {"action": "pause" | "resume" | "cancel" | "skip", "reason": "..."}"""
    sub = get_object_or_404(WasteSubscription, pk=pk, customer=request.user)
    action, reason = str(request.data.get("action", "")).lower(), str(request.data.get("reason", ""))
    try:
        if action == "pause":
            sub.pause()
        elif action == "resume":
            sub.resume()
        elif action == "cancel":
            sub.cancel(reason)
        elif action == "skip":
            sub.skip_next(reason)
        else:
            return fail("Action must be pause, resume, cancel or skip.")
    except DjangoValidationError as exc:
        return dj_fail(exc, status.HTTP_409_CONFLICT)
    return ok("Subscription updated.", subscription=WasteSubscriptionSerializer(sub).data)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def waste_collections(request):
    rider = _rider_of(request.user)
    if rider is not None:
        qs = WasteCollection.objects.filter(assigned_rider=rider)
    else:
        qs = WasteCollection.objects.filter(subscription__customer=request.user)
    if request.query_params.get("status"):
        qs = qs.filter(status=request.query_params["status"])
    return paginated(request, qs.select_related("subscription"), WasteCollectionSerializer)


def _waste_rider_or_error(request):
    rider = _rider_of(request.user)
    if rider is None:
        return None, fail("A rider account is required.", status.HTTP_403_FORBIDDEN)
    if not rider.can_accept_jobs or not rider.is_qualified_for("waste-bin"):
        return None, fail("You are not approved for waste collection.", status.HTTP_403_FORBIDDEN)
    return rider, None


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def waste_collections_available(request):
    """Unassigned pickups for today and tomorrow that an approved waste rider can claim."""
    rider, error = _waste_rider_or_error(request)
    if error:
        return error
    today = timezone.localdate()
    qs = (WasteCollection.objects.needs_rider()
          .filter(scheduled_date__gte=today, scheduled_date__lte=today + timedelta(days=1))
          .select_related("subscription").order_by("scheduled_date", "scheduled_time"))
    return paginated(request, qs, WasteCollectionSerializer)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
@throttle_classes([BookingThrottle])
def waste_collection_claim(request, collection_id):
    rider, error = _waste_rider_or_error(request)
    if error:
        return error
    try:
        with transaction.atomic():  # the row lock means two riders can never both claim it
            collection = get_object_or_404(WasteCollection.objects.select_for_update(), pk=collection_id)
            if collection.assigned_rider_id is not None:
                return fail("This pickup was already claimed.", status.HTTP_409_CONFLICT)
            collection.assign(rider)
    except DjangoValidationError as exc:
        return dj_fail(exc, status.HTTP_409_CONFLICT)
    _notify(collection.subscription.customer, "Rider assigned",
            f"A rider will collect your waste on {collection.scheduled_date}.", Notification.Category.BOOKING)
    return ok("Pickup claimed.", collection=WasteCollectionSerializer(collection).data)


@api_view(["POST"])
@parser_classes([MultiPartParser, FormParser, JSONParser])
@permission_classes([IsAuthenticated])
@throttle_classes([UploadThrottle])
@rider_required
def waste_collection_complete(request, rider, collection_id):
    collection = get_object_or_404(WasteCollection, pk=collection_id, assigned_rider=rider)
    serializer = WasteCollectionCompleteSerializer(data=request.data)
    if not serializer.is_valid():
        return validation_fail(serializer)
    d = serializer.validated_data
    try:
        collection.complete(bins_collected=d.get("bins_collected"), weight_kg=d.get("weight_kg"),
                            proof_photo=d.get("proof_photo"), notes=d.get("notes", ""))
    except DjangoValidationError as exc:
        return dj_fail(exc, status.HTTP_409_CONFLICT)
    _try("rider stats", rider.record_trip_completed)
    _notify(collection.subscription.customer, "Waste collected",
            f"Collection {collection.reference} is complete.", Notification.Category.BOOKING)
    return ok("Collection completed.", collection=WasteCollectionSerializer(collection).data)


# ===========================================================================
# 14. REVIEWS, COMPLAINTS, INCIDENTS, NOTIFICATIONS
# ===========================================================================
@api_view(["GET"])
@permission_classes([IsAuthenticated])
def reviews(request):
    """?subject_type=rider|customer&subject_id=<USER id>   or   ?mine=1 (reviews I wrote)."""
    q = request.query_params
    if q.get("mine") in ("1", "true"):
        return paginated(request, Review.objects.filter(reviewer=request.user), ReviewSerializer)
    subject_type, subject_id = q.get("subject_type"), q.get("subject_id")
    if subject_type not in Review.SubjectType.values or not (subject_id or "").isdigit():
        return fail("Provide subject_type (rider or customer) and a numeric subject_id.")
    summary = Review.objects.rating_summary(subject_type, int(subject_id))
    qs = Review.objects.visible().for_subject(subject_type, int(subject_id))
    return paginated(request, qs, ReviewSerializer,
                     average=round(summary["average"], 2) if summary["average"] else None,
                     count=summary["count"])


@api_view(["POST"])
@permission_classes([IsAuthenticated])
@throttle_classes([BookingThrottle])
def review_create(request):
    """Customers review the rider; riders review the customer. Completed trips only, once each way."""
    serializer = ReviewCreateSerializer(data=request.data)
    if not serializer.is_valid():
        return validation_fail(serializer)
    d = serializer.validated_data
    booking = _get_booking(request.user, d["booking_ref"])
    if booking.status != Booking.Status.COMPLETED:
        return fail("You can only review completed trips.", status.HTTP_409_CONFLICT)

    if booking.customer_id == request.user.pk:
        subject_type, subject_user_id = Review.SubjectType.RIDER, booking.rider_id
    elif booking.rider_id == request.user.pk:
        subject_type, subject_user_id = Review.SubjectType.CUSTOMER, booking.customer_id
    else:
        return fail("Only the customer or rider on this trip can review it.", status.HTTP_403_FORBIDDEN)
    if subject_user_id is None:
        return fail("This trip has no rider to review.", status.HTTP_409_CONFLICT)

    try:
        with transaction.atomic():
            review = Review.objects.create(
                booking_ref=booking.reference, reviewer=request.user, subject_type=subject_type,
                subject_profile_id=subject_user_id,  # the SUBJECT'S USER ID (see module docstring)
                rating=d["rating"], comment=d.get("comment", ""))
    except IntegrityError:
        return fail("You have already reviewed this trip.", status.HTTP_409_CONFLICT)

    subject_profile = (_rider_of(booking.rider) if subject_type == Review.SubjectType.RIDER
                       else _customer_of(booking.customer))
    if subject_profile is not None:
        _try("apply rating", subject_profile.apply_rating, d["rating"])
    return ok("Review submitted.", status.HTTP_201_CREATED, review=ReviewSerializer(review).data)


class ComplaintList(generics.ListCreateAPIView):
    serializer_class = RiderComplaintSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        qs = RiderComplaint.objects.all() if self.request.user.is_staff \
            else RiderComplaint.objects.filter(filed_by=self.request.user)
        return qs.select_related("rider")

    def perform_create(self, serializer):
        complaint = serializer.save(filed_by=self.request.user)
        audit(self.request.user, "complaint.filed", complaint, reference=complaint.reference)


class IncidentList(generics.ListCreateAPIView):
    serializer_class = RiderIncidentSerializer
    permission_classes = [IsAuthenticated, IsRider]

    def get_queryset(self):
        return RiderIncident.objects.filter(rider__user=self.request.user)

    def perform_create(self, serializer):
        incident = serializer.save(rider=_rider_of(self.request.user), reported_by=self.request.user)
        audit(self.request.user, "incident.reported", incident, severity=incident.severity)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def notifications_list(request):
    qs = Notification.objects.for_user(request.user)
    if request.query_params.get("unread") in ("1", "true"):
        qs = qs.unread()
    return paginated(request, qs, NotificationSerializer,
                     unread_count=Notification.objects.for_user(request.user).unread().count())


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def notification_mark_read(request, pk):
    notification = get_object_or_404(Notification, pk=pk, user=request.user)
    notification.mark_read()
    return ok("Marked as read.")


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def notifications_mark_all_read(request):
    count = Notification.objects.mark_all_read(request.user)
    return ok("All notifications marked as read.", updated=count)


# ===========================================================================
# 15. STAFF TOOLS (IsAdminUser). Every action is audited by the model methods or audit() below.
# ===========================================================================
def _first(data, *keys, default=None):
    return next((data[k] for k in keys if data.get(k) not in (None, "")), default)


@api_view(["POST"])
@permission_classes([IsAdminUser])
def staff_rider_action(request, rider_id):
    """POST {"action": "approve" | "reject" | "suspend" | "reinstate", ...}"""
    rider = get_object_or_404(RiderProfile, pk=rider_id)
    action = str(request.data.get("action", "")).lower()
    try:
        if action == "approve":
            rider.approve(request.user)
            _notify(rider.user, "You're verified!", "You can now go online and take jobs.")
        elif action == "reject":
            body = RiderRejectSerializer(data=request.data)
            if not body.is_valid():
                return validation_fail(body)
            rider.reject(request.user, body.validated_data["reason"])
            _notify(rider.user, "Verification declined", body.validated_data["reason"])
        elif action == "suspend":
            body = RiderSuspendSerializer(data=request.data)
            if not body.is_valid():
                return validation_fail(body)
            d = body.validated_data
            until = d.get("until") or (timezone.now() + timedelta(days=int(d["days"])) if d.get("days") else None)
            rider.suspend(request.user, d["reason"], until=until)
            _notify(rider.user, "Account suspended", d["reason"])
        elif action == "reinstate":
            rider.reinstate(request.user, str(request.data.get("reason", "Reinstated")))
            _notify(rider.user, "Account reinstated", "You can go online again.")
        else:
            return fail("Action must be approve, reject, suspend or reinstate.")
    except DjangoValidationError as exc:
        return dj_fail(exc, status.HTTP_409_CONFLICT)
    return ok(f"Rider {action} done.", rider=RiderProfileSerializer(rider, context={"request": request}).data)


@api_view(["POST"])
@permission_classes([IsAdminUser])
def staff_document_review(request, document_id):
    document = get_object_or_404(RiderDocument.objects.select_related("rider_profile__user"), pk=document_id)
    serializer = RiderDocumentReviewSerializer(data=request.data)
    if not serializer.is_valid():
        return validation_fail(serializer)
    d = serializer.validated_data
    approve = d.get("approve")
    if approve is None:
        approve = str(d.get("decision", "")).lower() in ("approve", "approved")
    try:
        outcome = document.review(request.user, bool(approve), d.get("notes", ""))
    except DjangoValidationError as exc:
        return dj_fail(exc)
    return ok("Document reviewed.", outcome=outcome, document_status=document.status,
              rider_status=document.rider_profile.verification_status)


@api_view(["POST"])
@permission_classes([IsAdminUser])
def staff_qualification_action(request, qualification_id):
    """POST {"action": "approve" | "reject" | "suspend" | "revoke", "reason": "..."}"""
    qualification = get_object_or_404(ServiceQualification.objects.select_related("rider__user"),
                                      pk=qualification_id)
    action, reason = str(request.data.get("action", "")).lower(), str(request.data.get("reason", ""))[:255]
    targets = {"reject": ServiceQualification.Status.REJECTED, "suspend": ServiceQualification.Status.SUSPENDED,
               "revoke": ServiceQualification.Status.REVOKED}
    if action == "approve":
        qualification.approve(request.user)
    elif action in targets:
        qualification.status, qualification.status_reason = targets[action], reason
        qualification.save(update_fields=["status", "status_reason", "updated_at"])
    else:
        return fail("Action must be approve, reject, suspend or revoke.")
    audit(request.user, f"qualification.{action}", qualification, service=qualification.service_id)
    _notify(qualification.rider.user, "Service update",
            f"{qualification.service_id}: {qualification.get_status_display()}. {reason}".strip())
    return ok("Qualification updated.", qualification=ServiceQualificationSerializer(qualification).data)


@api_view(["POST"])
@permission_classes([IsAdminUser])
def staff_wallet_deposit(request):
    """Credit a rider's wallet (cash top-up, bonus...). Safe to retry with the same source_reference."""
    serializer = StaffDepositSerializer(data=request.data)
    if not serializer.is_valid():
        return validation_fail(serializer)
    d = serializer.validated_data
    rider = get_object_or_404(RiderProfile, pk=d["rider_id"])
    try:
        deposit = WalletDeposit.record(_get_wallet(rider), d["amount"], d.get("source", "other"),
                                       source_reference=d.get("source_reference", ""), notes=d.get("notes", ""),
                                       recorded_by=request.user)
    except DjangoValidationError as exc:
        return dj_fail(exc)
    audit(request.user, "wallet.staff_deposit", rider, amount=d["amount"], reference=deposit.reference)
    _notify(rider.user, "Wallet credited", f"GH₵{money(deposit.amount)} was added to your wallet.",
            Notification.Category.PAYMENT)
    return ok("Deposit recorded.", status.HTTP_201_CREATED, deposit=WalletDepositSerializer(deposit).data)


@api_view(["POST"])
@permission_classes([IsAdminUser])
def staff_withdrawal_action(request, withdrawal_id):
    """POST {"action": "processing" | "complete" | "fail", "provider_reference": "...", "reason": "..."}"""
    withdrawal = get_object_or_404(WithdrawalRequest.objects.select_related("wallet__rider__user"), pk=withdrawal_id)
    action = str(request.data.get("action", "")).lower()
    try:
        if action == "processing":
            withdrawal.mark_processing(user=request.user)
        elif action == "complete":
            withdrawal.mark_completed(str(request.data.get("provider_reference", "")), user=request.user)
            _notify(withdrawal.wallet.rider.user, "Withdrawal paid",
                    f"GH₵{money(withdrawal.net_amount)} was sent to your mobile money.",
                    Notification.Category.PAYMENT)
        elif action == "fail":
            withdrawal.mark_failed(str(request.data.get("reason", "Payout failed")), user=request.user)
            _notify(withdrawal.wallet.rider.user, "Withdrawal failed",
                    "The money has been returned to your wallet.", Notification.Category.PAYMENT)
        else:
            return fail("Action must be processing, complete or fail.")
    except DjangoValidationError as exc:
        return dj_fail(exc, status.HTTP_409_CONFLICT)
    audit(request.user, f"withdrawal.{action}", withdrawal, reference=withdrawal.reference)
    return ok("Withdrawal updated.", withdrawal=WithdrawalSerializer(withdrawal).data)


def _complete_wallet_refund(refund, user):
    """Refunds of wallet payments go straight back into the customer's wallet."""
    payment = refund.payment
    booking = Booking.objects.filter(reference=payment.booking_ref).select_related("customer").first()
    if booking is None:
        raise DjangoValidationError("The booking for this payment no longer exists.")
    done, message = CustomerWallet.for_user(booking.customer).refund_to_wallet(
        refund.amount, booking, reference=f"RFDW-{refund.reference}", description=f"Refund {refund.reference}")
    if not done:
        raise DjangoValidationError(message)
    refund.start_processing(user=user)
    refund.mark_as_completed(user=user)


@api_view(["POST"])
@permission_classes([IsAdminUser])
def staff_refund_decision(request, refund_id):
    """POST {"decision": "approve" | "reject", "reason": "..."}. The approver cannot be the requester."""
    refund = get_object_or_404(Refund.objects.select_related("payment"), pk=refund_id)
    serializer = RefundDecisionSerializer(data=request.data)
    if not serializer.is_valid():
        return validation_fail(serializer)
    decision = str(serializer.validated_data["decision"]).lower()
    reason = serializer.validated_data.get("reason", "")
    try:
        with transaction.atomic():
            if decision == "approve":
                refund.approve(request.user)
                if refund.payment.method == Payment.Method.WALLET:
                    _complete_wallet_refund(refund, request.user)
            elif decision == "reject":
                refund.reject(request.user, reason)
            else:
                return fail("Decision must be approve or reject.")
    except DjangoValidationError as exc:
        return dj_fail(exc, status.HTTP_409_CONFLICT)
    audit(request.user, f"refund.{decision}", refund, reference=refund.reference)
    return ok(f"Refund {decision}d." if decision == "approve" else "Refund rejected.",
              refund=RefundSerializer(refund).data)


@api_view(["POST"])
@permission_classes([IsAdminUser])
def staff_refund_complete(request, refund_id):
    """After sending the money back through Paystack, record it: POST {"refund_transaction_id": "..."}."""
    refund = get_object_or_404(Refund.objects.select_related("payment"), pk=refund_id)
    try:
        refund.mark_as_completed(str(request.data.get("refund_transaction_id", "")), user=request.user)
    except DjangoValidationError as exc:
        return dj_fail(exc, status.HTTP_409_CONFLICT)
    audit(request.user, "refund.completed", refund, reference=refund.reference)
    booking = Booking.objects.filter(reference=refund.payment.booking_ref).first()
    if booking is not None:
        _notify(booking.customer, "Refund sent", f"GH₵{money(refund.amount)} has been refunded.",
                Notification.Category.PAYMENT)
    return ok("Refund completed.", refund=RefundSerializer(refund).data)


@api_view(["POST"])
@permission_classes([IsAdminUser])
def staff_business_review(request, business_id):
    """POST {"decision": "approve" | "reject", "discount_approved": 10, "notes": "..."}"""
    business = get_object_or_404(BusinessAccount, pk=business_id)
    serializer = BusinessReviewSerializer(data=request.data)
    if not serializer.is_valid():
        return validation_fail(serializer)
    d = serializer.validated_data
    decision = str(d["decision"]).lower()
    try:
        if decision == "approve":
            business.approve(discount_approved=d.get("discount_approved"), reviewed_by=request.user,
                             notes=d.get("notes", ""))
        elif decision == "reject":
            business.reject(reason=d.get("notes", ""), reviewed_by=request.user)
        else:
            return fail("Decision must be approve or reject.")
    except DjangoValidationError as exc:
        return dj_fail(exc, status.HTTP_409_CONFLICT)
    audit(request.user, f"business.{decision}", business)
    if business.created_by_id:
        _notify(business.created_by, "Business application",
                f"{business.business_name}: {business.get_status_display()}.")
    return ok("Business reviewed.", business=BusinessAccountApplicationSerializer(business).data)


@api_view(["POST"])
@permission_classes([IsAdminUser])
def staff_business_action(request, business_id):
    """POST {"action": "suspend" | "reinstate", "reason": "..."}"""
    business = get_object_or_404(BusinessAccount, pk=business_id)
    action = str(request.data.get("action", "")).lower()
    try:
        if action == "suspend":
            business.suspend(str(request.data.get("reason", "")))
        elif action == "reinstate":
            business.reinstate()
        else:
            return fail("Action must be suspend or reinstate.")
    except DjangoValidationError as exc:
        return dj_fail(exc, status.HTTP_409_CONFLICT)
    audit(request.user, f"business.{action}", business)
    return ok(f"Business {action}d.", business=BusinessAccountApplicationSerializer(business).data)

