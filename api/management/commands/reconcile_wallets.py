"""
Place at: api/management/commands/reconcile_wallets.py
Run daily (cron / Render cron job):  python manage.py reconcile_wallets

Checks, per wallet:
  balance          == sum(COMPLETED ledger amounts)
  pending_earnings == sum(PENDING ledger amounts)
  available_balance == balance - funds reserved by active withdrawals
Exits non-zero on any mismatch so your scheduler can alert.
"""
from decimal import Decimal

from django.core.management.base import BaseCommand, CommandError
from django.db.models import DecimalField, F, Q, Sum, Value
from django.db.models.functions import Coalesce

from api.models_wallet import RiderWallet, WithdrawalRequest, ACTIVE_WITHDRAWAL_STATUSES

ZERO = Value(Decimal("0.00"), output_field=DecimalField(max_digits=14, decimal_places=2))


def _sum(expr, **flt):
    return Coalesce(Sum(expr, filter=Q(**flt)), ZERO, output_field=DecimalField(max_digits=14, decimal_places=2))


class Command(BaseCommand):
    help = "Verify wallet balances against the transaction ledger."

    def handle(self, *args, **options):
        wallets = RiderWallet.objects.annotate(
            ledger_completed=_sum("transactions__amount", transactions__status="completed"),
            ledger_pending=_sum("transactions__amount", transactions__status="pending"),
        ).iterator(chunk_size=500)

        reserved = {
            row["wallet_id"]: row["total"]
            for row in WithdrawalRequest.objects.filter(status__in=ACTIVE_WITHDRAWAL_STATUSES)
            .values("wallet_id")
            .annotate(total=Sum(F("amount") + F("fee")))
        }

        problems, checked = [], 0
        for w in wallets:
            checked += 1
            expected_available = w.balance - reserved.get(w.pk, Decimal("0.00"))
            if w.balance != w.ledger_completed:
                problems.append(f"{w.pk}: balance {w.balance} != ledger {w.ledger_completed}")
            if w.pending_earnings != w.ledger_pending:
                problems.append(f"{w.pk}: pending {w.pending_earnings} != ledger {w.ledger_pending}")
            if w.available_balance != expected_available:
                problems.append(f"{w.pk}: available {w.available_balance} != expected {expected_available}")

        if problems:
            for p in problems:
                self.stderr.write(p)
            raise CommandError(f"{len(problems)} mismatch(es) across {checked} wallets.")
        self.stdout.write(self.style.SUCCESS(f"{checked} wallets reconciled, no mismatches."))
