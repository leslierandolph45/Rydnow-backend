"""
Rydnow - Rider wallet deposits (production-ready)

Flow
-—
1. POST /wallet/deposit/          -> creates a PENDING deposit + Paystack checkout URL
2. Rider pays (MoMo / card) on Paystack  -> Paystack calls our webhook
3. Webhook (or GET verify endpoint)      -> verifies with Paystack, credits wallet ONCE
4. Dispatch only offers jobs to riders whose wallet >= WALLET_MIN_BALANCE (GHS 50)
"""
import hashlib
import hmac
import json
import logging
import uuid
from datetime import timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

import requests
from django.apps import apps
from django.conf import settings
from django.contrib import admin, messages
from django.db import IntegrityError, models, transaction
from django.db.models import Q, Sum
from django.utils import timezone
from rest_framework import serializers
from rest_framework.decorators import (
    api_view, authentication_classes, permission_classes, throttle_classes,
)
from rest_framework.pagination import PageNumberPagination
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import UserRateThrottle

logger = logging.getLogger(__name__)


# ————————————————————————— #
# Config
# ————————————————————————— #
def _dec(name, default):
    return Decimal(str(getattr(settings, name, default))).quantize(Decimal("0.01"))


PAYSTACK_BASE = "https://api.paystack.co"
CURRENCY = "GHS"
MIN_DEPOSIT = _dec("WALLET_MIN_DEPOSIT", "50.00")
MIN_BALANCE = _dec("WALLET_MIN_BALANCE", "50.00")
MAX_DEPOSIT = _dec("WALLET_MAX_DEPOSIT", "5000.00")
DAILY_LIMIT = _dec("WALLET_DAILY_DEPOSIT_LIMIT", "10000.00")
MAX_PENDING = int(getattr(settings, "WALLET_MAX_PENDING_DEPOSITS", 3))
EXPIRY_MINUTES = int(getattr(settings, "WALLET_DEPOSIT_EXPIRY_MINUTES", 30))

WALLET_ELIGIBLE_Q = Q(
    user__rider_wallet__is_active=True,
    user__rider_wallet__balance__gte=MIN_BALANCE,
)


def _get_wallet_model():
    return apps.get_model("api", "RiderWallet")


def to_pesewas(amount: Decimal) -> int:
    return int((amount * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


# ————————————————————————— #
# Model
# ————————————————————————— #
class WalletDeposit(models.Model):
    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending"
        SUCCESS = "SUCCESS", "Successful"
        FAILED = "FAILED", "Failed"
        EXPIRED = "EXPIRED", "Expired"
        NEEDS_REVIEW = "NEEDS_REVIEW", "Needs review"

    class Channel(models.TextChoices):
        ANY = "any", "Any"
        MOBILE_MONEY = "mobile_money", "Mobile Money"
        CARD = "card", "Card"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="wallet_deposits"
    )
    wallet = models.ForeignKey("api.RiderWallet", on_delete=models.PROTECT, related_name="deposits")
    reference = models.CharField(max_length=64, unique=True, editable=False)
    idempotency_key = models.CharField(max_length=80, null=True, blank=True)

    amount = models.DecimalField(max_digits=12, decimal_places=2)
    gateway_fee = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    currency = models.CharField(max_length=3, default=CURRENCY)
    channel = models.CharField(max_length=20, choices=Channel.choices, default=Channel.ANY)
    status = models.CharField(max_length=14, choices=Status.choices, default=Status.PENDING, db_index=True)

    balance_before = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    balance_after = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)

    authorization_url = models.URLField(max_length=500, blank=True)
    gateway_response = models.CharField(max_length=255, blank=True)
    gateway_payload = models.JSONField(default=dict, blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    expires_at = models.DateTimeField()
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "rider_wallet_deposits"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["user", "status", "-created_at"]),
            models.Index(fields=["status", "expires_at"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["user", "idempotency_key"],
                condition=Q(idempotency_key__isnull=False),
                name="uniq_user_deposit_idempotency_key",
            ),
        ]

    def __str__(self):
        return f"{self.reference} {self.currency} {self.amount} [{self.status}]"


# ————————————————————————— #
# Errors + Paystack client
# ————————————————————————— #
class DepositError(Exception):
    def __init__(self, message, status_code=400):
        super().__init__(message)
        self.status_code = status_code


