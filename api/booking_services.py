"""
booking_services.py - business logic for bookings, promos, payments, rider lookup and revenue.
Views stay thin; every money movement is locked and idempotent.

Matched to models.py as it stands:
  * Booking has: subtotal, discount_amount, total_price (= subtotal - discount, computed in save()),
    currency, accept()/start()/complete()/cancel() lifecycle methods and
    ACCEPTED -> IN_PROGRESS -> COMPLETED statuses (there is no "arrived"/"in_transit" status).
  * Booking.rider is a USER; the rider's RiderProfile is user.rider_profile.
  * The fare snapshot (commission, rider earnings, platform revenue, quote id) is stored on the
    Payment row created with the booking: Payment.metadata = {"quote_id", "fare", "waits", ...}.
    One quote can therefore create one booking only.
  * Promos use PromoCode.quote() / redeem() and PromoRedemption.release().
  * Cash trips: the rider collects the fare, so the company's share is DEBITED from the rider
    wallet (WalletTransaction COMMISSION). Digital trips: the rider wallet is CREDITED with earnings
    once the Payment is paid.

Requires PaymentEvent and RefundEvent (see models_patch.py): Payment.mark_as_paid / cancel / refund
and Refund.* write to them.
"""
import functools
import logging
from collections import defaultdict
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from django.db.models import F, Q
from django.utils import timezone
from rest_framework import status as http
from rest_framework.exceptions import APIException, PermissionDenied
from rest_framework.exceptions import ValidationError as DRFValidationError

from . import pricing
from .models import (
    Booking, BookingStatusHistory, BookingStop, CustomerProfile, CustomerWallet,
    LoyaltyAccount, Notification, Payment, PromoCode, PromoRedemption, Refund,
    RiderProfile, RiderWallet, ServiceQualification, WalletTransaction, _safely, audit,
)

logger = logging.getLogger(__name__)

Status = Booking.Status
ZERO = pricing.ZERO
RIDER_COMMITTED = (Status.ACCEPTED, Status.IN_PROGRESS)       # a rider is tied up
OPEN_STATUSES = (Status.PENDING, Status.ACCEPTED, Status.IN_PROGRESS)
PROVIDER_PAYMENT_TTL = timedelta(minutes=15)


def _setting(name, default):
    return getattr(settings, name, default)


# ---------------------------------------------------------------------------
# Errors (DRF renders these automatically)
# ---------------------------------------------------------------------------
class InvalidTransition(APIException):
    status_code = http.HTTP_409_CONFLICT
    default_detail = "This status change is not allowed."
    default_code = "invalid_transition"


class BookingConflict(APIException):
    status_code = http.HTTP_409_CONFLICT
    default_detail = "Booking was already taken by another rider."
    default_code = "booking_conflict"


class RiderBusy(APIException):
    status_code = http.HTTP_409_CONFLICT
    default_detail = "Finish your current job before taking another."
    default_code = "rider_busy"


class QuoteRejected(APIException):
    status_code = http.HTTP_400_BAD_REQUEST
    default_detail = "This price quote cannot be used."
    default_code = "quote_rejected"


class PromoRejected(APIException):
    status_code = http.HTTP_400_BAD_REQUEST
    default_detail = "This promo code cannot be used."
    default_code = "promo_rejected"


class TripTooLong(APIException):
    status_code = http.HTTP_400_BAD_REQUEST
    default_code = "trip_too_long"


class TooManyActiveBookings(APIException):
    status_code = http.HTTP_409_CONFLICT
    default_detail = "You have too many active bookings. Finish or cancel one first."
    default_code = "too_many_active_bookings"


class CustomerBlocked(APIException):
    status_code = http.HTTP_403_FORBIDDEN
    default_detail = "Your account cannot place bookings right now."
    default_code = "customer_blocked"


class InsufficientWalletBalance(APIException):
    status_code = http.HTTP_402_PAYMENT_REQUIRED
    default_detail = "Your wallet balance is too low for this booking."
    default_code = "insufficient_wallet_balance"


class PaymentMismatch(APIException):
    status_code = http.HTTP_409_CONFLICT
    default_detail = "Payment does not match the booking."
    default_code = "payment_mismatch"


