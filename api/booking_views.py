"""
Rydnow booking API  (api/booking_views.py)

Written against the pasted models.py. Every model method used here exists there:
Booking.apply_quote / apply_promo / mark_searching / pay_with_wallet / assign_rider /
release_rider / start_booking / complete_booking / cancel_booking, PricingRule.quote,
PromoCode.validate_for, RiderProfile.go_online / go_offline.

Customer endpoints
    POST  bookings/quote/            server-side price for each service
    GET   bookings/                  my bookings (?status=active|history, ?page=, ?page_size=)
    POST  bookings/                  create (server prices it; send an Idempotency-Key header)
    GET   bookings/<id|ref>/         one booking
    PATCH bookings/<id|ref>/         {"status": "cancelled", "reason": "..."}
    POST  bookings/<id|ref>/cancel/  same as the PATCH
    POST  promos/validate/           {code, amount, service_type}

Rider endpoints (the rider is always request.user, never a client-supplied rider_id)
    GET   list-bookings/ (alias rider/bookings/)   my active job + history + nearby open jobs
    POST  rider/presence/            {online, latitude, longitude}
    POST  accept-booking/  decline-booking/  arrive-ride/  start-ride/  complete-ride/  release-booking/

Design notes
  * Money is never taken from the client: fare = PricingRule.quote(), discount = PromoCode.validate_for().
  * Booking has no "arrived" status. Arrival is stored as a BookingStatusHistory row
    (note=ARRIVED_NOTE) and reported as status "arrived" by the serializer. No migration needed.
  * Online payments (momo/visa/debit): booking stays "pending" until the Paystack webhook /
    payments/verify/ calls Booking.confirm_payment_verified(), which moves it to "searching_rider".
    Cash goes straight to dispatch; wallet is debited at creation (all-or-nothing).
  * Delivery category has no model field, so it is stored as a "[food]" prefix in
    ParcelDetail.description and parsed back out. Add a real field when you next run migrations.
"""
import logging
import math
import re
import uuid
from datetime import timedelta
from decimal import Decimal
from functools import wraps

from django.core.cache import cache
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import IntegrityError, transaction
from django.db.models import Exists, OuterRef, Q
from django.utils import timezone
from rest_framework import serializers, status
from rest_framework.decorators import api_view, permission_classes, throttle_classes
from rest_framework.pagination import PageNumberPagination
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import UserRateThrottle

