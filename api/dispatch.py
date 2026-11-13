"""
api/dispatch.py -- RYDNOW automatic dispatch engine

Flow
    1. A booking is created with status "searching_rider".
    2. dispatch_booking() offers it to ONE qualified rider for `offer_timeout_s` seconds.
       Qualified = RiderProfile.objects.dispatchable(service) (verified, active, online, fresh GPS,
       not restricted, licence valid, approved for the service, not on a job) AND wallet >= minimum
       AND no other pending offer AND not already offered this booking.
    3. Riders inside the search radius are ranked by a weighted score
       (proximity, rating, acceptance, reliability, fairness). The radius widens 3 -> 5 -> 8 km.
    4. Accept -> booking assigned. Decline / timeout -> next rider on the next tick.
       Nobody found for `search_window_s`, or `max_offers_per_booking` reached -> "no_rider_found".
    5. DB constraints + row locks make double assignment impossible:
         * one live (pending/accepted) offer per booking
         * one pending offer per rider

Running it
    * `python manage.py run_dispatch` (Render "Background Worker") calls dispatch_tick() every few
      seconds. This is the only process you need.
    * Celery is OPTIONAL. If it is installed and CELERY_BROKER_URL is set, new bookings are also
      dispatched immediately through a Celery task instead of waiting for the next tick.

Booking field names this file relies on (edit the constants below if yours differ):
    status, created_at, pickup_lat, pickup_lng, and the rider FK `assigned_rider`
    (the name RiderProfileQuerySet.dispatchable() already uses).
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, fields
from datetime import timedelta
from decimal import Decimal
import uuid

from django.conf import settings
from django.db import IntegrityError, models, transaction
from django.db.models import Q
from django.db.models.signals import post_save
from django.utils import timezone

from .models import Booking, RiderProfile, Service, SystemConfig

log = logging.getLogger("rydnow.dispatch")

# ---------------------------------------------------------------------------
# Booking adapter -- the only place that knows your Booking field/status names
# ---------------------------------------------------------------------------
STATUS_SEARCHING = "searching_rider"
STATUS_ASSIGNED = "assigned"
STATUS_NO_RIDER = "no_rider_found"
RIDER_FIELD = "assigned_rider"
LIVE_TRIP_STATUSES = ("assigned", "arrived", "in_progress")


def service_key(booking) -> str:
    """Map whatever the booking stores to a Service primary key: okada | parcel | waste-bin."""
    raw = getattr(booking, "service_id", None) or getattr(booking, "service_type", None) or ""
    key = str(raw).lower()
    if "deliver" in key or "parcel" in key:
        return "parcel"
    if "waste" in key:
        return "waste-bin"
    return "okada"


def pickup_point(booking):
    try:
        lat, lng = float(booking.pickup_lat), float(booking.pickup_lng)
    except (AttributeError, TypeError, ValueError):
        return None
    if not (-90 <= lat <= 90 and -180 <= lng <= 180) or (lat == 0 and lng == 0):
        return None
    return lat, lng


# ---------------------------------------------------------------------------
# Config (override in settings.py: DISPATCH = {...})
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class DispatchConfig:
    offer_timeout_s: int = 45
    radius_steps_km: tuple = (3.0, 5.0, 8.0)
    candidates_per_round: int = 15
    max_offers_per_booking: int = 8
    min_wallet: Decimal = Decimal("50.00")
    location_max_age_s: int = 90
    search_window_s: int = 180  # keep retrying this long before giving up on a booking
    w_proximity: float = 0.50
    w_rating: float = 0.20
    w_acceptance: float = 0.15
    w_reliability: float = 0.10
    w_fairness: float = 0.05


def get_config() -> DispatchConfig:
    raw = getattr(settings, "DISPATCH", None) or {}
    known = {f.name for f in fields(DispatchConfig)}
    return DispatchConfig(**{k: v for k, v in raw.items() if k in known})


def _min_wallet(cfg: DispatchConfig) -> Decimal:
    # Same source of truth as views.min_wallet_balance()
    return SystemConfig.get_decimal("MIN_RIDER_BOOKING_BALANCE", str(cfg.min_wallet))


class OfferError(Exception):
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------
def _distance_km(lat1, lng1, lat2, lng2) -> float:
    """Great-circle distance in km (same formula as models.haversine_km)."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi, dlam = p2 - p1, math.radians(lng2 - lng1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlam / 2) ** 2
    return 2 * 6371.0 * math.asin(min(1.0, math.sqrt(a)))


