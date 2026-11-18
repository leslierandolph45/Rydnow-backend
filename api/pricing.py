"""
pricing.py - the ONLY place trip prices are computed. No views, no DB access.

    ride_fare = max((base + distance_fare) * surge, minimum_fare)
    gross     = ride_fare + stop_fees + waiting + booking_fee
    total     = gross - discount

Money flow per trip:
    trip_earnings    = ride_fare + stop_fees + waiting     (the rider's side)
    commission       = trip_earnings * commission_rate     (company revenue)
    booking_fee      = flat fee, 100% company revenue
    discount         = promo, funded by the company, never by the rider
    platform_revenue = commission + booking_fee - discount
    total            = rider_earnings + platform_revenue

Anti-tamper design:
  * Clients never send an amount. A quote is a signed, expiring, user-bound token;
    booking creation trusts the token, never the request body.
  * The token signs the service and route, so a quote for 2 km cannot book 20 km.
  * Surge, commission and fees are never accepted from the client.

Service keys
  Pricing, Service and ServiceQualification use: okada | parcel | waste-bin.
  Booking.service_type stores:                    ride  | delivery | waste_collection.
  normalize_service_type() accepts either spelling and returns the pricing key;
  booking_service_value() / service_key_for_booking() convert between the two.
"""
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from math import asin, ceil, cos, radians, sin, sqrt
from zoneinfo import ZoneInfo

from django.conf import settings
from django.core import signing
from django.core.cache import cache


logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
CURRENCY = "GHS"
# On Windows, zoneinfo needs the `tzdata` package (add it to requirements.txt).
LOCAL_TZ = ZoneInfo("Africa/Accra")
EARTH_RADIUS_KM = 6371.0088
MONEY = Decimal("0.01")
COORD = Decimal("0.000001")
ONE = Decimal("1.0")
ZERO = Decimal("0.00")

ROAD_DISTANCE_FACTOR = Decimal("1.25")
MAX_DISTANCE_KM = Decimal("100")
# Long enough for a customer to review the price, pick a payment method and confirm.
QUOTE_VALIDITY = timedelta(minutes=getattr(settings, "PRICING_QUOTE_VALIDITY_MINUTES", 5))
MAX_WAIT_INPUT_MINUTES = 240
QUOTE_SALT = "rydnow.pricing.quote.v1"

_REQUIRED_QUOTE_KEYS = frozenset({"qid", "uid", "svc", "route", "fare"})


class QuoteError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


# ---------------------------------------------------------------------------
# Small safe helpers
# ---------------------------------------------------------------------------
def _dec(value, default: Decimal = ZERO) -> Decimal:
    """Any number-like -> finite Decimal; anything unusable -> default."""
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return default
    return result if result.is_finite() else default


def _aware(value: datetime) -> datetime:
    """Naive datetimes are treated as Africa/Accra local time."""
    return value if value.tzinfo is not None else value.replace(tzinfo=LOCAL_TZ)


def money(value: Decimal) -> Decimal:
    return value.quantize(MONEY, rounding=ROUND_HALF_UP)


# ---------------------------------------------------------------------------
# Rate configuration
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Rate:
    base: Decimal
    per_km: Decimal
    minimum: Decimal
    surge_enabled: bool
    max_surge: Decimal
    stop_fee: Decimal
    wait_per_min: Decimal
    free_wait_minutes: int
    max_wait_minutes: int
    max_stops: int
    booking_fee: Decimal          # flat platform fee per trip, never surged
    commission_rate: Decimal      # share of trip earnings kept by the company
    cancel_grace_minutes: int     # free cancellation window after acceptance
    cancel_fee_accepted: Decimal  # fee if cancelled after the grace window
    cancel_fee_arrived: Decimal   # fee if cancelled after the rider arrived


