"""
Serializers + DRF views for the rider wallet. Split into serializers.py / views.py if you prefer.
"""
from decimal import Decimal

from django.core.validators import RegexValidator
from rest_framework import serializers, status
from rest_framework.decorators import api_view, permission_classes, throttle_classes
from rest_framework.exceptions import NotFound
from rest_framework.pagination import PageNumberPagination
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import UserRateThrottle

from . import wallet_conf as conf
from .models_wallet import WalletTransaction
from .models_wallet import RiderWallet
from .models_wallet import WithdrawalRequest
from .wallet_services import WalletError, cancel_withdrawal, create_withdrawal_request, get_wallet

ERROR_STATUS = {
    "not_found": status.HTTP_404_NOT_FOUND,
    "wallet_frozen": status.HTTP_403_FORBIDDEN,
    "withdrawal_in_progress": status.HTTP_409_CONFLICT,
    "invalid_state": status.HTTP_409_CONFLICT,
}


# ───────────────────────── serializers ─────────────────────────

class WalletTransactionSerializer(serializers.ModelSerializer):
    class Meta:
        model = WalletTransaction
        fields = ("reference", "type", "amount", "balance_after", "status", "description", "created_at")


class WithdrawalRequestSerializer(serializers.ModelSerializer):
    account = serializers.CharField(source="masked_account", read_only=True)

    class Meta:
        model = WithdrawalRequest
        fields = ("reference", "amount", "fee", "currency", "status", "payout_network",
                  "account", "failure_reason", "created_at", "completed_at")


class WithdrawalCreateSerializer(serializers.Serializer):
    amount = serializers.DecimalField(max_digits=12, decimal_places=2, min_value=Decimal("0.01"))
    payout_network = serializers.ChoiceField(choices=[("MTN", "MTN"), ("Telecel", "Telecel"), ("AirtelTigo", "AirtelTigo"), ("Bank", "Bank")])
    payout_account_number = serializers.CharField(
        max_length=30, validators=[RegexValidator(r"^\d{8,20}$", "Enter digits only (8-20).")]
    )
    payout_account_name = serializers.CharField(max_length=100)


# ───────────────────────── helpers ─────────────────────────

class WithdrawalThrottle(UserRateThrottle):
    scope = "withdrawals"  # set DEFAULT_THROTTLE_RATES["withdrawals"] = "10/hour"


class TransactionPagination(PageNumberPagination):
    page_size = 20
    max_page_size = 100
    page_size_query_param = "page_size"


def _rider(request):
    rider = getattr(request.user, "rider_profile", None)
    if rider is None:
        raise NotFound("Rider profile not found.")
    return rider


def _wallet_error(exc: WalletError):
    return Response({"error": exc.message, "code": exc.code},
                    status=ERROR_STATUS.get(exc.code, status.HTTP_400_BAD_REQUEST))


# ───────────────────────── views ─────────────────────────

@api_view(["GET"])
@permission_classes([IsAuthenticated])
def get_rider_wallet(request):
    """Wallet status, eligibility and the 10 most recent transactions."""
    wallet = get_wallet(_rider(request))
    recent = wallet.transactions.all()[:10]
    return Response({
        "balance": wallet.balance,
        "available_balance": wallet.available_balance,
        "pending_earnings": wallet.pending_earnings,
        "currency": wallet.currency,
        "minimum_required": conf.min_balance(),
        "shortfall": wallet.shortfall,
        "commission_rate": conf.commission_rate(),
        "is_eligible_for_jobs": wallet.is_eligible,
        "is_frozen": wallet.is_frozen,
        "recent_transactions": WalletTransactionSerializer(recent, many=True).data,
        "updated_at": wallet.updated_at,
    })


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def list_wallet_transactions(request):
    """Paginated history. Optional filters: ?type=commission&status=completed"""
    wallet = get_wallet(_rider(request))
    qs = wallet.transactions.all()
    for field in ("type", "status"):
        value = request.query_params.get(field)
        if value:
            qs = qs.filter(**{field: value})
    paginator = TransactionPagination()
    page = paginator.paginate_queryset(qs, request)
    return paginator.get_paginated_response(WalletTransactionSerializer(page, many=True).data)


@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated])
@throttle_classes([WithdrawalThrottle])
def withdrawals(request):
    """GET: list my withdrawals. POST: request one (requires an Idempotency-Key header)."""
    rider = _rider(request)

    if request.method == "GET":
        qs = WithdrawalRequest.objects.filter(wallet__rider=rider)[:50]
        return Response(WithdrawalRequestSerializer(qs, many=True).data)

    serializer = WithdrawalCreateSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    data = serializer.validated_data
    try:
        wr, created = create_withdrawal_request(
            rider, data["amount"],
            network=data["payout_network"],
            account_number=data["payout_account_number"],
            account_name=data["payout_account_name"],
            idempotency_key=request.headers.get("Idempotency-Key", ""),
        )
    except WalletError as exc:
        return _wallet_error(exc)
    return Response(WithdrawalRequestSerializer(wr).data,
                    status=status.HTTP_201_CREATED if created else status.HTTP_200_OK)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def cancel_my_withdrawal(request, reference):
    try:
        wr = cancel_withdrawal(_rider(request), reference)
    except WalletError as exc:
        return _wallet_error(exc)
    return Response(WithdrawalRequestSerializer(wr).data)
