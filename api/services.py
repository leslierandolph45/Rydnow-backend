from django.db import transaction
from django.utils import timezone

from .models import ServiceQualification as SQ, Service


@transaction.atomic
def request_qualification(rider, service):
    obj, _ = SQ.objects.get_or_create(rider=rider, service=service)
    return obj


@transaction.atomic
def set_qualification_status(rider, service, status, *, admin_user, reason=""):
    qual, _ = SQ.objects.select_for_update().get_or_create(rider=rider, service=service)
    qual.status = status
    qual.status_reason = reason
    if status == SQ.Status.APPROVED:
        qual.approved_at = timezone.now()
        qual.approved_by = admin_user
    qual.save()
    return qual


def can_rider_accept_job(rider, service) -> bool:
    """Single gate for automated dispatch. Call at assignment time, not only at listing."""
    return (
        rider.is_active
        and rider.is_verified
        and service.is_active
        and SQ.objects.filter(rider=rider, service=service, status=SQ.Status.APPROVED).exists()
    )