# Keys MUST equal SERVICE_CHOICES in models.py (models.py asserts this at import).
RATES = {
    "okada": Rate(
        base=Decimal("5.00"), per_km=Decimal("1.50"), minimum=Decimal("20.00"),
        surge_enabled=True, max_surge=Decimal("2.0"),
        stop_fee=Decimal("2.00"), wait_per_min=Decimal("0.50"),
        free_wait_minutes=3, max_wait_minutes=15, max_stops=2,
        booking_fee=Decimal("1.00"), commission_rate=Decimal("0.15"),
        cancel_grace_minutes=2,
        cancel_fee_accepted=Decimal("3.00"), cancel_fee_arrived=Decimal("5.00"),
    ),
    "parcel": Rate(
        base=Decimal("8.00"), per_km=Decimal("2.00"), minimum=Decimal("35.00"),
        surge_enabled=True, max_surge=Decimal("1.5"),
        stop_fee=Decimal("3.00"), wait_per_min=Decimal("0.60"),
        free_wait_minutes=5, max_wait_minutes=20, max_stops=3,
        booking_fee=Decimal("1.50"), commission_rate=Decimal("0.18"),
        cancel_grace_minutes=2,
        cancel_fee_accepted=Decimal("4.00"), cancel_fee_arrived=Decimal("6.00"),
    ),
    "waste-bin": Rate(
        base=Decimal("15.00"), per_km=Decimal("2.50"), minimum=Decimal("50.00"),
        surge_enabled=False, max_surge=ONE,
        stop_fee=Decimal("5.00"), wait_per_min=Decimal("1.00"),
        free_wait_minutes=5, max_wait_minutes=30, max_stops=2,
        booking_fee=Decimal("2.00"), commission_rate=Decimal("0.20"),
        cancel_grace_minutes=5,
        cancel_fee_accepted=Decimal("10.00"), cancel_fee_arrived=Decimal("15.00"),
    ),
}

# ---------------------------------------------------------------------------
# Service key mapping (pricing key <-> Booking.service_type value)
# ---------------------------------------------------------------------------
BOOKING_SERVICE_VALUES = {"okada": "ride", "parcel": "delivery", "waste-bin": "waste_collection"}
_KEY_FOR_BOOKING_VALUE = {v: k for k, v in BOOKING_SERVICE_VALUES.items()}
SERVICE_ALIASES = {
    "okada": "okada", "ride": "okada", "okada_ride": "okada",
    "parcel": "parcel", "delivery": "parcel",
    "waste-bin": "waste-bin", "waste_bin": "waste-bin",
    "waste_collection": "waste-bin", "waste-collection": "waste-bin", "waste": "waste-bin",
}


def normalize_service_type(value) -> str:
    """Any accepted spelling -> pricing key. Raises ValueError if unknown."""
    key = SERVICE_ALIASES.get(str(value or "").strip().lower())
    if key is None or key not in RATES:
        raise ValueError(f"Unknown service type: {value!r}")
    return key


def booking_service_value(key: str) -> str:
    """Pricing key -> value stored in Booking.service_type."""
    return BOOKING_SERVICE_VALUES[key]


def service_key_for_booking(value: str) -> str:
    """Booking.service_type value -> pricing key."""
    return _KEY_FOR_BOOKING_VALUE[value]


# ---------------------------------------------------------------------------
# Surge
# ---------------------------------------------------------------------------
DEMAND_TIERS = (
    (Decimal("3.0"), Decimal("2.0")),
    (Decimal("2.0"), Decimal("1.8")),
    (Decimal("1.5"), Decimal("1.5")),
    (Decimal("1.0"), Decimal("1.2")),
)
PEAK_WINDOWS = (
    (time(6, 30), time(9, 0)),
    (time(16, 30), time(19, 30)),
)
PEAK_MULTIPLIER = Decimal("1.2")


@dataclass(frozen=True)
class DemandSnapshot:
    open_requests: int
    available_riders: int

    @property
    def ratio(self) -> Decimal:
        return Decimal(self.open_requests) / Decimal(max(self.available_riders, 1))


