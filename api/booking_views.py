"""
Booking + pricing API (api/booking_views.py). Thin views: validation in booking.py,
business rules in booking_services.py, fares in pricing.py.

Endpoints (see booking_urls.py):
  POST   /booking/calculate-price/     price a trip (anonymous allowed; logged-in users get quote_token)
  POST   /bookings/quote/              legacy alias of the above (deprecated)
  POST   /booking/create/              create a booking from a quote_token
  GET    /booking/                     my bookings (paginated, ?status=pending,accepted)
  GET    /booking/<ref>/               booking details
  PATCH  /booking/<ref>/status/        accepted | in_progress | completed | cancelled
  POST   /booking/<ref>/cancel/        cancel (a rider "cancelling" releases the job)
  POST   /booking/<ref>/release/       rider gives an accepted job back to the pool
  GET    /dispatch/rider/nearby/       available riders near a point
  POST   /booking/webhook/             payment-provider webhook (HMAC signed)
  GET    /booking/admin/revenue/       company revenue report (staff)
"""
import hashlib
import hmac
import logging
from datetime import datetime, time, timedelta

from django.conf import settings
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from django.db.models import Q
from django.urls import reverse
from django.utils.dateparse import parse_date
from rest_framework import mixins, permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import APIException
from rest_framework.pagination import PageNumberPagination
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView
from rest_framework.throttling import AnonRateThrottle
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.decorators import api_view, permission_classes, throttle_classes
from rest_framework import status
from decimal import Decimal, InvalidOperation
from django.utils import timezone
from rest_framework import serializers, status


from . import booking_services as services
from . import pricing
from .booking import (
    BookingCancelSerializer, BookingCreateSerializer, BookingDetailSerializer,
    BookingListSerializer, BookingStatusUpdateSerializer, BookingWebhookSerializer,
    NearbyRiderSerializer, NearbyRidersQuerySerializer, QuoteRequestSerializer,
    RiderBookingDetailSerializer, StaffBookingDetailSerializer,
)
from .models import Booking, WebhookEvent, DispatchOffer, RiderProfile
from .pricing import calculate_fare

logger = logging.getLogger(__name__)


class StandardPagination(PageNumberPagination):
    page_size = 20
    page_size_query_param = "page_size"
    max_page_size = 100