def api_errors(fn):
    """Turn Django ValidationErrors raised by model methods into DRF 400 responses.
    Put it OUTSIDE @transaction.atomic so the transaction rolls back first."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except DjangoValidationError as exc:
            detail = exc.message_dict if hasattr(exc, "error_dict") else exc.messages
            raise DRFValidationError(detail)
    return wrapper


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def _service_key(booking):
    return pricing.service_key_for_booking(booking.service_type)


def _payment_for(booking, *, lock=False):
    qs = Payment.objects.filter(booking_ref=booking.reference).order_by("-created_at")
    return (qs.select_for_update() if lock else qs).first()


def _rider_profile_of(booking):
    return RiderProfile.objects.filter(user_id=booking.rider_id).first() if booking.rider_id else None


def _note(note, source):
    note = note or ""
    if source and source != "api":
        note = f"[{source}] {note}".strip()
    return note[:255]


def _record(booking, status, actor, note="", source="api"):
    BookingStatusHistory.objects.create(
        booking=booking, status=getattr(status, "value", status), notes=_note(note, source),
        changed_by=actor if getattr(actor, "pk", None) else None,
    )


def _notify(user, title, message, booking):
    _safely("notify", Notification.notify, user, title, message,
            category=Notification.Category.BOOKING, data={"reference": booking.reference})


def _is_first_time(user):
    return not Booking.objects.filter(customer=user).exclude(status=Status.CANCELLED).exists()


# ---------------------------------------------------------------------------
# Promo codes (eligibility is PromoCode.quote(); the discount maths is pricing.py)
# ---------------------------------------------------------------------------
def promo_terms(promo: PromoCode) -> pricing.PromoTerms:
    if promo.discount_type == PromoCode.DiscountType.PERCENTAGE:
        return pricing.PromoTerms(
            code=promo.code, percent_off=promo.discount_value, max_discount=promo.maximum_discount
        )
    return pricing.PromoTerms(code=promo.code, flat_off=promo.discount_value)


def validate_promo(*, code, user, service_key, ride_fare, now) -> PromoCode:
    promo = PromoCode.find(code)
    if promo is None:
        raise PromoRejected("This promo code is not valid.")
    try:
        promo.quote(
            user, ride_fare, service_key, currency=pricing.CURRENCY,
            is_first_time_customer=_is_first_time(user), at=now,
        )
    except DjangoValidationError as exc:
        raise PromoRejected(" ".join(exc.messages))
    return promo


# ---------------------------------------------------------------------------
# Quoting
# ---------------------------------------------------------------------------
def quote_trip(*, user, service_type, route, wait_minutes, promo_code=""):
    """Price a trip. `service_type` is a pricing key. `user` is None for anonymous
    estimates (no token issued)."""
    distance = pricing.route_distance_km(route)
    if distance > pricing.MAX_DISTANCE_KM:
        raise TripTooLong(f"Trip exceeds the maximum distance of {pricing.MAX_DISTANCE_KM} km")

    now = timezone.now()
    surge = pricing.resolve_surge(service_type, now)
    fare = pricing.calculate_fare(service_type, distance, surge, wait_minutes)

    if promo_code:
        if user is None:
            raise PromoRejected("Log in to use a promo code.")
        promo = validate_promo(
            code=promo_code, user=user, service_key=service_type,
            ride_fare=fare["ride_fare"], now=now,
        )
        fare = pricing.calculate_fare(service_type, distance, surge, wait_minutes, promo=promo_terms(promo))

    token = None
    if user is not None:
        token, _ = pricing.issue_quote(
            user_id=user.pk, service_type=service_type, route=route,
            wait_minutes=wait_minutes, distance_km=distance, surge=surge, fare=fare,
        )
    return {
        "now": now,
        "valid_until": now + pricing.QUOTE_VALIDITY,
        "distance": distance,
        "surge": surge,
        "fare": fare,
        "token": token,
    }


# ---------------------------------------------------------------------------
# Create booking
# ---------------------------------------------------------------------------
def _booking_for_quote(qid):
    payment = Payment.objects.filter(metadata__quote_id=qid).first()
    return Booking.objects.filter(reference=payment.booking_ref).first() if payment else None


@api_errors
@transaction.atomic
def create_booking(*, customer, data: dict):
    """Returns (booking, created). The price comes ONLY from the signed quote."""
    try:
        quote = pricing.read_quote(data["quote_token"], user_id=customer.pk)
    except pricing.QuoteError as exc:
        raise QuoteRejected(exc.message, code=exc.code)

    # Serialise per customer: stops double-tap duplicates and races on the active-booking cap.
    get_user_model().objects.select_for_update().get(pk=customer.pk)

    qid = quote["qid"]
    prior = _booking_for_quote(qid)
    if prior is not None:
        if prior.customer_id == customer.pk:
            return prior, False  # idempotent retry (double tap, flaky network)
        raise QuoteRejected("This quote has already been used.", code="quote_used")

    service_key = data["service_type"]
    stops = data.get("stops", [])
    route = pricing.build_route(
        (data["pickup_latitude"], data["pickup_longitude"]), stops,
        (data["dropoff_latitude"], data["dropoff_longitude"]),
    )
    if not pricing.quote_matches_trip(quote, service_type=service_key, route=route):
        raise QuoteRejected(
            "Trip details do not match the quote. Please request a new price.", code="quote_mismatch"
        )

    profile = getattr(customer, "customer_profile", None)
    if profile is not None and not profile.can_book:
        reason = profile.blocked_reason if profile.is_blocked else "Please clear your outstanding fees first."
        raise CustomerBlocked(reason or CustomerBlocked.default_detail)

    active = Booking.objects.filter(customer=customer, status__in=OPEN_STATUSES).count()
    if active >= _setting("BOOKING_MAX_ACTIVE_PER_CUSTOMER", 3):
        raise TooManyActiveBookings()

    fare = quote["fare"]
    total, discount = Decimal(fare["total"]), Decimal(fare["discount"])
    method = data.get("payment_method", "cash")
    first_time = _is_first_time(customer)
    now = timezone.now()

    booking = Booking(
        customer=customer,
        service_type=pricing.booking_service_value(service_key),
        pickup_address=data["pickup_address"],
        pickup_latitude=data["pickup_latitude"], pickup_longitude=data["pickup_longitude"],
        dropoff_address=data.get("dropoff_address", ""),
        dropoff_latitude=data["dropoff_latitude"], dropoff_longitude=data["dropoff_longitude"],
        scheduled_for=data.get("scheduled_for"),
        notes=data.get("notes", ""),
        subtotal=total + discount,          # gross: ride + stops + waiting + booking fee
        discount_amount=discount,
        currency=pricing.CURRENCY,
    )
    booking.clean()
    booking.save()                          # generates the reference; computes total_price
    if booking.total_price != total:
        raise RuntimeError(f"Booking total {booking.total_price} differs from quote {total}")

    BookingStop.objects.bulk_create([
        BookingStop(booking=booking, order=i, address=s.get("address", ""),
                    latitude=s["lat"], longitude=s["lng"])
        for i, s in enumerate(stops, start=1)
    ])

    payment = Payment.objects.create(
        booking_ref=booking.reference, amount=booking.total_price, currency=pricing.CURRENCY,
        method=method, status=Payment.Status.PENDING,
        payer_profile_id=getattr(profile, "pk", None),
        expires_at=now + PROVIDER_PAYMENT_TTL if method in ("momo", "card") else None,
        metadata={"quote_id": qid, "fare": fare, "waits": quote["waits"], "distance_km": quote["dist"]},
    )

    if fare["promo_code"]:
        promo = PromoCode.find(fare["promo_code"])
        if promo is None:
            raise PromoRejected("This promo code is no longer valid.")
        try:
            promo.redeem(
                customer, booking.reference, Decimal(fare["ride_fare"]), service_key,
                currency=pricing.CURRENCY, is_first_time_customer=first_time, at=now,
            )
        except DjangoValidationError as exc:
            raise PromoRejected(" ".join(exc.messages))

    if method == "wallet":
        ok, message = CustomerWallet.for_user(customer).pay_for_booking(booking.total_price, booking)
        if not ok:
            raise InsufficientWalletBalance(message)
        payment.mark_as_paid(user=customer)

    _record(booking, Status.PENDING, customer, "Booking created")
    audit(customer, "booking.created", booking, total=booking.total_price, method=method)
    # Hook, after commit: transaction.on_commit(lambda: dispatch.offer_booking(booking.pk))
    return booking, True


# ---------------------------------------------------------------------------
# Rider lookup
# ---------------------------------------------------------------------------
def _available_riders():
    """Riders who could take a job now. Own query: RiderProfileQuerySet.dispatchable()
    excludes busy riders with a UUID-vs-user-id comparison that fails on PostgreSQL."""
    fresh = timezone.now() - timedelta(seconds=_setting("RIDER_LOCATION_STALE_SECONDS", 120))
    busy_users = Booking.objects.filter(status__in=RIDER_COMMITTED, rider__isnull=False).values("rider_id")
    return (
        RiderProfile.objects.active().verified().not_suspended().not_restricted()
        .filter(is_online=True, current_lat__isnull=False, current_lng__isnull=False,
                location_updated_at__gte=fresh)
        .exclude(user_id__in=busy_users)
        .select_related("user")
    )


def list_nearby_riders(*, lat: float, lng: float, radius_km: float = 5,
                       service_type: str | None = None, limit: int = 20):
    qs = _available_riders()
    if service_type:
        qs = qs.filter(qualifications__service_id=service_type,
                       qualifications__status=ServiceQualification.Status.APPROVED).distinct()
    pairs = qs.nearby(lat, lng, radius_km=radius_km, limit=limit)
    pairs.sort(key=lambda p: (p[1], -p[0].rating_average))
    return [
        {
            "id": str(r.public_id), "name": r.full_name, "rating": r.rating_average, "tier": r.tier,
            "distance_km": km, "latitude": r.current_lat, "longitude": r.current_lng,
            "vehicle": {"model": r.motorbike_model, "color": r.motorbike_color},
        }
        for r, km in pairs[:limit]
    ]


# ---------------------------------------------------------------------------
# Status transitions
# ---------------------------------------------------------------------------
def _authorize(*, actor, booking, new_status):
    """Who may move a booking to which status. Returns the actor's RiderProfile or None."""
    if actor is None or actor.is_staff:        # system / webhook / admin
        return None
    profile = getattr(actor, "rider_profile", None)
    if booking.customer_id == actor.pk:
        if new_status != Status.CANCELLED:
            raise PermissionDenied("Customers can only cancel their bookings.")
        return None
    if profile is not None:
        if new_status == Status.ACCEPTED:
            return profile
        if booking.rider_id == actor.pk:
            return profile
    raise PermissionDenied("You are not allowed to update this booking.")


