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

from . import booking_services as services
from . import pricing
from .booking import (
    BookingCancelSerializer, BookingCreateSerializer, BookingDetailSerializer,
    BookingListSerializer, BookingStatusUpdateSerializer, BookingWebhookSerializer,
    NearbyRiderSerializer, NearbyRidersQuerySerializer, QuoteRequestSerializer,
    RiderBookingDetailSerializer, StaffBookingDetailSerializer,
)
from .models import Booking, WebhookEvent

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

