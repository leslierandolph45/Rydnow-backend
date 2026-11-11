import re
from django.core.exceptions import ValidationError
from django.core.validators import RegexValidator
    
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



    # —————————————————————— #
    # Validation & persistence
    # —————————————————————— #
    def clean(self):
        errors = {}
        if self.date_of_birth:
            today = timezone.localdate()
            age = today.year - self.date_of_birth.year - (
                (today.month, today.day) < (self.date_of_birth.month, self.date_of_birth.day)
            )
            if age < MIN_AGE_YEARS:
                errors["date_of_birth"] = f"Riders must be at least {MIN_AGE_YEARS} years old."
        if (self.current_lat is None) != (self.current_lng is None):
            errors["current_lat"] = "Latitude and longitude must be set together."
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        # normalise identifiers so uniqueness checks are reliable
        self.license_number = (self.license_number or "").strip().upper()
        self.ghana_card_number = (self.ghana_card_number or "").strip().upper()
        self.motorbike_plate_number = (self.motorbike_plate_number or "").strip().upper()
        self.full_name = (self.full_name or "").strip()

        if self.verification_status == VerificationStatus.VERIFIED and not self.verified_at:
            self.verified_at = timezone.now()
        super().save(*args, **kwargs)

    def soft_delete(self):
        self.deleted_at = timezone.now()
        self.is_active = False
        self.is_online = False
        self.save(update_fields=["deleted_at", "is_active", "is_online", "updated_at"])



# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
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
