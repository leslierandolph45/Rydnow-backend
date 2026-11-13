"""
api/profile_views.py -- "who am I" endpoint for riders and customers.

    GET   /me/   the logged-in user's own profile
    PATCH /me/   update full_name / phone / email / profile_photo (multipart for the photo)

Security rules
    * Token required. There is NO id in the URL: the profile is always looked up from
      request.user, so nobody can ask for someone else's details.
    * Only display fields are returned. No password, no document numbers, no wallet data.
    * A blocked customer gets 403 (same rule as user_login).

Example response (rider)
    {
      "success": true,
      "id": "6c1f...", "role": "rider", "full_name": "John Doe",
      "email": "john@example.com", "phone_number": "0241234567",
      "profile_photo": "https://...", "status": "approved",
      "created_at": "2026-11-01T10:00:00+00:00", ...rider extras...
    }
"""
import logging

from django.db import IntegrityError, transaction
from rest_framework import serializers
from rest_framework import status as http
from rest_framework.decorators import (
    api_view,
    parser_classes,
    permission_classes,
    throttle_classes,
)
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.throttling import UserRateThrottle

from .models import CustomerProfile, RiderProfile, Service, ServiceQualification
from .validators import clean_phone, email_is_taken, split_name
from .views import fail, ok, validation_fail

logger = logging.getLogger(__name__)

MAX_PHOTO_MB = 5

# What the frontend shows. Anything unknown falls back to "pending".
RIDER_STATUS = {
    "pending": "pending",
    "under_review": "under_review",
    "verified": "approved",
    "rejected": "rejected",
    "suspended": "suspended",
}


class ProfileThrottle(UserRateThrottle):
    scope = "profile"
    rate = "60/min"


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------
def _photo_url(request, field):
    """Absolute URL of an ImageField, or None. A storage problem must never break the profile."""
    try:
        if field and field.name:
            return request.build_absolute_uri(field.url)
    except Exception:  # noqa: BLE001
        logger.warning("Profile photo URL unavailable", exc_info=True)
    return None


def _rider_payload(request, rider):
    user = rider.user
    qualified = {q.service_id: q.status for q in rider.qualifications.all()}
    services = []
    for service in Service.objects.filter(is_active=True):
        approved = qualified.get(service.pk) == ServiceQualification.Status.APPROVED
        services.append({"key": service.pk, "approved": approved, "active": approved})

    status_value = RIDER_STATUS.get(str(rider.verification_status), "pending")
    return {
        "id": str(rider.id),
        "role": "rider",
        "rider_id": str(rider.id),
        "rider_code": rider.rider_code,
        "full_name": rider.full_name,
        "email": user.email or "",
        "phone_number": rider.phone,
        "profile_photo": _photo_url(request, rider.profile_photo),
        "status": status_value,
        "rejection_reason": (rider.rejection_reason or "") if status_value == "rejected" else "",
        "created_at": rider.created_at.isoformat(),
        # dashboard extras
        "rating": rider.rating_average,
        "completed_jobs": rider.completed_trips,
        "vehicle_plate": rider.motorbike_plate_number,
        "acceptance_rate": rider.acceptance_rate,
        "completion_rate": rider.completion_rate,
        "is_online": rider.is_online,
        "services": services,
    }


def _customer_payload(request, customer):
    user = customer.user
    return {
        "id": customer.pk,
        "role": "customer",
        "full_name": customer.full_name or user.get_full_name() or "",
        # CustomerProfile.email is not filled at registration; the User row is the source of truth.
        "email": user.email or customer.email or "",
        "phone_number": customer.phone or "",
        "profile_photo": _photo_url(request, customer.profile_photo),
        "status": "blocked" if customer.is_blocked else "active",
        "created_at": customer.created_at.isoformat(),
    }


# ---------------------------------------------------------------------------
# Input (PATCH)
# ---------------------------------------------------------------------------
INPUT_ALIASES = {
    "full_name": ("full_name", "name"),
    "phone": ("phone", "phone_number"),
    "email": ("email",),
}


