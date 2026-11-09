"""
Wallet & withdrawal configuration.

All monetary values are in GHS (Ghana Cedi).

Every value can be overridden per environment from Django settings using the
WALLET_ prefix, without touching this file, for example:

    # settings/production.py
    WALLET_WITHDRAWAL_FEE = "0.50"
    WALLET_DAILY_WITHDRAWAL_LIMIT = "5000.00"
    WALLET_MAX_PENDING_WITHDRAWALS = 2
"""
import re
from decimal import ROUND_HALF_UP, Decimal

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured


# ————————————————————————— #
# Internal helpers
# ————————————————————————— #
def _money(name: str, default: str) -> Decimal:
    """Read WALLET_<name> from settings as a 2dp Decimal (strings are safest)."""
    raw = getattr(settings, f"WALLET_{name}", default)
    try:
        return Decimal(str(raw)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except Exception as exc:
        raise ImproperlyConfigured(f"WALLET_{name} must be a valid amount, got {raw!r}") from exc


def _int(name: str, default: int) -> int:
    raw = getattr(settings, f"WALLET_{name}", default)
    try:
        return int(raw)
    except (TypeError, ValueError) as exc:
        raise ImproperlyConfigured(f"WALLET_{name} must be an integer, got {raw!r}") from exc


def _tuple(name: str, default: tuple) -> tuple:
    raw = getattr(settings, f"WALLET_{name}", default)
    return tuple(str(v).strip().lower() for v in raw)


# ————————————————————————— #
# Constants
# ————————————————————————— #
ZERO = Decimal("0.00")
TWOPLACES = Decimal("0.01")

# ————————————————————————— #
# Currency
# ————————————————————————— #
CURRENCY = getattr(settings, "WALLET_CURRENCY", "GHS")
CURRENCY_SYMBOL = getattr(settings, "WALLET_CURRENCY_SYMBOL", "GH₵")

# ————————————————————————— #
# Amount limits
# ————————————————————————— #
MIN_AMOUNT = _money("MIN_AMOUNT", "1.00")                                  # per withdrawal
MAX_WITHDRAWAL_AMOUNT = _money("MAX_WITHDRAWAL_AMOUNT", "5000.00")         # per withdrawal
DAILY_WITHDRAWAL_LIMIT = _money("DAILY_WITHDRAWAL_LIMIT", "10000.00")      # per calendar day

# ————————————————————————— #
# Balance rules
# ————————————————————————— #
# Reserve that must always remain in the wallet (cannot be withdrawn)
MIN_WALLET_BALANCE = _money("MIN_WALLET_BALANCE", "0.00")

# ————————————————————————— #
# Fees
# ————————————————————————— #
# Flat fee per withdrawal. Set e.g. "0.50" when ready to charge.
WITHDRAWAL_FEE = _money("WITHDRAWAL_FEE", "0.00")

# Optional percentage fee (0.01 = 1%). Applied on top of the flat fee.
WITHDRAWAL_FEE_PERCENT = Decimal(str(getattr(settings, "WALLET_WITHDRAWAL_FEE_PERCENT", "0")))

# ————————————————————————— #
# Risk controls
# ————————————————————————— #
MAX_PENDING_WITHDRAWALS = _int("MAX_PENDING_WITHDRAWALS", 3)

# Withdrawals at or above this amount should go to manual (admin) review.
# Set to None to disable manual review.
MANUAL_REVIEW_THRESHOLD = getattr(settings, "WALLET_MANUAL_REVIEW_THRESHOLD", None)
if MANUAL_REVIEW_THRESHOLD is not None:
    MANUAL_REVIEW_THRESHOLD = _money("MANUAL_REVIEW_THRESHOLD", str(MANUAL_REVIEW_THRESHOLD))

# Minimum minutes between two withdrawal requests from the same wallet (0 = off)
WITHDRAWAL_COOLDOWN_MINUTES = _int("WALLET_COOLDOWN_MINUTES", 0)

# ————————————————————————— #
# Methods & providers
# ————————————————————————— #
WITHDRAWAL_METHODS = _tuple("WITHDRAWAL_METHODS", ("momo", "mobile_money", "bank_transfer"))

# Methods that pay out to a mobile wallet
MOMO_METHODS = ("momo", "mobile_money")

MOMO_PROVIDERS = _tuple("MOMO_PROVIDERS", ("mtn", "telecel", "at"))

# Accepted spellings -> canonical provider code
PROVIDER_ALIASES = {
    "mtn": "mtn",
    "mtn momo": "mtn",
    "mtn_momo": "mtn",
    "vodafone": "telecel",   # Vodafone Ghana rebranded to Telecel
    "vodafone cash": "telecel",
    "telecel": "telecel",
    "telecel cash": "telecel",
    "at": "at",
    "airteltigo": "at",
    "airtel tigo": "at",
    "airtel_tigo": "at",
}

# Ghana mobile number: 10 digits, leading 0
MOMO_NUMBER_RE = re.compile(r"^0\d{9}$")

# Network prefixes (first 3 digits). Verify against current NCA allocations
# before enabling strict matching, as ranges change over time.
MOMO_PREFIXES = {
    "mtn": ("024", "025", "053", "054", "055", "059"),
    "telecel": ("020", "050"),
    "at": ("026", "027", "056", "057"),
}
STRICT_PROVIDER_PREFIX_CHECK = bool(getattr(settings, "WALLET_STRICT_PROVIDER_PREFIX_CHECK", False))


# ————————————————————————— #
# Helpers
# ————————————————————————— #
def normalize_provider(provider: str) -> str:
    """'MTN MoMo' -> 'mtn'. Returns the cleaned input if no alias matches."""
    key = " ".join((provider or "").lower().split())
    return PROVIDER_ALIASES.get(key, key)


def momo_number_matches_provider(account_number: str, provider: str) -> bool:
    """True if the number's prefix belongs to the provider (or strict check is off)."""
    if not STRICT_PROVIDER_PREFIX_CHECK:
        return True
    return account_number[:3] in MOMO_PREFIXES.get(normalize_provider(provider), ())


def calculate_withdrawal_fee(amount: Decimal) -> Decimal:
    """Flat fee + optional percentage fee, rounded to 2dp."""
    fee = WITHDRAWAL_FEE + (Decimal(amount) * WITHDRAWAL_FEE_PERCENT)
    return fee.quantize(TWOPLACES, rounding=ROUND_HALF_UP)


def format_money(amount: Decimal) -> str:
    """Decimal('1234.5') -> 'GH₵1,234.50'."""
    return f"{CURRENCY_SYMBOL}{Decimal(amount).quantize(TWOPLACES):,.2f}"


def requires_manual_review(amount: Decimal) -> bool:
    return MANUAL_REVIEW_THRESHOLD is not None and Decimal(amount) >= MANUAL_REVIEW_THRESHOLD


# ————————————————————————— #
# Startup validation: fail fast on a bad config instead of at withdrawal time
# ————————————————————————— #
def _validate() -> None:
    errors = []

    if MIN_AMOUNT <= ZERO:
        errors.append("MIN_AMOUNT must be greater than zero.")
    if MAX_WITHDRAWAL_AMOUNT < MIN_AMOUNT:
        errors.append("MAX_WITHDRAWAL_AMOUNT must be >= MIN_AMOUNT.")
    if DAILY_WITHDRAWAL_LIMIT < MAX_WITHDRAWAL_AMOUNT:
        errors.append("DAILY_WITHDRAWAL_LIMIT must be >= MAX_WITHDRAWAL_AMOUNT.")
    if MIN_WALLET_BALANCE < ZERO:
        errors.append("MIN_WALLET_BALANCE cannot be negative.")
    if WITHDRAWAL_FEE < ZERO:
        errors.append("WITHDRAWAL_FEE cannot be negative.")
    if not (ZERO <= WITHDRAWAL_FEE_PERCENT < Decimal("1")):
        errors.append("WITHDRAWAL_FEE_PERCENT must be between 0 and 1 (e.g. 0.01 = 1%).")
    if MAX_PENDING_WITHDRAWALS < 1:
        errors.append("MAX_PENDING_WITHDRAWALS must be at least 1.")
    if WITHDRAWAL_COOLDOWN_MINUTES < 0:
        errors.append("WITHDRAWAL_COOLDOWN_MINUTES cannot be negative.")
    if not WITHDRAWAL_METHODS:
        errors.append("WITHDRAWAL_METHODS cannot be empty.")
    if MANUAL_REVIEW_THRESHOLD is not None and MANUAL_REVIEW_THRESHOLD < MIN_AMOUNT:
        errors.append("MANUAL_REVIEW_THRESHOLD must be >= MIN_AMOUNT.")

    if errors:
        raise ImproperlyConfigured("Invalid wallet configuration:\n - " + "\n - ".join(errors))


_validate()
