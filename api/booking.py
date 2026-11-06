"""Booking creation service — validation, idempotency, duplicate prevention.

Guarantees
----------
* Input is validated before touching the database.
* Duplicate / double-tap submissions are prevented race-free (row lock per customer).
* Retried requests carrying the same idempotency_key return the original booking.
* Side effects (dispatch, notifications) only fire after the transaction commits.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import timedelta
from typing import Optional

from django.db import IntegrityError, transaction
from django.utils import timezone

from .constants import (
    ACTIVE_BOOKING_STATUSES,
    DUPLICATE_BOOKING_WINDOW_SECONDS,
    DUPLICATE_LOCATION_TOLERANCE_KM,
    GHANA_BOUNDS,
    MAX_IDEMPOTENCY_KEY_LENGTH,
    MAX_NOTES_LENGTH,
)
from .exceptions import (
    DuplicateBookingError,
    InvalidBookingError,
    ServiceUnavailableError,
)
from .utils import haversine_km

logger = logging.getLogger(__name__)
Point = Optional[tuple[float, float]]


@dataclass(frozen=True)
class BookingResult:
    """Outcome of create_booking. created is False on an idempotent replay."""
    booking: "Booking"
    created: bool


# ── Validation helpers ───────────────────────────────────────────────
def _clean_point(label: str, lat, lng) -> Point:
    """Validate a coordinate pair. Returns (lat, lng) floats, or None if unset."""
    if lat is None and lng is None:
        return None
    if lat is None or lng is None:
        raise InvalidBookingError(f"{label} needs both latitude and longitude.")

    try:
        lat, lng = float(lat), float(lng)
    except (TypeError, ValueError):
        raise InvalidBookingError(f"{label} coordinates must be numbers.") from None

    if not (math.isfinite(lat) and math.isfinite(lng)):
        raise InvalidBookingError(f"{label} coordinates are invalid.")
    if not (-90 <= lat <= 90 and -180 <= lng <= 180):
        raise InvalidBookingError(f"{label} coordinates are out of range.")

    min_lat, max_lat, min_lng, max_lng = GHANA_BOUNDS
    if not (min_lat <= lat <= max_lat and min_lng <= lng <= max_lng):
        raise InvalidBookingError(f"{label} is outside our service area.")

    return lat, lng


def _clean_notes(notes) -> str:
    notes = (notes or "").strip()
    if len(notes) > MAX_NOTES_LENGTH:
        raise InvalidBookingError(
            f"Notes must be {MAX_NOTES_LENGTH} characters or fewer."
        )
    return notes


def _clean_key(key) -> Optional[str]:
    if key is None:
        return None
    key = str(key).strip()
    if not key:
        return None
    if len(key) > MAX_IDEMPOTENCY_KEY_LENGTH:
        raise InvalidBookingError("Idempotency key is too long.")
    return key


# ── Duplicate detection ─────────────────────────────────────────────
def _same_point(stored_lat, stored_lng, point: Point) -> bool:
    """True if a stored coordinate is within tolerance of point (or both unset)."""
    if point is None:
        return stored_lat is None or stored_lng is None
    if stored_lat is None or stored_lng is None:
        return False
    distance = haversine_km(float(stored_lat), float(stored_lng), point[0], point[1])
    return distance <= DUPLICATE_LOCATION_TOLERANCE_KM


def _find_duplicate(customer, service, pickup: Point, dropoff: Point):
    """Return a recent, still-active booking for the same trip, if any."""
    # Import here to avoid circular import
    from .models import Booking

    window_start = timezone.now() - timedelta(seconds=DUPLICATE_BOOKING_WINDOW_SECONDS)
    candidates = Booking.objects.filter(
        customer=customer,
        service=service,
        created_at__gte=window_start,
        status__in=ACTIVE_BOOKING_STATUSES,
    ).order_by("-created_at")[:10]

    for existing in candidates:
        if _same_point(existing.pickup_lat, existing.pickup_lng, pickup) and _same_point(
            existing.dropoff_lat, existing.dropoff_lng, dropoff
        ):
            return existing
    return None


def _find_by_key(customer, key: Optional[str]):
    # Import here to avoid circular import
    from .models import Booking

    if not key:
        return None
    return Booking.objects.filter(customer=customer, idempotency_key=key).first()


# ── Post-commit hook ────────────────────────────────────────────────
def _after_commit(booking_id) -> None:
    """Runs only if the transaction committed. Hook dispatch/notifications here."""
    logger.info("booking.committed", extra={"booking_id": str(booking_id)})


# ── Public API ──────────────────────────────────────────────────────
def create_booking(
    *,
    customer: "CustomerProfile",
    service: "Service",
    pickup_lat=None,
    pickup_lng=None,
    dropoff_lat=None,
    dropoff_lng=None,
    notes: str = "",
    idempotency_key: Optional[str] = None,
) -> BookingResult:
    """
    Create a booking with validation, idempotency and duplicate protection.

    Returns a BookingResult. Raises InvalidBookingError, ServiceUnavailableError
    or DuplicateBookingError on failure.
    """
    # Import here to avoid circular import
    from .models import Booking, CustomerProfile, Service

    if not service.is_active:
        raise ServiceUnavailableError("This service is currently unavailable.")

    pickup = _clean_point("Pickup", pickup_lat, pickup_lng)
    dropoff = _clean_point("Dropoff", dropoff_lat, dropoff_lng)
    if pickup is None:
        raise InvalidBookingError("A pickup location is required.")
    notes = _clean_notes(notes)
    key = _clean_key(idempotency_key)

    try:
        with transaction.atomic():
            # Serialise concurrent requests from the same customer
            CustomerProfile.objects.select_for_update().only("pk").get(pk=customer.pk)

            replay = _find_by_key(customer, key)
            if replay:
                logger.info(
                    "booking.idempotent_replay",
                    extra={"booking_id": str(replay.pk), "customer_id": str(customer.pk)},
                )
                return BookingResult(replay, created=False)

            duplicate = _find_duplicate(customer, service, pickup, dropoff)
            if duplicate:
                logger.warning(
                    "booking.duplicate_blocked",
                    extra={"existing_id": str(duplicate.pk), "customer_id": str(customer.pk)},
                )
                raise DuplicateBookingError(duplicate)

            booking = Booking.objects.create(
                customer=customer,
                service=service,
                pickup_lat=pickup[0],
                pickup_lng=pickup[1],
                dropoff_lat=dropoff[0] if dropoff else None,
                dropoff_lng=dropoff[1] if dropoff else None,
                notes=notes,
                idempotency_key=key,
            )
            transaction.on_commit(lambda: _after_commit(booking.pk))

    except IntegrityError:
        replay = _find_by_key(customer, key)
        if replay:
            return BookingResult(replay, created=False)
        raise

    logger.info(
        "booking.created",
        extra={
            "booking_ref": booking.booking_ref,
            "customer_id": str(customer.pk),
            "service_id": str(service.pk),
        },
    )
    return BookingResult(booking, created=True)