class MeUpdateSerializer(serializers.Serializer):
    """context: user, profile, role ("rider" | "customer")."""

    full_name = serializers.CharField(max_length=150, required=False)
    phone = serializers.CharField(max_length=30, required=False, allow_blank=True)
    email = serializers.EmailField(required=False)
    profile_photo = serializers.ImageField(required=False)  # Pillow checks it is a real image

    def validate_full_name(self, value):
        value = " ".join(value.split())
        if len(value) < 2:
            raise serializers.ValidationError("Enter your full name.")
        return value

    def validate_email(self, value):
        value = value.strip().lower()
        if email_is_taken(value, exclude_user_id=self.context["user"].pk):
            raise serializers.ValidationError("An account with this email already exists.")
        return value

    def validate_phone(self, value):
        role, profile = self.context["role"], self.context["profile"]
        if not value.strip():
            if role == "rider":
                raise serializers.ValidationError("A phone number is required.")
            return ""
        try:
            phone = clean_phone(value)
        except Exception:  # noqa: BLE001
            raise serializers.ValidationError("Enter a valid phone number.") from None
        if not phone:
            raise serializers.ValidationError("Enter a valid phone number.")
        model = RiderProfile if role == "rider" else CustomerProfile
        if model.objects.filter(phone=phone).exclude(pk=profile.pk).exists():
            raise serializers.ValidationError("This phone number is already registered.")
        return phone

    def validate_profile_photo(self, f):
        if f.size > MAX_PHOTO_MB * 1024 * 1024:
            raise serializers.ValidationError(f"Photo too large. Maximum size is {MAX_PHOTO_MB} MB.")
        return f


def _collect_input(request):
    data = {}
    for field, keys in INPUT_ALIASES.items():
        for key in keys:
            if key in request.data:
                data[field] = request.data[key]
                break
    if "profile_photo" in request.FILES:
        data["profile_photo"] = request.FILES["profile_photo"]
    return data


# ---------------------------------------------------------------------------
# View
# ---------------------------------------------------------------------------
@api_view(["GET", "PATCH"])
@parser_classes([JSONParser, MultiPartParser, FormParser])
@permission_classes([IsAuthenticated])
@throttle_classes([ProfileThrottle])
def my_profile(request):
    rider = (
        RiderProfile.objects.select_related("user")
        .filter(user=request.user, deleted_at__isnull=True)
        .first()
    )
    customer = None
    if rider is None:
        customer = CustomerProfile.objects.select_related("user").filter(user=request.user).first()
    if rider is None and customer is None:
        return fail("Profile not found.", http.HTTP_404_NOT_FOUND)
    if customer is not None and customer.is_blocked:
        return fail("This account has been blocked. Contact support.", http.HTTP_403_FORBIDDEN)

    role = "rider" if rider else "customer"
    profile = rider or customer
    build = _rider_payload if rider else _customer_payload

    if request.method == "GET":
        return ok(**build(request, profile))

    serializer = MeUpdateSerializer(
        data=_collect_input(request),
        context={"user": profile.user, "profile": profile, "role": role},
        partial=True,
    )
    if not serializer.is_valid():
        return validation_fail(serializer)
    values = serializer.validated_data
    if not values:
        return fail("Nothing to update.")

    user = profile.user
    try:
        with transaction.atomic():
            user_fields = []
            if "full_name" in values:
                profile.full_name = values["full_name"]
                user.first_name, user.last_name = split_name(values["full_name"])
                user_fields += ["first_name", "last_name"]
            if "email" in values and values["email"] != (user.email or "").lower():
                old_email = (user.email or "").lower()
                user.email = values["email"]
                user_fields.append("email")
                # Accounts are created with username = email and login looks up the username,
                # so keep them in step or the new email could not be used to sign in.
                if (user.username or "").lower() == old_email:
                    user.username = values["email"]
                    user_fields.append("username")
                if customer is not None:
                    customer.email = values["email"]
            if "phone" in values:
                profile.phone = values["phone"] or None
            if "profile_photo" in values:
                profile.profile_photo = values["profile_photo"]
            if user_fields:
                user.save(update_fields=user_fields)
            profile.save()
    except IntegrityError:
        return fail("That phone number or email is already in use.", http.HTTP_409_CONFLICT)

    return ok("Profile updated.", **build(request, profile))