def _check_rider_eligible(profile, booking):
    if not profile.can_accept_jobs:
        raise PermissionDenied("Your account cannot take jobs right now.")
    if not profile.is_online:
        raise PermissionDenied("Go online to accept jobs.")
    if not profile.is_qualified_for(_service_key(booking)):
        raise PermissionDenied("You are not approved for this service.")
    if not profile.has_minimum_deposit:
        raise PermissionDenied("Top up your wallet to the minimum balance to take jobs.")
    payment = _payment_for(booking)
    if payment is not None and payment.method == Payment.Method.CASH and not profile.can_take_cash_jobs:
        raise PermissionDenied("Your wallet balance is too low for cash trips.")
    if Booking.objects.filter(rider_id=profile.user_id, status__in=RIDER_COMMITTED).exists():
        raise RiderBusy()


def _accept(booking, profile):
    if booking.status != Status.PENDING or booking.rider_id:
        raise BookingConflict()
    if profile is None:
        raise InvalidTransition("A rider is required to accept a booking.")
    # Lock the rider row so one rider cannot win two jobs at once.
    locked = RiderProfile.objects.select_for_update().get(pk=profile.pk)
    _check_rider_eligible(locked, booking)
    booking.accept(locked.user)
    _notify(booking.customer, "Rider on the way", f"{locked.full_name} accepted your booking.", booking)