def _clamp01(x) -> float:
    return min(max(float(x), 0.0), 1.0)


def _score(rider, dist_km: float, radius_km: float, now, cfg: DispatchConfig) -> float:
    proximity = _clamp01(1.0 - dist_km / radius_km)
    rating = _clamp01((rider.rating_average or 0) / 5.0)
    acceptance = _clamp01(rider.acceptance_rate / 100.0)  # property is a percentage
    reliability = 1.0 - _clamp01(rider.cancellation_rate / 100.0)  # property is a percentage
    if rider.last_job_at:  # fairness: riders idle longest get a small boost (caps at 1 hour)
        fairness = _clamp01((now - rider.last_job_at).total_seconds() / 3600.0)
    else:
        fairness = 1.0
    return round(
        cfg.w_proximity * proximity
        + cfg.w_rating * rating
        + cfg.w_acceptance * acceptance
        + cfg.w_reliability * reliability
        + cfg.w_fairness * fairness,
        4,
    )


def rank_candidates(booking, lat, lng, radius_km, exclude_ids, cfg, now):
    """[(score, distance_km, rider)] best first. Every hard rule is enforced here."""
    service = Service.objects.filter(pk=service_key(booking), is_active=True).first()
    if service is None:
        log.error("dispatch: no active Service for key=%s booking=%s", service_key(booking), booking.pk)
        return []

    dlat = radius_km / 111.0
    dlng = radius_km / (111.0 * max(math.cos(math.radians(lat)), 0.01))
    holding_offer = DispatchOffer.objects.filter(status=DispatchOffer.Status.PENDING).values("rider_id")

    qs = (
        RiderProfile.objects.dispatchable(
            service=service,
            max_location_age_seconds=cfg.location_max_age_s,
            exclude_ids=list(exclude_ids) or None,
        )
        .filter(
            wallet__balance__gte=_min_wallet(cfg),
            current_lat__range=(lat - dlat, lat + dlat),
            current_lng__range=(lng - dlng, lng + dlng),
        )
        .exclude(pk__in=holding_offer)[:200]
    )

    ranked = []
    for rider in qs:
        dist = _distance_km(lat, lng, rider.current_lat, rider.current_lng)
        if dist <= radius_km:
            ranked.append((_score(rider, dist, radius_km, now, cfg), dist, rider))
    ranked.sort(key=lambda t: t[0], reverse=True)
    return ranked[: cfg.candidates_per_round]


# ---------------------------------------------------------------------------
# Notification hooks (never raise)
# ---------------------------------------------------------------------------
def notify_rider(rider, payload: dict) -> None:
    try:
        log.info("notify rider=%s payload=%s", rider.pk, payload)
        # push.send(rider.user, payload)   # FCM / websocket goes here
    except Exception:  # noqa: BLE001
        log.exception("rider notification failed")


def notify_customer(booking, event: str) -> None:
    try:
        log.info("notify customer booking=%s event=%s", booking.pk, event)
    except Exception:  # noqa: BLE001
        log.exception("customer notification failed")


# ---------------------------------------------------------------------------
# Dispatch flow
# ---------------------------------------------------------------------------
def _mark_no_rider(booking):
    booking.status = STATUS_NO_RIDER
    booking.save(update_fields=["status"])
    transaction.on_commit(lambda: notify_customer(booking, "no_rider_found"))
    log.warning("dispatch: no rider found booking=%s", booking.pk)
    return None


