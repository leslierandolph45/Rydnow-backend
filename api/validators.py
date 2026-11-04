import re
from django.core.exceptions import ValidationError




def validate_phone_number(value):
    # Accepts: 0244XXXXXXX OR +233244XXXXXXX
    ghana_pattern = r'^(0[2-9][0-9]{8}|\+233[2-9][0-9]{9})$'
    if not re.match(ghana_pattern, str(value).strip()):
        raise ValidationError('Enter a valid Ghanaian number (e.g., 0244203327)')




def validate_non_negative_amount(value):
    if value < 0:
        raise ValidationError('Amount cannot be negative.')
