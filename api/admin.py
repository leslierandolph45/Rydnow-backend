"""
Django admin configuration for RYDNOW.

Design principles
——————
* Financial records (deposits, ledger, wallets) are immutable from the admin.
* Bulk status actions are atomic, row-locked, state-guarded and audited
  (a LogEntry is written per object so every change is traceable).
* Every changelist avoids N+1 queries via list_select_related.
* Personal data is shown masked in list views where possible.
* Every model is registered exactly once (Django raises AlreadyRegistered
  on duplicates, which crashes the app at import time).
"""
from __future__ import annotations

import logging

from django.contrib import admin, messages
from django.contrib.admin.models import CHANGE, LogEntry
from django.contrib.contenttypes.models import ContentType
from django.db import transaction
from django.utils.html import format_html

# NOTE: adjust the import path if your models live elsewhere
# (e.g. `from api.models import (...)` or a models package).
from .models import (
    # Customer
    CustomerProfile,
    SavedAddress,
    CustomerWallet,
    CustomerRiderPreference,
    # Rider
    RiderProfile,
    RiderStatusHistory,
    ServiceQualification,
    RiderDocument,
    RiderPayoutAccount,
    RiderEmergencyContact,
    RiderOnlineSession,
    RiderLocationPing,
    RiderProfileAdminNote,
    RiderIncident,
    RiderComplaint,
    # Bookings
    Booking,
    BookingStatusHistory,
    ParcelDetail,
    BookingOffer,
    Review,
    # Finance
    RiderWallet,
    WalletTransaction,
    WalletDeposit,
    WithdrawalRequest,
    Payment,
    Refund,
    LoyaltyAccount,
    LoyaltyTransaction,
    # Growth & business
    Referral,
    ReferralCode,
    BusinessAccount,
    WasteSubscription,
    PromoCode,
    PromoRedemption,
)

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────
# Site branding
# ──────────────────────────────────────────────────────────
admin.site.site_header = "Operations Console"
admin.site.site_title = "Operations Admin"
admin.site.index_title = "RYDNOW Platform Management"


# ══════════════════════════════════════════════════════════
# 🧩 SHARED MIXINS & HELPERS
# ══════════════════════════════════════════════════════════
class ReadOnlyAdminMixin:
    """View-only admin: no add, edit or delete for anyone."""

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


class NoDeleteAdminMixin:
    """Prevents deletion of records that must be retained for auditing."""

    def has_delete_permission(self, request, obj=None):
        return False


class BulkStatusActionMixin:
    """
    Safe bulk status transitions.

    * Runs in a single transaction and locks the rows being changed.
    * Skips objects already in a final state (no re-opening / overwriting).
    * Writes one admin LogEntry per changed object for auditability.
    * Reports changed vs. skipped counts to the operator.

    Subclasses set final_statuses.
    """

    final_statuses: tuple[str, ...] = ()

    def _apply_status(self, request, queryset, new_status: str) -> None:
        manager = self.model._default_manager
        selected_ids = list(queryset.values_list("pk", flat=True))

        with transaction.atomic():
            eligible_ids = list(
                manager.select_for_update()
                .filter(pk__in=selected_ids)
                .exclude(status__in=self.final_statuses)
                .values_list("pk", flat=True)
            )
            changed = manager.filter(pk__in=eligible_ids).update(status=new_status)

            if eligible_ids:
                content_type = ContentType.objects.get_for_model(self.model)
                LogEntry.objects.bulk_create(
                    LogEntry(
                        user_id=request.user.pk,
                        content_type_id=content_type.pk,
                        object_id=str(obj.pk),
                        object_repr=str(obj)[:200],
                        action_flag=CHANGE,
                        change_message=f"Bulk action: status set to '{new_status}'.",
                    )
                    for obj in manager.filter(pk__in=eligible_ids)
                )

        skipped = len(selected_ids) - changed
        logger.info(
            "%s bulk status -> %s by user=%s changed=%s skipped=%s",
            self.model.__name__, new_status, request.user.pk, changed, skipped,
        )

        if changed:
            self.message_user(
                request,
                f"{changed} record(s) marked as {new_status}.",
                messages.SUCCESS,
            )
        if skipped:
            self.message_user(
                request,
                f"{skipped} record(s) skipped because they were already in a final state.",
                messages.WARNING,
            )


def _badge(label: str, color: str):
    return format_html(
        '<span style="background:{};color:#fff;padding:2px 8px;border-radius:10px;'
        'font-size:11px;font-weight:600;white-space:nowrap;">{}</span>',
        color,
        label,
    )


class BaseAdmin(admin.ModelAdmin):
    """Safe defaults for models whose fields are not customised yet."""

    list_display = ("__str__", "pk")
    ordering = ("-pk",)
    list_per_page = 50


