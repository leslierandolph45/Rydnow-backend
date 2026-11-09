"""
Wallet service layer: the ONLY place balances change.

Pattern for every operation: open a transaction, lock the wallet row, THEN check
rules, THEN write. Every ledger row has a deterministic reference, so retries,
double taps and webhook replays are harmless.
"""
import logging
import uuid
import re
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, transaction
from django.db.models import Sum
from django.utils import timezone

# ✅ CORRECT IMPORTS — EVERYTHING FROM YOUR EXISTING models.py
from .models import RiderWallet, WalletTransaction, WithdrawalRequest

# ✅ ALIASES MATCHING YOUR CODE
WT = WalletTransaction
WDR = WithdrawalRequest

# ✅ CONFIG — no more missing wallet_conf!
MIN_AMOUNT = Decimal("1.00")
MIN_WALLET_BALANCE = Decimal("0.00")
WITHDRAWAL_FEE = Decimal("0.00")
WITHDRAWAL_METHODS = ["mobile_money"]
MAX_WITHDRAWAL_AMOUNT = Decimal("5000.00")
DAILY_WITHDRAWAL_LIMIT = Decimal("10000.00")
MAX_PENDING_WITHDRAWALS = 3
MAX_PENDING = MAX_PENDING_WITHDRAWALS
DAILY_LIMIT = DAILY_WITHDRAWAL_LIMIT
MAX_AMOUNT = MAX_WITHDRAWAL_AMOUNT
ZERO = Decimal("0.00")
TWOPLACES = Decimal("0.01")
GH_MOMO_RE = re.compile(r"^0[2-5][0-9]{7,8}$")

logger = logging.getLogger(__name__)


# ==========================================
# ✅ WALLET ERROR — unified, no duplicates
# ==========================================
class WalletError(Exception):
    """Business-rule failure; views turn it into a 400/403/409 response."""
    def __init__(self, message: str, code: str, status: int = 400):
        super().__init__(message)
        self.message = message
        self.code = code
        self.status = status


# ==========================================
# ✅ HELPERS — unified
# ==========================================
def round_money(value):
    return Decimal(str(value)).quantize(TWOPLACES, rounding=ROUND_HALF_UP)


def _to_money(value) -> Decimal:
    """Parse and normalise to 2dp. Rejects NaN/Infinity/garbage."""
    try:
        amount = Decimal(str(value)).quantize(TWOPLACES, rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError, TypeError):
        raise WalletError("Invalid amount.", code="invalid_amount", status=400)
    if not amount.is_finite() or amount <= ZERO:
        raise WalletError("Amount must be greater than zero.", code="invalid_amount", status=400)
    return amount


def _positive(amount) -> Decimal:
    amount = _to_money(amount)
    if amount <= ZERO:
        raise WalletError("Amount must be greater than zero.", "invalid_amount")
    return amount


def _mask(account_number: str) -> str:
    return f"{'*' * max(len(account_number) - 4, 0)}{account_number[-4:]}"


def new_withdrawal_ref() -> str:
    return f"WDR-{uuid.uuid4().hex[:12].upper()}"


# ==========================================
# ✅ WALLET GETTERS — unified
# ==========================================
def get_wallet(rider=None, *, wallet_id=None, for_update: bool = False,
               require_active: bool = False, create_if_missing: bool = False,
               requesting_user=None, select_related: tuple = ("user",)) -> RiderWallet:
    """
    Fetch a rider's wallet by wallet ID or by rider.
    """
    if (wallet_id is None) == (rider is None):
        raise WalletError("Provide exactly one of wallet_id or rider.",
                         code="invalid_lookup", status=400)

    if for_update and not connection.in_atomic_block:
        raise RuntimeError("get_wallet(for_update=True) must run inside transaction.atomic().")

    qs = RiderWallet.objects.all()
    if select_related:
        qs = qs.select_related(*select_related)
    if for_update:
        qs = qs.select_for_update(of=("self",))

    lookup = {"pk": wallet_id} if wallet_id is not None else {"user_id": getattr(rider, "pk", rider)}

    try:
        wallet = qs.get(**lookup)
    except RiderWallet.DoesNotExist:
        if create_if_missing and rider is not None:
            wallet = _create_wallet_safely(rider, qs)
        else:
            raise WalletError("Wallet not found.", code="not_found", status=404)
    except (ValueError, TypeError, ValidationError):
        raise WalletError("Invalid wallet identifier.", code="invalid_id", status=400)
    except RiderWallet.MultipleObjectsReturned:
        logger.error("Duplicate wallets found for lookup=%s", lookup)
        raise WalletError("Wallet configuration error. Please contact support.",
                         code="duplicate_wallet", status=500)

    if requesting_user is not None:
        is_owner = wallet.user_id == requesting_user.pk
        if not (is_owner or getattr(requesting_user, "is_staff", False)):
            logger.warning("Unauthorised wallet access: user=%s wallet=%s",
                          requesting_user.pk, wallet.pk)
            raise WalletError("Wallet not found.", code="not_found", status=404)

    if require_active and not getattr(wallet, "is_active", True):
        raise WalletError("This wallet is not active.", code="wallet_frozen", status=403)

    return wallet


