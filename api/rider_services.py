—————————————————————————
# SERVICE LAYER
# —————————————————————————
S = RiderStatusHistory.Status

ACTIVE_FLAG_BY_STATUS = {
    S.PENDING: False, S.ACTIVE: True, S.INACTIVE: False,
    S.SUSPENDED: False, S.DEACTIVATED: False, S.BLACKLISTED: False,
}


def current_status(rider_profile):
    """Latest recorded status; falls back to the profile's flags for riders with no history yet."""
    latest = rider_profile.status_history.order_by("-changed_at").first()
    if latest:
        return latest.new_status
    if getattr(rider_profile, "is_active", False):
        return S.ACTIVE
    if not getattr(rider_profile, "is_verified", False):
        return S.PENDING
    return S.INACTIVE


def validate_status_change(rider_profile, new_status, reason="", effective_until=None, force=False):
    """Raise DjangoValidationError if the change breaks a rule. Returns the old status."""
    old = current_status(rider_profile)
    labels = dict(S.choices)
    if new_status not in labels:
        raise DjangoValidationError({"new_status": "Unknown status."})
    if new_status == old:
        raise DjangoValidationError(f"Rider is already {labels[old]}.")
    if not force and new_status not in RiderStatusHistory.ALLOWED_TRANSITIONS.get(old, set()):
        raise DjangoValidationError(
            f"A rider cannot move from {labels[old]} to {labels[new_status]}."
        )
    leaving_restriction = old in RiderStatusHistory.RESTRICTIVE_STATUSES
    if (new_status in RiderStatusHistory.REASON_REQUIRED_STATUSES or leaving_restriction) \
            and not (reason or "").strip():
        raise DjangoValidationError({"reason": "A reason is required for this change."})
    if effective_until:
        if new_status != S.SUSPENDED:
            raise DjangoValidationError({"effective_until": "An end date only applies to suspensions."})
        if effective_until <= timezone.now():
            raise DjangoValidationError({"effective_until": "The end date must be in the future."})
    return old


@transaction.atomic
def change_rider_status(
    rider_profile, new_status, *, changed_by=None, reason="", reason_category="other",
    internal_notes="", effective_until=None, source="admin",
    ip_address=None, metadata=None, force=False,
):
    """Validate, record and apply a status change atomically. Returns the history row."""
    rider_model = type(rider_profile)
    rider = rider_model.objects.select_for_update().get(pk=rider_profile.pk)
    old = validate_status_change(rider, new_status, reason, effective_until, force)

    record = RiderStatusHistory.objects.create(
        rider_profile=rider,
        old_status=old,
        new_status=new_status,
        reason_category=reason_category,
        reason=(reason or "").strip(),
        internal_notes=(internal_notes or "").strip(),
        effective_until=effective_until,
        changed_by=changed_by if getattr(changed_by, "pk", None) else None,
        source=source,
        ip_address=ip_address,
        metadata=metadata or ({"forced": True} if force else {}),
    )

    fields = []
    if hasattr(rider, "is_active"):
        rider.is_active = ACTIVE_FLAG_BY_STATUS[new_status]
        fields.append("is_active")
    if hasattr(rider, "status"):
        rider.status = new_status
        fields.append("status")
    if hasattr(rider, "updated_at"):
        fields.append("updated_at")
    if fields:
        rider.save(update_fields=fields)
    return record


def expire_suspensions(now=None):
    """Reinstate riders whose timed suspension has ended. Run hourly. Returns the count."""
    now = now or timezone.now()
    due = (
        RiderStatusHistory.objects.filter(new_status=S.SUSPENDED, effective_until__lte=now)
        .latest_only()
        .select_related("rider_profile")
    )
    reinstated = 0
    for entry in due:
        try:
            change_rider_status(
                entry.rider_profile, S.ACTIVE,
                reason="Suspension period ended.",
                reason_category=RiderStatusHistory.Category.ADMINISTRATIVE,
                source=RiderStatusHistory.Source.SYSTEM,
                metadata={"auto_reinstated_from": str(entry.pk)},
            )
            reinstated += 1
        except DjangoValidationError:
            continue
    return reinstated


def get_client_ip(request):
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR")
    return (forwarded.split(",")[0].strip() if forwarded else request.META.get("REMOTE_ADDR")) or None