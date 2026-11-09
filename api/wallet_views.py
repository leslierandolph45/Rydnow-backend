"""
api/wallet_views.py  —  RYDNOW rider wallet (GET /api/rider/wallet/)

One call powers the whole wallet screen of the rider app:
balance, job eligibility, earnings insights, recent activity,
loyalty tier, and smart nudges that keep riders online and earning.

ASSUMED MODELS (rename to match yours)
—————————————
RiderWallet(rider_profile 1-1, balance, currency, is_active, updated_at)
WalletTransaction(wallet FK related_name="transactions", amount (always +),
                  direction "credit"|"debit", kind, description,
                  reference, created_at)
  kind: trip_earning | top_up | bonus | referral | payout | commission | penalty
RiderProfile.user -> related_name "rider_profile"
"""
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Count, DecimalField, Q, Sum, Value
from django.db.models.functions import Coalesce
from django.utils import timezone
from rest_framework import permissions, serializers
from rest_framework.response import Response
from rest_framework.throttling import UserRateThrottle
from rest_framework.views import APIView

from .models import RiderWallet, WalletTransaction, WithdrawalRequest


ZERO = Decimal("0.00")
MONEY = DecimalField(max_digits=12, decimal_places=2)

MIN_WALLET = Decimal(getattr(settings, "RIDER_MIN_WALLET", "50.00"))
LOW_BALANCE_BUFFER = Decimal("20.00")
WEEKLY_GOAL_DEFAULT = Decimal("500.00")

# Loyalty tiers: (name, min completed trips in last 30 days, commission discount %)
TIERS = [
    ("Bronze", 0, 0),
    ("Silver", 40, 2),
    ("Gold", 100, 4),
    ("Platinum", 200, 6),
]


# ————————————————————— selectors
def get_or_create_wallet(rider: RiderProfile) -> RiderWallet:
    """Race-safe: two parallel first requests can't create duplicate wallets."""
    try:
        with transaction.atomic():
            wallet, _ = RiderWallet.objects.get_or_create(rider_profile=rider)
        return wallet
    except IntegrityError:
        return RiderWallet.objects.get(rider_profile=rider)


def _sum(qs):
    return qs.aggregate(t=Coalesce(Sum("amount"), Value(ZERO), output_field=MONEY))["t"]


def earnings_summary(wallet: RiderWallet) -> dict:
    now = timezone.now()
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week_start = today - timedelta(days=today.weekday())
    earn = wallet.transactions.filter(
        direction="credit", kind__in=["trip_earning", "bonus", "referral"]
    )
    return {
        "today": _sum(earn.filter(created_at__gte=today)),
        "this_week": _sum(earn.filter(created_at__gte=week_start)),
        "this_month": _sum(earn.filter(created_at__gte=today.replace(day=1))),
        "trips_this_week": wallet.transactions.filter(
            kind="trip_earning", created_at__gte=week_start
        ).count(),
    }


def loyalty_tier(wallet: RiderWallet) -> dict:
    since = timezone.now() - timedelta(days=30)
    trips = wallet.transactions.filter(kind="trip_earning", created_at__gte=since).count()
    current = max((t for t in TIERS if trips >= t[1]), key=lambda t: t[1])
    nxt = next((t for t in TIERS if t[1] > current[1]), None)
    return {
        "name": current[0],
        "commission_discount_pct": current[2],
        "trips_last_30_days": trips,
        "next_tier": nxt[0] if nxt else None,
        "trips_to_next_tier": max(nxt[1] - trips, 0) if nxt else 0,
    }


def smart_nudges(wallet: RiderWallet, earnings: dict, tier: dict) -> list:
    """Friendly, actionable messages that build trust and keep riders active."""
    nudges = []
    shortfall = max(MIN_WALLET - wallet.balance, ZERO)
    if shortfall > 0:
        nudges.append({
            "type": "top_up_required",
            "severity": "critical",
            "message": f"Add GHS {shortfall} to start receiving jobs again.",
        })
    elif wallet.balance < MIN_WALLET + LOW_BALANCE_BUFFER:
        nudges.append({
            "type": "low_balance",
            "severity": "warning",
            "message": f"Your balance is close to the GHS {MIN_WALLET} minimum. Top up to avoid missing jobs.",
        })
    goal_left = WEEKLY_GOAL_DEFAULT - earnings["this_week"]
    if 0 < goal_left <= WEEKLY_GOAL_DEFAULT * Decimal("0.3"):
        nudges.append({
            "type": "weekly_goal",
            "severity": "info",
            "message": f"You're GHS {goal_left} away from your weekly goal. Keep going!",
        })
    if tier["next_tier"] and tier["trips_to_next_tier"] <= 10:
        nudges.append({
            "type": "tier_progress",
            "severity": "info",
            "message": f"{tier['trips_to_next_tier']} more trips to reach {tier['next_tier']} and lower your commission.",
        })
    return nudges


# ————————————————————— serializers
class TransactionSerializer(serializers.ModelSerializer):
    class Meta:
        model = WalletTransaction
        fields = ["id", "kind", "direction", "amount", "description", "reference", "created_at"]


# ——————————————————————— view
class WalletThrottle(UserRateThrottle):
    rate = "60/min"


class RiderWalletView(APIView):
    """GET /api/rider/wallet/?transactions=10"""
    permission_classes = [permissions.IsAuthenticated]
    throttle_classes = [WalletThrottle]

    def get(self, request):
        rider = getattr(request.user, "rider_profile", None)
        if rider is None:
            return Response(
                {"code": "rider_profile_not_found", "detail": "Rider profile not found."},
                status=404,
            )

        wallet = get_or_create_wallet(rider)

        try:
            limit = min(max(int(request.query_params.get("transactions", 10)), 0), 50)
        except ValueError:
            limit = 10

        shortfall = max(MIN_WALLET - wallet.balance, ZERO)
        earnings = earnings_summary(wallet)
        tier = loyalty_tier(wallet)
        recent = wallet.transactions.order_by("-created_at")[:limit]

        pending_payout = _sum(wallet.transactions.filter(kind="payout")) \
            if hasattr(WalletTransaction, "status") else ZERO

        data = {
            "balance": wallet.balance,
            "currency": wallet.currency,
            "is_active": wallet.is_active,
            "updated_at": wallet.updated_at,
            "job_eligibility": {
                "can_receive_jobs": wallet.is_active and shortfall == 0,
                "minimum_required": MIN_WALLET,
                "shortfall": shortfall,
            },
            "pending_payout": pending_payout,
            "earnings": earnings,
            "weekly_goal": {
                "target": WEEKLY_GOAL_DEFAULT,
                "progress_pct": min(int(earnings["this_week"] / WEEKLY_GOAL_DEFAULT * 100), 100),
            },
            "loyalty": tier,
            "nudges": smart_nudges(wallet, earnings, tier),
            "recent_transactions": TransactionSerializer(recent, many=True).data,
        }
        response = Response(data)
        response["Cache-Control"] = "private, no-store"
        return response