def _create_wallet_safely(rider, qs) -> RiderWallet:
    """Race-safe creation."""
    user_id = getattr(rider, "pk", rider)
    try:
        with transaction.atomic():
            RiderWallet.objects.create(user_id=user_id)
        logger.info("Wallet auto-created for user=%s", user_id)
    except IntegrityError:
        pass
    return qs.get(user_id=user_id)


def _lock_wallet(rider) -> RiderWallet:
    """Must be called inside transaction.atomic()."""
    return get_wallet(rider, for_update=True)


def is_wallet_eligible(rider) -> bool:
    return get_wallet(rider).is_eligible


def get_wallet_summary(*, wallet: RiderWallet) -> dict:
    pending = (
        WDR.objects.filter(wallet=wallet, status=WDR.Status.PENDING)
        .aggregate(total=Sum("amount"))["total"]
        or ZERO
    )
    withdrawable = max(wallet.balance - MIN_WALLET_BALANCE - WITHDRAWAL_FEE, ZERO)
    total_withdrawn = getattr(wallet, "total_withdrawn", ZERO)
    is_active = getattr(wallet, "is_active", True)

    return {
        "wallet_id": wallet.pk,
        "currency": wallet.currency,
        "balance": wallet.balance,
        "withdrawable_balance": withdrawable,
        "pending_withdrawals": pending,
        "reserve_balance": MIN_WALLET_BALANCE,
        "withdrawal_fee": WITHDRAWAL_FEE,
        "total_withdrawn": total_withdrawn,
        "is_active": is_active,
    }


# ==========================================
# ✅ LEDGER POSTING
# ==========================================
def _post(wallet, txn_type, amount, reference, *, booking=None, description="",
          admin_user=None, provider_reference="", status=WT.Status.SUCCESS):
    """Write one ledger row and apply it to a LOCKED wallet. Idempotent by reference."""
    existing = WT.objects.filter(reference=reference).first()
    if existing:
        return existing, False

    amount = _to_money(amount)
    update_fields = ["updated_at"]

    if status == WT.Status.COMPLETED:
        if amount > 0:
            wallet.balance += amount
            wallet.available_balance += amount
        else:
            wallet.balance -= abs(amount)
            wallet.available_balance -= abs(amount)
        update_fields += ["balance", "available_balance"]
    elif status == WT.Status.PENDING:
        wallet.pending_earnings += amount
        update_fields += ["pending_earnings"]

    wallet.save(update_fields=update_fields)

    txn = WT.objects.create(
        wallet=wallet, type=txn_type, amount=amount, balance_after=wallet.balance,
        reference=reference, provider_reference=provider_reference, description=description,
        currency=wallet.currency, status=status, booking=booking, created_by=admin_user,
    )
    return txn, True


# ==========================================
# ✅ CREDITS & DEBITS
# ==========================================
@transaction.atomic
def top_up(rider, amount, reference, *, provider_reference=""):
    """Add verified funds (e.g. after a Paystack/MoMo confirmation)."""
    amount = _positive(amount)
    wallet = _lock_wallet(rider)
    txn, _ = _post(wallet, WT.Type.TOPUP, amount, reference,
                   description="Wallet top-up", provider_reference=provider_reference)
    return txn