from .models import (
    PROVIDER_METHODS,
    SERVICE_CHOICES,
    Booking,
    BookingOffer,
    BookingStatusHistory,
    CustomerProfile,
    CustomerRiderPreference,
    Notification,
    ParcelDetail,
    PricingRule,
    PromoCode,
    RiderProfile,
    Service,
    ServiceQualification,
    SystemConfig,
    Zone,
    compute_fare_split,
    haversine_km,
    normalize_gh_phone,
    phone_validator,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
CURRENCY = "GHS"
SERVICE_ALIASES = {"delivery": "parcel", "waste": "waste-bin"}  # old frontend keys
CUSTOMER_PAYMENT_METHODS = ("momo", "visa", "debit", "wallet", "cash")
ONLINE_METHODS = PROVIDER_METHODS  # ("momo", "visa", "debit")
PAID_STATUSES = ("held", "released")
CLOSED_STATUSES = ("completed", "cancelled")
CANCELLABLE_BY_CUSTOMER = ("pending", "searching_rider", "assigned")
ARRIVED_NOTE = "rider_arrived"
DELIVERY_CATEGORIES = ("food", "parcel", "documents", "other")
CATEGORY_RE = re.compile(r"^\[(\w+)\]\s*(.*)$", re.S)

ROAD_FACTOR = 1.3       # straight line -> road distance when the client sends no usable distance
AVG_SPEED_KMH = 25
MIN_TRIP_KM = 0.1
PAYMENT_WINDOW_MIN = 30  # unpaid online bookings are auto-cancelled after this
SCHEDULE_LEAD_MIN = 30   # scheduled jobs reach riders this long before the pickup time


# ---------------------------------------------------------------------------
# Responses, errors, throttles
# ---------------------------------------------------------------------------
def ok(message=None, status_code=status.HTTP_200_OK, **kwargs):
    data = {"success": True}
    if message:
        data["message"] = message
    data.update(kwargs)
    return Response(data, status=status_code)


def fail(message, status_code=status.HTTP_400_BAD_REQUEST, **extra):
    # "error", "detail" and "message" are all set because the customer page, rider
    # dashboard and views.py each read a different one of them.
    return Response(
        {"success": False, "error": message, "detail": message, "message": message, **extra},
        status=status_code,
    )


def _flatten(errors, field=""):
    if isinstance(errors, dict):
        for key, value in errors.items():
            yield from _flatten(value, "" if key in ("non_field_errors", "detail") else key)
    elif isinstance(errors, (list, tuple)):
        for item in errors:
            yield from _flatten(item, field)
    else:
        yield f"{field}: {errors}" if field else str(errors)


def validation_fail(serializer):
    return fail(" ".join(_flatten(serializer.errors)) or "Invalid request.", status.HTTP_400_BAD_REQUEST)


class BookingRejected(Exception):
    """Raised anywhere in this module to abort with a clean API error (and roll back)."""

    def __init__(self, message, status_code=status.HTTP_400_BAD_REQUEST, **extra):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.extra = extra


class BookingThrottle(UserRateThrottle):
    scope = "booking"
    rate = "60/min"


class QuoteThrottle(UserRateThrottle):
    scope = "booking_quote"
    rate = "60/min"


class RiderActionThrottle(UserRateThrottle):
    scope = "rider_action"
    rate = "120/min"


def _guard(view):
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        try:
            return view(request, *args, **kwargs)
        except BookingRejected as exc:
            return fail(exc.message, exc.status_code, **exc.extra)
    return wrapper


def _rider_required(view):
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        rider = RiderProfile.objects.filter(user=request.user, deleted_at__isnull=True).first()
        if rider is None:
            return fail("Rider profile not found.", status.HTTP_404_NOT_FOUND)
        return view(request, *args, rider=rider, **kwargs)
    return wrapper


def _customer_of(user):
    profile = CustomerProfile.objects.filter(user=user).first()
    if profile is None:
        raise BookingRejected("A customer account is required.", status.HTTP_403_FORBIDDEN)
    return profile


def _money(value):
    return f"{Decimal(str(value)):.2f}"


def _lookup(ident):
    """Accept either the UUID `id` or the `booking_ref`."""
    try:
        return Q(id=uuid.UUID(str(ident)))
    except ValueError:
        return Q(booking_ref=str(ident).strip().upper())


def _booking_qs():
    return (
        Booking.objects.select_related("customer", "assigned_rider", "parcel_detail")
        .annotate(has_arrived=Exists(
            BookingStatusHistory.objects.filter(booking=OuterRef("pk"), note=ARRIVED_NOTE)))
    )


# ---------------------------------------------------------------------------
# Output serializer
# ---------------------------------------------------------------------------
class BookingSerializer(serializers.ModelSerializer):
    """
    context["role"]  = "customer" | "rider"
    context["rider"] = the RiderProfile of the viewer (rider role only)
    Riders see customer contact details only for their own active job.
    Start / recipient codes are customer-only.
    """

    status = serializers.SerializerMethodField()
    total_amount = serializers.SerializerMethodField()
    price = serializers.SerializerMethodField()
    parcel_details = serializers.SerializerMethodField()
    delivery_category = serializers.SerializerMethodField()
    rider = serializers.SerializerMethodField()
    rider_name = serializers.SerializerMethodField()
    rider_phone = serializers.SerializerMethodField()
    rider_lat = serializers.SerializerMethodField()
    rider_lon = serializers.SerializerMethodField()
    rider_plate = serializers.SerializerMethodField()
    rider_rating = serializers.SerializerMethodField()
    customer_name = serializers.SerializerMethodField()
    customer_phone = serializers.SerializerMethodField()
    start_code = serializers.SerializerMethodField()
    recipient_name = serializers.SerializerMethodField()
    recipient_phone = serializers.SerializerMethodField()
    recipient_code = serializers.SerializerMethodField()
    requires_recipient_code = serializers.SerializerMethodField()
    rider_payout = serializers.SerializerMethodField()
    distance_km = serializers.SerializerMethodField()
    can_cancel = serializers.SerializerMethodField()
    payment_required = serializers.SerializerMethodField()

    class Meta:
        model = Booking
        fields = [
            "id", "booking_ref", "service_type", "status", "pickup", "pickup_lat", "pickup_lng",
            "destination", "destination_lat", "destination_lng", "estimated_distance_km",
            "estimated_duration_min", "notes", "parcel_details", "delivery_category",
            "fare_before_discount", "surge_multiplier", "discount_amount", "total_fare",
            "total_amount", "price", "tip_amount", "payment_status", "payment_method",
            "payment_required", "scheduled_at", "created_at", "accepted_at", "started_at",
            "completed_at", "cancelled_at", "cancellation_reason", "cancellation_fee",
            "rider", "rider_name", "rider_phone", "rider_lat", "rider_lon", "rider_plate",
            "rider_rating", "customer_name", "customer_phone", "start_code",
            "recipient_name", "recipient_phone", "recipient_code", "requires_recipient_code",
            "rider_payout", "distance_km", "can_cancel",
        ]
        read_only_fields = fields

    # -- helpers
    def _is_customer(self):
        return self.context.get("role", "customer") == "customer"

    def _mine(self, obj):
        viewer = self.context.get("rider")
        return bool(viewer and obj.assigned_rider_id == viewer.pk)

    @staticmethod
    def _parcel(obj):
        return getattr(obj, "parcel_detail", None)

    @staticmethod
    def _active(obj):
        return obj.status in Booking.ACTIVE_STATUSES

    # -- fields
    def get_status(self, obj):
        if obj.status == "assigned":
            arrived = getattr(obj, "has_arrived", None)
            if arrived is None:
                arrived = obj.status_history.filter(note=ARRIVED_NOTE).exists()
            if arrived:
                return "arrived"
        return obj.status

    def get_total_amount(self, obj):
        return _money(obj.total_fare)

    def get_price(self, obj):
        return _money(obj.total_fare)

    def get_delivery_category(self, obj):
        parcel = self._parcel(obj)
        match = CATEGORY_RE.match(parcel.description) if parcel else None
        return match.group(1) if match else None

    def get_parcel_details(self, obj):
        parcel = self._parcel(obj)
        item = ""
        if parcel and parcel.description:
            match = CATEGORY_RE.match(parcel.description)
            item = (match.group(2) if match else parcel.description).strip()
        return " | ".join(p for p in (item, obj.notes) if p)

    def get_rider(self, obj):
        return str(obj.assigned_rider_id) if obj.assigned_rider_id else None

    def get_rider_name(self, obj):
        return obj.assigned_rider.full_name if obj.assigned_rider_id and self._is_customer() else None

    def get_rider_phone(self, obj):
        if obj.assigned_rider_id and self._is_customer() and self._active(obj):
            return obj.assigned_rider.phone
        return None

    def get_rider_lat(self, obj):
        r = obj.assigned_rider if obj.assigned_rider_id else None
        if r and self._is_customer() and self._active(obj) and r.location_is_fresh:
            return r.current_lat
        return None

    def get_rider_lon(self, obj):
        r = obj.assigned_rider if obj.assigned_rider_id else None
        if r and self._is_customer() and self._active(obj) and r.location_is_fresh:
            return r.current_lng
        return None

    def get_rider_plate(self, obj):
        return obj.assigned_rider.motorbike_plate_number if obj.assigned_rider_id and self._is_customer() else None

    def get_rider_rating(self, obj):
        return obj.assigned_rider.rating_average if obj.assigned_rider_id and self._is_customer() else None

    def get_customer_name(self, obj):
        name = (obj.customer.full_name if obj.customer_id else "") or "Customer"
        if self._is_customer() or self._mine(obj):
            return name
        return name.split()[0]  # open jobs: first name only

    def get_customer_phone(self, obj):
        if not obj.customer_id:
            return None
        if self._is_customer() or (self._mine(obj) and self._active(obj)):
            return obj.customer.phone or None
        return None

    def get_start_code(self, obj):
        return obj.start_code if self._is_customer() and obj.status == "assigned" else None

    def get_recipient_name(self, obj):
        parcel = self._parcel(obj)
        return parcel.recipient_name if parcel and (self._is_customer() or self._mine(obj)) else None

    def get_recipient_phone(self, obj):
        parcel = self._parcel(obj)
        return parcel.recipient_phone if parcel and (self._is_customer() or self._mine(obj)) else None

    def get_recipient_code(self, obj):
        parcel = self._parcel(obj)
        return parcel.recipient_code if parcel and self._is_customer() and self._active(obj) else None

    def get_requires_recipient_code(self, obj):
        parcel = self._parcel(obj)
        return bool(parcel and parcel.require_recipient_code)

    def get_rider_payout(self, obj):
        if self._is_customer():
            return None
        if self._mine(obj):
            return _money(obj.rider_payout)
        viewer = self.context.get("rider")
        rate = viewer.commission_rate if viewer else None
        return _money(compute_fare_split(obj.total_fare, rate)[1]) if rate is not None else None

    def get_distance_km(self, obj):
        return getattr(obj, "distance_km", None)  # set by _available_jobs

    def get_can_cancel(self, obj):
        return self._is_customer() and obj.status in CANCELLABLE_BY_CUSTOMER

    def get_payment_required(self, obj):
        return (obj.status == "pending" and obj.payment_method in ONLINE_METHODS
                and obj.payment_status not in PAID_STATUSES)


def _serialize(booking, role="customer", rider=None):
    return BookingSerializer(booking, context={"role": role, "rider": rider}).data


class BookingPagination(PageNumberPagination):
    page_size = 50
    page_size_query_param = "page_size"
    max_page_size = 100


# ---------------------------------------------------------------------------
# Input serializers
# ---------------------------------------------------------------------------
def _valid_service_key(value):
    value = (value or "").strip().lower()
    value = SERVICE_ALIASES.get(value, value)
    if value not in {k for k, _ in SERVICE_CHOICES}:
        raise serializers.ValidationError("Unknown service.")
    return value


def _check_point(label, lat, lng):
    if lat is None or lng is None:
        raise serializers.ValidationError({label: "Choose a location from the suggestions."})
    if lat == 0 and lng == 0:
        raise serializers.ValidationError({label: "Invalid coordinates."})


class QuoteSerializer(serializers.Serializer):
    service_type = serializers.CharField(max_length=20, required=False, allow_blank=True)
    pickup_lat = serializers.FloatField(min_value=-90, max_value=90)
    pickup_lng = serializers.FloatField(min_value=-180, max_value=180)
    destination_lat = serializers.FloatField(min_value=-90, max_value=90, required=False, allow_null=True)
    destination_lng = serializers.FloatField(min_value=-180, max_value=180, required=False, allow_null=True)
    distance_km = serializers.FloatField(min_value=0, required=False, allow_null=True)
    duration_min = serializers.FloatField(min_value=0, required=False, allow_null=True)

    def validate_service_type(self, value):
        return _valid_service_key(value) if value else ""

    def validate(self, attrs):
        _check_point("pickup_lat", attrs["pickup_lat"], attrs["pickup_lng"])
        return attrs


class BookingCreateSerializer(serializers.Serializer):
    service_type = serializers.CharField(max_length=20)
    pickup = serializers.CharField(max_length=255)
    pickup_lat = serializers.FloatField(min_value=-90, max_value=90)
    pickup_lng = serializers.FloatField(min_value=-180, max_value=180)
    destination = serializers.CharField(max_length=255, required=False, allow_blank=True, default="")
    destination_lat = serializers.FloatField(min_value=-90, max_value=90, required=False, allow_null=True)
    destination_lng = serializers.FloatField(min_value=-180, max_value=180, required=False, allow_null=True)
    distance_km = serializers.FloatField(min_value=0, required=False, allow_null=True)
    duration_min = serializers.FloatField(min_value=0, required=False, allow_null=True)
    notes = serializers.CharField(max_length=500, required=False, allow_blank=True, default="")
    payment_method = serializers.ChoiceField(choices=CUSTOMER_PAYMENT_METHODS)
    promo_code = serializers.CharField(max_length=30, required=False, allow_blank=True, default="")
    scheduled_at = serializers.DateTimeField(required=False, allow_null=True)
    delivery_category = serializers.ChoiceField(choices=DELIVERY_CATEGORIES, required=False, allow_null=True)
    item_description = serializers.CharField(max_length=200, required=False, allow_blank=True, default="")
    is_fragile = serializers.BooleanField(required=False, default=False)
    declared_value = serializers.DecimalField(max_digits=10, decimal_places=2, required=False,
                                              min_value=Decimal("0"), default=Decimal("0.00"))
    recipient_name = serializers.CharField(max_length=150, required=False, allow_blank=True, default="")
    recipient_phone = serializers.CharField(max_length=30, required=False, allow_blank=True, default="")

    def validate_service_type(self, value):
        return _valid_service_key(value)

    def validate_promo_code(self, value):
        return value.strip().upper()

    def validate_scheduled_at(self, value):
        if value is None:
            return value
        now = timezone.now()
        if value < now + timedelta(minutes=5):
            raise serializers.ValidationError("Choose a time at least 5 minutes from now.")
        if value > now + timedelta(days=30):
            raise serializers.ValidationError("Bookings can be scheduled up to 30 days ahead.")
        return value

    def validate(self, attrs):
        service = attrs["service_type"]
        _check_point("pickup_lat", attrs["pickup_lat"], attrs["pickup_lng"])
        attrs["pickup"] = attrs["pickup"].strip()
        if service != "waste-bin":
            if not attrs.get("destination", "").strip():
                raise serializers.ValidationError({"destination": "Enter a destination."})
            _check_point("destination_lat", attrs.get("destination_lat"), attrs.get("destination_lng"))
        if service == "parcel":
            name = attrs.get("recipient_name", "").strip()
            phone = normalize_gh_phone(attrs.get("recipient_phone", ""))
            if len(name) < 2:
                raise serializers.ValidationError({"recipient_name": "Enter the recipient's name."})
            try:
                phone_validator(phone)
            except DjangoValidationError:
                raise serializers.ValidationError({"recipient_phone": "Enter a valid recipient phone number."})
            attrs["recipient_name"], attrs["recipient_phone"] = name, phone
            attrs["delivery_category"] = attrs.get("delivery_category") or "other"
        return attrs


class PromoValidateSerializer(serializers.Serializer):
    code = serializers.CharField(max_length=30)
    amount = serializers.DecimalField(max_digits=10, decimal_places=2, min_value=Decimal("0"))
    service_type = serializers.CharField(max_length=20)

    def validate_code(self, value):
        return value.strip().upper()

    def validate_service_type(self, value):
        return _valid_service_key(value)


# ---------------------------------------------------------------------------
# Pricing
# ---------------------------------------------------------------------------
def _trip_metrics(service_key, p_lat, p_lng, d_lat, d_lng, client_km, client_min):
    """
    Distance and duration used for pricing. The client's OSRM numbers are accepted only when
    they are plausible against the straight-line distance; otherwise the server estimate is used.
    """
    if service_key == "waste-bin":
        return 0.0, 0
    straight = haversine_km(p_lat, p_lng, d_lat, d_lng)
    if straight is None:
        raise BookingRejected("Choose a destination.", status.HTTP_422_UNPROCESSABLE_ENTITY)
    if straight < MIN_TRIP_KM:
        raise BookingRejected("Pickup and destination are too close together.", status.HTTP_422_UNPROCESSABLE_ENTITY)

    km = round(straight * ROAD_FACTOR, 2)
    if client_km is not None and straight * 0.95 <= client_km <= max(straight * 3, straight + 2):
        km = round(float(client_km), 2)
    max_km = float(SystemConfig.get_decimal("MAX_TRIP_KM", "60"))
    if km > max_km:
        raise BookingRejected(f"Trips are limited to {max_km:g} km.", status.HTTP_422_UNPROCESSABLE_ENTITY)

    default_min = max(1, math.ceil(km / AVG_SPEED_KMH * 60))
    minutes = default_min
    if client_min is not None and 1 <= client_min <= default_min * 3:
        minutes = max(1, round(client_min))
    return km, int(minutes)


def _quote(service_key, *, p_lat, p_lng, d_lat=None, d_lng=None, client_km=None, client_min=None, when=None):
    service = Service.objects.filter(pk=service_key, is_active=True).first()
    if service is None:
        raise BookingRejected("This service is not available right now.", status.HTTP_422_UNPROCESSABLE_ENTITY)

    zone = Zone.for_point(p_lat, p_lng)
    if zone is not None and zone.services.exists() and not zone.services.filter(pk=service_key).exists():
        raise BookingRejected(f"{service.name} is not available in {zone.name} yet.",
                              status.HTTP_422_UNPROCESSABLE_ENTITY)
    if zone is None and SystemConfig.is_enabled("REQUIRE_SERVICE_ZONE"):
        raise BookingRejected("This pickup point is outside our service area.", status.HTTP_422_UNPROCESSABLE_ENTITY)

    km, minutes = _trip_metrics(service_key, p_lat, p_lng, d_lat, d_lng, client_km, client_min)
    try:
        quote = PricingRule.quote(service_key, km, minutes, zone=zone, when=when)
    except DjangoValidationError:
        logger.error("No pricing rule for service=%s zone=%s", service_key, zone)
        raise BookingRejected("Pricing is not available for this service yet.",
                              status.HTTP_503_SERVICE_UNAVAILABLE) from None
    return {"zone": zone, "distance_km": km, "duration_min": minutes, **quote}


def _quote_payload(q):
    return {
        "available": True,
        "distance_km": q["distance_km"],
        "duration_min": q["duration_min"],
        "subtotal": _money(q["subtotal"]),
        "surge_multiplier": str(q["surge_multiplier"]),
        "total_fare": _money(q["total_fare"]),
        "cancellation_fee": _money(q["cancellation_fee"]),
        "currency": CURRENCY,
        "zone": q["zone"].name if q["zone"] else None,
    }


# ---------------------------------------------------------------------------
# Customer: creation
# ---------------------------------------------------------------------------
def _cannot_book_message(profile):
    if profile.is_blocked:
        return "Your account is blocked. Contact support."
    return f"Clear your outstanding fee of GH₵{_money(profile.outstanding_fees)} before booking."


def _expire_stale_unpaid(profile, user):
    """An abandoned online checkout must not block the customer from booking again."""
    cutoff = timezone.now() - timedelta(minutes=PAYMENT_WINDOW_MIN)
    stale = Booking.objects.filter(
        customer=profile, status="pending", payment_method__in=ONLINE_METHODS,
        payment_status__in=("initiated", "pending_confirmation", "failed"), created_at__lt=cutoff)
    for booking in stale:
        booking.cancel_booking(cancelled_by_user=user, reason="Payment not completed in time", by_role="system")


def _create_booking(user, profile, data):
    service_key = data["service_type"]
    is_waste = service_key == "waste-bin"
    d_lat = data["pickup_lat"] if is_waste else data.get("destination_lat")
    d_lng = data["pickup_lng"] if is_waste else data.get("destination_lng")

    with transaction.atomic():
        # Row lock: two simultaneous requests cannot both pass the "no active booking" check.
        locked = CustomerProfile.objects.select_for_update().get(pk=profile.pk)
        if not locked.can_book:
            raise BookingRejected(_cannot_book_message(locked), status.HTTP_403_FORBIDDEN)

        _expire_stale_unpaid(locked, user)
        active = Booking.objects.filter(customer=locked).exclude(status__in=CLOSED_STATUSES).first()
        if active:
            raise BookingRejected("You already have an active booking.", status.HTTP_409_CONFLICT,
                                  active_booking=active.booking_ref)

        quote = _quote(service_key, p_lat=data["pickup_lat"], p_lng=data["pickup_lng"], d_lat=d_lat, d_lng=d_lng,
                       client_km=data.get("distance_km"), client_min=data.get("duration_min"),
                       when=data.get("scheduled_at"))

        booking = Booking(
            service_type=service_key,
            source=Booking.Source.APP,
            customer=locked,
            zone=quote["zone"],
            pickup=data["pickup"],
            pickup_lat=data["pickup_lat"],
            pickup_lng=data["pickup_lng"],
            destination=data["pickup"] if is_waste else data["destination"].strip(),
            destination_lat=d_lat,
            destination_lng=d_lng,
            estimated_distance_km=quote["distance_km"],
            estimated_duration_min=quote["duration_min"],
            notes=data.get("notes", "").strip(),
            payment_method=data["payment_method"],
            scheduled_at=data.get("scheduled_at"),
        )
        booking.apply_quote(quote)
        booking.save()  # also splits the fare into commission / rider payout

        if service_key == "parcel":
            category = data["delivery_category"]
            ParcelDetail.objects.create(
                booking=booking,
                size=ParcelDetail.Size.DOCUMENT if category == "documents" else ParcelDetail.Size.SMALL,
                description=f"[{category}] {data.get('item_description', '').strip()}".strip()[:255],
                declared_value=data.get("declared_value") or Decimal("0.00"),
                is_fragile=data.get("is_fragile", False),
                recipient_name=data["recipient_name"],
                recipient_phone=data["recipient_phone"],
            )

        if data.get("promo_code"):
            applied, message = booking.apply_promo(data["promo_code"])
            if not applied:
                raise BookingRejected(message, status.HTTP_422_UNPROCESSABLE_ENTITY)

        booking._log("", "pending", by=user, note="Booking created")

        method = booking.payment_method
        if method == "wallet":
            paid, message = booking.pay_with_wallet()
            if not paid:
                raise BookingRejected(message, status.HTTP_402_PAYMENT_REQUIRED)
        elif booking.total_fare <= 0:
            # A 100% promo: nothing to collect, go straight to dispatch.
            paid, message = booking.confirm_payment_verified("promo", payment_ref=f"FREE-{booking.booking_ref}")
            if not paid:
                raise BookingRejected(message, status.HTTP_409_CONFLICT)
        elif method == "cash":
            started, message = booking.mark_searching()
            if not started:
                raise BookingRejected(message, status.HTTP_409_CONFLICT)
        # momo / visa / debit: stays "pending" until Paystack confirms (webhook or payments/verify/).

    logger.info("Booking created ref=%s service=%s method=%s", booking.booking_ref, service_key, method)
    return _booking_qs().get(pk=booking.pk)


def _payment_info(booking):
    required = (booking.status == "pending" and booking.payment_method in ONLINE_METHODS
                and booking.payment_status not in PAID_STATUSES)
    return {
        "required": required,
        "method": booking.payment_method,
        "status": booking.payment_status,
        "booking_ref": booking.booking_ref,  # POST payments/initialize/ {purpose:"booking", booking_ref}
    }


# ---------------------------------------------------------------------------
# Customer endpoints
# ---------------------------------------------------------------------------
@api_view(["POST"])
@permission_classes([IsAuthenticated])
@throttle_classes([QuoteThrottle])
@_guard
def booking_quote(request):
    """Server-side price for one service (service_type) or for every active service."""
    serializer = QuoteSerializer(data=request.data)
    if not serializer.is_valid():
        return validation_fail(serializer)
    data = serializer.validated_data

    keys = [data["service_type"]] if data.get("service_type") else list(
        Service.objects.filter(is_active=True).values_list("key", flat=True))
    quotes = {}
    for key in keys:
        needs_destination = key != "waste-bin"
        if needs_destination and data.get("destination_lat") is None:
            quotes[key] = {"available": False, "reason": "Choose a destination to see the price."}
            continue
        try:
            quotes[key] = _quote_payload(_quote(
                key, p_lat=data["pickup_lat"], p_lng=data["pickup_lng"],
                d_lat=data.get("destination_lat"), d_lng=data.get("destination_lng"),
                client_km=data.get("distance_km"), client_min=data.get("duration_min")))
        except BookingRejected as exc:
            quotes[key] = {"available": False, "reason": exc.message}
    return ok(quotes=quotes, currency=CURRENCY)


@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated])
@throttle_classes([BookingThrottle])
@_guard
def bookings_collection(request):
    profile = _customer_of(request.user)
    ctx = {"role": "customer"}

    if request.method == "GET":
        qs = _booking_qs().filter(customer=profile)
        scope = request.query_params.get("status")
        if scope == "active":
            qs = qs.exclude(status__in=CLOSED_STATUSES)
        elif scope == "history":
            qs = qs.filter(status__in=CLOSED_STATUSES)
        paginator = BookingPagination()
        page = paginator.paginate_queryset(qs, request)
        return paginator.get_paginated_response(BookingSerializer(page, many=True, context=ctx).data)

    serializer = BookingCreateSerializer(data=request.data)
    if not serializer.is_valid():
        return validation_fail(serializer)

    idem = (request.headers.get("Idempotency-Key") or "").strip()[:80]
    idem_key = f"booking:idem:{request.user.pk}:{idem}" if idem else None
    if idem_key:
        ref = cache.get(idem_key)
        existing = _booking_qs().filter(customer=profile, booking_ref=ref).first() if ref else None
        if existing:  # a retry of a request that already succeeded
            return ok("Booking already placed.", booking=_serialize(existing), payment=_payment_info(existing),
                      id=str(existing.id), booking_ref=existing.booking_ref)

    booking = _create_booking(request.user, profile, serializer.validated_data)
    if idem_key:
        cache.set(idem_key, booking.booking_ref, 600)
    return ok("Booking placed.", status.HTTP_201_CREATED, booking=_serialize(booking),
              payment=_payment_info(booking), id=str(booking.id), booking_ref=booking.booking_ref)


