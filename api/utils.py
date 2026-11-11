import re

def clean_phone(phone_number):
    """Clean and validate Ghanaian phone numbers"""
    if not phone_number:
        return ""
    
    # Keep only digits
    digits = re.sub(r"\D", "", phone_number)
    
    # Handle international format starting with +233 or 233
    if digits.startswith("233") and len(digits) == 12:
        return "0" + digits[3:]
    elif digits.startswith("0") and len(digits) == 10:
        return digits
    elif len(digits) == 9 and digits.startswith("2"):
        return "0" + digits
    else:
        # Return original if we can't normalize it — let validation handle it
        return phone_number