@transaction.atomic
def admin_adjustment(rider, amount, reason, admin_user):
    """Manual correction (+/-). Always attributed and has a reason."""
    amount = round_money(amount)
    if amount == ZERO or not reason.strip():
        raise WalletError("A non-zero amount and a reason are required.", "invalid_adjustment")
    wallet = _lock_wallet(rider)
    txn, _ = _post(wallet, WT.Type.ADJUSTMENT, amount,
                   f"ADJ-{uuid.uuid4().hex[:16].upper()}",
                   description=reason[:255], admin_user=admin_user)
    return txn


def commission_for(fare) -> Decimal:
    # Default 20% — move to conf/settings.py later
    return round_money(round_money(fare) * Decimal("0.20"))


@transaction.atomic
def charge_commission(booking):
    """Take platform's cut from rider's wallet."""
    commission = commission_for(booking.fare)
    if commission <= ZERO:
        return None
    wallet = _lock_wallet(booking.rider)
    txn, _ = _post(wallet, WT.Type.COMMISSION, -commission,
                   f"COMM-{booking.pk}", booking=booking,
                   description=f"20% commission, booking {booking.pk}")
    return txn


# ==========================================
# ✅ JOB EARNINGS — hold/release/cancel
# ==========================================
@transaction.atomic
def hold_job_earnings(booking):
    """Hold rider's net share until completion."""
    net = round_money(booking.fare) - commission_for(booking.fare)
    if net <= ZERO:
        raise WalletError("Net earnings must be positive.", "invalid_amount")
    wallet = _lock_wallet(booking.rider)
    txn, _ = _post(wallet, WT.Type.PAYOUT, net, f"PAY-{booking.pk}", booking=booking,
                   description=f"Earnings for booking {booking.pk}", status=WT.Status.PENDING)
    return txn


@transaction.atomic
def release_job_earnings(booking, *, payment_verified: bool):
    """Move held earnings to available balance."""
    if not payment_verified:
        raise WalletError("Payment must be verified first.", "payment_unverified")
    wallet = _lock_wallet(booking.rider)
    txn = WT.objects.select_for_update().filter(reference=f"PAY-{booking.pk}").first()

    if txn is None:
        raise WalletError("No held earnings for this booking.", "not_found")
    if txn.status == WT.Status.COMPLETED:
        return txn
    if txn.status != WT.Status.PENDING:
        raise WalletError("Held earnings can no longer be released.", "invalid_state")

    wallet.pending_earnings -= txn.amount
    wallet.balance += txn.amount
    wallet.available_balance += txn.amount
    wallet.save(update_fields=["pending_earnings", "balance", "available_balance", "updated_at"])

    txn.status = WT.Status.COMPLETED
    txn.balance_after = wallet.balance
    txn.save(update_fields=["status", "balance_after", "updated_at"])
    return txn


@transaction.atomic
def cancel_job_earnings(booking, reason=""):
    """Release held earnings hold."""
    wallet = _lock_wallet(booking.rider)
    txn = WT.objects.select_for_update().filter(reference=f"PAY-{booking.pk}").first()

    if txn is None or txn.status == WT.Status.FAILED:
        return txn
    if txn.status != WT.Status.PENDING:
        raise WalletError("Earnings were already released; issue a refund instead.", "invalid_state")

    wallet.pending_earnings -= txn.amount
    wallet.save(update_fields=["pending_earnings", "updated_at"])

    txn.status = WT.Status.FAILED
    txn.description = (txn.description + f" | cancelled: {reason}")[:255]
    txn.save(update_fields=["status", "description", "updated_at"])
    return txn


# ==========================================
# ✅ WITHDRAWAL HELPERS
# ==========================================
def _validate_destination(method: str, provider: str, account_number: str, account_name: str):
    method = (method or "").strip().lower()
    provider = (provider or "").strip().lower()
    account_name = " ".join((account_name or "").split())
    account_number = re.sub(r"[\s\-]", "", account_number or "")

    if method not in WITHDRAWAL_METHODS:
        raise WalletError("Unsupported withdrawal method.", code="invalid_method", status=400)
    if not provider:
        raise WalletError("Provider is required.", code="invalid_provider", status=400)
    if len(account_name) < 2 or len(account_name) > 100:
        raise WalletError("Invalid account name.", code="invalid_account_name", status=400)
    if method in ("momo", "mobile_money") and not GH_MOMO_RE.match(account_number):
        raise WalletError("Enter a valid 10-digit mobile money number.",
                         code="invalid_account_number", status=400)
    if not account_number.isdigit() or not (8 <= len(account_number) <= 20):
        raise WalletError("Invalid account number.", code="invalid_account_number", status=400)

    return method, provider, account_number, account_name


