"""Business logic for bookings. Views stay thin and call into this module."""
import math
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from rest_framework import status as http
from rest_framework.exceptions import APIException, PermissionDenied

from .models import Booking, BookingStatusHistory, Rider

Status = Booking.Status

# Override any of these in settings.py, e.g. BOOKING_PRICING = {...}
DEFAULT_PRICING = {
    "bike": {"base": Decimal("2.00"), "per_km": Decimal("0.80")},
    "car": {"base": Decimal("4.00"), "per_km": Decimal("1.50")},
    "van": {"base": Decimal("8.00"), "per_km": Decimal("2.50")},
}
LOCATION_FRESHNESS = timedelta(minutes=5)


# ---------- Exceptions (DRF renders these automatically) ----------
class InvalidTransition(APIException):
    status_code = http.HTTP_409_CONFLICT
    default_detail = "This status change is not allowed."
    default_code = "invalid_transition"


class BookingConflict(APIException):
    status_code = http.HTTP_409_CONFLICT
    default_detail = "Booking was already taken by another rider."
    default_code = "booking_conflict"


# ---------- Geo helpers ----------
def haversine_km(lat1, lon1, lat2, lon2) -> float:
    lat1, lon1, lat2, lon2 = map(lambda v: math.radians(float(v)), (lat1, lon1, lat2, lon2))
    a = (
        math.sin((lat2 - lat1) / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    )
    return 6371.0088 * 2 * math.asin(math.sqrt(a))


def estimate_price(vehicle_type: str, distance_km: float) -> Decimal:
    pricing = getattr(settings, "BOOKING_PRICING", DEFAULT_PRICING)[vehicle_type]
    total = Decimal(pricing["base"]) + Decimal(pricing["per_km"]) * Decimal(str(distance_km))
    return total.quantize(Decimal("0.01"))


# ---------- create_booking ----------
@transaction.atomic
def create_booking(*, customer, data: dict) -> Booking:
    distance = round(
        haversine_km(
            data["pickup_latitude"], data["pickup_longitude"],
            data["dropoff_latitude"], data["dropoff_longitude"],
        ),
        2,
    )
    booking = Booking.objects.create(
        customer=customer,
        distance_km=Decimal(str(distance)),
        price=estimate_price(data["vehicle_type"], distance),
        currency=getattr(settings, "BOOKING_CURRENCY", "USD"),
        **data,
    )
    BookingStatusHistory.objects.create(
        booking=booking, from_status="", to_status=booking.status,
        changed_by=customer, source="api", note="Booking created",
    )
    # Hook: notify nearby riders here (Celery task, Channels group_send, FCM push...)
    return booking


# ---------- list_nearby_riders ----------
def list_nearby_riders(*, lat: float, lng: float, radius_km: float = 5,
                       vehicle_type: str | None = None, limit: int = 20):
    """Bounding-box prefilter in SQL, exact haversine ranking in Python.
    Swap for PostGIS (Distance / D) if you outgrow this."""
    lat_delta = radius_km / 111.0
    lng_delta = radius_km / (111.0 * max(math.cos(math.radians(lat)), 0.01))

    qs = Rider.objects.select_related("user").filter(
        status=Rider.Status.AVAILABLE,
        is_verified=True,
        last_location_at__gte=timezone.now() - LOCATION_FRESHNESS,
        latitude__range=(lat - lat_delta, lat + lat_delta),
        longitude__range=(lng - lng_delta, lng + lng_delta),
    )
    if vehicle_type:
        qs = qs.filter(vehicle_type=vehicle_type)

    riders = []
    for rider in qs:
        distance = haversine_km(lat, lng, rider.latitude, rider.longitude)
        if distance <= radius_km:
            rider.distance_km = round(distance, 2)
            riders.append(rider)
    riders.sort(key=lambda r: (r.distance_km, -r.rating))
    return riders[:limit]


# ---------- status transitions ----------
def _authorize(*, actor, booking: Booking, new_status: str):
    """Who may move a booking to which status."""
    if actor is None or actor.is_staff:  # system/webhook or admin
        return None
    rider = getattr(actor, "rider_profile", None)

    if booking.customer_id == actor.id:
        if new_status != Status.CANCELLED:
            raise PermissionDenied("Customers can only cancel their bookings.")
        return None
    if rider:
        if new_status == Status.ACCEPTED:
            if not rider.is_verified or rider.status != Rider.Status.AVAILABLE:
                raise PermissionDenied("Rider must be verified and available.")
            if rider.vehicle_type != booking.vehicle_type:
                raise PermissionDenied("Vehicle type does not match this booking.")
            return rider
        if booking.rider_id == rider.id:
            return rider
    raise PermissionDenied("You are not allowed to update this booking.")


@transaction.atomic
def transition_booking(*, booking_id, new_status: str, actor=None,
                       note: str = "", source: str = "api") -> Booking:
    # Row lock prevents two riders accepting the same booking.
    booking = Booking.objects.select_for_update().select_related("rider").get(pk=booking_id)
    rider = _authorize(actor=actor, booking=booking, new_status=new_status)

    if not booking.can_transition_to(new_status):
        raise InvalidTransition(
            f"Cannot change status from '{booking.status}' to '{new_status}'."
        )

    now = timezone.now()
    old_status = booking.status

    if new_status == Status.ACCEPTED:
        if booking.rider_id:
            raise BookingConflict()
        if rider is None:
            raise InvalidTransition("A rider is required to accept a booking.")
        booking.rider = rider
        booking.accepted_at = now
        Rider.objects.filter(pk=rider.pk).update(status=Rider.Status.BUSY)
    elif new_status == Status.ARRIVED:
        booking.arrived_at = now
    elif new_status == Status.IN_TRANSIT:
        booking.picked_up_at = now
    elif new_status == Status.COMPLETED:
        booking.completed_at = now
    elif new_status == Status.CANCELLED:
        booking.cancelled_at = now
        booking.cancelled_by = actor
        booking.cancellation_reason = note[:255]

    if new_status in Booking.TERMINAL_STATUSES and booking.rider_id:
        Rider.objects.filter(pk=booking.rider_id).update(status=Rider.Status.AVAILABLE)

    booking.status = new_status
    booking.save()

    BookingStatusHistory.objects.create(
        booking=booking, from_status=old_status, to_status=new_status,
        changed_by=actor, source=source, note=note[:255],
    )
    # Hook: push realtime update to customer/rider after commit:
    # transaction.on_commit(lambda: notify_booking_update.delay(booking.id))
    return booking


# ---------- cancel_booking ----------
def cancel_booking(*, booking_id, actor, reason: str = "") -> Booking:
    return transition_booking(
        booking_id=booking_id, new_status=Status.CANCELLED, actor=actor, note=reason
    )