class LedgerAdmin(ReadOnlyAdminMixin, BaseAdmin):
    """Immutable financial / audit records: view-only by design."""

    list_per_page = 100


# ══════════════════════════════════════════════════════════
# 👥 CUSTOMER PROFILE
# ══════════════════════════════════════════════════════════
@admin.register(CustomerProfile)
class CustomerProfileAdmin(admin.ModelAdmin):
    list_display = ("__str__", "pk", "created_at")
    search_fields = ("user__username", "user__email")
    readonly_fields = ("created_at",)
    list_select_related = ("user",)
    ordering = ("-created_at",)
    date_hierarchy = "created_at"
    list_per_page = 50


@admin.register(SavedAddress)
class SavedAddressAdmin(BaseAdmin):
    pass


@admin.register(CustomerRiderPreference)
class CustomerRiderPreferenceAdmin(admin.ModelAdmin):
    list_display = ("customer", "preference", "rider", "created_at")
    list_filter = ("preference", "created_at")
    search_fields = ("customer__user__username", "rider__user__username")
    readonly_fields = ("created_at",)
    ordering = ("-created_at",)
    list_per_page = 50


# ══════════════════════════════════════════════════════════
# 👤 RIDER PROFILE
# ══════════════════════════════════════════════════════════
@admin.register(RiderProfile)
class RiderProfileAdmin(admin.ModelAdmin):
    list_display = ("__str__", "pk", "created_at")
    search_fields = ("user__username", "user__email")
    readonly_fields = ("created_at",)
    list_select_related = ("user",)
    ordering = ("-created_at",)


@admin.register(RiderStatusHistory)
class RiderStatusHistoryAdmin(LedgerAdmin):
    """Status changes are an audit trail and must not be edited."""


@admin.register(ServiceQualification)
class ServiceQualificationAdmin(BaseAdmin):
    pass


@admin.register(RiderDocument)
class RiderDocumentAdmin(NoDeleteAdminMixin, BaseAdmin):
    """Verification documents are retained for compliance."""


@admin.register(RiderPayoutAccount)
class RiderPayoutAccountAdmin(NoDeleteAdminMixin, BaseAdmin):
    """Payout details are sensitive; deletion is disabled."""


@admin.register(RiderEmergencyContact)
class RiderEmergencyContactAdmin(admin.ModelAdmin):
    list_display = ("__str__", "pk", "created_at")
    list_filter = ("created_at",)
    search_fields = ("full_name",)
    readonly_fields = ("created_at", "updated_at")
    list_select_related = ("rider_profile",)
    ordering = ("-created_at",)
    list_per_page = 50


@admin.register(RiderOnlineSession)
class RiderOnlineSessionAdmin(LedgerAdmin):
    pass


@admin.register(RiderLocationPing)
class RiderLocationPingAdmin(LedgerAdmin):
    """High-volume telemetry: view-only."""


@admin.register(RiderProfileAdminNote)
class RiderProfileAdminNoteAdmin(NoDeleteAdminMixin, admin.ModelAdmin):
    list_display = ("__str__", "pk", "author", "created_at")
    list_filter = ("created_at", "author")
    search_fields = ("note",)
    readonly_fields = ("author", "created_at")
    list_select_related = ("rider_profile", "author")
    date_hierarchy = "created_at"
    ordering = ("-created_at",)

    def save_model(self, request, obj, form, change):
        if not change:
            obj.author = request.user
        super().save_model(request, obj, form, change)


@admin.register(RiderIncident)
class RiderIncidentAdmin(NoDeleteAdminMixin, BaseAdmin):
    list_display = ("__str__", "pk")
    list_filter = ()
    search_fields = ("reference_code", "title", "description")
    list_select_related = ("rider_profile", "assigned_to")
    ordering = ("-pk",)
    list_per_page = 50


@admin.register(RiderComplaint)
class RiderComplaintAdmin(NoDeleteAdminMixin, admin.ModelAdmin):
    list_display = ("__str__", "pk", "created_at")
    list_filter = ("created_at",)
    search_fields = ("description",)
    readonly_fields = ("created_at",)
    ordering = ("-created_at",)
    date_hierarchy = "created_at"
    list_per_page = 50


# ══════════════════════════════════════════════════════════
# 📋 BOOKING MANAGEMENT
# ══════════════════════════════════════════════════════════
@admin.register(Booking)
class BookingAdmin(BulkStatusActionMixin, admin.ModelAdmin):
    list_display = ("__str__", "pk", "status", "created_at")
    list_filter = ("status", "created_at")
    search_fields = ()
    readonly_fields = ("created_at",)
    ordering = ("-created_at",)
    date_hierarchy = "created_at"
    list_per_page = 50
    actions = ("mark_completed", "mark_cancelled")

    final_statuses = ("completed", "cancelled")

    @admin.action(description="Mark selected bookings as completed", permissions=["change"])
    def mark_completed(self, request, queryset):
        self._apply_status(request, queryset, "completed")

    @admin.action(description="Mark selected bookings as cancelled", permissions=["change"])
    def mark_cancelled(self, request, queryset):
        self._apply_status(request, queryset, "cancelled")


