from __future__ import annotations
import uuid
from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.db.models import Q
from django.utils import timezone
from .models import AppendOnlyModel

from django.db import models 


# from core.models import AppendOnlyModel  # <- adjust to your project's path


LAT_VALIDATORS = [MinValueValidator(-90), MaxValueValidator(90)]
LNG_VALIDATORS = [MinValueValidator(-180), MaxValueValidator(180)]


class Booking(AppendOnlyModel):
    """A ride, delivery, or waste collection booking."""

    class ServiceType(models.TextChoices):
        OKADA = "okada", "Okada Ride"
        PARCEL = "parcel", "Parcel Delivery"
        WASTE_BIN = "waste-bin", "Waste Collection"

    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        PENDING = "pending", "Pending"
        ACCEPTED = "accepted", "Accepted"
        EN_ROUTE = "en-route", "En Route"
        ARRIVED = "arrived", "Arrived"
        COMPLETED = "completed", "Completed"
        CANCELLED = "cancelled", "Cancelled"
        FAILED = "failed", "Failed"

    class PaymentStatus(models.TextChoices):
        UNPAID = "unpaid", "Unpaid"
        PAID = "paid", "Paid"
        REFUNDED = "refunded", "Refunded"

    class PaymentMethod(models.TextChoices):
        CASH = "cash", "Cash"
        MOBILE_MONEY = "momo", "Mobile Money"
        CARD = "card", "Card"
        WALLET = "wallet", "Wallet"

    # Allowed status transitions (state machine)
    TRANSITIONS = {
        Status.DRAFT: {Status.PENDING, Status.CANCELLED},
        Status.PENDING: {Status.ACCEPTED, Status.CANCELLED, Status.FAILED},
        Status.ACCEPTED: {Status.EN_ROUTE, Status.CANCELLED, Status.FAILED},
        Status.EN_ROUTE: {Status.ARRIVED, Status.CANCELLED, Status.FAILED},
        Status.ARRIVED: {Status.COMPLETED, Status.CANCELLED, Status.FAILED},
        Status.COMPLETED: set(),
        Status.CANCELLED: set(),
        Status.FAILED: set(),
    }
    TERMINAL_STATUSES = {Status.COMPLETED, Status.CANCELLED, Status.FAILED}
    ACTIVE_STATUSES = {Status.PENDING, Status.ACCEPTED, Status.EN_ROUTE, Status.ARRIVED}

    # ------------------------------------------------------------------
    # Core
    # ------------------------------------------------------------------
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    reference = models.CharField(
        max_length=12, unique=True, editable=False, blank=True,
        help_text="Short human-friendly code, e.g. BK-7F3A9C",
    )
    service_type = models.CharField(max_length=20, choices=ServiceType.choices, db_index=True)
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.DRAFT, db_index=True
    )

    # ------------------------------------------------------------------
    # Parties
    # ------------------------------------------------------------------
    customer = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="bookings",
    )
    driver = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="assigned_bookings",
    )

    # ------------------------------------------------------------------
    # Locations
    # ------------------------------------------------------------------
    pickup_address = models.CharField(max_length=255)
    pickup_lat = models.FloatField(null=True, blank=True, validators=LAT_VALIDATORS)
    pickup_lng = models.FloatField(null=True, blank=True, validators=LNG_VALIDATORS)
    destination_address = models.CharField(max_length=255)
    destination_lat = models.FloatField(null=True, blank=True, validators=LAT_VALIDATORS)
    destination_lng = models.FloatField(null=True, blank=True, validators=LNG_VALIDATORS)

    # ------------------------------------------------------------------
    # Service details
    # ------------------------------------------------------------------
    notes = models.TextField(blank=True, help_text="Instructions for the driver/collector")
    scheduled_for = models.DateTimeField(
        null=True, blank=True, help_text="Leave empty for immediate service"
    )
    details = models.JSONField(
        default=dict, blank=True,
        help_text="Service-specific data (parcel size/weight, bin count, passenger count...)",
    )

    # ------------------------------------------------------------------
    # Pricing & payment
    # ------------------------------------------------------------------
    total_fare = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True,
        validators=[MinValueValidator(Decimal("0.00"))],
    )
    currency = models.CharField(max_length=3, default="GHS")
    payment_method = models.CharField(
        max_length=10, choices=PaymentMethod.choices, default=PaymentMethod.CASH
    )
    payment_status = models.CharField(
        max_length=10, choices=PaymentStatus.choices, default=PaymentStatus.UNPAID, db_index=True
    )

    # ------------------------------------------------------------------
    # Estimates
    # ------------------------------------------------------------------
    estimated_duration = models.PositiveIntegerField(null=True, blank=True, help_text="Minutes")
    estimated_distance = models.FloatField(
        null=True, blank=True,
        validators=[MinValueValidator(0)],
        help_text="Kilometers",
    )

    # ------------------------------------------------------------------
    # Lifecycle timestamps
    # ------------------------------------------------------------------
    created_at = models.DateTimeField(default=timezone.now, editable=False)
    updated_at = models.DateTimeField(auto_now=True)
    requested_at = models.DateTimeField(null=True, blank=True, editable=False)
    accepted_at = models.DateTimeField(null=True, blank=True, editable=False)
    started_at = models.DateTimeField(null=True, blank=True, editable=False)
    arrived_at = models.DateTimeField(null=True, blank=True, editable=False)
    completed_at = models.DateTimeField(null=True, blank=True, editable=False)
    cancelled_at = models.DateTimeField(null=True, blank=True, editable=False)
    cancellation_reason = models.CharField(max_length=255, blank=True)

    # Maps a status to the timestamp field it should stamp
    _STATUS_TIMESTAMPS = {
        Status.PENDING: "requested_at",
        Status.ACCEPTED: "accepted_at",
        Status.EN_ROUTE: "started_at",
        Status.ARRIVED: "arrived_at",
        Status.COMPLETED: "completed_at",
        Status.CANCELLED: "cancelled_at",
    }

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "booking"
        verbose_name_plural = "bookings"
        indexes = [
            models.Index(fields=["customer", "-created_at"], name="booking_customer_idx"),
            models.Index(fields=["driver", "status"], name="booking_driver_status_idx"),
            models.Index(fields=["service_type", "status"], name="booking_service_status_idx"),
        ]
        constraints = [
            models.CheckConstraint(
                condition=Q(total_fare__isnull=True) | Q(total_fare__gte=0),
                name="booking_fare_non_negative",
            ),
            models.CheckConstraint(
                condition=~Q(status="cancelled") | Q(cancelled_at__isnull=False),
                name="booking_cancelled_has_timestamp",
            ),
        ]

    def __str__(self):
        return f"{self.reference or self.pk} · {self.get_service_type_display()} · {self.status}"

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------
    def save(self, *args, **kwargs):
        if not self.reference:
            self.reference = self._generate_reference()
        super().save(*args, **kwargs)

    @staticmethod
    def _generate_reference():
        return f"BK-{uuid.uuid4().hex[:6].upper()}"

    def clean(self):
        super().clean()
        if self.scheduled_for and self.scheduled_for < timezone.now() and self._state.adding:
            raise ValidationError({"scheduled_for": "Scheduled time cannot be in the past."})
        for prefix in ("pickup", "destination"):
            lat, lng = getattr(self, f"{prefix}_lat"), getattr(self, f"{prefix}_lng")
            if (lat is None) != (lng is None):
                raise ValidationError(
                    f"Provide both latitude and longitude for {prefix}, or neither."
                )

    # ------------------------------------------------------------------
    # State machine
    # ------------------------------------------------------------------
    def can_transition_to(self, new_status) -> bool:
        return new_status in self.TRANSITIONS.get(self.status, set())

    def transition_to(self, new_status, *, reason: str = "", save: bool = True):
        """Move to `new_status`, validating the transition and stamping timestamps."""
        if not self.can_transition_to(new_status):
            raise ValidationError(
                f"Cannot change booking from '{self.status}' to '{new_status}'."
            )
        self.status = new_status
        stamp_field = self._STATUS_TIMESTAMPS.get(new_status)
        if stamp_field:
            setattr(self, stamp_field, timezone.now())
        if new_status == self.Status.CANCELLED:
            self.cancellation_reason = reason
        if save:
            self.save()
        return self

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    @property
    def is_active(self) -> bool:
        return self.status in self.ACTIVE_STATUSES

    @property
    def is_terminal(self) -> bool:
        return self.status in self.TERMINAL_STATUSES

    @property
    def is_scheduled(self) -> bool:
        return self.scheduled_for is not None

    @property
    def has_pickup_coords(self) -> bool:
        return self.pickup_lat is not None and self.pickup_lng is not None

    @property
    def has_destination_coords(self) -> bool:
        return self.destination_lat is not None and self.destination_lng is not None

