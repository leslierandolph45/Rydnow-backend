"""
Custom exceptions for wallet operations.

All expected business-rule violations raise WalletError (or a subclass) with a
machine-readable `code`, so API clients and the mobile apps can react to the
code, never to the message text.

Existing calls like WalletError("...", code="not_found", status=404) keep
working unchanged. Subclasses just provide sensible defaults.
"""
import logging

logger = logging.getLogger(__name__)


class ErrorCode:
    """Central registry of error codes. Treat these as a public API contract."""

    INVALID_LOOKUP = "invalid_lookup"
    INVALID_ID = "invalid_id"
    NOT_FOUND = "not_found"
    FORBIDDEN = "forbidden"
    DUPLICATE_WALLET = "duplicate_wallet"

    WALLET_FROZEN = "wallet_frozen"
    INSUFFICIENT_FUNDS = "insufficient_funds"

    INVALID_AMOUNT = "invalid_amount"
    AMOUNT_TOO_LOW = "amount_too_low"
    AMOUNT_TOO_HIGH = "amount_too_high"
    DAILY_LIMIT = "daily_limit"
    TOO_MANY_PENDING = "too_many_pending"
    COOLDOWN_ACTIVE = "cooldown_active"

    INVALID_METHOD = "invalid_method"
    INVALID_PROVIDER = "invalid_provider"
    INVALID_ACCOUNT_NUMBER = "invalid_account_number"
    INVALID_ACCOUNT_NAME = "invalid_account_name"

    INVALID_STATE = "invalid_state"
    DUPLICATE_REQUEST = "duplicate_request"
    PAYOUT_FAILED = "payout_failed"
    PROVIDER_UNAVAILABLE = "provider_unavailable"


class WalletError(Exception):
    """
    Base class for expected wallet errors.

    Attributes:
        message:    Human-readable, safe to show to the end user.
        code:       Machine-readable identifier (see ErrorCode).
        status:     HTTP status to return from the API.
        details:    Optional structured context (e.g. {"limit": "5000.00"}).
        retryable:  True if the client may retry the same request later.
    """

    default_message = "A wallet error occurred."
    default_code = "wallet_error"
    default_status = 400
    default_retryable = False

    def __init__(
        self,
        message: str | None = None,
        code: str | None = None,
        status: int | None = None,
        *,
        details: dict | None = None,
        retryable: bool | None = None,
    ):
        self.message = message or self.default_message
        self.code = code or self.default_code
        self.status = status or self.default_status
        self.details = details or {}
        self.retryable = self.default_retryable if retryable is None else retryable
        super().__init__(self.message)

    def to_dict(self) -> dict:
        """Stable API error payload."""
        payload = {"code": self.code, "message": self.message}
        if self.details:
            payload["details"] = self.details
        if self.retryable:
            payload["retryable"] = True
        return payload

    def __repr__(self) -> str:
        return f"{type(self).__name__}(code={self.code!r}, status={self.status}, message={self.message!r})"


    # ————————————————————————— #
    # Specific errors
    # ————————————————————————— #
class ErrorCode:
    class WalletError(Exception):
        default_message = "A wallet error occurred."
        default_code = "wallet_error"
        default_status = 400
        default_retryable = False

    def __init__(self, message=None, code=None, status=None, *, details=None, retryable=None):
        self.message = message or self.default_message
        self.code = code or self.default_code
        self.status = status or self.default_status
        self.details = details or {}
        self.retryable = self.default_retryable if retryable is None else retryable
        super().__init__(self.message)

    def to_dict(self) -> dict:
        payload = {"code": self.code, "message": self.message}
        if self.details:
            payload["details"] = self.details
        if self.retryable:
            payload["retryable"] = True
        return payload

    def __repr__(self) -> str:
        return f"{type(self).__name__}(code={self.code!r}, status={self.status}, message={self.message!r})"




    class WalletNotFoundError(WalletError):
        default_message = "Wallet not found."
        default_code = ErrorCode.NOT_FOUND
        default_status = 404


    class WalletFrozenError(WalletError):
        default_message = "This wallet is not active."
        default_code = ErrorCode.WALLET_FROZEN
        default_status = 403


    class InsufficientFundsError(WalletError):
        default_message = "Insufficient funds for this transaction."
        default_code = ErrorCode.INSUFFICIENT_FUNDS
        default_status = 400


    class InvalidAmountError(WalletError):
        default_message = "Invalid amount."
        default_code = ErrorCode.INVALID_AMOUNT
        default_status = 400


    class LimitExceededError(WalletError):
        """Per-transaction, daily, or pending-count limits."""

        default_message = "Withdrawal limit exceeded."
        default_code = ErrorCode.DAILY_LIMIT
        default_status = 400


    class TooManyRequestsError(WalletError):
        default_message = "Too many requests. Please try again later."
        default_code = ErrorCode.TOO_MANY_PENDING
        default_status = 429
        default_retryable = True


    class InvalidStateError(WalletError):
        """e.g. completing a withdrawal that is no longer pending."""

        default_message = "This action is not allowed in the current state."
        default_code = ErrorCode.INVALID_STATE
        default_status = 409


    class DuplicateRequestError(WalletError):
        default_message = "This request has already been processed."
        default_code = ErrorCode.DUPLICATE_REQUEST
        default_status = 409


    class PayoutProviderError(WalletError):
        """Mobile money / bank provider failed or is unreachable."""

        default_message = "Payout provider is unavailable. Please try again shortly."
        default_code = ErrorCode.PROVIDER_UNAVAILABLE
        default_status = 503
        default_retryable = True


    # ————————————————————————— #
    # DRF integration
    # ————————————————————————— #
    def wallet_exception_handler(exc, context):
        """
        Drop-in DRF exception handler that turns WalletError into a clean response:

            {"error": {"code": "insufficient_funds", "message": "...", "details": {...}}}

        Enable in settings:
            REST_FRAMEWORK = {
                "EXCEPTION_HANDLER": "yourapp.wallet_exceptions.wallet_exception_handler",
            }
        """
        from rest_framework.response import Response
        from rest_framework.views import exception_handler as drf_default_handler

        if isinstance(exc, WalletError):
            level = logging.ERROR if exc.status >= 500 else logging.INFO
            logger.log(level, "WalletError %s: %s", exc.code, exc.message)
            response = Response({"error": exc.to_dict()}, status=exc.status)
            if exc.status == 429:
                response["Retry-After"] = "60"
            return response

        return drf_default_handler(exc, context)