@dataclass(frozen=True)
class SurgeResult:
    multiplier: Decimal
    reason: str  # none | demand | peak_hours | override | disabled

    @property
    def active(self) -> bool:
        return self.multiplier > ONE


def get_demand_snapshot(service_type: str) -> DemandSnapshot:
    """Reads counters kept by dispatch:
    demand:<service>:open_requests / demand:<service>:available_riders"""
    return DemandSnapshot(
        open_requests=int(cache.get(f"demand:{service_type}:open_requests", 0)),
        available_riders=int(cache.get(f"demand:{service_type}:available_riders", 0)),
    )


def _quantize_multiplier(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)


def _is_peak(now: datetime) -> bool:
    local = _aware(now).astimezone(LOCAL_TZ)
    if local.weekday() >= 5:
        return False
    return any(start <= local.time() < end for start, end in PEAK_WINDOWS)


def _demand_multiplier(snapshot: DemandSnapshot) -> Decimal:
    for threshold, multiplier in DEMAND_TIERS:
        if snapshot.ratio >= threshold:
            return multiplier
    return ONE


def resolve_surge(service_type: str, now: datetime | None = None) -> SurgeResult:
    rate = RATES[service_type]
    if not rate.surge_enabled:
        return SurgeResult(ONE, "disabled")
    if now is None:
        now = datetime.now(LOCAL_TZ)
    try:
        override = cache.get(f"surge:override:{service_type}")
        if override is not None:
            value = min(max(Decimal(str(override)), ONE), rate.max_surge)
            return SurgeResult(_quantize_multiplier(value), "override")

        demand = _demand_multiplier(get_demand_snapshot(service_type))
        peak = PEAK_MULTIPLIER if _is_peak(now) else ONE
        multiplier = min(max(demand, peak), rate.max_surge)
        if multiplier <= ONE:
            return SurgeResult(ONE, "none")
        reason = "demand" if demand >= peak else "peak_hours"
        return SurgeResult(_quantize_multiplier(multiplier), reason)
    except (InvalidOperation, ValueError, TypeError):
        logger.exception("Surge calculation failed for %s; using 1.0x", service_type)
    except Exception:  # cache backend down etc. - never block a quote
        logger.exception("Surge lookup unavailable for %s; using 1.0x", service_type)
    return SurgeResult(ONE, "none")


# ---------------------------------------------------------------------------
# Geometry helpers
# ---------------------------------------------------------------------------
def coord_str(value) -> str:
    return format(_dec(value).quantize(COORD, rounding=ROUND_HALF_UP), "f")


def haversine_km(lat1, lng1, lat2, lng2) -> float:
    lat1, lng1, lat2, lng2 = (radians(float(v)) for v in (lat1, lng1, lat2, lng2))
    a = sin((lat2 - lat1) / 2) ** 2 + cos(lat1) * cos(lat2) * sin((lng2 - lng1) / 2) ** 2
    return 2 * EARTH_RADIUS_KM * asin(sqrt(a))


def build_route(pickup, stops, destination):
    """Build route coordinates, accepting both 'lat/lng' and 'latitude/longitude' key styles."""
    def get_coords(point):
        lat = point.get("lat") or point.get("latitude")
        lng = point.get("lng") or point.get("longitude")
        if lat is None or lng is None:
            raise ValueError(f"Missing coordinates in point: {point}")
        return (float(lat), float(lng))
    
    route = [get_coords(pickup)]
    for stop in stops:
        route.append(get_coords(stop))
    route.append(get_coords(destination))
    return route



def route_distance_km(points) -> Decimal:
    straight = sum(haversine_km(a[0], a[1], b[0], b[1]) for a, b in zip(points, points[1:]))
    return money(Decimal(str(straight)) * ROAD_DISTANCE_FACTOR)


def route_signature(route) -> list:
    return [[coord_str(a), coord_str(b)] for a, b in route]


# ---------------------------------------------------------------------------
# Promo terms (pure data; eligibility is validated in booking_services.py)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class PromoTerms:
    code: str
    percent_off: Decimal = ZERO
    flat_off: Decimal = ZERO
    max_discount: Decimal | None = None


