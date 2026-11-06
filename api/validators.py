from decimal import Decimal


from django.core.exceptions import ValidationError




def validate_non_negative_amount(value):
    if value is not None and value < Decimal('0'):
        raise ValidationError("Amount cannot be negative.")




def validate_positive_amount(value):
    if value is None or value <= Decimal('0'):
        raise ValidationError("Amount must be greater than zero.")




def validate_phone_number(value):
    if not value:
        return
    digits = ''.join(ch for ch in str(value) if ch.isdigit())
    if not 9 <= len(digits) <= 15:
        raise ValidationError("Phone number must contain 9-15 digits.")




def validate_latitude(value):
    if value is not None and not -90.0 <= float(value) <= 90.0:
        raise ValidationError("Latitude must be between -90 and 90 degrees.")




def validate_longitude(value):
    if value is not None and not -180.0 <= float(value) <= 180.0:
        raise ValidationError("Longitude must be between -180 and 180 degrees.")




DOCUMENT_TYPES = [
    ("ghana_card", "Ghana Card"),
    ("passport_photo", "Passport Photo"),
    ("license_doc", "Driver's License"),
]