# ---------------------------------------------------------------------------
# Pricing quote
# ---------------------------------------------------------------------------
class QuoteView(APIView):
    """POST /booking/calculate-price/ - price a trip. Logged-in users also get a signed
    `quote_token`, which is the only thing booking creation accepts."""

    permission_classes = [permissions.AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "quote"

    def post(self, request):
        serializer = QuoteRequestSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(
                {"error": "Invalid request", "details": serializer.errors},
                status=status.HTTP_400_BAD_REQUEST,
            )
        d = serializer.validated_data
        service_type = d["service_type"]          # pricing key (okada | parcel | waste-bin)
        rate = pricing.RATES[service_type]
        stops = d["stops"]

        route = pricing.build_route(
            (d["pickup_lat"], d["pickup_lng"]), stops, (d["destination_lat"], d["destination_lng"])
        )
        user = request.user if request.user and request.user.is_authenticated else None
        try:
            q = services.quote_trip(
                user=user, service_type=service_type, route=route,
                wait_minutes=[s["wait_minutes"] for s in stops],
                promo_code=d.get("promo_code", ""),
            )
        except services.TripTooLong as exc:
            return Response({"error": str(exc.detail)}, status=status.HTTP_400_BAD_REQUEST)

        fare, surge, waiting = q["fare"], q["surge"], q["fare"]["waiting"]
        logger.info(
            "price_quote service=%s distance_km=%s stops=%d surge=%s reason=%s total=%s user=%s",
            service_type, q["distance"], len(stops), surge.multiplier, surge.reason,
            fare["total"], getattr(user, "pk", None),
        )
        payload = {
            "service_type": service_type,
            "booking_service_type": pricing.booking_service_value(service_type),
            "currency": pricing.CURRENCY,
            "distance_km": float(q["distance"]),
            "price_per_km": float(rate.per_km),
            "breakdown": {
                "base_fare": float(fare["base_fare"]),
                "distance_fare": float(fare["distance_fare"]),
                "subtotal": float(fare["subtotal"]),
                "surge_amount": float(fare["surge_amount"]),
                "minimum_fare": float(fare["minimum_fare"]),
                "minimum_applied": fare["minimum_applied"],
                "ride_fare": float(fare["ride_fare"]),
                "stop_fees": float(fare["stop_fees"]),
                "waiting_charge": float(waiting["charge"]),
                "booking_fee": float(fare["booking_fee"]),
                "discount": float(fare["discount"]),
                "promo_code": fare["promo_code"],
            },
            "stops": {
                "count": len(stops),
                "max_allowed": rate.max_stops,
                "fee_per_stop": float(rate.stop_fee),
            },
            "waiting": {
                "rate_per_minute": float(rate.wait_per_min),
                "free_minutes_per_stop": rate.free_wait_minutes,
                "max_billable_minutes_per_stop": pricing.max_billable_wait_minutes(service_type),
                "billable_minutes": waiting["billable_minutes"],
                "charge": float(waiting["charge"]),
                "per_stop": [
                    {
                        "stop": s["stop"],
                        "wait_minutes": float(s["wait_minutes"]),
                        "billable_minutes": s["billable_minutes"],
                        "charge": float(s["charge"]),
                    }
                    for s in waiting["stops"]
                ],
            },
            "surge": {
                "active": surge.active,
                "multiplier": float(surge.multiplier),
                "reason": surge.reason,
            },
            "total_price": float(fare["total"]),
            "quote_token": q["token"],  # null for anonymous estimates
            "quoted_at": q["now"].isoformat(),
            "valid_until": q["valid_until"].isoformat(),
            "expires_in_seconds": int(pricing.QUOTE_VALIDITY.total_seconds()),
        }
        return self.respond(payload)

    def respond(self, payload):
        response = Response(payload)
        response["Cache-Control"] = "no-store"   # quotes are personalised and signed
        return response


class LegacyQuoteView(QuoteView):
    """POST /bookings/quote/ - old endpoint kept for existing app versions. Same logic as
    QuoteView, so prices cannot drift; adds `surge_multiplier` plus Deprecation/Link headers
    (and Sunset when settings.LEGACY_QUOTE_SUNSET is an HTTP-date)."""

    def respond(self, payload):
        payload["surge_multiplier"] = payload["surge"]["multiplier"]
        response = super().respond(payload)
        response["Deprecation"] = "true"
        response["Link"] = f'<{reverse("calculate-price")}>; rel="successor-version"'
        sunset = getattr(settings, "LEGACY_QUOTE_SUNSET", "")
        if sunset:
            response["Sunset"] = sunset
        logger.info(
            "legacy_quote_called user=%s agent=%s",
            getattr(self.request.user, "pk", None),
            self.request.headers.get("User-Agent", "")[:120],
        )
        return response


# ---------------------------------------------------------------------------
# Bookings
# ---------------------------------------------------------------------------
class BookingViewSet(
    mixins.CreateModelMixin,
    mixins.RetrieveModelMixin,
    mixins.ListModelMixin,
    viewsets.GenericViewSet,
):
    permission_classes = [permissions.IsAuthenticated]
    pagination_class = StandardPagination
    throttle_classes = [ScopedRateThrottle]
    lookup_field = "reference"
    lookup_url_kwarg = "booking_ref"

    @property
    def throttle_scope(self):
        return "booking_create" if getattr(self, "action", None) == "create" else "booking"

    def get_queryset(self):
        user = self.request.user
        qs = (
            Booking.objects
            .select_related("customer__customer_profile", "rider__rider_profile")
            .prefetch_related("status_history", "stops")
        )
        if user.is_staff:
            pass
        elif (profile := getattr(user, "rider_profile", None)) is not None:
            # Riders see their own jobs, their own bookings as a customer, and open jobs
            # for services they are approved for. Booking.rider is a USER, not a RiderProfile.
            approved = profile.qualifications.filter(status="approved").values_list("service_id", flat=True)
            open_values = [pricing.booking_service_value(k) for k in approved if k in pricing.RATES]
            qs = qs.filter(
                Q(rider=user) | Q(customer=user)
                | Q(status=Booking.Status.PENDING, service_type__in=open_values)
            )
        else:
            qs = qs.filter(customer=user)

        status_param = self.request.query_params.get("status")
        if status_param:
            qs = qs.filter(status__in=[s.strip() for s in status_param.split(",") if s.strip()])
        return qs

    def _detail_serializer_class(self):
        user = self.request.user
        if user.is_staff:
            return StaffBookingDetailSerializer
        if hasattr(user, "rider_profile"):
            return RiderBookingDetailSerializer
        return BookingDetailSerializer

    def get_serializer_class(self):
        if self.action == "list":
            return BookingListSerializer
        if self.action == "create":
            return BookingCreateSerializer
        if self.action == "update_status":
            return BookingStatusUpdateSerializer
        if self.action in ("cancel", "release"):
            return BookingCancelSerializer
        return self._detail_serializer_class()

    def _detail_response(self, booking_id, http_status=status.HTTP_200_OK):
        booking = self.get_queryset().model.objects.select_related(
            "customer__customer_profile", "rider__rider_profile"
        ).prefetch_related("status_history", "stops").get(pk=booking_id)
        serializer = self._detail_serializer_class()(booking, context=self.get_serializer_context())
        return Response(serializer.data, status=http_status)

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        booking, created = services.create_booking(customer=request.user, data=serializer.validated_data)
        return self._detail_response(
            booking.pk, status.HTTP_201_CREATED if created else status.HTTP_200_OK
        )

    @action(detail=True, methods=["patch"], url_path="status")
    def update_status(self, request, booking_ref=None):
        booking = self.get_object()  # 404 if the user cannot see it
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        services.transition_booking(
            booking_id=booking.pk,
            new_status=serializer.validated_data["status"],
            actor=request.user,
            note=serializer.validated_data.get("note", ""),
        )
        return self._detail_response(booking.pk)

    @action(detail=True, methods=["post"])
    def cancel(self, request, booking_ref=None):
        booking = self.get_object()
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        services.cancel_booking(
            booking_id=booking.pk, actor=request.user,
            reason=serializer.validated_data.get("reason", ""),
        )
        return self._detail_response(booking.pk)

    @action(detail=True, methods=["post"])
    def release(self, request, booking_ref=None):
        booking = self.get_object()
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        services.release_rider(
            booking_id=booking.pk, actor=request.user,
            reason=serializer.validated_data.get("reason", ""),
        )
        return self._detail_response(booking.pk)


class NearbyRidersView(APIView):
    """GET /dispatch/rider/nearby/?lat=..&lng=..&radius_km=5&service_type=okada&limit=20"""

    permission_classes = [permissions.IsAuthenticated]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "riders_nearby"

    def get(self, request):
        query = NearbyRidersQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        params = query.validated_data
        riders = services.list_nearby_riders(
            lat=params["lat"], lng=params["lng"], radius_km=params["radius_km"],
            service_type=params.get("service_type"), limit=params["limit"],
        )
        return Response({"count": len(riders), "results": NearbyRiderSerializer(riders, many=True).data})


# ---------------------------------------------------------------------------
# Webhook
# ---------------------------------------------------------------------------
class BookingWebhookView(APIView):
    """
    POST /booking/webhook/
    Header: X-Signature: hex(HMAC_SHA256(BOOKING_WEBHOOK_SECRET, raw_body))
    Body:   {"event_id", "event_type", "source"?, "data": {"booking_reference", ...}}

    Every verified delivery is logged in WebhookEvent (write-once raw data). Replays of the
    same event_id are acknowledged and skipped. Business-level rejections are recorded as
    SKIPPED and acknowledged (200) so the provider stops retrying; unexpected errors return 500
    and are retried with backoff. payment.succeeded must carry amount, currency and
    transaction_id; amount and currency are checked against the stored payment.
    """

    authentication_classes = []
    permission_classes = [permissions.AllowAny]
    throttle_classes = []

    def post(self, request):
        raw_body = request.body                      # read BEFORE request.data
        if not self._valid_signature(request, raw_body):
            return Response({"detail": "Invalid signature."}, status=status.HTTP_401_UNAUTHORIZED)

        serializer = BookingWebhookSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        payload = serializer.validated_data

        event, created = WebhookEvent.record(
            payload["event_type"],
            dict(request.data) if not isinstance(request.data, dict) else request.data,
            source=payload["source"],
            headers=dict(request.headers),
            external_id=payload["event_id"],
            raw_body=raw_body.decode("utf-8", "replace"),
            ip_address=self._client_ip(request),
            signature_valid=True,
        )
        if not created and event.status in (WebhookEvent.Status.PROCESSED, WebhookEvent.Status.SKIPPED):
            return Response({"status": "duplicate"}, status=status.HTTP_200_OK)
        if not event.claim():
            return Response({"status": "busy"}, status=status.HTTP_200_OK)

        try:
            with transaction.atomic():
                note = self._handle(payload["event_type"], payload["data"])
        except (APIException, DjangoValidationError, Booking.DoesNotExist, KeyError) as exc:
            event.mark_skipped(f"Rejected: {exc}"[:500])
            logger.warning("Webhook %s ignored: %s", payload["event_id"], exc)
            return Response({"status": "ignored", "reason": str(exc)}, status=status.HTTP_200_OK)
        except Exception as exc:
            logger.exception("Webhook %s failed", payload["event_id"])
            event.mark_failed(f"{type(exc).__name__}: {exc}"[:1000])
            return Response({"detail": "Processing failed."}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        event.mark_processed(note or "")
        return Response({"status": "processed"}, status=status.HTTP_200_OK)

    @staticmethod
    def _valid_signature(request, raw_body: bytes) -> bool:
        secret = getattr(settings, "BOOKING_WEBHOOK_SECRET", "")
        signature = request.headers.get("X-Signature", "")
        if not secret or not signature:
            return False
        expected = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, signature)

    @staticmethod
    def _client_ip(request):
        forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
        return (forwarded.split(",")[0].strip() or request.META.get("REMOTE_ADDR")) or None

    @staticmethod
    def _handle(event_type: str, data: dict) -> str:
        booking = Booking.objects.get(reference=data["booking_reference"])
        if event_type.startswith("payment."):
            return services.apply_payment_event(event_type=event_type, booking=booking, data=data)
        if event_type == "booking.status_changed":
            services.transition_booking(
                booking_id=booking.pk, new_status=data["status"], actor=None,
                note=data.get("note", "Updated via webhook"), source="webhook",
            )
            return "status updated"
        return f"ignored event type {event_type}"


# ---------------------------------------------------------------------------
# Admin revenue report
# ---------------------------------------------------------------------------
class RevenueReportView(APIView):
    """GET /booking/admin/revenue/?start=YYYY-MM-DD&end=YYYY-MM-DD  (staff only)
    Company revenue: commission + booking fees - promo discounts, plus cancellation fees."""

    permission_classes = [permissions.IsAdminUser]

    def get(self, request):
        today = datetime.now(pricing.LOCAL_TZ).date()
        end_date = parse_date(request.query_params.get("end", "")) or today
        start_date = parse_date(request.query_params.get("start", "")) or (end_date - timedelta(days=29))
        if start_date > end_date or (end_date - start_date).days > 366:
            return Response({"detail": "Invalid date range."}, status=status.HTTP_400_BAD_REQUEST)
        start = datetime.combine(start_date, time.min, tzinfo=pricing.LOCAL_TZ)
        end = datetime.combine(end_date + timedelta(days=1), time.min, tzinfo=pricing.LOCAL_TZ)
        return Response({
            "currency": pricing.CURRENCY,
            "start": start_date.isoformat(),
            "end": end_date.isoformat(),
            **services.revenue_summary(start=start, end=end),
        })



# ─────────────────────────────────────────────
# Lifecycle definition
# ─────────────────────────────────────────────

OPEN_STATUSES = {"pending", "searching", "searching_rider"}
ACTIVE_STATUSES = {"accepted", "arrived", "in_progress"}
MAX_PROMO_FARE = Decimal("1000000")

# action -> (required current status, new status, timestamp field)
TRANSITIONS = {
    "arrive": ({"accepted"}, "arrived", "arrived_at"),
    "start": ({"arrived"}, "in_progress", "started_at"),
    "complete": ({"in_progress"}, "completed", "completed_at"),
}


# ─────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────

def _error(detail, code, http_status):
    return Response({"detail": detail, "code": code}, status=http_status)


def _booking_ref(request):
    ref = request.data.get("booking_ref") or request.data.get("ref")
    ref = str(ref).strip() if ref else ""
    return ref or None


def _rider_or_none(user):
    try:
        return user.riderprofile
    except RiderProfile.DoesNotExist:
        return None


def _parse_bool(value, default=True):
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    raise ValueError("Invalid boolean")


def _snapshot(booking, **extra):
    return Response({"ref": booking.ref, "status": booking.status, **extra})


def _transition(request, action):
    """Move a booking owned by the calling rider through one lifecycle step."""
    rider = _rider_or_none(request.user)
    if rider is None:
        return _error("Rider profile required", "no_rider_profile", status.HTTP_403_FORBIDDEN)

    ref = _booking_ref(request)
    if not ref:
        return _error("booking_ref is required", "missing_ref", status.HTTP_400_BAD_REQUEST)

    allowed_from, new_status, stamp_field = TRANSITIONS[action]

    with transaction.atomic():
        booking = Booking.objects.select_for_update().filter(ref=ref, rider=rider).first()
        if booking is None:
            return _error("Booking not found", "not_found", status.HTTP_404_NOT_FOUND)

        if booking.status not in allowed_from:
            return _error(
                f"Cannot {action} a booking that is '{booking.status}'",
                "invalid_state",
                status.HTTP_409_CONFLICT,
            )

        booking.status = new_status
        setattr(booking, stamp_field, timezone.now())
        booking.save(update_fields=["status", stamp_field])

    logger.info("booking %s: rider %s -> %s", ref, rider.pk, new_status)
    extra = {"fare": str(booking.fare)} if action == "complete" else {}
    return _snapshot(booking, **extra)


# ─────────────────────────────────────────────
# Rider booking actions
# ─────────────────────────────────────────────

@api_view(["POST"])
@permission_classes([IsAuthenticated])
def accept_booking(request):
    """Rider accepts an open booking. First rider to commit wins."""
    rider = _rider_or_none(request.user)
    if rider is None:
        return _error("Rider profile required", "no_rider_profile", status.HTTP_403_FORBIDDEN)

    ref = _booking_ref(request)
    if not ref:
        return _error("booking_ref is required", "missing_ref", status.HTTP_400_BAD_REQUEST)

    with transaction.atomic():
        booking = Booking.objects.select_for_update().filter(ref=ref).first()
        if booking is None:
            return _error("Booking not found", "not_found", status.HTTP_404_NOT_FOUND)

        if booking.status not in OPEN_STATUSES or booking.rider_id:
            return _error("Booking no longer available", "unavailable", status.HTTP_409_CONFLICT)

        if Booking.objects.filter(rider=rider, status__in=ACTIVE_STATUSES).exists():
            return _error("Finish your current ride first", "rider_busy", status.HTTP_409_CONFLICT)

        booking.rider = rider
        booking.status = "accepted"
        booking.accepted_at = timezone.now()
        booking.save(update_fields=["rider", "status", "accepted_at"])

        # Close the dispatch round for everyone else.
        DispatchOffer.objects.filter(booking=booking, rider=rider).update(status="accepted")
        DispatchOffer.objects.filter(booking=booking).exclude(rider=rider).update(status="expired")

    logger.info("booking %s accepted by rider %s", ref, rider.pk)
    return _snapshot(booking)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def decline_booking(request):
    """
    Rider declines an offer. Only this rider's offer is closed; the booking
    itself stays open for other riders.
    """
    rider = _rider_or_none(request.user)
    if rider is None:
        return _error("Rider profile required", "no_rider_profile", status.HTTP_403_FORBIDDEN)

    ref = _booking_ref(request)
    if not ref:
        return _error("booking_ref is required", "missing_ref", status.HTTP_400_BAD_REQUEST)

    reason = str(request.data.get("reason", ""))[:255]

    booking = Booking.objects.filter(ref=ref).first()
    if booking is None:
        return _error("Booking not found", "not_found", status.HTTP_404_NOT_FOUND)

    if booking.rider_id == rider.pk or booking.status in ACTIVE_STATUSES | {"completed"}:
        return _error("Cannot decline — already accepted", "invalid_state", status.HTTP_409_CONFLICT)

    DispatchOffer.objects.filter(booking=booking, rider=rider).update(status="declined")
    logger.info("booking %s declined by rider %s (%s)", ref, rider.pk, reason or "no reason")
    return Response({"ref": booking.ref, "status": "declined"})


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def arrive_ride(request):
    """Rider has arrived at pickup."""
    return _transition(request, "arrive")


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def start_ride(request):
    """Rider starts the trip."""
    return _transition(request, "start")


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def complete_ride(request):
    """Rider completes the trip."""
    return _transition(request, "complete")


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def release_booking(request):
    """
    Rider gives up an accepted booking before the trip starts.
    The booking returns to the dispatch pool.
    """
    rider = _rider_or_none(request.user)
    if rider is None:
        return _error("Rider profile required", "no_rider_profile", status.HTTP_403_FORBIDDEN)

    ref = _booking_ref(request)
    if not ref:
        return _error("booking_ref is required", "missing_ref", status.HTTP_400_BAD_REQUEST)

    reason = str(request.data.get("reason", "rider_cancelled"))[:255]

    with transaction.atomic():
        booking = Booking.objects.select_for_update().filter(ref=ref, rider=rider).first()
        if booking is None:
            return _error("Booking not found", "not_found", status.HTTP_404_NOT_FOUND)

        if booking.status not in {"accepted", "arrived"}:
            return _error(
                "Only accepted or arrived bookings can be released",
                "invalid_state",
                status.HTTP_409_CONFLICT,
            )

        booking.rider = None
        booking.status = "searching"
        booking.accepted_at = None
        booking.arrived_at = None
        booking.save(update_fields=["rider", "status", "accepted_at", "arrived_at"])

        # This rider should not be re-offered the same booking.
        DispatchOffer.objects.filter(booking=booking, rider=rider).update(status="declined")

    logger.info("booking %s released by rider %s (%s)", ref, rider.pk, reason)
    return _snapshot(booking)


# ─────────────────────────────────────────────
# Rider presence
# ─────────────────────────────────────────────

@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated])
def rider_presence(request):
    """Read or update the rider's online state."""
    rider = _rider_or_none(request.user)
    if rider is None:
        return _error("Rider profile required", "no_rider_profile", status.HTTP_403_FORBIDDEN)

    if request.method == "POST":
        try:
            is_online = _parse_bool(request.data.get("is_online"), default=True)
        except ValueError:
            return _error("is_online must be a boolean", "invalid_value", status.HTTP_400_BAD_REQUEST)

        if not is_online and rider.booking_set.filter(status__in=ACTIVE_STATUSES).exists():
            return _error("Finish your active ride before going offline", "rider_busy",
                          status.HTTP_409_CONFLICT)

        rider.is_online = is_online
        rider.last_seen = timezone.now()
        rider.save(update_fields=["is_online", "last_seen"])

    return Response({"is_online": rider.is_online, "last_seen": rider.last_seen})


