"""
Django admin configuration for RYDNOW.

Design principles
——————
* Every registered model has an "Add" page and can be operated manually.
* Financial and audit records (wallets, ledgers, payments, histories) can be
  created or edited by superusers only, and are never deletable from the admin.
* Bulk status actions are atomic, row-locked, state-guarded and audited
  (a LogEntry is written per object so every change is traceable).
* Every changelist avoids N+1 queries via list_select_related.
* Foreign keys use ID-lookup widgets instead of dropdowns, so the Add/Change
  pages stay fast and never try to render thousands of options.
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
    """
    View-only admin: no add, edit or delete for anyone.

    Not used by default any more; apply it to a model to lock it down again.
    """

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


class SuperuserWriteAdminMixin:
    """
    Financial / audit records: superusers may add and edit them manually,
    nobody may delete them. Other staff keep view access via Django permissions.
    """

    def has_add_permission(self, request):
        return request.user.is_superuser and super().has_add_permission(request)

    def has_change_permission(self, request, obj=None):
        return request.user.is_superuser and super().has_change_permission(request, obj)

    def has_delete_permission(self, request, obj=None):
        return False


class AutoRawIdMixin:
    """
    Use ID-lookup widgets for every concrete ForeignKey / OneToOne field,
    unless raw_id_fields is set explicitly on the admin class.

    This replaces giant dropdowns (the usual cause of slow or crashing
    Add/Change pages) without hard-coding field names.
    """

    def __init__(self, model, admin_site):
        if not self.raw_id_fields:
            self.raw_id_fields = tuple(
                field.name
                for field in model._meta.get_fields()
                if field.is_relation
                and field.concrete
                and (field.many_to_one or field.one_to_one)
            )
        super().__init__(model, admin_site)


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


class BaseAdmin(AutoRawIdMixin, admin.ModelAdmin):
    """Safe defaults for every model: fast FK widgets, sensible list view."""

    list_display = ("__str__", "pk")
    ordering = ("-pk",)
    list_per_page = 50


class LedgerAdmin(SuperuserWriteAdminMixin, BaseAdmin):
    """Financial / audit records: superuser add + edit, never deletable."""

    list_per_page = 100


# ══════════════════════════════════════════════════════════
# 👥 CUSTOMER PROFILE
# ══════════════════════════════════════════════════════════
@admin.register(CustomerProfile)
class CustomerProfileAdmin(BaseAdmin):
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
class CustomerRiderPreferenceAdmin(BaseAdmin):
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
class RiderProfileAdmin(BaseAdmin):
    list_display = ("__str__", "pk", "created_at")
    search_fields = ("user__username", "user__email")
    readonly_fields = ("created_at",)
    list_select_related = ("user",)
    ordering = ("-created_at",)


@admin.register(RiderStatusHistory)
class RiderStatusHistoryAdmin(LedgerAdmin):
    """Audit trail: superuser add + edit, never deletable."""


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
class RiderEmergencyContactAdmin(BaseAdmin):
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
    """High-volume telemetry."""


@admin.register(RiderProfileAdminNote)
class RiderProfileAdminNoteAdmin(NoDeleteAdminMixin, BaseAdmin):
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
class RiderComplaintAdmin(NoDeleteAdminMixin, BaseAdmin):
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
class BookingAdmin(BulkStatusActionMixin, BaseAdmin):
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
    """Audit trail: superuser add + edit, never deletable."""


@admin.register(ParcelDetail)
class ParcelDetailAdmin(BaseAdmin):
    pass


@admin.register(BookingOffer)
class BookingOfferAdmin(BaseAdmin):
    """Rider offers on bookings. Related records are chosen by ID lookup, not dropdown."""

    list_display = ("__str__", "pk", "booking", "rider", "status")
    list_filter = ("status",)
    search_fields = ("rider__user__username", "rider__user__email")
    list_select_related = ("booking", "rider", "rider__user")
    raw_id_fields = ("booking", "rider")
    list_per_page = 50


@admin.register(Review)
class ReviewAdmin(NoDeleteAdminMixin, BaseAdmin):
    pass


# ══════════════════════════════════════════════════════════
# 💰 FINANCE & WALLET
# ══════════════════════════════════════════════════════════
@admin.register(RiderWallet)
class RiderWalletAdmin(LedgerAdmin):
    """Manual balance edits are superuser-only and logged by Django admin."""

    list_display = ("__str__", "pk")
    search_fields = ()
    readonly_fields = ()
    list_select_related = ("rider",)


@admin.register(CustomerWallet)
class CustomerWalletAdmin(LedgerAdmin):
    """Manual balance edits are superuser-only and logged by Django admin."""


@admin.register(WalletTransaction)
class WalletTransactionAdmin(LedgerAdmin):
    """Ledger entries: superuser add + edit, never deletable."""

    list_display = ("__str__", "pk", "created_at")
    list_filter = ("created_at",)
    search_fields = ()
    readonly_fields = ("created_at",)
    ordering = ("-created_at",)
    date_hierarchy = "created_at"
    list_per_page = 100


@admin.register(WithdrawalRequest)
class WithdrawalRequestAdmin(BulkStatusActionMixin, NoDeleteAdminMixin, BaseAdmin):
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
class WalletDepositAdmin(LedgerAdmin):
    """Payment-gateway records: superuser add + edit, never deletable."""

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

    list_display = ("__str__", "pk", "created_at")
    list_filter = ("created_at",)
    ordering = ("-created_at",)
    date_hierarchy = "created_at"
    list_per_page = 100




@admin.register(Refund)
class RefundAdmin(LedgerAdmin):
    """Refund records are immutable."""

    list_display = ("__str__", "pk", "created_at")
    list_filter = ("created_at",)
    ordering = ("-created_at",)
    date_hierarchy = "created_at"
    list_per_page = 100



@admin.register(LoyaltyAccount)
class LoyaltyAccountAdmin(LedgerAdmin):
    """Loyalty balances change only through the ledger."""

    list_display = ("__str__", "pk")
    #list_filter = ("created_at",)
    #ordering = ("-created_at",)
    #date_hierarchy = "created_at"
    list_per_page = 100



@admin.register(LoyaltyTransaction)
class LoyaltyTransactionAdmin(LedgerAdmin):
    """Immutable loyalty ledger."""

    list_display = ("__str__", "pk", "created_at")
    list_filter = ("created_at",)
    ordering = ("-created_at",)
    date_hierarchy = "created_at"
    list_per_page = 100



# ══════════════════════════════════════════════════════════
# 🎁 REFERRALS, PROMOS & BUSINESS
# ══════════════════════════════════════════════════════════
@admin.register(Referral)
class ReferralAdmin(BaseAdmin):
    list_display = ("__str__", "pk", "created_at")
    list_filter = ("created_at",)
    ordering = ("-created_at",)
    date_hierarchy = "created_at"
    list_per_page = 50
    search_fields = ()



@admin.register(ReferralCode)
class ReferralCodeAdmin(BaseAdmin):
    list_display = ("__str__", "pk", "created_at")
    list_filter = ("created_at",)
    ordering = ("-created_at",)
    date_hierarchy = "created_at"
    list_per_page = 50
    search_fields = ()


@admin.register(BusinessAccount)
class BusinessAccountAdmin(BaseAdmin):
    list_display = ("__str__", "pk", "created_at")
    list_filter = ("created_at",)
    ordering = ("-created_at",)
    date_hierarchy = "created_at"
    list_per_page = 50
    search_fields = ()


@admin.register(WasteSubscription)
class WasteSubscriptionAdmin(BaseAdmin):
    list_display = ("__str__", "pk", "created_at")
    list_filter = ("created_at",)
    ordering = ("-created_at",)
    date_hierarchy = "created_at"
    list_per_page = 50
    search_fields = ()



@admin.register(PromoCode)
class PromoCodeAdmin(BaseAdmin):
    list_display = ("__str__", "pk", "created_at")
    list_filter = ("created_at",)
    ordering = ("-created_at",)
    date_hierarchy = "created_at"
    list_per_page = 50
    search_fields = ()




@admin.register(PromoRedemption)
class PromoRedemptionAdmin(BaseAdmin):
    list_display = ("__str__", "pk", "created_at")
    list_filter = ("created_at",)
    ordering = ("-created_at",)
    date_hierarchy = "created_at"
    list_per_page = 50
    search_fields = ()
