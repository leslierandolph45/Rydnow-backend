"""
Booking + pricing API. Replaces the booking classes that lived in views.py
(BookingViewSet, NearbyRidersView, BookingWebhookView, StandardPagination) and
the calculate_price view that lived in booking_views.py.
"""
import hashlib
import hmac
import logging
from datetime import datetime, time, timedelta

from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils.dateparse import parse_date
from rest_framework import mixins, permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import APIException
from rest_framework.pagination import PageNumberPagination
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from . import pricing, services
from .models import Booking, WebhookEvent
from .serializers import (
    BookingCancelSerializer,
    BookingCreateSerializer,
    BookingDetailSerializer,
    BookingListSerializer,
    BookingStatusUpdateSerializer,
    BookingStopEventSerializer,
    BookingWebhookSerializer,
    NearbyRiderSerializer,
    NearbyRidersQuerySerializer,
    QuoteRequestSerializer,
    RiderBookingDetailSerializer,
    StaffBookingDetailSerializer,
)

logger = logging.getLogger(__name__)


class StandardPagination(PageNumberPagination):
    page_size = 20
    page_size_query_param = "page_size"
    max_page_size = 100


# ---------------------------------------------------------------------------
# Pricing quote  (same URL and response keys as the old calculate_price view)
# ---------------------------------------------------------------------------
class QuoteView(APIView):
    """POST /booking/calculate-price/ - price a trip. Logged-in users also get a
    signed `quote_token`, which is the only thing booking creation accepts."""

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
        service_type = d["service_type"]
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
        return Response({
            "service_type": service_type,
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
        })


# ---------------------------------------------------------------------------
# Bookings
# ---------------------------------------------------------------------------
class BookingViewSet(
    mixins.CreateModelMixin,
    mixins.RetrieveModelMixin,
    mixins.ListModelMixin,
    viewsets.GenericViewSet,
):
    """
    POST   booking/create/                         create_booking (needs quote_token)
    GET    booking/<ref>/                          booking details
    PATCH  booking/<ref>/status/                   update_booking_status
    POST   booking/<ref>/cancel/                   cancel_booking
    POST   booking/<ref>/stops/<sequence>/         rider records arrived/departed at a stop
    """

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
        qs = Booking.objects.select_related("customer", "rider__user").prefetch_related(
            "history", "stops"
        )
        if user.is_staff:
            pass
        elif hasattr(user, "rider_profile"):
            # Riders see their own jobs plus open jobs for their service type.
            rider = user.rider_profile
            qs = qs.filter(
                Q(rider=rider)
                | Q(status=Booking.Status.PENDING, service_type=rider.service_type)
            )
        else:
            qs = qs.filter(customer=user)

        status_param = self.request.query_params.get("status")
        if status_param:
            qs = qs.filter(status__in=status_param.split(","))
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
        if self.action == "cancel":
            return BookingCancelSerializer
        if self.action == "stop_event":
            return BookingStopEventSerializer
        return self._detail_serializer_class()

    def _detail_response(self, booking_id, http_status=status.HTTP_200_OK):
        booking = (
            Booking.objects.select_related("customer", "rider__user")
            .prefetch_related("history", "stops").get(pk=booking_id)
        )
        return Response(
            self._detail_serializer_class()(booking, context=self.get_serializer_context()).data,
            status=http_status,
        )

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        booking, created = services.create_booking(
            customer=request.user, data=serializer.validated_data
        )
        return self._detail_response(
            booking.pk, status.HTTP_201_CREATED if created else status.HTTP_200_OK
        )

    @action(detail=True, methods=["patch"], url_path="status")
    def update_status(self, request, booking_ref=None):
        booking = self.get_object()  # 404 if the user can't see it
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
            booking_id=booking.pk,
            actor=request.user,
            reason=serializer.validated_data.get("reason", ""),
        )
        return self._detail_response(booking.pk)

    @action(detail=True, methods=["post"], url_path=r"stops/(?P<sequence>\d+)")
    def stop_event(self, request, booking_ref=None, sequence=None):
        booking = self.get_object()
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        services.record_stop_event(
            booking_id=booking.pk, sequence=int(sequence),
            event=serializer.validated_data["event"], actor=request.user,
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
            lat=float(params["lat"]),
            lng=float(params["lng"]),
            radius_km=params["radius_km"],
            service_type=params.get("service_type"),
            limit=params["limit"],
        )
        return Response(
            {"count": len(riders), "results": NearbyRiderSerializer(riders, many=True).data}
        )


# ---------------------------------------------------------------------------
# Webhook
# ---------------------------------------------------------------------------
class BookingWebhookView(APIView):
    """
    POST /booking/webhook/
    Header: X-Signature: hex(HMAC_SHA256(BOOKING_WEBHOOK_SECRET, raw_body))
    Idempotent: replays of the same event_id are acknowledged and skipped.
    payment.succeeded must carry `amount` and `currency`; both are checked
    against the stored booking price before it is marked paid.
    """

    authentication_classes = []
    permission_classes = [permissions.AllowAny]
    throttle_classes = []

    def post(self, request):
        if not self._valid_signature(request):
            return Response({"detail": "Invalid signature."}, status=status.HTTP_401_UNAUTHORIZED)

        serializer = BookingWebhookSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        payload = serializer.validated_data

        try:
            with transaction.atomic():
                # Row lock makes two simultaneous deliveries of one event run one at a time.
                event, _ = WebhookEvent.objects.select_for_update().get_or_create(
                    event_id=payload["event_id"],
                    defaults={"event_type": payload["event_type"], "payload": request.data},
                )
                if event.processed:
                    return Response({"status": "duplicate"}, status=status.HTTP_200_OK)
                try:
                    with transaction.atomic():
                        self._handle(payload["event_type"], payload["data"])
                except (APIException, Booking.DoesNotExist) as exc:
                    # Business-level rejection: record it and ack so the provider stops retrying.
                    event.error = str(exc)
                    event.save(update_fields=["error"])
                    logger.warning("Webhook %s ignored: %s", event.event_id, exc)
                    return Response(
                        {"status": "ignored", "reason": str(exc)}, status=status.HTTP_200_OK
                    )
                event.processed, event.error = True, ""
                event.save(update_fields=["processed", "error"])
            return Response({"status": "processed"}, status=status.HTTP_200_OK)
        except Exception:
            # Unexpected -> 500 so the provider retries.
            logger.exception("Webhook %s failed", payload["event_id"])
            raise

    @staticmethod
    def _valid_signature(request) -> bool:
        secret = getattr(settings, "BOOKING_WEBHOOK_SECRET", "")
        signature = request.headers.get("X-Signature", "")
        if not secret or not signature:
            return False
        expected = hmac.new(secret.encode(), request.body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, signature)

    @staticmethod
    def _handle(event_type: str, data: dict):
        booking = Booking.objects.get(reference=data["booking_reference"])
        if event_type.startswith("payment."):
            services.apply_payment_event(event_type=event_type, booking=booking, data=data)
        elif event_type == "booking.status_changed":
            services.transition_booking(
                booking_id=booking.pk,
                new_status=data["status"],
                actor=None,  # system actor
                note=data.get("note", "Updated via webhook"),
                source="webhook",
            )


# ---------------------------------------------------------------------------
# Admin revenue report
# ---------------------------------------------------------------------------
class RevenueReportView(APIView):
    """GET /booking/admin/revenue/?start=YYYY-MM-DD&end=YYYY-MM-DD  (staff only)
    Company revenue: commission + booking fees - promo discounts + cancellation fees."""

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

