"""
rider_domain.py : rider business operations (service layer)
============================================================
This file holds NO models, NO urls and NO views.

  * Models live in models.py (RiderProfile, RiderDocument, ServiceQualification,
    RiderPayoutAccount, RiderEmergencyContact, RiderWallet, WithdrawalRequest ...).
  * Routes live in api/urls.py.
  * Views/serializers call the functions below.

Import rule that prevents circular imports:
    models.py  <-  rider_domain.py  <-  views.py / serializers.py / admin.py
rider_domain.py must never import views, serializers or admin.

Every function that changes data is atomic, takes a row lock on the rider
where races matter, checks who is acting, and writes an AuditLog row.
Failures raise RiderDomainError, a subclass of Django's ValidationError,
so existing `except ValidationError` code keeps working.
"""
import logging
import math
from datetime import timedelta
from decimal import Decimal, InvalidOperation

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Sum
from django.utils import timezone

# Everything below is imported from models.py. Names re-exported at the bottom
# keep old `from .rider_domain import RiderDocument, RiderProfile` lines working.
from .models import (
    AuditLog,
    Booking,
    Gender,
    LICENCE_WARNING_DAYS,
    LOCATION_STALE_SECONDS,
    LOW_ACCEPTANCE,
    LOW_RATING,
    HIGH_CANCEL,
    HIGH_EXPIRY,
    MAX_CONTACTS_PER_RIDER,
    MAX_STRIKES,
    MIN_SAMPLE,
    Notification,
    RiderDocument,
    RiderEmergencyContact,
    RiderIncident,
    RiderPayoutAccount,
    RiderProfile,
    RiderStatusHistory,
    RiderTier,
    RiderWallet,
    Service,
    ServiceQualification,
    SystemConfig,
    VerificationStatus,
    WithdrawalRequest,
    ghana_card_validator,
    normalize_gh_phone,
    phone_validator,
    round_money,
)

logger = logging.getLogger(__name__)

# —————————————————————————
# Constants owned by this module
# —————————————————————————
MIN_AGE_YEARS = 18
DOCUMENT_MAX_MB = 10
STRIKE_SUSPENSION_THRESHOLD = MAX_STRIKES            # same value models.py uses
STALE_LOCATION_SECONDS = LOCATION_STALE_SECONDS      # same value models.py uses
AUTO_SUSPENSION_DAYS = 7                             # mirrors RiderProfile.add_strike (hard-coded there)

_DT = RiderDocument.DocumentType
_DS = RiderDocument.DocumentStatus
_QS = ServiceQualification.Status

# Documents that must carry an expiry date when uploaded
EXPIRY_REQUIRED = {_DT.RIDING_LICENSE.value, _DT.INSURANCE.value}

QUALIFICATION_TRANSITIONS = {
    _QS.PENDING.value: {_QS.APPROVED.value, _QS.REJECTED.value},
    _QS.APPROVED.value: {_QS.SUSPENDED.value, _QS.REVOKED.value},
    _QS.SUSPENDED.value: {_QS.APPROVED.value, _QS.REVOKED.value},
    _QS.REJECTED.value: {_QS.PENDING.value},
    _QS.REVOKED.value: {_QS.PENDING.value},
}
QUALIFICATION_REASON_REQUIRED = {
    _QS.REJECTED.value, _QS.SUSPENDED.value, _QS.REVOKED.value,
}

# WithdrawalRequest statuses that are still moving
WITHDRAWAL_IN_FLIGHT = ("pending", "reserved", "validated", "initiated")
WITHDRAWAL_NOT_COUNTED = ("failed", "reversed", "timeout")


class RiderDomainError(ValidationError):
    """A rider operation is not allowed. Map to HTTP 400/403 in views."""


# —————————————————————————
# Small helpers
# —————————————————————————
def _locked(rider):
    """Re-read the rider row under a lock so every status check sees fresh data."""
    return RiderProfile.objects.select_for_update().get(pk=getattr(rider, "pk", rider))


def _require_staff(user):
    if not (user and getattr(user, "is_active", False) and getattr(user, "is_staff", False)):
        raise RiderDomainError("Only staff can perform this action.")


def _forbid_self_action(user, rider):
    if user is not None and user.pk == rider.user_id:
        raise RiderDomainError("You cannot perform this action on your own account.")


