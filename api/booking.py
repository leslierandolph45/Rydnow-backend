"""
Booking serializers (api/booking.py).

Matched to models.Booking, BookingStop, BookingStatusHistory and Payment:
  * Booking.rider is a USER (not a RiderProfile); rider details come from user.rider_profile.
  * Booking.customer is a USER; contact details come from user.customer_profile.
  * BookingStop uses `order` (exposed to clients as `sequence`).
  * BookingStatusHistory uses `status` / `notes` and related_name="status_history".
  * Fare breakdown, rider earnings and platform fee live on the Payment row created with the
    booking (Payment.metadata["fare"]); Booking itself stores subtotal / discount / total.
  * Booking's own column names differ from the names clients see. Output serializers map them
    with source=:  total_fare -> total_price,  destination_address -> dropoff_address,
    pickup_lat / pickup_lng -> pickup_latitude / pickup_longitude,
    destination_lat / destination_lng -> dropoff_latitude / dropoff_longitude.

Clients may send either spelling of the service ("okada"/"ride", "parcel"/"delivery",
"waste-bin"/"waste_collection") and either coordinate naming; both are normalised here.

Query tip for views (avoids N+1 on list/detail endpoints):
    Booking.objects.select_related("customer__customer_profile", "rider__rider_profile")
                   .prefetch_related("stops", "status_history")
"""
import math
from collections.abc import Mapping
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from django.utils import timezone
from rest_framework import serializers

from . import pricing
from .models import Booking, BookingStatusHistory, BookingStop, Payment


# Money is rendered as JSON numbers, like the quote endpoint.
JSON_NUMBER = {"coerce_to_string": False}


PAYMENT_METHOD_ALIASES = {
    "mobile_money": "momo", "mobile-money": "momo", "mtn": "momo", "momo": "momo",
    "visa": "card", "visa_card": "card", "debit": "card", "debit_card": "card",
    "credit_card": "card", "card": "card",
    "cash": "cash", "wallet": "wallet",
}
BOOKING_PAYMENT_METHODS = ("cash", "momo", "card", "wallet")


QUOTE_ALIASES = {
    "pickup_latitude": "pickup_lat", "pickup_longitude": "pickup_lng",
    "dropoff_latitude": "destination_lat", "dropoff_longitude": "destination_lng",
    "dropoff_lat": "destination_lat", "dropoff_lng": "destination_lng",
    "destination_latitude": "destination_lat", "destination_longitude": "destination_lng",
}
CREATE_ALIASES = {
    "pickup_lat": "pickup_latitude", "pickup_lng": "pickup_longitude",
    "destination_lat": "dropoff_latitude", "destination_lng": "dropoff_longitude",
    "dropoff_lat": "dropoff_latitude", "dropoff_lng": "dropoff_longitude",
    "destination_address": "dropoff_address",
    "quote": "quote_token",
}


def _with_aliases(data, aliases):
    """Copy incoming data, filling canonical names from their aliases."""
    if not isinstance(data, Mapping):
        return data
    data = data.dict() if hasattr(data, "dict") else dict(data)
    for alias, canonical in aliases.items():
        if canonical not in data and alias in data:
            data[canonical] = data[alias]
    return data


def _rider_status_choices():
    """
    Statuses a rider may move a booking to. Booking.TRANSITIONS only allows
    ACCEPTED -> EN_ROUTE -> ARRIVED -> COMPLETED (plus CANCELLED), so ARRIVED must be here or
    a rider can never finish a trip. Unknown names are skipped rather than raising at import.
    """
    names = ("ACCEPTED", "EN_ROUTE", "ARRIVED", "COMPLETED", "CANCELLED")
    return [getattr(Booking.Status, n) for n in names if hasattr(Booking.Status, n)]


# ---------------------------------------------------------------------------
# Fields
# ---------------------------------------------------------------------------
class CoordinateField(serializers.Field):
    """Latitude/longitude as a Decimal rounded to 6 places (the precision signed into quotes)."""
    default_error_messages = {
        "invalid": "A valid coordinate is required.",
        "range": "Coordinate is out of range.",
    }

    def __init__(self, *, limit, **kwargs):
        self.limit = Decimal(limit)
        super().__init__(**kwargs)

    def to_internal_value(self, data):
        if isinstance(data, bool):
            self.fail("invalid")
        try:
            value = Decimal(str(data)).quantize(pricing.COORD, rounding=ROUND_HALF_UP)
        except (InvalidOperation, ValueError, TypeError):
            self.fail("invalid")
        if not value.is_finite() or abs(value) > self.limit:
            self.fail("range")
        return value

    def to_representation(self, value):
        return float(value)