# ─────────────────────────────────────────────
# Promo validation
# ─────────────────────────────────────────────

class PromoValidateThrottle(AnonRateThrottle):
    """Public endpoint: throttle to stop promo-code guessing.
    Add 'promo_validate': '20/min' to REST_FRAMEWORK['DEFAULT_THROTTLE_RATES']."""
    scope = "promo_validate"


@api_view(["POST"])
@permission_classes([AllowAny])
@throttle_classes([PromoValidateThrottle])
def promo_validate(request):
    """Validate a promo code against a fare and preview the discount."""
    from .pricing import PromoTerms, _promo_discount

    code = str(request.data.get("code", "")).strip().upper()
    if not code or len(code) > 64:
        return _error("A valid code is required", "invalid_code", status.HTTP_400_BAD_REQUEST)

    try:
        ride_fare = Decimal(str(request.data.get("ride_fare", "0")))
    except InvalidOperation:
        return _error("ride_fare must be a number", "invalid_fare", status.HTTP_400_BAD_REQUEST)

    if not ride_fare.is_finite() or ride_fare <= 0 or ride_fare > MAX_PROMO_FARE:
        return _error("ride_fare out of range", "invalid_fare", status.HTTP_400_BAD_REQUEST)

    promo = PromoTerms.objects.filter(code__iexact=code, is_active=True).first()
    if promo is None:
        # Same response for unknown/inactive codes so codes can't be probed.
        return Response({"valid": False, "message": "Promo code not valid"})

    discount = min(_promo_discount(promo, ride_fare, ride_fare), ride_fare)
    discount = max(discount, Decimal("0"))

    return Response({
        "valid": True,
        "code": promo.code,
        "percent_off": str(promo.percent_off),
        "flat_off": str(promo.flat_off),
        "max_discount": str(promo.max_discount) if promo.max_discount else None,
        "discount_applied": str(discount),
        "final_fare": str(ride_fare - discount),
    })