def _audit(actor, action, obj, **changes):
    AuditLog.record(actor, action, obj, changes={k: str(v) for k, v in changes.items()})


def _notify(rider, title, body):
    """Send after the surrounding transaction commits, never inside it."""
    user = rider.user
    transaction.on_commit(
        lambda: Notification.notify(user, title, body, category=Notification.Category.SYSTEM)
    )


def _resolve_service(service):
    if isinstance(service, Service):
        return service
    try:
        return Service.objects.get(pk=service)
    except Service.DoesNotExist:
        raise RiderDomainError(f"Unknown service: {service}")


def _age_years(dob, today=None):
    today = today or timezone.localdate()
    return today.year - dob.year - ((today.month, today.day) < (dob.month, dob.day))


def validate_coordinates(lat, lng):
    """Returns (lat, lng) as floats or raises. (0, 0) is rejected: it means 'no GPS fix'."""
    try:
        lat, lng = float(lat), float(lng)
    except (TypeError, ValueError):
        raise RiderDomainError("Latitude and longitude must be numbers.")
    if not (math.isfinite(lat) and math.isfinite(lng)):
        raise RiderDomainError("Invalid coordinates.")
    if not (-90.0 <= lat <= 90.0 and -180.0 <= lng <= 180.0):
        raise RiderDomainError("Coordinates are out of range.")
    if lat == 0.0 and lng == 0.0:
        raise RiderDomainError("No valid GPS fix.")
    return lat, lng


def _optional_float(value, name, low, high):
    if value is None:
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        raise RiderDomainError(f"{name} must be a number.")
    if not math.isfinite(v) or not (low <= v <= high):
        raise RiderDomainError(f"{name} must be between {low} and {high}.")
    return v


# —————————————————————————
# 1. Verification workflow
# —————————————————————————
def verification_blockers(rider, *, for_submission=False):
    """Plain-English list of what stops this rider from being submitted/approved."""
    out = []
    if not (rider.full_name or "").strip():
        out.append("Full name is missing.")
    if not rider.ghana_card_number:
        out.append("Ghana Card number is missing.")
    if not rider.license_number:
        out.append("Licence number is missing.")
    if not rider.license_expiry_date:
        out.append("Licence expiry date is missing.")
    elif rider.licence_expired:
        out.append("Riding licence has expired.")
    if not rider.date_of_birth:
        out.append("Date of birth is missing.")
    elif _age_years(rider.date_of_birth) < MIN_AGE_YEARS:
        out.append(f"Rider must be at least {MIN_AGE_YEARS} years old.")
    docs_ok = rider.documents_uploaded if for_submission else rider.documents_complete
    if not docs_ok:
        out.append("Required documents are missing." if for_submission
                   else "All required documents must be approved first.")
    return out


@transaction.atomic
def submit_for_review(rider, *, by=None):
    r = _locked(rider)
    if by is not None and not (by.pk == r.user_id or getattr(by, "is_staff", False)):
        raise RiderDomainError("Not allowed.")
    blockers = verification_blockers(r, for_submission=True)
    if blockers:
        raise RiderDomainError(blockers)
    old = r.verification_status
    r.submit_for_review()
    _audit(by, "rider.submit_for_review", r, from_status=old, to_status=r.verification_status)
    return r


@transaction.atomic
def approve_rider(rider, by):
    _require_staff(by)
    r = _locked(rider)
    _forbid_self_action(by, r)
    blockers = verification_blockers(r)
    if blockers:
        raise RiderDomainError(blockers)
    old = r.verification_status
    r.approve(by)
    _audit(by, "rider.approve", r, from_status=old, to_status=r.verification_status)
    _notify(r, "Account verified", "You are verified. Go online to start receiving jobs.")
    return r


@transaction.atomic
def reject_rider(rider, by, reason):
    _require_staff(by)
    reason = (reason or "").strip()
    if not reason:
        raise RiderDomainError("A rejection reason is required.")
    r = _locked(rider)
    _forbid_self_action(by, r)
    old = r.verification_status
    r.reject(by, reason)
    _audit(by, "rider.reject", r, from_status=old, to_status=r.verification_status, reason=reason)
    _notify(r, "Verification needs attention", reason)
    return r