def _customer_cancel(user, booking, reason):
    if booking.status not in CANCELLABLE_BY_CUSTOMER:
        raise BookingRejected(
            "This trip has already started. Contact support if something is wrong."
            if booking.status == "in_progress" else "This booking can no longer be cancelled.",
            status.HTTP_409_CONFLICT)
    done, message = booking.cancel_booking(cancelled_by_user=user, reason=(reason or "Cancelled by customer")[:500],
                                           by_role="customer")
    if not done:
        raise BookingRejected(message, status.HTTP_409_CONFLICT)
    return _booking_qs().get(pk=booking.pk)


@api_view(["GET", "PATCH"])
@permission_classes([IsAuthenticated])
@throttle_classes([BookingThrottle])
@_guard
def booking_detail(request, ref):
    profile = _customer_of(request.user)
    booking = _booking_qs().filter(_lookup(ref), customer=profile).first()
    if booking is None:
        raise BookingRejected("Booking not found.", status.HTTP_404_NOT_FOUND)

    if request.method == "GET":
        return ok(booking=_serialize(booking), payment=_payment_info(booking))

    if request.data.get("status") != "cancelled":
        raise BookingRejected("Only cancelling is supported: send {\"status\": \"cancelled\"}.")
    booking = _customer_cancel(request.user, booking, str(request.data.get("reason") or ""))
    return ok("Booking cancelled.", booking=_serialize(booking))