def dispatch_booking(booking_id):
    """Offer the booking to the best available rider. Idempotent and safe to call concurrently."""
    cfg = get_config()
    with transaction.atomic():
        try:
            booking = Booking.objects.select_for_update().get(pk=booking_id)
        except Booking.DoesNotExist:
            return None
        if booking.status != STATUS_SEARCHING:
            return None
        if booking.dispatch_offers.filter(status=DispatchOffer.Status.PENDING).exists():
            return None  # a live offer is still waiting for its answer

        tried = list(booking.dispatch_offers.values_list("rider_id", flat=True))
        if len(tried) >= cfg.max_offers_per_booking:
            return _mark_no_rider(booking)

        point = pickup_point(booking)
        if point is None:
            log.error("dispatch: booking=%s has no usable pickup coordinates", booking.pk)
            return _mark_no_rider(booking)
        lat, lng = point
        now = timezone.now()

        for round_no, radius in enumerate(cfg.radius_steps_km, start=1):
            for score, dist, rider in rank_candidates(booking, lat, lng, radius, tried, cfg, now):
                try:
                    with transaction.atomic():  # savepoint: a constraint clash only skips this rider
                        offer = DispatchOffer.objects.create(
                            booking=booking,
                            rider=rider,
                            round_no=round_no,
                            distance_km=round(dist, 2),
                            score=score,
                            expires_at=now + timedelta(seconds=cfg.offer_timeout_s),
                        )
                except IntegrityError:
                    tried.append(rider.pk)
                    continue
                payload = {
                    "type": "job_offer",
                    "offer_id": str(offer.id),
                    "booking_id": str(booking.pk),
                    "service": service_key(booking),
                    "pickup_distance_km": float(offer.distance_km),
                    "expires_at": offer.expires_at.isoformat(),
                }
                transaction.on_commit(lambda r=rider, p=payload: notify_rider(r, p))
                log.info("dispatch: offered booking=%s to rider=%s round=%s", booking.pk, rider.pk, round_no)
                return offer

        # Nobody qualified right now. Keep the booking searching until the window closes:
        # riders may come online or top up their wallet while the customer waits.
        created = getattr(booking, "created_at", None)
        if created and (now - created).total_seconds() < cfg.search_window_s:
            return None
        return _mark_no_rider(booking)


def accept_offer(offer_id, rider) -> DispatchOffer:
    """Rider accepts. Raises OfferError with an HTTP-style status_code."""
    cfg = get_config()
    now = timezone.now()
    with transaction.atomic():
        offer = DispatchOffer.objects.select_for_update().filter(pk=offer_id, rider=rider).first()
        if offer is None:
            raise OfferError("Offer not found.", 404)
        if offer.status != DispatchOffer.Status.PENDING:
            raise OfferError("This offer is no longer available.", 409)
        if offer.is_expired:
            offer.status, offer.responded_at = DispatchOffer.Status.EXPIRED, now
            offer.save(update_fields=["status", "responded_at"])
            raise OfferError("This offer has expired.", 409)

        if not _rider_can_work(rider, now):
            raise OfferError("Your account cannot accept jobs right now.", 403)
        wallet = getattr(rider, "wallet", None)
        if wallet is None or wallet.balance < _min_wallet(cfg):
            raise OfferError("Add money to your wallet (deposit) to accept jobs.", 402)

        booking = Booking.objects.select_for_update().get(pk=offer.booking_id)
        if booking.status != STATUS_SEARCHING:
            raise OfferError("Job already assigned or cancelled.", 409)
        if (
            Booking.objects.filter(status__in=Booking.ACTIVE_STATUSES, **{RIDER_FIELD: rider})
            .exclude(pk=booking.pk)
            .exists()
        ):
            raise OfferError("Finish your current trip first.", 409)

        offer.status, offer.responded_at = DispatchOffer.Status.ACCEPTED, now
        offer.save(update_fields=["status", "responded_at"])
        setattr(booking, RIDER_FIELD, rider)
        booking.status = STATUS_ASSIGNED
        booking.save(update_fields=[RIDER_FIELD, "status"])
        rider.record_offer("accepted", (now - offer.offered_at).total_seconds())
        transaction.on_commit(lambda: notify_customer(booking, "rider_assigned"))
    return offer