def _start(booking):
    payment = _payment_for(booking)
    if payment and payment.method in Payment.PROVIDER_METHODS and not payment.is_successful:
        raise InvalidTransition("Waiting for the customer's payment to be confirmed.")
    booking.start()


def _complete(booking, actor):
    radius = _setting("BOOKING_COMPLETION_GEOFENCE_KM", 0)
    if radius and actor is not None and not actor.is_staff:
        profile = _rider_profile_of(booking)
        distance = profile.get_distance_km(booking.dropoff_latitude, booking.dropoff_longitude) if profile else None
        if distance is None or distance > radius:
            raise PermissionDenied("You must be at the drop-off point to complete this trip.")
    booking.complete()
    _after_complete(booking)


def _earn_loyalty(profile, booking):
    account, _ = LoyaltyAccount.objects.get_or_create(customer=profile)
    account.earn(booking.total_price, booking=booking)


def _after_complete(booking):
    """Non-critical bookkeeping runs in savepoints; money settlement never blocks completion."""
    rider = _rider_profile_of(booking)
    if rider is not None:
        _safely("rider trip stats", rider.record_trip_completed)
    customer_profile = getattr(booking.customer, "customer_profile", None)
    if customer_profile is not None:
        _safely("customer stats", customer_profile.record_completed_booking, booking)
        _safely("loyalty", _earn_loyalty, customer_profile, booking)
    _settle_quietly(booking)
    _notify(booking.customer, "Trip completed", "Thanks for riding with RYDNOW.", booking)