@api_view(["POST"])
@permission_classes([IsAuthenticated])
@throttle_classes([BookingThrottle])
@_guard
def booking_cancel(request, ref):
    profile = _customer_of(request.user)
    booking = _booking_qs().filter(_lookup(ref), customer=profile).first()
    if booking is None:
        raise BookingRejected("Booking not found.", status.HTTP_404_NOT_FOUND)
    booking = _customer_cancel(request.user, booking, str(request.data.get("reason") or ""))
    return ok("Booking cancelled.", booking=_serialize(booking))


@api_view(["POST"])
@permission_classes([IsAuthenticated])
@throttle_classes([QuoteThrottle])
@_guard
def promo_validate(request):
    """Preview only. The discount is recomputed from the server fare when the booking is created."""
    serializer = PromoValidateSerializer(data=request.data)
    if not serializer.is_valid():
        return validation_fail(serializer)
    data = serializer.validated_data
    profile = _customer_of(request.user)

    promo = PromoCode.objects.filter(code=data["code"]).first()
    if promo is None:
        return ok(valid=False, discount_amount="0.00", message="We don't recognise that code.")
    valid, message, discount = promo.validate_for(profile, data["service_type"], data["amount"])
    return ok(valid=bool(valid), discount_amount=_money(discount if valid else 0),
              message=message if not valid else f"{promo.code} applied: GH₵{_money(discount)} off")