def _withdrawn_today(wallet) -> Decimal:
    start = timezone.localtime().replace(hour=0, minute=0, second=0, microsecond=0)
    total = (
        WDR.objects.filter(wallet=wallet, created_at__date=start)
        .exclude(status__in=[WDR.Status.REJECTED, WDR.Status.FAILED])
        .aggregate(t=Sum("amount"))["t"]
    )
    return total or ZERO


def _after_withdrawal_created(withdrawal_id):
    """Hook: enqueue payout / notify rider."""
    logger.info("Withdrawal %s committed; ready for processing.", withdrawal_id)


# ==========================================
# ✅ CREATE WITHDRAWAL
# ==========================================
@transaction.atomic
def create_withdrawal_request(
    *, wallet, amount, method: str, provider: str,
    account_number: str, account_name: str,
    idempotency_key=None, requested_by=None,
) -> WithdrawalRequest:
    """Create a PENDING withdrawal and place a hold on the wallet balance."""
    amount = _to_money(amount)
    method, provider, account_number, account_name = _validate_destination(
        method, provider, account_number, account_name
    )

    if amount < MIN_AMOUNT:
        raise WalletError(f"Minimum withdrawal is {MIN_AMOUNT}.",
                         code="amount_too_low", status=400)
    if amount > MAX_AMOUNT:
        raise WalletError(f"Maximum withdrawal is {MAX_AMOUNT}.",
                         code="amount_too_high", status=400)

    try:
        with transaction.atomic():
            wallet = get_wallet(wallet_id=wallet.pk, for_update=True)

        if idempotency_key:
            existing = WDR.objects.filter(
                wallet=wallet, idempotency_key=idempotency_key
            ).first()
        if existing:
            logger.info("Idempotent replay for withdrawal %s", existing.id)
        return existing

        if not getattr(wallet, "is_active", True):
            raise WalletError("This wallet is not active.", code="wallet_frozen", status=403)

            pending = WDR.objects.filter(
                wallet=wallet, status=WDR.Status.PENDING
            ).count()
            if pending >= MAX_PENDING:
                raise WalletError(
                    "You have too many pending withdrawals. Please wait for them to complete.",
                    code="too_many_pending",
                    status=429,
                )

            if _withdrawn_today(wallet) + amount > DAILY_LIMIT:
                raise WalletError("Daily withdrawal limit exceeded.", code="daily_limit", status=400)

            fee = WITHDRAWAL_FEE
            total_debit = amount + fee
            if wallet.balance < total_debit + MIN_WALLET_BALANCE:
                raise WalletError(
                    "Insufficient funds for withdrawal plus reserve balance.",
                    code="insufficient_funds",
                    status=400,
                )

            balance_before = wallet.balance
            wallet.balance = balance_before - total_debit
            wallet.save(update_fields=["balance"])

            withdrawal = WDR.objects.create(
                wallet=wallet,
                amount=amount,
                fee=fee,
                method=method,
                provider=provider,
                account_number=account_number,
                account_name=account_name,
                status=WDR.Status.PENDING,
                idempotency_key=idempotency_key,
                requested_by=requested_by,
            )

            WT.objects.create(
                wallet=wallet,
                category=WT.Category.WITHDRAWAL,
                transaction_type=WT.Type.DEBIT,
                amount=total_debit,
                balance_before=balance_before,
                balance_after=wallet.balance,
                reference=f"WDR-{withdrawal.id}",
                description=f"Withdrawal to {provider} {_mask(account_number)}",
            )

            transaction.on_commit(lambda: _after_withdrawal_created(withdrawal.id))

    except IntegrityError:
        if idempotency_key:
            existing = WDR.objects.filter(
                wallet_id=wallet.pk, idempotency_key=idempotency_key
            ).first()
            if existing:
                return existing
        raise

    logger.info(
        "Withdrawal %s created: amount=%s fee=%s wallet=%s dest=%s",
        withdrawal.id, amount, fee, wallet.pk, _mask(account_number),
    )
    return withdrawal