class PaymentGatewayError(Exception):
    pass


def _paystack(method, path, **kwargs):
    secret = getattr(settings, "PAYSTACK_SECRET_KEY", "")
    if not secret:
        raise PaymentGatewayError("Payment gateway is not configured.")
    try:
        resp = requests.request(
            method, f"{PAYSTACK_BASE}{path}",
            headers={"Authorization": f"Bearer {secret}"}, timeout=15, **kwargs,
        )
        body = resp.json()
    except (requests.RequestException, ValueError) as exc:
        logger.exception("Paystack request failed: %s %s", method, path)
        raise PaymentGatewayError("Payment gateway is unreachable. Try again shortly.") from exc
    if not resp.ok or not body.get("status"):
        raise PaymentGatewayError(body.get("message") or "Payment gateway error.")
    return body["data"]


# ————————————————————————— #
# Business logic
# ————————————————————————— #
def get_wallet(user):
    Wallet = _get_wallet_model()
    wallet, _ = Wallet.objects.get_or_create(user=user, defaults={"currency": CURRENCY})
    return wallet


def wallet_summary(wallet):
    shortfall = max(Decimal("0.00"), MIN_BALANCE - wallet.balance)
    suggested = sorted({MIN_DEPOSIT, Decimal("100"), Decimal("200"), Decimal("500"),
                        max(shortfall, MIN_DEPOSIT)})
    return {
        "balance": wallet.balance,
        "currency": wallet.currency,
        "wallet_active": wallet.is_active,
        "min_balance_required": MIN_BALANCE,
        "min_deposit": MIN_DEPOSIT,
        "max_deposit": MAX_DEPOSIT,
        "can_receive_jobs": wallet.is_active and wallet.balance >= MIN_BALANCE,
        "shortfall": shortfall,
        "suggested_amounts": suggested,
    }


def _enforce_limits(user, amount):
    if amount < MIN_DEPOSIT:
        raise DepositError(f"Minimum deposit is {CURRENCY} {MIN_DEPOSIT}.")
    if amount > MAX_DEPOSIT:
        raise DepositError(f"Maximum single deposit is {CURRENCY} {MAX_DEPOSIT}.")
    now = timezone.now()
    open_count = WalletDeposit.objects.filter(
        user=user, status=WalletDeposit.Status.PENDING, expires_at__gt=now
    ).count()
    if open_count >= MAX_PENDING:
        raise DepositError("You have too many unfinished deposits. Complete one or wait a few minutes.", 429)
    day_start = timezone.localtime(now).replace(hour=0, minute=0, second=0, microsecond=0)
    today = WalletDeposit.objects.filter(
        user=user, created_at__gte=day_start,
        status__in=[WalletDeposit.Status.SUCCESS, WalletDeposit.Status.PENDING],
    ).aggregate(t=Sum("amount"))["t"] or Decimal("0.00")
    if today + amount > DAILY_LIMIT:
        raise DepositError(f"Daily deposit limit of {CURRENCY} {DAILY_LIMIT} reached.", 429)


def initialize_deposit(*, user, amount, channel="any", idempotency_key=None, ip=None):
    wallet = get_wallet(user)
    if not wallet.is_active:
        raise DepositError("Your wallet is disabled. Please contact support.", 403)

    if idempotency_key:
        existing = WalletDeposit.objects.filter(user=user, idempotency_key=idempotency_key).first()
        if existing:
            return existing

    _enforce_limits(user, amount)

    reference = f"RYD-DEP-{uuid.uuid4().hex[:20].upper()}"
    try:
        deposit = WalletDeposit.objects.create(
            user=user, wallet=wallet, reference=reference, amount=amount, channel=channel,
            idempotency_key=idempotency_key or None, ip_address=ip,
            expires_at=timezone.now() + timedelta(minutes=EXPIRY_MINUTES),
        )
    except IntegrityError:
        return WalletDeposit.objects.get(user=user, idempotency_key=idempotency_key)

    payload = {
        "email": user.email or f"{user.pk}@rydnow.app",
        "amount": to_pesewas(amount),
        "currency": CURRENCY,
        "reference": reference,
        "metadata": {"user_id": str(user.pk), "purpose": "rider_wallet_deposit"},
    }
    if getattr(settings, "PAYSTACK_CALLBACK_URL", ""):
        payload["callback_url"] = settings.PAYSTACK_CALLBACK_URL
    if channel != "any":
        payload["channels"] = [channel]

    try:
        data = _paystack("POST", "/transaction/initialize", json=payload)
    except PaymentGatewayError as exc:
        deposit.status = WalletDeposit.Status.FAILED
        deposit.gateway_response = str(exc)[:255]
        deposit.save(update_fields=["status", "gateway_response", "updated_at"])
        raise

    deposit.authorization_url = data["authorization_url"]
    deposit.save(update_fields=["authorization_url", "updated_at"])
    return deposit


