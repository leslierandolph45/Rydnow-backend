"""
Admin for wallets, the read-only ledger and withdrawals. Import this module from admin.py.
"""
import csv

from django.conf import settings
from django.contrib import admin, messages
from django.http import HttpResponse

from .models_wallet import RiderWallet, WalletTransaction, WithdrawalRequest
from .wallet_services import WalletError, transition_withdrawal
from . import wallet_conf as conf


class BalanceStatusFilter(admin.SimpleListFilter):
    title = "balance status"
    parameter_name = "balance_status"

    def lookups(self, request, model_admin):
        return [("low", "Below minimum"), ("ok", "Meets minimum")]

    def queryset(self, request, queryset):
        if self.value() == "low":
            return queryset.filter(available_balance__lt=conf.min_balance())
        if self.value() == "ok":
            return queryset.filter(available_balance__gte=conf.min_balance())


@admin.site.register(RiderWallet)
class RiderWalletAdmin(admin.ModelAdmin):
    list_display = ("rider", "balance", "available_balance", "pending_earnings",
                    "eligible", "is_frozen", "updated_at")
    list_filter = (BalanceStatusFilter, "is_frozen", "currency")
    search_fields = ("rider__full_name", "rider__phone")
    list_select_related = ("rider",)
    readonly_fields = ("rider", "balance", "available_balance", "pending_earnings",
                       "currency", "created_at", "updated_at")
    actions = ["freeze_wallets", "unfreeze_wallets", "export_csv"]

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    @admin.display(boolean=True, description="Eligible for jobs")
    def eligible(self, obj):
        return obj.is_eligible

    @admin.action(description="Freeze selected wallets")
    def freeze_wallets(self, request, queryset):
        n = queryset.update(is_frozen=True, freeze_reason=f"Frozen by {request.user}")
        self.message_user(request, f"{n} wallet(s) frozen.")

    @admin.action(description="Unfreeze selected wallets")
    def unfreeze_wallets(self, request, queryset):
        n = queryset.update(is_frozen=False, freeze_reason="")
        self.message_user(request, f"{n} wallet(s) unfrozen.")

    @admin.action(description="Export selected to CSV")
    def export_csv(self, request, queryset):
        response = HttpResponse(content_type="text/csv")
        response["Content-Disposition"] = 'attachment; filename="wallets.csv"'
        writer = csv.writer(response)
        writer.writerow(["Rider", "Phone", "Balance", "Available", "Pending", "Frozen"])
        for w in queryset.select_related("rider"):
            writer.writerow([w.rider.full_name, getattr(w.rider, "phone", ""), w.balance,
                             w.available_balance, w.pending_earnings, w.is_frozen])
        return response


@admin.register(WalletTransaction)
class WalletTransactionAdmin(admin.ModelAdmin):
    """Read-only ledger: nobody edits or deletes financial history."""
    list_display = ("created_at", "wallet", "type", "amount", "balance_after", "status", "reference")
    list_filter = ("type", "status", "created_at")
    search_fields = ("reference", "provider_reference", "wallet__rider__full_name", "wallet__rider__phone")
    list_select_related = ("wallet__rider",)
    date_hierarchy = "created_at"
    list_per_page = 50

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(WithdrawalRequest)
class WithdrawalRequestAdmin(admin.ModelAdmin):
    list_display = ("reference", "wallet", "amount", "fee", "payout_network",
                    "masked_account", "status", "created_at", "reviewed_by")
    list_filter = ("status", "payout_network", "created_at")
    search_fields = ("reference", "provider_reference", "wallet__rider__full_name", "wallet__rider__phone")
    list_select_related = ("wallet__rider", "reviewed_by")
    date_hierarchy = "created_at"
    actions = ["approve", "reject", "mark_processing"]

    def get_readonly_fields(self, request, obj=None):
        editable = {"admin_notes"}
        return [f.name for f in self.model._meta.fields if f.name not in editable]

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def _run(self, request, queryset, new_status, reason=""):
        if not request.user.has_perm("api.approve_withdrawal"):
            self.message_user(request, "You do not have permission to review withdrawals.", messages.ERROR)
            return
        ok = 0
        for pk in queryset.values_list("pk", flat=True):
            try:
                transition_withdrawal(pk, new_status, admin_user=request.user, reason=reason)
                ok += 1
            except WalletError as exc:
                self.message_user(request, f"{pk}: {exc.message}", messages.WARNING)
        self.message_user(request, f"{ok} withdrawal(s) moved to '{new_status}'.")

    @admin.action(description="Approve selected")
    def approve(self, request, queryset):
        self._run(request, queryset, "approved")

    @admin.action(description="Reject selected (funds returned)")
    def reject(self, request, queryset):
        self._run(request, queryset, "rejected", reason="Rejected by admin")

    @admin.action(description="Mark as sent for payout")
    def mark_processing(self, request, queryset):
        self._run(request, queryset, "processing")
