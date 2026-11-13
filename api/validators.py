"""
RYDNOW validators and shared helpers.

Self-contained: imports nothing from models.py or serializers.py, so it can be
imported from either without creating a circular import.
"""
import re

from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.validators import RegexValidator
from django.core.validators import validate_email as django_validate_email
from django.db.models import Q
from rest_framework import serializers


# ---------------------------------------------------------------------------
# Validators
# ---------------------------------------------------------------------------
# Accepts: 0244XXXXXXX  OR  +233244XXXXXXX
GHANA_PHONE_PATTERN = r"^(0[2-9]\d{8}|\+233[2-9]\d{8})$"


def validate_phone_number(value):
    if not re.match(GHANA_PHONE_PATTERN, str(value).strip()):
        raise ValidationError("Enter a valid Ghanaian number (e.g., 0244203327)")


ghana_card_validator = RegexValidator(
    regex=r"^[A-Z0-9]{8,12}$",
    message="Enter a valid Ghana Card number.",
)

# Canonical stored format: +233 followed by 9 digits
phone_validator = RegexValidator(
    regex=r"^\+233\d{9}$",
    message="Enter a valid Ghanaian phone number: +233XXXXXXXXX",
)


def validate_non_negative_amount(value):
    if value < 0:
        raise ValidationError("Amount cannot be negative.")


def validate_email(value):
    """Validate email format."""
    try:
        django_validate_email(value)
    except ValidationError:
        raise ValidationError("Enter a valid email address.")
    return value


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def normalize_gh_phone(value):
    """Best-effort normalise a Ghana number to +233XXXXXXXXX.
    Anything it cannot recognise is returned cleaned but unchanged, so
    phone_validator rejects it with a normal validation message."""
    phone = re.sub(r"[\s\-().]", "", str(value or ""))
    if phone.startswith("+233"):
        return phone
    if phone.startswith("233"):
        return "+" + phone
    if phone.startswith("0") and len(phone) == 10:
        return "+233" + phone[1:]
    return phone


def clean_phone(value: str) -> str:
    """Normalize phone number — keep digits and leading + only"""
    import re
    digits = re.sub(r'[\s\-\(\)]+', '', value)
    if not digits.startswith('+'):
        digits = '+' + digits
    return digits



def check_password_strength(password, user=None):
    try:
        validate_password(password, user=user)
    except ValidationError as exc:
        raise serializers.ValidationError(list(exc.messages))
    return password


def split_name(full_name: str) -> tuple [str, str]:
    """Split full name into first_name, last_name"""
    parts = full_name.strip().split(maxsplit=1)
    if len(parts) == 1:
        return parts [0], ""
    return parts [0], parts [1]



def email_is_taken(email: str, exclude_user_id=None) -> bool:
    """Check if email exists (excluding optional user ID)"""
    from django.contrib.auth import get_user_model
    User = get_user_model()
    qs = User.objects.filter(email__iexact=email)
    if exclude_user_id:
        qs = qs.exclude(pk=exclude_user_id)
    return qs.exists()