# ---------------------------------------------------------------------------
# Rider: presence + job list
# ---------------------------------------------------------------------------
def _truthy(value):
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _float_or_none(value, low, high):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if low <= number <= high else None


@api_view(["POST"])
@permission_classes([IsAuthenticated])
@throttle_classes([RiderActionThrottle])
@_guard
@_rider_required
def rider_presence(request, rider):
    """Go online/offline. The dashboard's PATCH {online} must call this (see notes)."""
    if "online" not in request.data:
        raise BookingRejected("`online` is required.")

    if _truthy(request.data["online"]):
        if not rider.has_minimum_deposit:
            minimum = SystemConfig.get_decimal("MIN_RIDER_BOOKING_BALANCE", "50.00")
            raise BookingRejected(f"Please deposit at least GH₵{minimum} to start accepting bookings",
                                  status.HTTP_403_FORBIDDEN)
        lat = _float_or_none(request.data.get("latitude"), -90, 90)
        lng = _float_or_none(request.data.get("longitude"), -180, 180)
        try:
            rider.go_online(lat, lng)
        except DjangoValidationError as exc:
            raise BookingRejected(exc.messages[0], status.HTTP_403_FORBIDDEN) from None
    else:
        if Booking.objects.filter(assigned_rider=rider, status__in=Booking.ACTIVE_STATUSES).exists():
            raise BookingRejected("Finish your current trip before going offline.", status.HTTP_409_CONFLICT)
        rider.go_offline()
    return ok(is_online=rider.is_online)


