"""Domain exceptions raised by the service layer.

Design rules
------------
* Every error derives from RydnowError so callers can catch one base type.
* Each error carries a stable machine code (for apps/clients), an
  http_status (for the API layer) and a retryable flag (for automation:
  Celery tasks, dispatch loops, webhooks decide retry-vs-fail from this alone).
* message and details are safe to show to clients. Never put secrets,
  stack traces or internal IDs you wouldn't expose into them.
* Services raise these; views never build error responses by hand. The DRF
  exception handler (see exception_handler.py) converts them uniformly.
"""
from __future__ import annotations

from typing import Any, Optional


# ── Base ────────────────────────────────────────────────────────────
class RydnowError(Exception):
    """Root of all Rydnow domain errors."""

    default_message = "Something went wrong."
    code = "rydnow_error"
    http_status = 400
    retryable = False

    def __init__(
        self,
        message: Optional[str] = None,
        *,
        code: Optional[str] = None,
        details: Optional[dict[str, Any]] = None,
    ):
        self.message = message or self.default_message
        if code:
            self.code = code
        self.details = details or {}
        super().__init__(self.message)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.details:
            payload["details"] = self.details
        return payload

    def __repr__(self) -> str:
        return f"{type(self).__name__}(code={self.code!r}, message={self.message!r})"


def is_retryable(exc: BaseException) -> bool:
    """True if automation may safely retry the operation that raised exc."""
    return isinstance(exc, RydnowError) and exc.retryable


# ── Generic / cross-cutting ──────────────────────────────────────────
class ValidationFailedError(RydnowError):
    default_message = "The request contains invalid data."
    code = "validation_failed"
    http_status = 400


class NotFoundError(RydnowError):
    default_message = "The requested resource was not found."
    code = "not_found"
    http_status = 404


class PermissionDeniedError(RydnowError):
    default_message = "You do not have permission to perform this action."
    code = "permission_denied"
    http_status = 403


class RateLimitExceededError(RydnowError):
    default_message = "Too many requests. Please slow down."
    code = "rate_limited"
    http_status = 429
    retryable = True

    def __init__(self, message: Optional[str] = None, *, retry_after: Optional[int] = None, **kw):
        super().__init__(message, **kw)
        self.retry_after = retry_after
        if retry_after is not None:
            self.details.setdefault("retry_after_seconds", retry_after)


class ConcurrencyError(RydnowError):
    """Lost a race (row changed under us). Safe to retry the whole operation."""

    default_message = "The resource was modified by another request. Please retry."
    code = "concurrency_conflict"
    http_status = 409
    retryable = True


class ExternalServiceError(RydnowError):
    """A third party (maps, SMS, push, etc.) failed or timed out."""

    default_message = "An upstream service is temporarily unavailable."
    code = "external_service_error"
    http_status = 502
    retryable = True


# ── Bookings ─────────────────────────────────────────────────────────
class BookingError(RydnowError):
    """Base class for booking-domain errors."""

    default_message = "The booking could not be processed."
    code = "booking_error"


class InvalidBookingError(BookingError):
    """Booking input validation failed."""

    default_message = "The booking details are invalid."
    code = "invalid_booking"
    http_status = 400


class ServiceUnavailableError(BookingError):
    """The requested service is currently deactivated."""

    default_message = "This service is currently unavailable."
    code = "service_unavailable"
    http_status = 409


class DuplicateBookingError(BookingError):
    """An equivalent open booking already exists for this customer."""

    code = "duplicate_booking"
    http_status = 409

    def __init__(self, existing):
        ref = getattr(existing, "booking_ref", None)
        super().__init__(
            f"Duplicate of open booking {ref}" if ref else "Duplicate booking.",
            details={"existing_booking_ref": ref} if ref else None,
        )
        self.existing = existing


class BookingNotFoundError(BookingError, NotFoundError):
    default_message = "Booking not found."
    code = "booking_not_found"
    http_status = 404


class InvalidStateTransitionError(BookingError):
    """Booking lifecycle rule violated (e.g. completing a cancelled booking)."""

    default_message = "This action is not allowed in the booking's current state."
    code = "invalid_state_transition"
    http_status = 409

    def __init__(self, current: str, target: str, **kw):
        super().__init__(
            f"Cannot move booking from '{current}' to '{target}'.",
            details={"current": current, "target": target},
            **kw,
        )
        self.current, self.target = current, target


class BookingNotCancellableError(BookingError):
    default_message = "This booking can no longer be cancelled."
    code = "booking_not_cancellable"
    http_status = 409


# ── Dispatch / assignment ──────────────────────────────────────────
class DispatchError(RydnowError):
    default_message = "Dispatch failed."
    code = "dispatch_error"
    http_status = 503


class NoRidersAvailableError(DispatchError):
    """Nobody nearby right now. The dispatcher should widen radius / retry."""

    default_message = "No riders are available nearby. We'll keep searching."
    code = "no_riders_available"
    retryable = True


class RiderUnavailableError(DispatchError):
    """Chosen rider went offline, got busy or declined."""

    default_message = "The rider is no longer available."
    code = "rider_unavailable"
    http_status = 409
    retryable = True


class AssignmentConflictError(DispatchError):
    """Booking was already assigned by another worker."""

    default_message = "This booking has already been assigned."
    code = "assignment_conflict"
    http_status = 409
    retryable = False


class DispatchTimeoutError(DispatchError):
    default_message = "No rider accepted in time."
    code = "dispatch_timeout"
    http_status = 504
    retryable = True


# ── Payments ────────────────────────────────────────────────────────
class PaymentError(RydnowError):
    default_message = "Payment could not be completed."
    code = "payment_error"
    http_status = 402


class PaymentFailedError(PaymentError):
    """Declined / rejected by the customer's wallet or bank. Do not auto-retry."""

    code = "payment_failed"


class InsufficientFundsError(PaymentError):
    default_message = "Insufficient funds."
    code = "insufficient_funds"


class PaymentProviderError(PaymentError):
    """Mobile-money / gateway outage or timeout. Safe to retry with the same reference."""

    default_message = "The payment provider is temporarily unavailable."
    code = "payment_provider_error"
    http_status = 502
    retryable = True