@api_errors
@transaction.atomic
def transition_booking(*, booking_id, new_status, actor=None, note: str = "", source: str = "api"):
    try:
        new_status = Status(new_status)
    except ValueError:
        raise InvalidTransition(f"Unknown status '{new_status}'.")
    if new_status == Status.CANCELLED:
        return cancel_booking(booking_id=booking_id, actor=actor, reason=note, source=source)

    # select_for_update without select_related: `rider` is a nullable FK.
    booking = Booking.objects.select_for_update().get(pk=booking_id)
    profile = _authorize(actor=actor, booking=booking, new_status=new_status)
    if not booking.can_transition_to(new_status):
        raise InvalidTransition(f"Cannot change status from '{booking.status}' to '{new_status.value}'.")

    if new_status == Status.ACCEPTED:
        _accept(booking, profile)
    elif new_status == Status.IN_PROGRESS:
        _start(booking)
    elif new_status == Status.COMPLETED:
        _complete(booking, actor)
    else:
        raise InvalidTransition("This status cannot be set directly.")

    _record(booking, new_status, actor, note, source)
    return booking


# ---------------------------------------------------------------------------
# Rider release, cancellation, expiry
# ---------------------------------------------------------------------------
def _release_locked(booking, actor, note="", source="api"):
    """Accepted -> pending again. Deliberately NOT a model transition: only a rider
    releasing their own job (or staff) can put a job back in the pool."""
    if booking.status != Status.ACCEPTED or booking.rider_id is None:
        raise InvalidTransition("Only an accepted booking can be released.")
    if actor is not None and not actor.is_staff and booking.rider_id != actor.pk:
        raise PermissionDenied("This is not your booking.")
    profile = _rider_profile_of(booking)
    Booking.objects.filter(pk=booking.pk).update(
        status=Status.PENDING, rider=None, accepted_at=None, updated_at=timezone.now())
    booking.refresh_from_db()
    if profile is not None:
        _safely("rider cancelled stat", profile.record_trip_cancelled)
    _record(booking, Status.PENDING, actor, note or "Rider released the booking", source)
    _notify(booking.customer, "Looking for a new rider", "Your rider cancelled. We are finding another.", booking)
    return booking


@api_errors
@transaction.atomic
def release_rider(*, booking_id, actor, reason: str = "", source: str = "api"):
    booking = Booking.objects.select_for_update().get(pk=booking_id)
    return _release_locked(booking, actor, reason, source)


def _charge_outstanding(booking, fee):
    profile = getattr(booking.customer, "customer_profile", None)
    if profile is None:
        logger.warning("Cancellation fee %s on %s has no customer profile to charge", fee, booking.reference)
        return
    CustomerProfile.objects.filter(pk=profile.pk).update(outstanding_fees=F("outstanding_fees") + fee)


def _unwind_payment(booking, actor, fee, reason):
    payment = _payment_for(booking, lock=True)
    if payment is None:
        return
    fee = min(fee, payment.amount)
    if fee > 0:
        payment.metadata = {**(payment.metadata or {}), "cancellation_fee": str(fee)}
        payment.save(update_fields=["metadata", "updated_at"])
        audit(actor, "booking.cancellation_fee", booking, fee=fee)

    if payment.status == Payment.Status.PENDING:
        payment.cancel(user=actor, reason=reason)
        if fee > 0:
            _charge_outstanding(booking, fee)
    elif payment.is_successful:
        refundable = payment.amount - payment.refunded_amount - fee
        if refundable <= 0:
            return
        if payment.method == Payment.Method.WALLET:
            ok, message = CustomerWallet.for_user(booking.customer).refund_to_wallet(
                refundable, booking=booking, reference=f"RFDWAL-{booking.reference}",
                description=f"Refund for {booking.reference}")
            if not ok:
                raise PaymentMismatch(message)
            payment.refund(amount=refundable, reason="Booking cancelled", user=actor)
        else:   # provider money goes back through the refund workflow (staff approve it)
            Refund.request(
                payment, Refund.Reason.CUSTOMER_REQUEST, amount=refundable,
                reason_details="Booking cancelled",
                requested_by=actor if getattr(actor, "pk", None) else None,
                idempotency_key=f"cancel:{booking.reference}",
            )