def LatField(**kwargs):
    return CoordinateField(limit=90, **kwargs)


def LngField(**kwargs):
    return CoordinateField(limit=180, **kwargs)


# ---------------------------------------------------------------------------
# Quote
# ---------------------------------------------------------------------------
class TripStopSerializer(serializers.Serializer):
    address = serializers.CharField(max_length=255, required=False, allow_blank=True, default="")
    lat = LatField()
    lng = LngField()
    # Decimal default so a missing value has the same type as a validated one
    # (DRF skips validate_<field> when the default is used).
    wait_minutes = serializers.FloatField(
        min_value=0, max_value=pricing.MAX_WAIT_INPUT_MINUTES, required=False,
        default=Decimal("0"),
    )

    def to_internal_value(self, data):
        data = _with_aliases(
            data,
            {"latitude": "lat", "longitude": "lng", "expected_wait_minutes": "wait_minutes"},
        )
        return super().to_internal_value(data)

    def validate_wait_minutes(self, value):
        if not math.isfinite(value):
            raise serializers.ValidationError("A valid number of minutes is required.")
        return Decimal(str(round(value, 2)))


class _ServiceMixin:
    def validate_service_type(self, value):
        try:
            return pricing.normalize_service_type(value)
        except ValueError:
            raise serializers.ValidationError("Unknown service type.")

    @staticmethod
    def _check_stops(attrs):
        rate = pricing.RATES[attrs["service_type"]]
        if len(attrs.get("stops", [])) > rate.max_stops:
            raise serializers.ValidationError(
                {"stops": f"This service allows at most {rate.max_stops} stops."}
            )


class QuoteRequestSerializer(_ServiceMixin, serializers.Serializer):
    service_type = serializers.CharField()
    pickup_lat = LatField()
    pickup_lng = LngField()
    destination_lat = LatField()
    destination_lng = LngField()
    stops = TripStopSerializer(many=True, required=False, default=list)
    promo_code = serializers.CharField(max_length=50, required=False, allow_blank=True, default="")

    def to_internal_value(self, data):
        return super().to_internal_value(_with_aliases(data, QUOTE_ALIASES))

    def validate_promo_code(self, value):
        return value.strip()

    def validate(self, attrs):
        self._check_stops(attrs)
        return attrs


# ---------------------------------------------------------------------------
# Create / status / cancel / webhook
# ---------------------------------------------------------------------------
class BookingCreateSerializer(_ServiceMixin, serializers.Serializer):
    quote_token = serializers.CharField()
    service_type = serializers.CharField()
    pickup_address = serializers.CharField(max_length=255)
    pickup_latitude = LatField()
    pickup_longitude = LngField()
    dropoff_address = serializers.CharField(max_length=255, required=False, allow_blank=True, default="")
    dropoff_latitude = LatField()
    dropoff_longitude = LngField()
    stops = TripStopSerializer(many=True, required=False, default=list)
    notes = serializers.CharField(required=False, allow_blank=True, default="")
    payment_method = serializers.CharField(required=False, default="cash")
    scheduled_for = serializers.DateTimeField(required=False, allow_null=True, default=None)

    def to_internal_value(self, data):
        return super().to_internal_value(_with_aliases(data, CREATE_ALIASES))

    def validate_payment_method(self, value):
        method = PAYMENT_METHOD_ALIASES.get(str(value).strip().lower())
        if method not in BOOKING_PAYMENT_METHODS:
            raise serializers.ValidationError(
                f"Choose one of: {', '.join(BOOKING_PAYMENT_METHODS)}."
            )
        return method

    def validate_scheduled_for(self, value):
        if value is not None and value <= timezone.now():
            raise serializers.ValidationError("Scheduled time must be in the future.")
        return value

    def validate(self, attrs):
        self._check_stops(attrs)
        if attrs["service_type"] != "waste-bin" and not attrs.get("dropoff_address", "").strip():
            raise serializers.ValidationError(
                {"dropoff_address": "A drop-off address is required for this service."}
            )
        return attrs