def _available_jobs(rider):
    """Open, paid, in-range jobs this rider may accept right now (cheap SQL box, exact Haversine after)."""
    if not (rider.is_online and rider.can_receive_offers and rider.has_minimum_deposit):
        return []
    if rider.current_lat is None or rider.current_lng is None:
        return []

    services = list(ServiceQualification.objects.filter(
        rider=rider, status=ServiceQualification.Status.APPROVED).values_list("service_id", flat=True))
    if not services:
        return []

    radius = float(SystemConfig.get_decimal("DISPATCH_MAX_PICKUP_KM", "10"))
    dlat = radius / 111.0
    dlng = radius / (111.0 * max(math.cos(math.radians(rider.current_lat)), 0.01))
    lead = timezone.now() + timedelta(minutes=SCHEDULE_LEAD_MIN)

    qs = (
        _booking_qs()
        .filter(status="searching_rider", assigned_rider__isnull=True, service_type__in=services,
                pickup_lat__range=(rider.current_lat - dlat, rider.current_lat + dlat),
                pickup_lng__range=(rider.current_lng - dlng, rider.current_lng + dlng))
        .filter(Q(scheduled_at__isnull=True) | Q(scheduled_at__lte=lead))
        .exclude(pk__in=BookingOffer.objects.filter(
            rider=rider, status__in=[BookingOffer.Status.DECLINED, BookingOffer.Status.EXPIRED]
        ).values("booking_id"))
        .exclude(customer_id__in=CustomerRiderPreference.objects.filter(
            rider=rider, preference="blocked").values("customer_id"))
    )
    if not rider.can_take_cash_jobs:
        qs = qs.exclude(payment_method="cash")

    jobs = []
    for booking in qs[:100]:
        distance = haversine_km(rider.current_lat, rider.current_lng, booking.pickup_lat, booking.pickup_lng)
        if distance is not None and distance <= radius:
            booking.distance_km = distance
            jobs.append(booking)
    jobs.sort(key=lambda b: b.distance_km)
    return jobs[:20]


