"""
api/dispatch.py -- RYDNOW automatic dispatch engine
=====================================================
Services: okada_ride | delivery | waste_collection
How it works (Automated "waterfall" dispatch)
----------------------------------------------
1. Booking is created with status SEARCHING -> `dispatch_booking_task` fires.
2. Engine finds QUALIFIED riders only (verified, active, online, approved for
THIS service, licence valid, not restricted, wallet >= GHS 50, fresh GPS,
not already on a job / holding another offer).
3. Candidates are ranked by a weighted score (proximity, rating, acceptance,
reliability, fairness) - not just distance.
4. ONE rider is offered the job for OFFER_TIMEOUT seconds.
Accept -> assigned. Decline / timeout -> next best rider. Search radius
widens automatically (3 -> 5 -> 8 km). Nobody left -> NO_RIDER_FOUND.
5. Database constraints + row locks make double-assignment impossible,
even with thousands of concurrent bookings.
ASSUMPTIONS (adjust names to match your models)
-----------------------------------------------
RiderProfile : user, is_verified, is_active, is_available, deleted_at,
licence_expired, is_restricted_now, current_lat, current_lng,
last_location_at, performance_score (0-5), acceptance_rate (0-1),
cancellation_rate (0-1), last_trip_completed_at
Wallet : user.rider_wallet.balance / .is_active
ServiceQual. : rider FK(related_name="qualifications"), service FK, status
Service : has a unique `key` ("okada_ride", "delivery", "waste_collection")
Booking : service FK, rider FK(null), pickup_lat, pickup_lng, status,
with Booking.Status.SEARCHING / ASSIGNED / NO_RIDER_FOUND /
ACCEPTED / IN_PROGRESS (add the missing ones)
"""
from __future__ import annotations
import logging
import math
import uuid
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from celery import shared_task
from django.conf import settings
from django.db import IntegrityError, models, transactionfrom django.db.models import Exists, F, FloatField, OuterRef, Q, Value
from django.db.models.functions import ACos, Cos, Greatest, Least, Radians, Sin
from django.utils import timezone
from rest_framework import permissions, serializers, status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response
from .models import Booking, RiderProfile, ServiceQualification
log = logging.getLogger("rydnow.dispatch")
EARTH_RADIUS_KM = 6371.0
# --------------------------------------------------------------------------
# 1. CONFIG (override any value in settings.py via DISPATCH = {...})
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class DispatchConfig:
    offer_timeout_s: int = 20
    radius_steps_km: tuple = (3.0, 5.0, 8.0)
    candidates_per_round: int = 15
    max_offers_per_booking: int = 8
    min_wallet: Decimal = Decimal("50.00")
    location_max_age_s: int = 90
# scoring weights (sum = 1.0)
    w_proximity: float = 0.50
    w_rating: float = 0.20
    w_acceptance: float = 0.15
    w_reliability: float = 0.10
    w_fairness: float = 0.05 # riders idle longest get a small boost
    def get_config() -> DispatchConfig:
