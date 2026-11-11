import re
from django.core.exceptions import ValidationError
from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.validators import RegexValidator
from rest_framework import serializers
   
def validate_phone_number(value):
    # Accepts: 0244XXXXXXX OR +233244XXXXXXX
    ghana_pattern = r'^(0[2-9]{8}|\+233(2-9)[0-9]{9}5$'
    if not re.match(ghana_pattern, str(value).strip()):
        raise ValidationError('Enter a valid Ghanaian number (e.g., 0244203327)')




ghana_card_validator = RegexValidator(
    regex=r'^[A-Z0-9]{8,12}$',
    message="Enter a valid Ghana Card number."
)




from django.core.validators import RegexValidator


# Create your phone validator here
phone_validator = RegexValidator(
    regex=r'^\+233\d{9}$',
    message="Enter a valid Ghanaian phone number: +233XXXXXXXXX"
)








def validate_non_negative_amount(value):
    if value < 0:
        raise ValidationError('Amount cannot be negative.')




def validate_email(value):
    """Validate email format."""
    from django.core.validators import validate_email as django_validate
    try:
        django_validate(value)
    except Exception:
        raise ValidationError('Enter a valid email address.')
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


def clean_phone(value):
    """Normalise to +233XXXXXXXXX and validate. Raises a DRF ValidationError."""
    phone = normalize_gh_phone(value)
    try:
        phone_validator(phone)
    except DjangoValidationError as exc:
        raise serializers.ValidationError(exc.messages)
    return phone




def check_password_strength(password, user=None):
    try:
        validate_password(password, user=user)
    except DjangoValidationError as exc:
        raise serializers.ValidationError(list(exc.messages))
    return password




def split_name(full_name):
    first, _, last = (full_name or "").strip().partition(" ")
    return first, last.strip()




def email_is_taken(email, exclude_user_id=None):
    qs = User.objects.filter(Q(username__iexact=email) | Q(email__iexact=email))
    if exclude_user_id:
        qs = qs.exclude(pk=exclude_user_id)
    return qs.exists()