def _after_success(deposit):
    logger.info("Wallet credited: %s %s %s", deposit.reference, deposit.currency, deposit.amount)


def apply_gateway_result(deposit_id, data):
    Wallet = _get_wallet_model()
    gw_status = data.get("status")

    with transaction.atomic():
        deposit = WalletDeposit.objects.select_for_update().get(pk=deposit_id)
        if deposit.status != WalletDeposit.Status.PENDING:
            return deposit

        deposit.gateway_payload = {
            k: data.get(k) for k in ("id", "status", "amount", "currency", "channel", "paid_at", "fees")
        }
        deposit.gateway_response = (data.get("gateway_response") or "")[:255]
        now = timezone.now()

        if gw_status == "success":
            if data.get("amount") != to_pesewas(deposit.amount) or data.get("currency") != deposit.currency:
                deposit.status = WalletDeposit.Status.NEEDS_REVIEW
                deposit.gateway_response = "Amount/currency mismatch - manual review required."
                logger.critical("Deposit mismatch %s: %s", deposit.reference, deposit.gateway_payload)
            else:
                wallet = Wallet.objects.select_for_update().get(pk=deposit.wallet_id)
                deposit.balance_before = wallet.balance
                wallet.balance = wallet.balance + deposit.amount
                wallet.save(update_fields=["balance"])
                deposit.balance_after = wallet.balance
                deposit.gateway_fee = Decimal(data.get("fees") or 0) / 100
                deposit.status = WalletDeposit.Status.SUCCESS
                deposit.completed_at = now
                transaction.on_commit(lambda: _after_success(deposit))
        elif gw_status in ("failed", "reversed"):
            deposit.status = WalletDeposit.Status.FAILED
            deposit.completed_at = now
        elif deposit.expires_at <= now:
            deposit.status = WalletDeposit.Status.EXPIRED
            deposit.completed_at = now

        deposit.save()
        return deposit


def verify_deposit(reference, user=None):
    qs = WalletDeposit.objects.filter(reference=reference)
    if user is not None:
        qs = qs.filter(user=user)
    deposit = qs.first()
    if not deposit:
        raise DepositError("Deposit not found.", 404)
    if deposit.status != WalletDeposit.Status.PENDING:
        return deposit
    data = _paystack("GET", f"/transaction/verify/{reference}")
    return apply_gateway_result(deposit.pk, data)


def expire_stale_deposits():
    stale = WalletDeposit.objects.filter(
        status=WalletDeposit.Status.PENDING, expires_at__lte=timezone.now()
    )
    for dep in stale.iterator():
        try:
            verify_deposit(dep.reference)
        except (PaymentGatewayError, DepositError):
            WalletDeposit.objects.filter(pk=dep.pk, status=WalletDeposit.Status.PENDING).update(
                status=WalletDeposit.Status.EXPIRED, completed_at=timezone.now()
            )


# ————————————————————————— #
# Serializers
# ————————————————————————— #
class InitializePaymentSerializer(serializers.Serializer):
    amount = serializers.DecimalField(max_digits=12, decimal_places=2)
    channel = serializers.ChoiceField(choices=WalletDeposit.Channel.choices, default="any")

    def validate_amount(self, value):
        try:
            value = Decimal(value).quantize(Decimal("0.01"))
        except InvalidOperation:
            raise serializers.ValidationError("Invalid amount.")
        if value < MIN_DEPOSIT:
            raise serializers.ValidationError(f"Minimum deposit is {CURRENCY} {MIN_DEPOSIT}.")
        if value > MAX_DEPOSIT:
            raise serializers.ValidationError(f"Maximum single deposit is {CURRENCY} {MAX_DEPOSIT}.")
        return value