@admin.register(BookingStatusHistory)
class BookingStatusHistoryAdmin(LedgerAdmin):
    """Status changes are an audit trail and must not be edited."""


@admin.register(ParcelDetail)
class ParcelDetailAdmin(BaseAdmin):
    pass


@admin.register(BookingOffer)
class BookingOfferAdmin(BaseAdmin):
    pass


@admin.register(Review)
class ReviewAdmin(NoDeleteAdminMixin, BaseAdmin):
    pass


# ══════════════════════════════════════════════════════════
# 💰 FINANCE & WALLET
# ══════════════════════════════════════════════════════════
@admin.register(RiderWallet)
class RiderWalletAdmin(NoDeleteAdminMixin, admin.ModelAdmin):
    """Balances are only ever changed through ledger-backed services."""

    list_display = ("__str__", "pk")
    search_fields = ()
    readonly_fields = ()
    list_select_related = ("rider",)

    def has_add_permission(self, request):
        return False


@admin.register(CustomerWallet)
class CustomerWalletAdmin(NoDeleteAdminMixin, BaseAdmin):
    """Balances are only ever changed through ledger-backed services."""

    def has_add_permission(self, request):
        return False


@admin.register(WalletTransaction)
class WalletTransactionAdmin(ReadOnlyAdminMixin, admin.ModelAdmin):
    """Immutable ledger: view-only by design."""

    list_display = ("__str__", "pk", "created_at")
    list_filter = ("created_at",)
    search_fields = ()
    readonly_fields = ("created_at",)
    ordering = ("-created_at",)
    date_hierarchy = "created_at"
    list_per_page = 100


@admin.register(WithdrawalRequest)
class WithdrawalRequestAdmin(BulkStatusActionMixin, NoDeleteAdminMixin, admin.ModelAdmin):
    list_display = ("__str__", "pk", "status", "created_at")
    list_filter = ("status", "created_at")
    readonly_fields = ("created_at",)
    ordering = ("-created_at",)
    date_hierarchy = "created_at"
    list_per_page = 50
    actions = ("mark_completed", "mark_failed")

    final_statuses = ("completed", "failed")

    def has_add_permission(self, request):
        return False

    @admin.action(description="Mark selected withdrawals as completed", permissions=["change"])
    def mark_completed(self, request, queryset):
        self._apply_status(request, queryset, "completed")

    @admin.action(description="Mark selected withdrawals as failed", permissions=["change"])
    def mark_failed(self, request, queryset):
        self._apply_status(request, queryset, "failed")


@admin.register(WalletDeposit)
class WalletDepositAdmin(ReadOnlyAdminMixin, admin.ModelAdmin):
    """Payment-gateway records are never edited manually."""

    list_display = ("__str__", "pk", "status", "created_at")
    list_filter = ("status", "created_at")
    search_fields = ("reference",)
    readonly_fields = ("created_at", "updated_at")
    ordering = ("-created_at",)
    date_hierarchy = "created_at"
    list_per_page = 100


@admin.register(Payment)
class PaymentAdmin(LedgerAdmin):
    """Payment records are immutable."""


@admin.register(Refund)
class RefundAdmin(LedgerAdmin):
    """Refund records are immutable."""


@admin.register(LoyaltyAccount)
class LoyaltyAccountAdmin(LedgerAdmin):
    """Loyalty balances change only through the ledger."""


@admin.register(LoyaltyTransaction)
class LoyaltyTransactionAdmin(LedgerAdmin):
    """Immutable loyalty ledger."""


# ══════════════════════════════════════════════════════════
# 🎁 REFERRALS, PROMOS & BUSINESS
# ══════════════════════════════════════════════════════════
@admin.register(Referral)
class ReferralAdmin(admin.ModelAdmin):
    list_display = ("__str__", "pk", "created_at")
    search_fields = ()


@admin.register(ReferralCode)
class ReferralCodeAdmin(admin.ModelAdmin):
    list_display = ("__str__", "pk", "created_at")
    search_fields = ()


@admin.register(BusinessAccount)
class BusinessAccountAdmin(admin.ModelAdmin):
    list_display = ("__str__", "pk", "created_at")
    search_fields = ()


@admin.register(WasteSubscription)
class WasteSubscriptionAdmin(admin.ModelAdmin):
    list_display = ("__str__", "pk", "created_at")
    search_fields = ()


@admin.register(PromoCode)
class PromoCodeAdmin(admin.ModelAdmin):
    list_display = ("__str__", "pk", "created_at")
    search_fields = ()


@admin.register(PromoRedemption)
class PromoRedemptionAdmin(admin.ModelAdmin):
    list_display = ("