@api_errors
@transaction.atomic
def cancel_booking(*, booking_id, actor, reason: str = "", source: str = "api"):
    booking = Booking.objects.select_for_update().get(pk=booking_id)
    is_staff = actor is not None and actor.is_staff
    by_customer = actor is not None and not is_staff and booking.customer_id == actor.pk

    if actor is not None and not is_staff:
        if by_customer:
            if booking.status not in (Status.PENDING, Status.ACCEPTED):
                raise InvalidTransition("Only pending or accepted bookings can be cancelled.")
        elif booking.rider_id == actor.pk:
            return _release_locked(booking, actor, reason, source)   # a rider "cancelling" releases the job
        else:
            raise PermissionDenied("You are not allowed to cancel this booking.")
    if not booking.can_transition_to(Status.CANCELLED):
        raise InvalidTransition(f"A {booking.get_status_display().lower()} booking cannot be cancelled.")

    now = timezone.now()
    old_status = booking.status
    fee = ZERO
    if by_customer and booking.rider_id:
        fee = pricing.cancellation_fee(
            _service_key(booking), status=old_status, accepted_at=booking.accepted_at, now=now)

    rider_user = booking.rider
    booking.cancel(actor if getattr(actor, "pk", None) else None, reason or "")
    _unwind_payment(booking, actor, fee, reason or "Booking cancelled")
    for redemption in PromoRedemption.objects.filter(
            booking_ref=booking.reference, status=PromoRedemption.Status.REDEEMED):
        redemption.release("Booking cancelled")
    _record(booking, Status.CANCELLED, actor, reason, source)
    if rider_user is not None:
        _notify(rider_user, "Booking cancelled", "The customer cancelled this booking.", booking)
    return booking


def expire_stale_pending(*, older_than=timedelta(minutes=10)) -> int:
    """Cancel bookings no rider picked up. Run from Celery beat / cron."""
    cutoff = timezone.now() - older_than
    ids = list(
        Booking.objects.filter(status=Status.PENDING)
        .filter(Q(scheduled_for__isnull=True, created_at__lt=cutoff) | Q(scheduled_for__lt=cutoff))
        .values_list("pk", flat=True)
    )
    expired = 0
    for pk in ids:
        try:
            cancel_booking(booking_id=pk, actor=None, reason="No rider available", source="system")
            expired += 1
        except (InvalidTransition, DRFValidationError, Booking.DoesNotExist):
            continue
    return expired


# ---------------------------------------------------------------------------
# Settlement (rider wallet) and payment events
# ---------------------------------------------------------------------------
def settle_booking(booking):
    """Move rider money for a completed booking. Idempotent: safe to call repeatedly.
      cash   -> mark the payment paid, debit the company's share from the rider wallet
      digital-> once the payment is paid, credit the rider's earnings
    """
    if booking.status != Status.COMPLETED or not booking.rider_id:
        return
    with transaction.atomic():
        payment = _payment_for(booking, lock=True)
        profile = _rider_profile_of(booking)
        if payment is None or profile is None:
            return
        fare = (payment.metadata or {}).get("fare", {})
        wallet, _ = RiderWallet.objects.get_or_create(rider=profile)
        ref = booking.reference

        if payment.method == Payment.Method.CASH:
            if payment.status == Payment.Status.PENDING:
                payment.mark_as_paid(user=booking.rider)
            owed = Decimal(fare.get("platform_revenue", "0"))
            if owed > 0:
                wallet.post(owed, WalletTransaction.Type.COMMISSION, booking_ref=ref,
                            description=f"Platform share for {ref}", idempotency_key=f"commission:{ref}")
        elif payment.is_successful:
            earned = Decimal(fare.get("rider_earnings", "0"))
            if earned > 0:
                wallet.post(earned, WalletTransaction.Type.EARNING, booking_ref=ref,
                            description=f"Earnings for {ref}", idempotency_key=f"earning:{ref}")


def _settle_quietly(booking):
    try:
        settle_booking(booking)
    except Exception as exc:  # never block a completed trip; re-run settle_booking() to retry
        logger.exception("Settlement failed for %s", booking.reference)
        audit(None, "booking.settlement_failed", booking, error=exc)