@api_view(["GET"])
@permission_classes([IsAuthenticated])
@throttle_classes([RiderActionThrottle])
@_guard
@_rider_required
def rider_bookings(request, rider):
    """
    Same shape the rider dashboard already reads from /list-bookings/: one flat `bookings`
    list. Customer phone numbers are included only for the rider's own active job.
    """
    active = list(_booking_qs().filter(assigned_rider=rider, status__in=Booking.ACTIVE_STATUSES))
    history = list(_booking_qs().filter(assigned_rider=rider, status__in=CLOSED_STATUSES)[:100])
    available = [] if active else _available_jobs(rider)

    reason = None
    if not rider.is_online:
        reason = "offline"
    elif not rider.has_minimum_deposit:
        reason = "wallet"
    elif not rider.can_receive_offers:
        reason = "account"

    ctx = {"role": "rider", "rider": rider}
    return ok(bookings=BookingSerializer(active + history + available, many=True, context=ctx).data,
              open_jobs=len(available), blocked_reason=reason)


# ---------------------------------------------------------------------------
# Rider: job actions
# ---------------------------------------------------------------------------
def _booking_ident(request):
    ident = request.data.get("booking_id") or request.data.get("booking") or request.data.get("booking_ref")
    if not ident:
        raise BookingRejected("booking_id is required.")
    return ident


def _find_booking(request):
    booking = _booking_qs().filter(_lookup(_booking_ident(request))).first()
    if booking is None:
        raise BookingRejected("Booking not found.", status.HTTP_404_NOT_FOUND)
    return booking


def _owned_booking(request, rider):
    booking = _find_booking(request)
    if booking.assigned_rider_id != rider.pk:
        raise BookingRejected("This booking is not assigned to you.", status.HTTP_403_FORBIDDEN)
    return booking


def _rider_payload(request, booking, rider):
    fresh = _booking_qs().get(pk=booking.pk)
    return BookingSerializer(fresh, context={"role": "rider", "rider": rider}).data