class BookingStatusUpdateSerializer(serializers.Serializer):
    status = serializers.ChoiceField(choices=_rider_status_choices())
    note = serializers.CharField(max_length=255, required=False, allow_blank=True, default="")


class BookingCancelSerializer(serializers.Serializer):
    reason = serializers.CharField(max_length=255, required=False, allow_blank=True, default="")


class BookingWebhookSerializer(serializers.Serializer):
    event_id = serializers.CharField(max_length=255)
    event_type = serializers.CharField(max_length=100)
    source = serializers.CharField(max_length=50, required=False, default="booking")
    data = serializers.DictField()


# ---------------------------------------------------------------------------
# Nearby riders
# ---------------------------------------------------------------------------
class NearbyRidersQuerySerializer(_ServiceMixin, serializers.Serializer):
    lat = serializers.FloatField(min_value=-90, max_value=90)
    lng = serializers.FloatField(min_value=-180, max_value=180)
    radius_km = serializers.FloatField(min_value=0.5, max_value=25, default=5)
    service_type = serializers.CharField(required=False)
    limit = serializers.IntegerField(min_value=1, max_value=50, default=20)

    def validate(self, attrs):
        # NaN slips past min/max validators because every comparison with it is False.
        for key in ("lat", "lng", "radius_km"):
            if not math.isfinite(attrs[key]):
                raise serializers.ValidationError({key: "A finite number is required."})
        return attrs


class NearbyRiderSerializer(serializers.Serializer):
    """Public-safe rider card: no phone number, no identity documents."""
    id = serializers.CharField()
    name = serializers.CharField()
    rating = serializers.FloatField()
    tier = serializers.CharField()
    distance_km = serializers.FloatField()
    latitude = serializers.FloatField()
    longitude = serializers.FloatField()
    vehicle = serializers.DictField()


# ---------------------------------------------------------------------------
# Helpers for output serializers
# ---------------------------------------------------------------------------
def _payment_of(booking):
    """Latest Payment for a booking, cached on the instance (Payment links by booking_ref)."""
    if not hasattr(booking, "_payment_cache"):
        booking._payment_cache = (
            Payment.objects.filter(booking_ref=booking.reference).order_by("-created_at").first()
        )
    return booking._payment_cache


def _fare_of(booking):
    payment = _payment_of(booking)
    return ((payment.metadata or {}).get("fare") or {}) if payment else {}


def _display_name(user):
    return (user.get_full_name() or user.get_username()) if user else ""


def _customer_name(booking):
    profile = getattr(booking.customer, "customer_profile", None)
    if profile is not None and profile.full_name:
        return profile.full_name
    return _display_name(booking.customer)


def _customer_phone(booking):
    profile = getattr(booking.customer, "customer_profile", None)
    if profile is None:
        return ""
    return str(profile.mobile) if profile.mobile else (profile.phone or "")


# ---------------------------------------------------------------------------
# Booking output
# ---------------------------------------------------------------------------
class BookingStopSerializer(serializers.ModelSerializer):
    sequence = serializers.IntegerField(source="order", read_only=True)

    class Meta:
        model = BookingStop
        fields = ["sequence", "address", "latitude", "longitude"]
        read_only_fields = fields
        # BookingStop.latitude/longitude are model FloatFields, which already render as JSON
        # numbers. coerce_to_string is a DecimalField-only argument and raises TypeError on a
        # FloatField, so no extra_kwargs here.


class BookingStatusHistorySerializer(serializers.ModelSerializer):
    at = serializers.DateTimeField(source="created_at", read_only=True)

    class Meta:
        model = BookingStatusHistory
        fields = ["status", "notes", "at"]
        read_only_fields = fields