return DispatchConfig(**getattr(settings, "DISPATCH", {}))
# --------------------------------------------------------------------------
# 2. MODEL - every offer is an audit record (great for disputes & analytics)
# --------------------------------------------------------------------------
class DispatchOffer(models.Model):
class Status(models.TextChoices):
PENDING = "pending", "Pending"
ACCEPTED = "accepted", "Accepted"
DECLINED = "declined", "Declined"
EXPIRED = "expired", "Expired"
CANCELLED = "cancelled", "Cancelled"id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
booking = models.ForeignKey(Booking, on_delete=models.CASCADE, related_name="dispatch_off
rider = models.ForeignKey(RiderProfile, on_delete=models.CASCADE, related_name="dispatch_
status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING,
round_no = models.PositiveSmallIntegerField(default=1)
distance_km = models.DecimalField(max_digits=6, decimal_places=2)
score = models.FloatField(default=0)
decline_reason = models.CharField(max_length=60, blank=True)
offered_at = models.DateTimeField(auto_now_add=True)
expires_at = models.DateTimeField()
responded_at = models.DateTimeField(null=True, blank=True)
class Meta:
    db_table = "dispatch_offers"
    ordering = ["-offered_at"]
    indexes = [models.Index(fields=["status", "expires_at"])]
    constraints = [
# A booking can have only ONE live offer / accepted rider at a time
models.UniqueConstraint(
    fields=["booking"],
    condition=Q(status__in=["pending", "accepted"]),
    name="uniq_live_offer_per_booking",
),
# A rider can hold only ONE pending offer at a time
models.UniqueConstraint(
    fields=["rider"],
    condition=Q(status="pending"),
    name="uniq_pending_offer_per_rider",
),
]
    def __str__(self):
        return f"Offer {self.id} -> {self.rider_id} [{self.status}]"
    @property
    def is_expired(self) -> bool:
        return timezone.now() >= self.expires_at
# --------------------------------------------------------------------------
# 3. MATCHING - qualified riders only, ranked by score
# --------------------------------------------------------------------------
def _haversine_km(lat: float, lng: float):
    """DB-side great-circle distance (accurate, unlike the degrees*111 shortcut)."""
    lat_r = maths.radians(lat) 
    lng_r = math.radians(lng),
    
    cos_angle = (
        Cos(Value(lat_r)) * Cos(Radians(F("current_lat")))
        * Cos(Radians(F("current_lng")) - Value(lng_r))
        + Sin(Value(lat_r)) * Sin(Radians(F("current_lat")))
    )
# clamp to [-1, 1] to avoid acos domain errors from float rounding
    clamped = Greatest(Value(-1.0), Least(Value(1.0), cos_angle))
    return ACos(clamped, output_field=FloatField()) * EARTH_RADIUS_KM

def qualified_riders(service_key: str, lat: float, lng: float, radius_km: float, exclude_ids=
"""Base queryset: every hard rule a rider must pass to see this job."""
cfg = get_config()
now = timezone.now()

# Approved for this service
riders_qual = (
    ServiceQualification.objects.filter(
        service=service_key,
        status=ServiceQualification.Status.APPROVED,
    )
    .select_related("rider")
)

# BUSY ON ANOTHER JOB 
busy_on_job = Booking.objects.filter(
    rider=OuterRef("pk"),
    status__in=[
        Booking.Status.ASSIGNED,
        Booking.Status.ACCEPTED,
        Booking.Status.IN_PROGRESS,
    ]    
) .values("rider")

# cheap bounding box first (index friendly), exact distance second
dlat = cfg.DEGREE_PER_KM 
dlng = dlat / math.cos(math.radians(lat))
return (
    RiderProfile.objects.actice()
    .exclude(id__in=exclude_ids)
    .exclude(id__in+busy_on_job)
    .filter(
        servicequalification__service=service_key,
        servicequalification__status=ServiceQualificatiom.Status.APPROVED,
        is_active= True,
        wallet__balance__gte=cfg.MIN_WALLET_BALANCE,
        current_lat__range=(lat - dlat * radius_km, lat + dlat * radius_km),
        current_lng__range=(lng - dlng * radius_km, lng + dlng * radius_km),
    )
    # EXCLUDE RIDERS WHO ARE BUSY ON JOB
    .exclude(id__in=exclude_ids)
    .filter(
        Exists(
            DispatchOdder.objects.filter(
                rider=OuterRef("pk"),
                status= DispatchOffer.Status.PENDING,
            
            )
        )
    )
    .filter(~Exists(busy_on_job))
    # DISTANCE + FINAL SORT
    .annotate(distance_km= haversine_km(lat, lng))
    .filter(distance_km__lte=radius_km)
    .order_by ("distance_km", "-rating") 
)

def _score(rider: RiderProfile, radius_km: float, now) -> float:
    cfg = get_config()
    # HOW CLOSE: 1.0 = at pickup, 0.0 = at max radius
    proximity = max(0.0, 1.0 - float(rider.distance_km) / radius_km)
    # Perfomace Rating : normalize 0-1
    rating = min(max(float(rider.performance_score or 0) / 5.0, 0.0), 1.0)
    # Acceptance Rate
    acceptance = float(rider.acceptance_rate if rider.acceptance_rate is not None else 1.0)
    # Reliability / cancellation rate
    reliability = 1.0 - float(rider.cancellation_rate or 0)
    # Fairness bonus: riders idle longest a small boost
    idle_min = 60.0
    if rider.last_trip_completed_at:
        idle_min = (now - rider.last_trip_completed_at).total_seconds() / 60
        idle_min= min(max(idle_min/ 60.0, 1.0), 2.0)

    return round(
        cfg.w_proximity * proximity
        + cfg.w_rating * rating
        + cfg.w_acceptance * acceptance
        + cfg.w_reliability * reliability
        + cfg.w_fairness * fairness,
        4,
)
def rank_candidates(booking: Booking, radius_km: float, exclude_ids):
    cfg = get_config()
    now = timezone.now()
    
    qs = (
        get_qualified_riders(
            booking.service.key,
            booking.pickup_lat,
            booking.pickup_lng,
            exclude_ids=exclude_ids,
        )
    .select_for_update(skip_locked=True, of=("self",)) # other dispatch workers skip lo.order_by("distance_km")[: cfg.candidates_per_round]
    )
    scored = [(_score(r, radius_km, now), r) for r in qs]
    scored.sort(key=lambda t: t[0], reverse=True)
    return scored
# --------------------------------------------------------------------------
# 4. DISPATCH FLOW
# --------------------------------------------------------------------------
def notify_rider(rider: RiderProfile, payload: dict) -> None:
    """Hook: send FCM push / websocket event. Never raise from here."""
    try:
        log.info("notify rider=%s payload=%s", rider.pk, payload)
        # fcm.send(rider.user.device_token, payload)
        except Exception: # noqa: BLE001
        log.exception("rider notification failed")def notify_customer(booking: Booking, event: str) -> None:
        log.info("notify customer booking=%s event=%s", booking.pk, event)
@transaction.atomic
def dispatch_booking(booking_id) -> DispatchOffer | None:
    """Offer the booking to the single best available rider. Idempotent & race-safe."""
    cfg = get_config()booking = (Booking.objects.select_for_update().select_related("service").get(pk=booking_id)
    )
if booking.status != Booking.Status.SEARCHING:
    return None
# a live offer already exists -> wait for it to resolve
if booking.dispatch_offers.filter(status=DispatchOffer.Status.PENDING).exists():
    return None
tried = list(booking.dispatch_offers.values_list("rider_id", flat=True))
if len(tried) >= cfg.max_offers_per_booking:
    return _mark_no_rider(booking)
for round_no, radius in enumerate(cfg.radius_steps_km, start=1):
    for score, rider in rank_candidates(booking, radius, tried):
        try:
            with transaction.atomic():
                offer= DispatchOffer.objects.create(
                    booking=booking,
                    rider=rider,
                    round_no=round_no,
                    distance_km=round(rider.distance_km, 2),
                    score=score,
                    expires_at=timezone.now() + timedelta(seconds=cfg.offer_timeout_s),
                ) # savepoint: a constraint clash only skips offer = DispatchOffer.objects.create(
            
        except IntegrityError:
            tried.append(rider.pk)
            continue
            payload = {
                "type": "job_offer",
                "offer_id": str(offer.id),
                "service": booking.service.key,
                "pickup_distance_km": float(offer.distance_km),
                "expires_at": offer.expires_at.isoformat(),
            }
            transaction.on_commit(lambda r=rider, p=payload: notify_rider(r, p))
            transaction.on_commit(lambda oid=offer.id: expire_offer_task.apply_async(args=[str(oid)], countdown=cfg.offer_timeout_s + 1
            )
        )
        return offer
    return _mark_no_rider(booking)
def _mark_no_rider(booking: Booking):
    booking.status = Booking.Status.NO_RIDER_FOUND
    booking.save(update_fields=["status"])
    transaction.on_commit(lambda: notify_customer(booking, "no_rider_found"))
    log.warning("No rider found for booking=%s", booking.pk)
    return None
@transaction.atomic
def accept_offer(offer_id, rider: RiderProfile) -> DispatchOffer:
    offer = DispatchOffer.objects.select_for_update().get(pk=offer_id, rider=rider)
    if offer.status != DispatchOffer.Status.PENDING:
        raise serializers.ValidationError("This offer is no longer available.")
    if offer.is_expired:
        offer.status = DispatchOffer.Status.EXPIRED
        offer.save(update_fields=["status"])
        raise serializers.ValidationError("This offer has expired.")
    booking = Booking.objects.select_for_update().get(pk=offer.booking_id)
    if booking.status != Booking.Status.SEARCHING:
        raise serializers.ValidationError("Job already assigned or cancelled.")

    now = timezone.now()
    offer.status, 
    offer.responded_at = DispatchOffer.Status.ACCEPTED,
    offer.responded_at = now
    offer.save(update_fields=["status", "responded_at"])

    booking.rider = rider
    booking.status = rider, Booking.Status.ASSIGNED
    booking.save(update_fields=["rider", "status"])
    transaction.on_commit(lambda: notify_customer(booking, "rider_assigned"))
    return offer
@transaction.atomic
def decline_offer(offer_id, rider: RiderProfile, reason: str = "") -> None:
    offer = DispatchOffer.objects.select_for_update().get(pk=offer_id, rider=rider)
    
    if offer.status != DispatchOffer.Status.PENDING:
        return
    offer.status = DispatchOffer.Status.DECLINED
    offer.decline_reason = reason[:60]offer.responded_at = timezone.now()
    offer.save(update_fields=["status", "decline_reason", "responded_at"])
    
    transaction.on_commit(lambda: dispatch_booking_task.delay(str(offer.booking_id)))
@transaction.atomic
def expire_offer(offer_id) -> None:
    offer = DispatchOffer.objects.select_for_update().filter(pk=offer_id).first()
    if not offer or offer.status != DispatchOffer.Status.PENDING:
        return
    offer.status = DispatchOffer.Status.EXPIRED
    offer.responded_at = timezone.now()
    offer.save(update_fields=["status", "responded_at"])

    transaction.on_commit(lambda: dispatch_booking_task.delay(str(offer.booking_id)))

def cancel_booking_offers(booking: Booking) -> None:
    """Call when a customer cancels: frees any rider holding a pending offer."""
    booking.dispatch_offers.filter(status=DispatchOffer.Status.PENDING).update(
        status=DispatchOffer.Status.CANCELLED, responded_at=timezone.now()
    )
# --------------------------------------------------------------------------
# 5. CELERY TASKS
# --------------------------------------------------------------------------
@shared_task(bind=True, max_retries=3, default_retry_delay=2)
def dispatch_booking_task(self, booking_id: str):
    try:
        dispatch_booking(booking_id)
    except Exception as exc: # noqa: BLE001
        log.exception("dispatch failed booking=%s", booking_id)
        raise self.retry(exc=exc)
@shared_task
def expire_offer_task(offer_id: str):
    expire_offer(offer_id)

@shared_task
def sweep_stale_offers():
    """Safety net (run every 30s via celery beat) in case a countdown task is lost."""
    ids = DispatchOffer.objects.filter(
        status=DispatchOffer.Status.PENDING,
        expires_at__lt=timezone.now()
    ).values_list("id", flat=True)[:500]
    for oid in ids:
        expire_offer(oid)

# Auto-trigger: dispatch the moment a booking is created
def trigger_dispatch_on_create(sender, instance: Booking, created, **kwargs):
    if created and instance.status == Booking.Status.SEARCHING:
        transaction.on_commit(lambda: dispatch_booking_task.delay(str(instance.pk)))


# connect in apps.py ready(): post_save.connect(trigger_dispatch_on_create, sender=Booking)
# --------------------------------------------------------------------------
# 6. API (rider app)
# --------------------------------------------------------------------------
class DispatchOfferSerializer(serializers.ModelSerializer):
    service = serializers.CharField(source="booking.service.key", read_only=True)
    pickup_lat = serializers.FloatField(source="booking.pickup_lat", read_only=True)
    pickup_lng = serializers.FloatField(source="booking.pickup_lng", read_only=True)
    seconds_left = serializers.SerializerMethodField()
    
    class Meta:
        model = DispatchOfferfields = ["id", "service", "pickup_lat", "pickup_lng", "distance_km",
        "expires_at", "seconds_left", "status"]
    
    def get_seconds_left(self, obj):
        return max(0, int((obj.expires_at - timezone.now()).total_seconds()))
class DeclineSerializer(serializers.Serializer):
    reason = serializers.CharField(required=False, allow_blank=True, max_length=60)
class LocationSerializer(serializers.Serializer):
    lat = serializers.FloatField(min_value=-90, max_value=90)
    lng = serializers.FloatField(min_value=-180, max_value=180)
class RiderOfferViewSet(viewsets.GenericViewSet):
    permission_classes = [permissions.IsAuthenticated]
    serializer_class = DispatchOfferSerializer
    def get_queryset(self):
        return DispatchOffer.objects.filter(
            rider__user=self.request.user
            ).select_related("booking")
@action(detail=False, methods=["get"])
def pending(self, request):
    qs = self.get_queryset().filter(status="pending", expires_at__gt=timezone.now())return Response(self.get_serializer(qs, many=True).data)
@action(detail=True, methods=["post"])
def accept(self, request, pk=None):
    offer = accept_offer(pk, request.user.rider_profile)
    return Response(self.get_serializer(offer).data)
@action(detail=True, methods=["post"])
def decline(self, request, pk=None):
    data = DeclineSerializer(data=request.data)
    data.is_valid(raise_exception=True)
    decline_offer(pk, request.user.rider_profile, data.validated_data.get("reason", ""))
    return Response(status=status.HTTP_204_NO_CONTENT)
@action(detail=False, methods=["post"])
def location(self, request):
    """Rider app pings this every ~5-10s while online."""
    data = LocationSerializer(data=request.data)
    data.is_valid(raise_exception=True)
    RiderProfile.objects.filter(user=request.user).update(
        current_lat=data.validated_data["lat"],
        current_lng=data.validated_data["lng"],
        last_location_at=timezone.now(),
    )
    return Response(status=status.HTTP_204_NO_CONTENT)
# router.register("rider/offers", RiderOfferViewSet, basename="rider-offers")