def _record_offer(booking, rider, outcome):
    """Best-effort acceptance metrics, de-duplicated by the (booking, rider) unique constraint."""
    now = timezone.now()
    try:
        with transaction.atomic():
            _, created = BookingOffer.objects.get_or_create(
                booking=booking, rider=rider,
                defaults={
                    "status": {"accepted": BookingOffer.Status.ACCEPTED, "declined": BookingOffer.Status.DECLINED,
                               "expired": BookingOffer.Status.EXPIRED}[outcome],
                    "expires_at": now, "responded_at": now,
                    "distance_to_pickup_km": rider.get_distance_km(booking.pickup_lat, booking.pickup_lng),
                })
            if created:
                rider.record_offer(outcome)
    except IntegrityError:
        pass
    except Exception:  # metrics must never block a job action
        logger.exception("Could not record offer outcome booking=%s rider=%s", booking.pk, rider.pk)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
@throttle_classes([RiderActionThrottle])
@_guard
@_rider_required
def accept_booking(request, rider):
    booking = _find_booking(request)
    offer = BookingOffer.objects.filter(
        booking=booking, rider=rider, status=BookingOffer.Status.SENT, expires_at__gte=timezone.now()).first()
    if offer:  # a dispatcher offer exists: use the model path so its metrics are recorded
        accepted, message = offer.accept()
    else:
        accepted, message = booking.assign_rider(rider)
        if accepted:
            _record_offer(booking, rider, "accepted")

    if not accepted:
        text = message.lower()
        if "no longer available" in text or "answered" in text or "expired" in text:
            code = status.HTTP_409_CONFLICT
        elif "active booking" in text:
            code = status.HTTP_400_BAD_REQUEST
        else:  # deposit / wallet / not eligible / not qualified / offline
            code = status.HTTP_403_FORBIDDEN
        raise BookingRejected(message, code)
    return ok(message, booking=_rider_payload(request, booking, rider))


@api_view(["POST"])
@permission_classes([IsAuthenticated])
@throttle_classes([RiderActionThrottle])
@_guard
@_rider_required
def decline_booking(request, rider):
    booking = _find_booking(request)
    if booking.status == "searching_rider":
        timed_out = str(request.data.get("reason", "")).lower() == "timeout"
        _record_offer(booking, rider, "expired" if timed_out else "declined")
    return ok("Job declined.")


@api_view(["POST"])
@permission_classes([IsAuthenticated])
@throttle_classes([RiderActionThrottle])
@_guard
@_rider_required
def arrive_ride(request, rider):
    booking = _owned_booking(request, rider)
    if booking.status != "assigned":
        raise BookingRejected("You can only mark arrival on an assigned job.", status.HTTP_409_CONFLICT)

    with transaction.atomic():
        locked = Booking.objects.select_for_update().get(pk=booking.pk)
        already = BookingStatusHistory.objects.filter(booking=locked, note=ARRIVED_NOTE).exists()
        if locked.status != "assigned":
            raise BookingRejected("You can only mark arrival on an assigned job.", status.HTTP_409_CONFLICT)
        if not already:
            locked._log("assigned", "assigned", by=request.user, note=ARRIVED_NOTE)

    if not already and booking.customer_id:
        Notification.notify(booking.customer.user, "Your rider has arrived",
                            f"{rider.full_name} is at your pickup point. Share your start code to begin.",
                            category=Notification.Category.BOOKING, data={"booking": booking.booking_ref})
    return ok("Arrival confirmed.", booking=_rider_payload(request, booking, rider))


@api_view(["POST"])
@permission_classes([IsAuthenticated])
@throttle_classes([RiderActionThrottle])
@_guard
@_rider_required
def start_ride(request, rider):
    booking = _owned_booking(request, rider)
    code = str(request.data.get("code") or request.data.get("start_code") or "").strip()
    started, message = booking.start_booking(code=code, by=request.user)
    if not started:
        raise BookingRejected(message, status.HTTP_400_BAD_REQUEST if "code" in message.lower()
                              else status.HTTP_409_CONFLICT)
    return ok(message, booking=_rider_payload(request, booking, rider))


@api_view(["POST"])
@permission_classes([IsAuthenticated])
@throttle_classes([RiderActionThrottle])
@_guard
@_rider_required
def complete_ride(request, rider):
    booking = _owned_booking(request, rider)
    recipient_code = str(request.data.get("recipient_code") or "").strip() or None
    # complete_booking re-checks payment_status on the locked row; this only reports what we know.
    verified = booking.payment_status in PAID_STATUSES
    done, message = booking.complete_booking(payment_verified=verified, recipient_code=recipient_code,
                                             by=request.user)
    if not done:
        text = message.lower()
        raise BookingRejected(message, status.HTTP_400_BAD_REQUEST if "code" in text
                              else status.HTTP_402_PAYMENT_REQUIRED if "payment" in text
                              else status.HTTP_409_CONFLICT)
    return ok(message, booking=_rider_payload(request, booking, rider))


@api_view(["POST"])
@permission_classes([IsAuthenticated])
@throttle_classes([RiderActionThrottle])
@_guard
@_rider_required
def release_booking(request, rider):
    """Rider drops a job before starting it; the booking goes back into dispatch."""
    booking = _owned_booking(request, rider)
    released, message = booking.release_rider(reason=str(request.data.get("reason") or "")[:255])
    if not released:
        raise BookingRejected(message, status.HTTP_409_CONFLICT)
    return ok(message)