def _promo_discount(promo, ride_fare: Decimal, ceiling: Decimal) -> Decimal:
    """Applies to the ride fare only, and never exceeds the company's own margin
    on the trip (commission + booking fee), so riders are never short-paid."""
    if promo is None:
        return ZERO
    percent = max(_dec(getattr(promo, "percent_off", None)), ZERO)
    flat = max(_dec(getattr(promo, "flat_off", None)), ZERO)
    raw = ride_fare * percent / Decimal(100) + flat
    cap = getattr(promo, "max_discount", None)
    if cap is not None:
        raw = min(raw, max(_dec(cap), ZERO))
    return money(max(min(raw, ride_fare, ceiling), ZERO))


# ---------------------------------------------------------------------------
# Fare calculation
# ---------------------------------------------------------------------------
def calculate_waiting(service_type: str, wait_minutes: list) -> dict:
    """Billable minutes per stop = ceil(min(wait, max_wait)) - free grace, >= 0."""
    rate = RATES[service_type]
    per_stop = []
    for index, wait in enumerate(wait_minutes, start=1):
        waited = max(_dec(wait), ZERO)
        counted = min(waited, Decimal(rate.max_wait_minutes))
        billable = max(ceil(counted) - rate.free_wait_minutes, 0)
        per_stop.append({
            "stop": index,
            "wait_minutes": waited,
            "billable_minutes": billable,
            "charge": money(Decimal(billable) * rate.wait_per_min),
        })
    return {
        "stops": per_stop,
        "billable_minutes": sum(s["billable_minutes"] for s in per_stop),
        "charge": sum((s["charge"] for s in per_stop), ZERO),
    }


def max_billable_wait_minutes(service_type: str) -> int:
    rate = RATES[service_type]
    return max(rate.max_wait_minutes - rate.free_wait_minutes, 0)


def calculate_fare(service_type, distance_km, surge, wait_minutes, promo=None) -> dict:
    rate = RATES[service_type]

    distance_km = max(_dec(distance_km), ZERO)
    distance_fare = money(distance_km * rate.per_km)
    subtotal = money(rate.base + distance_fare)
    surge_amount = money(subtotal * (surge.multiplier - ONE))
    ride_fare = money(max(subtotal + surge_amount, rate.minimum))

    stop_fees = money(rate.stop_fee * len(wait_minutes))
    waiting = calculate_waiting(service_type, wait_minutes)

    trip_earnings = ride_fare + stop_fees + waiting["charge"]
    commission = money(trip_earnings * rate.commission_rate)
    discount = _promo_discount(promo, ride_fare, commission + rate.booking_fee)
    total = money(trip_earnings + rate.booking_fee - discount)

    return {
        "base_fare": rate.base,
        "distance_fare": distance_fare,
        "subtotal": subtotal,
        "surge_amount": surge_amount,
        "minimum_fare": rate.minimum,
        "minimum_applied": (subtotal + surge_amount) < rate.minimum,
        "ride_fare": ride_fare,
        "stop_fees": stop_fees,
        "waiting": waiting,
        "waiting_charge": waiting["charge"],
        "booking_fee": rate.booking_fee,
        "discount": discount,
        "promo_code": promo.code if promo else "",
        "total": total,
        "commission_rate": rate.commission_rate,
        "platform_commission": commission,
        "rider_earnings": money(trip_earnings - commission),
        "platform_revenue": money(commission + rate.booking_fee - discount),
    }


_SNAPSHOT_MONEY = (
    "base_fare", "distance_fare", "subtotal", "surge_amount", "minimum_fare",
    "ride_fare", "stop_fees", "waiting_charge", "booking_fee", "discount",
    "total", "platform_commission", "rider_earnings", "platform_revenue",
)