@transaction.atomic
def suspend_rider(rider, by, reason, until=None):
    """Suspends the rider. Trips already ASSIGNED (not started) go back to dispatch.
    A trip already IN PROGRESS is left alone and logged for ops to handle."""
    _require_staff(by)
    reason = (reason or "").strip()
    if not reason:
        raise RiderDomainError("A suspension reason is required.")
    if until is not None and until <= timezone.now():
        raise RiderDomainError("`until` must be in the future.")
    r = _locked(rider)
    _forbid_self_action(by, r)

    released = []
    for b in Booking.objects.filter(assigned_rider=r, status=Booking.Status.ASSIGNED):
        ok, _msg = b.release_rider(reason="Rider suspended")
        if ok:
            released.append(b.booking_ref)
    in_progress = list(
        Booking.objects.filter(assigned_rider=r, status=Booking.Status.IN_PROGRESS)
        .values_list("booking_ref", flat=True)
    )
    if in_progress:
        logger.warning("Rider %s suspended with trips in progress: %s", r.pk, in_progress)

    old = r.verification_status
    r.suspend(by=by, reason=reason, until=until)
    _audit(by, "rider.suspend", r, from_status=old, reason=reason,
           until=until or "indefinite", released=released, in_progress=in_progress)
    _notify(r, "Account suspended", reason)
    return r


@transaction.atomic
def reinstate_rider(rider, by, reason="Reinstated"):
    _require_staff(by)
    r = _locked(rider)
    _forbid_self_action(by, r)
    r.reinstate(by, reason)
    _audit(by, "rider.reinstate", r, reason=reason)
    _notify(r, "Account reinstated", "You can go online again.")
    return r


@transaction.atomic
def add_strike(rider, by, reason=""):
    """models.py suspends automatically at MAX_STRIKES (7 days)."""
    _require_staff(by)
    r = _locked(rider)
    _forbid_self_action(by, r)
    r.add_strike(reason=reason)
    _audit(by, "rider.strike", r, strikes=r.strike_count,
           status=r.verification_status, reason=reason)
    return r


# —————————————————————————
# 2. Documents
# —————————————————————————
def _looks_like_allowed_file(f):
    """Checks the first bytes, not just the extension (jpg, png, webp, pdf)."""
    head = f.read(16)
    f.seek(0)
    return (head.startswith(b"\xff\xd8\xff")
            or head.startswith(b"\x89PNG\r\n\x1a\n")
            or head.startswith(b"%PDF-")
            or (head[:4] == b"RIFF" and head[8:12] == b"WEBP"))


@transaction.atomic
def upload_document(rider, document_type, file, *, expiry_date=None, by=None):
    r = _locked(rider)
    if by is not None and not (by.pk == r.user_id or getattr(by, "is_staff", False)):
        raise RiderDomainError("Not allowed.")
    if not r.is_active or r.deleted_at is not None:
        raise RiderDomainError("This account is inactive.")
    if r.verification_status in (VerificationStatus.UNDER_REVIEW, VerificationStatus.SUSPENDED):
        raise RiderDomainError("Documents cannot be changed while the account is under review or suspended.")
    if document_type not in _DT.values:
        raise RiderDomainError("Unknown document type.")
    if file is None:
        raise RiderDomainError("No file uploaded.")
    if file.size > DOCUMENT_MAX_MB * 1024 * 1024:
        raise RiderDomainError(f"File too large (max {DOCUMENT_MAX_MB} MB).")
    if not _looks_like_allowed_file(file):
        raise RiderDomainError("File content is not a valid JPG, PNG, WEBP or PDF.")
    if document_type in EXPIRY_REQUIRED and not expiry_date:
        raise RiderDomainError("An expiry date is required for this document.")
    if expiry_date and expiry_date <= timezone.localdate():
        raise RiderDomainError("This document has already expired.")

    # A newer upload replaces any older one that was still waiting for review.
    RiderDocument.objects.filter(
        rider_profile=r, document_type=document_type, status=_DS.PENDING
    ).update(status=_DS.REJECTED, notes="Superseded by a newer upload.")

    doc = RiderDocument(rider_profile=r, document_type=document_type,
                        file=file, expiry_date=expiry_date)
    doc.full_clean()
    doc.save