class WalletDepositSerializer(serializers.ModelSerializer):
    class Meta:
        model = WalletDeposit
        fields = [
            "reference", "amount", "currency", "channel", "status", "authorization_url",
            "gateway_response", "balance_after", "created_at", "expires_at", "completed_at",
        ]
        read_only_fields = fields


# ————————————————————————— #
# Views
# ————————————————————————— #
class DepositThrottle(UserRateThrottle):
    scope = "wallet_deposit"
    rate = "10/min"


def _client_ip(request):
    fwd = request.META.get("HTTP_X_FORWARDED_FOR")
    return fwd.split(",")[0].strip() if fwd else request.META.get("REMOTE_ADDR")


def _require_rider(user):
    if not getattr(user, "rider_profile", None):
        raise DepositError("Rider profile not found", 404)


def _handle(fn):
    try:
        return fn()
    except DepositError as exc:
        return Response({"error": str(exc)}, status=exc.status_code)
    except PaymentGatewayError as exc:
        return Response({"error": str(exc)}, status=502)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def wallet_overview(request):
    def run():
        _require_rider(request.user)
        return Response(wallet_summary(get_wallet(request.user)))
    return _handle(run)

# ——— 1. Wallet Overview ———
@api_view(["GET"])
@permission_classes([IsAuthenticated])
def wallet_overview(request):
    def run():
        _require_rider(request.user)
        return Response(wallet_summary(get_wallet(request.user)))
    return _handle(run)


# ——— 2. Deposit ———
@api_view(["POST"])
@permission_classes([IsAuthenticated])
@throttle_classes([DepositThrottle])
def wallet_deposit(request):
    def run():
        _require_rider(request.user)
        serializer = InitializePaymentSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        deposit = initialize_payment(
            user=request.user,
            amount=serializer.validated_data["amount"],
            currency=serializer.validated_data.get("currency", "GHS"),
        )
        return Response(
            {"message": "Deposit initialized", **WalletDepositSerializer(deposit).data},
            status=201
        )
    return _handle(run)


# ——— 3. Verify Deposit ———
@api_view(["GET"])
@permission_classes([IsAuthenticated])
@throttle_classes([DepositThrottle])
def deposit_verify(request, reference):
    def run():
        _require_rider(request.user)
        deposit = verify_deposit(reference, user=request.user)
        body = WalletDepositSerializer(deposit).data
        body["wallet"] = wallet_summary(get_wallet(request.user))
        return Response(body)
    return _handle(run)


# ——— 4. Deposit History ———
@api_view(["GET"])
@permission_classes([IsAuthenticated])
def deposit_history(request):
    def run():
        _require_rider(request.user)
        qs = WalletDeposit.objects.filter(user=request.user)
        if request.query_params.get("status"):
            qs = qs.filter(status=request.query_params["status"].upper())
        paginator = PageNumberPagination()
        paginator.page_size = 20
        page = paginator.paginate_queryset(qs, request)
        return paginator.get_paginated_response(WalletDepositSerializer(page, many=True).data)
    return _handle(run)


# ——— 5. Paystack Webhook ———
@api_view(["POST"])
@authentication_classes([])
@permission_classes([AllowAny])
@throttle_classes([])
def paystack_webhook(request):
    secret = getattr(settings, "PAYSTACK_SECRET_KEY", "")
    raw = request.body
    signature = request.headers.get("x-paystack-signature", "")
    expected = hmac.new(secret.encode(), raw, hashlib.sha512).hexdigest() if secret else ""
    if not secret or not hmac.compare_digest(expected, signature):
        return Response(status=401)
    try:
        event = json.loads(raw)
    except ValueError:
        return Response(status=400)

    if event.get("event") == "charge.success":
        data = event.get("data") or {}
        reference = data.get("reference")
        if reference:
            try:
                deposit = WalletDeposit.objects.get(reference=reference)
                if deposit.status != "completed":
                    deposit.status = "completed"
                    deposit.save()
            except WalletDeposit.DoesNotExist:
                pass
    return Response(status=200)