def decline_offer(offer_id, rider, reason: str = "") -> None:
    now = timezone.now()
    with transaction.atomic():
        offer = DispatchOffer.objects.select_for_update().filter(pk=offer_id, rider=rider).first()
        if offer is None or offer.status != DispatchOffer.Status.PENDING:
            return
        offer.status = DispatchOffer.Status.DECLINED
        offer.decline_reason, offer.responded_at = (reason or "")[:60], now
        offer.save(update_fields=["status", "decline_reason", "responded_at"])
        rider.record_offer("declined", (now - offer.offered_at).total_seconds())
        kick_dispatch(offer.booking_id)


def expire_offer(offer_id) -> None:
    with transaction.atomic():
        offer = DispatchOffer.objects.select_for_update().select_related("rider").filter(pk=offer_id).first()
        if offer is None or offer.status != DispatchOffer.Status.PENDING or not offer.is_expired:
            return
        offer.status, offer.responded_at = DispatchOffer.Status.EXPIRED, timezone.now()
        offer.save(update_fields=["status", "responded_at"])
        offer.rider.record_offer("expired")
        kick_dispatch(offer.booking_id)


def cancel_booking_offers(booking) -> None:
    """Customer cancelled (or the booking moved on): free any rider still holding an offer."""
    DispatchOffer.objects.filter(booking=booking, status=DispatchOffer.Status.PENDING).update(
        status=DispatchOffer.Status.CANCELLED, responded_at=timezone.now()
    )


def _rider_can_work(rider, now) -> bool:
    # Written out here because RiderProfile.can_accept_jobs depends on properties that are being fixed.
    if not (rider.is_verified and rider.is_active and rider.deleted_at is None):
        return False
    if rider.license_expiry_date and rider.license_expiry_date < timezone.localdate():
        return False
    if rider.is_restricted and (rider.restricted_until is None or rider.restricted_until > now):
        return False
    return True


# ---------------------------------------------------------------------------
# Scheduler: one tick = expire stale offers + offer every waiting booking
# ---------------------------------------------------------------------------
def expire_stale_offers(limit: int = 500) -> int:
    ids = list(
        DispatchOffer.objects.filter(status=DispatchOffer.Status.PENDING, expires_at__lt=timezone.now())
        .values_list("pk", flat=True)[:limit]
    )
    for oid in ids:
        try:
            expire_offer(oid)
        except Exception:  # noqa: BLE001
            log.exception("dispatch: expire failed offer=%s", oid)
    return len(ids)


def dispatch_tick(limit: int = 200) -> dict:
    expired = expire_stale_offers()
    booking_ids = list(
        Booking.objects.filter(status=STATUS_SEARCHING)
        .exclude(dispatch_offers__status=DispatchOffer.Status.PENDING)
        .values_list("pk", flat=True)[:limit]
    )
    offered = 0
    for bid in booking_ids:
        try:
            if dispatch_booking(bid) is not None:
                offered += 1
        except Exception:  # noqa: BLE001
            log.exception("dispatch: failed booking=%s", bid)
    return {"expired": expired, "waiting": len(booking_ids), "offered": offered}


# ---------------------------------------------------------------------------
# Optional Celery (immediate dispatch). Without it the worker tick does the job.
# ---------------------------------------------------------------------------
try:
    from celery import shared_task
except ImportError:  # pragma: no cover
    shared_task = None

if shared_task is not None:

    @shared_task
    def dispatch_booking_task(booking_id):
        dispatch_booking(booking_id)

    @shared_task
    def dispatch_tick_task():
        return dispatch_tick()


def _celery_enabled() -> bool:
    return shared_task is not None and bool(getattr(settings, "CELERY_BROKER_URL", ""))


def kick_dispatch(booking_id) -> None:
    """Ask for an immediate dispatch attempt. No-op without Celery (the worker tick covers it)."""
    if _celery_enabled():
        transaction.on_commit(lambda: dispatch_booking_task.delay(str(booking_id)))


def _on_booking_saved(sender, instance, created, **kwargs):
    if instance.status == STATUS_SEARCHING:
        if created:
            kick_dispatch(instance.pk)
        return
    if not created:
        cancel_booking_offers(instance)


def connect_signals() -> None:
    """Called from ApiConfig.ready()."""
    post_save.connect(_on_booking_saved, sender=Booking, dispatch_uid="rydnow_dispatch_booking_saved")