class BookingListSerializer(serializers.ModelSerializer):
    service_key = serializers.SerializerMethodField()
    status_label = serializers.CharField(source="get_status_display", read_only=True)

    # Map frontend names -> actual model fields
    dropoff_address = serializers.CharField(source="destination_address", read_only=True)
    total_price = serializers.DecimalField(
        source="total_fare",
        max_digits=10,
        decimal_places=2,
        read_only=True,
        coerce_to_string=False,
    )

    class Meta:
        model = Booking
        fields = [
            "reference", "service_type", "service_key", "status", "status_label",
            "pickup_address", "dropoff_address", "scheduled_for",
            "subtotal", "discount_amount", "total_price", "currency", "created_at",
        ]
        read_only_fields = fields
        # subtotal / discount_amount are DecimalFields. total_price is declared above.
        extra_kwargs = {f: JSON_NUMBER for f in ("subtotal", "discount_amount")}

    def get_service_key(self, obj):
        return pricing.service_key_for_booking(obj.service_type)


class BookingDetailSerializer(BookingListSerializer):
    """What the booking's customer sees."""
    # Model columns are pickup_lat / pickup_lng / destination_lat / destination_lng.
    pickup_latitude = serializers.FloatField(source="pickup_lat", read_only=True)
    pickup_longitude = serializers.FloatField(source="pickup_lng", read_only=True)
    dropoff_latitude = serializers.FloatField(source="destination_lat", read_only=True)
    dropoff_longitude = serializers.FloatField(source="destination_lng", read_only=True)
    stops = BookingStopSerializer(many=True, read_only=True)
    history = BookingStatusHistorySerializer(source="status_history", many=True, read_only=True)
    rider = serializers.SerializerMethodField()
    payment = serializers.SerializerMethodField()

    class Meta(BookingListSerializer.Meta):
        fields = BookingListSerializer.Meta.fields + [
            "pickup_latitude", "pickup_longitude", "dropoff_latitude", "dropoff_longitude",
            "notes", "accepted_at", "started_at", "arrived_at", "completed_at", "cancelled_at",
            "cancellation_reason", "stops", "history", "rider", "payment",
        ]
        read_only_fields = fields

    def get_rider(self, obj):
        if not obj.rider_id:
            return None
        profile = getattr(obj.rider, "rider_profile", None)
        if profile is None:
            return {"name": _display_name(obj.rider)}
        photo = None
        if profile.profile_photo:
            request = self.context.get("request")
            url = profile.profile_photo.url
            photo = request.build_absolute_uri(url) if request else url
        return {
            "id": str(profile.public_id),
            "name": profile.full_name,
            "phone": profile.phone,
            "rating": profile.rating_average,
            "completed_trips": profile.completed_trips,
            "photo": photo,
            "vehicle": {
                "model": profile.motorbike_model,
                "plate": profile.motorbike_plate_number,
                "color": profile.motorbike_color,
            },
        }

    def get_payment(self, obj):
        payment = _payment_of(obj)
        if payment is None:
            return None
        return {
            "reference": payment.reference,
            "method": payment.method,
            "status": payment.status,
            "amount": float(payment.amount),
        }


class RiderBookingDetailSerializer(BookingDetailSerializer):
    """What a rider sees: own earnings, and the customer's contact only once assigned."""
    customer = serializers.SerializerMethodField()
    earnings = serializers.SerializerMethodField()

    class Meta(BookingDetailSerializer.Meta):
        fields = BookingDetailSerializer.Meta.fields + ["customer", "earnings"]
        read_only_fields = fields

    def get_customer(self, obj):
        data = {"name": _customer_name(obj)}
        request = self.context.get("request")
        if request and obj.rider_id and obj.rider_id == request.user.pk:
            data["phone"] = _customer_phone(obj)
        return data

    def get_earnings(self, obj):
        fare = _fare_of(obj)
        return float(fare["rider_earnings"]) if "rider_earnings" in fare else None

    def get_payment(self, obj):
        # Riders need the method (cash vs digital) but not provider references.
        payment = _payment_of(obj)
        return None if payment is None else {"method": payment.method, "status": payment.status}


class StaffBookingDetailSerializer(BookingDetailSerializer):
    """Everything, including the stored fare snapshot and revenue split."""
    customer = serializers.SerializerMethodField()
    fare = serializers.SerializerMethodField()

    class Meta(BookingDetailSerializer.Meta):
        fields = BookingDetailSerializer.Meta.fields + ["customer", "fare"]
        read_only_fields = fields

    def get_customer(self, obj):
        return {
            "id": obj.customer_id,
            "name": _customer_name(obj),
            "phone": _customer_phone(obj),
        }

    def get_fare(self, obj):
        return _fare_of(obj) or None