@api_errors
@transaction.atomic
def apply_payment_event(*, event_type: str, booking: Booking, data: dict):
    payment = _payment_for(booking, lock=True)
    if payment is None:
        raise PaymentMismatch("This booking has no payment.")

    if event_type == "payment.succeeded":
        if payment.method in (Payment.Method.CASH, Payment.Method.WALLET):
            raise PaymentMismatch("This payment is not settled through a provider.")
        if booking.status == Status.CANCELLED:
            logger.error("Provider payment for cancelled booking %s needs a manual refund", booking.reference)
            raise PaymentMismatch("Booking is cancelled; the payment needs a manual refund.")
        if str(data.get("currency", "")).upper() != payment.currency:
            raise PaymentMismatch("Payment currency does not match the booking.")
        try:
            paid = Decimal(str(data["amount"]))
        except Exception:
            raise PaymentMismatch("Payment amount is missing or invalid.")
        if paid < payment.amount:
            logger.error("Underpayment on %s: paid=%s expected=%s", booking.reference, paid, payment.amount)
            raise PaymentMismatch("Paid amount is less than the booking price.")
        if paid > payment.amount:
            logger.warning("Overpayment on %s: paid=%s expected=%s", booking.reference, paid, payment.amount)
        payment.mark_as_paid(transaction_id=str(data.get("transaction_id", "")))
        _settle_quietly(booking)
        return "payment recorded"

    if event_type == "payment.failed":
        payment.mark_as_failed(reason=str(data.get("reason", ""))[:255])
        if booking.status in (Status.PENDING, Status.ACCEPTED):
            cancel_booking(booking_id=booking.pk, actor=None, reason="Payment failed", source="webhook")
        return "payment failed"

    if event_type == "payment.refunded":
        amount = data.get("amount")
        payment.refund(amount=None if amount is None else Decimal(str(amount)),
                       reason=str(data.get("reason", "Provider refund"))[:255])
        return "refund recorded"

    raise PaymentMismatch(f"Unsupported payment event: {event_type}")


# ---------------------------------------------------------------------------
# Revenue
# ---------------------------------------------------------------------------
def _blank_row():
    return {"trips": 0, "gross": ZERO, "platform_revenue": ZERO, "rider_earnings": ZERO, "discounts": ZERO}


def _add(row, payment_total, fare):
    row["trips"] += 1
    row["gross"] += payment_total
    row["platform_revenue"] += Decimal(fare.get("platform_revenue", "0"))
    row["rider_earnings"] += Decimal(fare.get("rider_earnings", "0"))
    row["discounts"] += Decimal(fare.get("discount", "0"))


def revenue_summary(*, start, end) -> dict:
    """Company revenue between two aware datetimes (end exclusive), read from the fare
    snapshots stored on each booking's Payment."""
    completed = Booking.objects.filter(status=Status.COMPLETED, completed_at__gte=start, completed_at__lt=end)
    meta = {b["reference"]: b for b in completed.values("reference", "service_type", "completed_at")}

    daily, totals = defaultdict(_blank_row), _blank_row()
    payments = (
        Payment.objects.filter(booking_ref__in=completed.values("reference"))
        .exclude(status__in=[Payment.Status.CANCELLED, Payment.Status.FAILED])
        .values("booking_ref", "amount", "metadata")
    )
    for p in payments.iterator(chunk_size=2000):
        fare = (p["metadata"] or {}).get("fare")
        info = meta.get(p["booking_ref"])
        if not fare or info is None:
            continue
        day = info["completed_at"].astimezone(pricing.LOCAL_TZ).date()
        _add(daily[(day, info["service_type"])], p["amount"], fare)
        _add(totals, p["amount"], fare)

    cancelled = Booking.objects.filter(status=Status.CANCELLED, cancelled_at__gte=start, cancelled_at__lt=end)
    fees = [
        Decimal(m["cancellation_fee"])
        for m in Payment.objects.filter(
            booking_ref__in=cancelled.values("reference"), metadata__has_key="cancellation_fee",
        ).values_list("metadata", flat=True)
    ]
    return {
        "daily": [{"day": d, "service_type": s, **row} for (d, s), row in sorted(daily.items())],
        "totals": totals,
        "cancellation_fees": {"count": len(fees), "collected": sum(fees, ZERO)},
    }