# --------------------------------------------------------------------------
# Serializer
# --------------------------------------------------------------------------
class RiderBookingListSerializer(serializers.ModelSerializer):
    """Read-only representation of a booking for the rider's history list."""

    fare = serializers.DecimalField(
        max_digits=10, decimal_places=2, coerce_to_string=True, read_only=True
    )

    class Meta:
        model = Booking
        fields = ("ref", "status", "pickup", "destination", "fare", "created_at")
        read_only_fields = fields


# --------------------------------------------------------------------------
# Pagination
# --------------------------------------------------------------------------
class BookingPagination(PageNumberPagination):
    page_size = 20
    page_size_query_param = "page_size"
    max_page_size = 100

    def get_paginated_response(self, data):
        # Keeps the original response shape: {"results": [...], "count": n}
        response = super().get_paginated_response(data)
        response.data = {
            "count": self.page.paginator.count,
            "next": response.data["next"],
            "previous": response.data["previous"],
            "results": data,
        }
        return response


# --------------------------------------------------------------------------
# View
# --------------------------------------------------------------------------
@api_view(["GET"])
@permission_classes([IsAuthenticated])
def rider_bookings(request):
    """
    List bookings for the authenticated rider, newest first.

    Query params:
        status     -- optional; filter by booking status (e.g. ?status=completed)
        page       -- page number (default 1)
        page_size  -- items per page (default 20, max 100)
    """
    rider = _rider_or_none(request.user)
    if rider is None:
        return _error(
            "Rider profile required",
            "no_rider_profile",
            status.HTTP_403_FORBIDDEN,
        )

    queryset = (
        Booking.objects.filter(rider=rider)
        .only("ref", "status", "pickup", "destination", "fare", "created_at")
        .order_by("-created_at", "-id")  # "-id" gives a stable tie-breaker
    )

    status_filter = request.query_params.get("status")
    if status_filter:
        valid_statuses = {choice for choice, _ in Booking._meta.get_field("status").choices}
        if valid_statuses and status_filter not in valid_statuses:
            return _error(
                f"Invalid status '{status_filter}'",
                "invalid_status",
                status.HTTP_400_BAD_REQUEST,
            )
        queryset = queryset.filter(status=status_filter)

    paginator = BookingPagination()
    page = paginator.paginate_queryset(queryset, request)
    serializer = RiderBookingListSerializer(page, many=True)
    return paginator.get_paginated_response(serializer.data)
