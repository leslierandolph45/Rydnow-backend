"""Pure helper functions (no database access)."""
import os
import uuid
from decimal import Decimal, ROUND_HALF_UP
from math import asin, cos, radians, sin, sqrt

from .constants import COMMISSION_RATE

EARTH_RADIUS_KM = 6371


# ── Money ───────────────────────────────────────────────────────
def round_money(value):
    """Consistent 2-decimal rounding — ALL financial math goes through this."""
    return Decimal(str(value)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)


def split_fare(total_fare):
    """
    Split a fare into (platform_commission, rider_payout).
    The payout is derived by subtraction so the two parts always sum
    to the fare exactly — no stray pesewa from independent rounding.
    """
    total = round_money(total_fare)
    commission = round_money(total * COMMISSION_RATE)
    return commission, total - commission


# ── References ──────────────────────────────────────────────────
def _make_ref(prefix, length):
    return f"{prefix}-{uuid.uuid4().hex[:length].upper()}"


def generate_transaction_ref():
    return _make_ref('TXN', 12)


def generate_booking_ref():
    return _make_ref('BK', 12)


def generate_withdrawal_ref():
    return _make_ref('WD', 10)


def generate_payment_ref():
    return _make_ref('PAY', 10)


# ── Geo ─────────────────────────────────────────────────────────
def _clamp(value, low, high):
    return max(low, min(high, float(value)))


def haversine_km(lat1, lng1, lat2, lng2):
    """Great-circle distance in km, or None if any coordinate is missing."""
    if None in (lat1, lng1, lat2, lng2):
        return None
    lat1, lat2 = _clamp(lat1, -90, 90), _clamp(lat2, -90, 90)
    lng1, lng2 = _clamp(lng1, -180, 180), _clamp(lng2, -180, 180)
    dlat, dlng = radians(lat2 - lat1), radians(lng2 - lng1)
    a = sin(dlat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlng / 2) ** 2
    a = max(0.0, min(1.0, a))
    return round(EARTH_RADIUS_KM * 2 * asin(sqrt(a)), 2)


# ── Files ───────────────────────────────────────────────────────
def get_safe_upload_path(instance, filename):
    """Unique, extension-preserving upload path — prevents path traversal."""
    ext = os.path.splitext(filename)[1].lower() or '.bin'
    return f"rider_docs/{uuid.uuid4().hex[:8]}/{uuid.uuid4().hex}{ext}"