# ==========================================
# ✅ WITHDRAWAL LIFECYCLE — complete/fail/cancel
# ==========================================
@transaction.atomic
def complete_withdrawal(*, withdrawal_id, provider_reference: str = "") -> WithdrawalRequest:
    """Mark paid out and count it toward total_withdrawn."""
    withdrawal = WDR.objects.select_for_update().get(pk=withdrawal_id)
    if withdrawal.status != WDR.Status.PENDING:
        raise WalletError("Withdrawal is not pending.", code="invalid_state", status=409)

    wallet = get_wallet(wallet_id=withdrawal.wallet_id, for_update=True)
    wallet.total_withdrawn = getattr(wallet, "total_withdrawn", ZERO) + withdrawal.amount
    wallet.save(update_fields=["total_withdrawn"])

    withdrawal.status = WDR.Status.COMPLETED
    withdrawal.provider_reference = provider_reference
    withdrawal.processed_at = timezone.now()
    withdrawal.save(update_fields=["status", "provider_reference", "processed_at"])
    return withdrawal


@transaction.atomic
def fail_withdrawal(*, withdrawal_id, reason: str = "") -> WithdrawalRequest:
    """Release the hold (amount + fee) back to the wallet."""
    withdrawal = WDR.objects.select_for_update().get(pk=withdrawal_id)
    if withdrawal.status != WDR.Status.PENDING:
        raise WalletError("Withdrawal is not pending.", code="invalid_state", status=409)

    wallet = get_wallet(wallet_id=withdrawal.wallet_id, for_update=True)
    refund = withdrawal.amount + withdrawal.fee
    balance_before = wallet.balance
    wallet.balance = balance_before + refund
    wallet.save(update_fields=["balance"])

    WT.objects.create(
        wallet=wallet,
        category=WT.Category.REVERSAL,
        transaction_type=WT.Type.CREDIT,
        amount=refund,
        balance_before=balance_before,
        balance_after=wallet.balance,
        reference=f"WDR-{withdrawal.id}-REFUND",
        description=f"Withdrawal refunded: {reason or 'failed'}",
    )

    withdrawal.status = WDR.Status.FAILED
    withdrawal.failure_reason = reason
    withdrawal.processed_at = timezone.now()
    withdrawal.save(update_fields=["status", "failure_reason", "processed_at"])
    return withdrawal


@transaction.atomic
def cancel_withdrawal(withdrawal_id, requested_by=None, reason="Cancelled by user"):
    """
    Cancel a pending withdrawal and refund the amount to the wallet.

    - Runs in a single DB transaction (all-or-nothing).
    - Locks the withdrawal and wallet rows to prevent double refunds
      from concurrent requests.
    - Only PENDING withdrawals can be cancelled.
    - Non-staff users may only cancel their own withdrawals.
    """
    try:
        withdrawal = WDR.objects.select_for_update().get(id=withdrawal_id)
    except WDR.DoesNotExist:
        raise WalletError(
            "Withdrawal request not found.",
            code="not_found",
            status=404,
        )

    if requested_by is not None and not getattr(requested_by, "is_staff", False):
        if withdrawal.wallet.user_id != requested_by.id:
            raise WalletError(
                "You do not have permission to cancel this withdrawal.",
                code="forbidden",
                status=403,
            )

    if withdrawal.status != WDR.Status.PENDING:
        raise WalletError(
            "Only pending withdrawals can be cancelled.",
            code="invalid_state",
            status=409,
        )

    wallet = get_wallet(wallet_id=withdrawal.wallet_id, for_update=True)
    wallet.balance += withdrawal.amount + withdrawal.fee
    wallet.save(update_fields=["balance"])

    withdrawal.status = WDR.Status.CANCELLED
    withdrawal.failure_reason = reason
    withdrawal.processed_at = timezone.now()
    if hasattr(withdrawal, "processed_by"):
        withdrawal.processed_by = requested_by
    withdrawal.save(
        update_fields=["status", "failure_reason", "processed_at"]
    )

    logger.info(
        "Withdrawal %s cancelled by %s; refunded %s to wallet %s",
        withdrawal.id,
        getattr(requested_by, "id", "system"),
        withdrawal.amount,
        wallet.pk,
    )

    return withdrawal