def fare_snapshot(fare: dict, surge: SurgeResult) -> dict:
    """Flat, JSON-safe record of a fare (Decimals as strings)."""
    snap = {k: str(fare[k]) for k in _SNAPSHOT_MONEY}
    snap.update({
        "surge_multiplier": str(surge.multiplier),
        "surge_reason": surge.reason,
        "minimum_applied": fare["minimum_applied"],
        "commission_rate": str(fare["commission_rate"]),
        "promo_code": fare["promo_code"],
        "waiting_billable_minutes": fare["waiting"]["billable_minutes"],
    })
    return snap


def finalize_fare(service_type: str, snapshot: dict, wait_minutes: list) -> dict:
    """Re-price a completed trip from SERVER-measured waits. Ride fare, stop fees,
    surge and commission rate stay as quoted. Not wired in yet: BookingStop has no
    arrival/departure timestamps to measure waits from."""
    waiting = calculate_waiting(service_type, wait_minutes)
    ride_fare = Decimal(snapshot["ride_fare"])
    stop_fees = Decimal(snapshot["stop_fees"])
    booking_fee = Decimal(snapshot["booking_fee"])

    trip_earnings = ride_fare + stop_fees + waiting["charge"]
    commission = money(trip_earnings * Decimal(snapshot["commission_rate"]))
    discount = min(Decimal(snapshot["discount"]), commission + booking_fee)
    total = money(trip_earnings + booking_fee - discount)

    final = dict(snapshot)
    final.update({
        "waiting_charge": str(waiting["charge"]),
        "waiting_billable_minutes": waiting["billable_minutes"],
        "discount": str(discount),
        "total": str(total),
        "platform_commission": str(commission),
        "rider_earnings": str(money(trip_earnings - commission)),
        "platform_revenue": str(money(commission + booking_fee - discount)),
    })
    return final


def cancellation_fee(service_type: str, *, status: str, accepted_at, now) -> Decimal:
    """Fee a CUSTOMER owes for cancelling after a rider has committed.
    `status` uses Booking.Status values; only "accepted" and "arrived" carry a fee."""
    rate = RATES[service_type]
    if status == "arrived":
        return rate.cancel_fee_arrived
    if status == "accepted" and accepted_at is not None:
        if _aware(now) - _aware(accepted_at) > timedelta(minutes=rate.cancel_grace_minutes):
            return rate.cancel_fee_accepted
    return ZERO


# ---------------------------------------------------------------------------
# Signed quotes
# ---------------------------------------------------------------------------
def _signing_key() -> str:
    return getattr(settings, "PRICING_SIGNING_KEY", None) or settings.SECRET_KEY


def issue_quote(*, user_id, service_type, route, wait_minutes, distance_km, surge, fare):
    qid = uuid.uuid4().hex
    payload = {
        "qid": qid,
        "uid": str(user_id),
        "svc": service_type,
        "route": route_signature(route),
        "waits": [str(_dec(w)) for w in wait_minutes],
        "dist": str(distance_km),
        "fare": fare_snapshot(fare, surge),
    }
    token = signing.dumps(payload, key=_signing_key(), salt=QUOTE_SALT, compress=True)
    return token, qid


def read_quote(token: str, *, user_id) -> dict:
    try:
        data = signing.loads(
            token, key=_signing_key(), salt=QUOTE_SALT,
            max_age=int(QUOTE_VALIDITY.total_seconds()),
        )
    except signing.SignatureExpired:
        raise QuoteError("quote_expired", "This price has expired. Please request a new quote.") from None
    except signing.BadSignature:
        raise QuoteError("quote_invalid", "Invalid price quote.") from None
    if not isinstance(data, dict) or not _REQUIRED_QUOTE_KEYS <= data.keys():
        raise QuoteError("quote_invalid", "Invalid price quote.")
    if data.get("uid") != str(user_id):
        raise QuoteError("quote_invalid", "Invalid price quote.")
    return data


def quote_matches_trip(data: dict, *, service_type: str, route) -> bool:
    """True only if the quote was issued for exactly this service and route."""
    return data.get("svc") == service_type and data.get("route") == route_signature(route)

