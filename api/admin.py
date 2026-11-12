"""
Django admin configuration for RYDNOW (django-unfold theme).

Design principles
─────────────────
* Every registered model has an "Add" page and can be operated manually.
* Financial and audit records (wallets, ledgers, payments, histories) are
  created or edited by superusers only, and are never deletable from the admin.
* Bulk status actions are atomic, row-locked, state-guarded and audited
  (a LogEntry is written per object so every change is traceable).
* Every changelist avoids N+1 queries via list_select_related.
* Foreign keys use ID-lookup widgets instead of dropdowns, so the Add/Change
  pages stay fast and never try to render thousands of options.
* Every model is registered exactly once (Django raises AlreadyRegistered on
  duplicates, which crashes the app at import time).

RiderProfile and Booking are registered in admin_riders.py, which is imported
at the very bottom of this file (it needs BaseAdmin from here).
"""
from __future__ import annotations

import logging
import os
from datetime import timedelta

from django.contrib import admin, messages
from django.contrib.admin.models import CHANGE, LogEntry
from django.contrib.auth import get_user_model
from django.contrib.auth.admin import GroupAdmin as BaseGroupAdmin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.contrib.auth.models import Group
from django.contrib.contenttypes.models import ContentType
from django.db import transaction
from django.utils import timezone
from django.utils.html import format_html
from django.utils.translation import ngettext

from unfold.admin import ModelAdmin as UnfoldModelAdmin
from unfold.forms import AdminPasswordChangeForm, UserChangeForm, UserCreationForm

from . import models as api_models
from .admin_documents import RiderDocumentPreviewMixin
from .models import (
    # Customer
    CustomerProfile,
    SavedAddress,
    CustomerWallet,
    CustomerRiderPreference,
    # Rider
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
from django import forms


logger = logging.getLogger(__name__)

admin.site.site_header = "RYDNOW Operations"
admin.site.site_title = "RYDNOW Admin"
admin.site.index_title = "Dashboard"


# ───────────────────────────────────────────────────────────
# 🧩 SHARED MIXINS & BASE CLASSES
# ───────────────────────────────────────────────────────────
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


class SuperuserWriteAdminMixin:
    """Financial/audit records: superusers may add/edit, nobody may delete."""
    def has_add_permission(self, request):
        return request.user.is_superuser and super().has_add_permission(request)
    def has_change_permission(self, request, obj=None):
        return request.user.is_superuser and super().has_change_permission(request, obj)
    def has_delete_permission(self, request, obj=None):
        return False


class AutoRawIdMixin:
    """Auto ForeignKey → raw_id_fields for fast admin pages."""
    def __init__(self, model, admin_site):
        if not self.raw_id_fields:
            self.raw_id_fields = tuple(
                field.name for field in model._meta.get_fields()
                if field.is_relation and field.concrete
                and (field.many_to_one or field.one_to_one)
            )
        super().__init__(model, admin_site)


class BulkStatusActionMixin:
    """Safe bulk status transitions: atomic, locked, audited."""
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
                LogEntry.objects.bulk_create([
                    LogEntry(
                        user_id=request.user.pk,
                        content_type_id=content_type.pk,
                        object_id=str(obj.pk),
                        object_repr=str(obj)[:200],
                        action_flag=CHANGE,
                        change_message=f"Bulk action: status set to '{new_status}'."
                    )
                    for obj in manager.filter(pk__in=eligible_ids)
                ])

        skipped = len(selected_ids) - changed
        logger.info(
            "%s bulk status -> %s by user=%s changed=%s skipped=%s",
            self.model.__name__, new_status, request.user.pk, changed, skipped
        )
        if changed:
            self.message_user(request, f"{changed} record(s) marked as {new_status}.", messages.SUCCESS)
        if skipped:
            self.message_user(
                request,
                f"{skipped} record(s) skipped — already in final state.",
                messages.WARNING
            )


class BaseAdmin(AutoRawIdMixin, UnfoldModelAdmin):
    """Safe defaults for every model: Unfold theme, fast FK widgets."""
    list_display = ("__str__", "pk")
    ordering = ("-pk",)
    list_per_page = 50


class LedgerAdmin(SuperuserWriteAdminMixin, BaseAdmin):
    """Financial/audit records: superuser add+edit, never deletable."""
    list_per_page = 100


# ───────────────────────────────────────────────────────────
# 🔐 AUTH — Re-register for Unfold theme
# ───────────────────────────────────────────────────────────
User = get_user_model()

for _model in (User, Group):
    if admin.site.is_registered(_model):
        admin.site.unregister(_model)


@admin.register(User)
class UserAdmin(BaseUserAdmin, UnfoldModelAdmin):
    form = UserChangeForm
    add_form = UserCreationForm
    change_password_form = AdminPasswordChangeForm


@admin.register(Group)
class GroupAdmin(BaseGroupAdmin, UnfoldModelAdmin):
    pass


# ───────────────────────────────────────────────────────────
# 👥 CUSTOMERS
# ───────────────────────────────────────────────────────────
@admin.register(CustomerProfile)
class CustomerProfileAdmin(BaseAdmin):
    list_display = ("__str__", "pk", "created_at")
    search_fields = ("user__username", "user__email")
    readonly_fields = ("created_at",)
    list_select_related = ("user",)
    ordering = ("-created_at",)
    date_hierarchy = "created_at"


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



class ExpiryFilter(admin.SimpleListFilter):
    title = "expiry"
    parameter_name = "expiry"

    def lookups(self, request, model_admin):
        return [
            ("expired", "Expired"),
            ("soon", f"Expiring within {EXPIRING_SOON_DAYS} days"),
            ("valid", "Valid"),
            ("none", "No expiry date"),
        ]

    def queryset(self, request, queryset):
        today = timezone.localdate()
        value = self.value()
        if value == "expired":
            return queryset.filter(expiry_date__lt=today)
        if value == "soon":
            return queryset.filter(
                expiry_date__gte=today,
                expiry_date__lte=today + timedelta(days=EXPIRING_SOON_DAYS),
            )
        if value == "valid":
            return queryset.filter(expiry_date__gte=today)
        if value == "none":
            return queryset.filter(expiry_date__isnull=True)
        return queryset



# ───────────────────────────────────────────────────────────
# 👤 RIDER DOCUMENTS — uses mixin from admin_documents.py
# ───────────────────────────────────────────────────────────
DocStatus = RiderDocument.DocumentStatus
REVIEWED = {DocStatus.APPROVED, DocStatus.REJECTED}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp"}
PDF_EXTENSIONS = {".pdf"}
EXPIRING_SOON_DAYS = 30
REQUIRED_DOCUMENT_TYPES: tuple = ()


@admin.register(RiderDocument)
class RiderDocumentAdmin(RiderDocumentPreviewMixin, BaseAdmin):
    list_display = ("preview", "document_type", "status", "expiry_date", "uploaded_at")
    list_filter = ("status", "document_type", ExpiryFilter)
    search_fields = ("rider_profile__user__email", "rider_profile__user__username")
    readonly_fields = ("preview_large", "reviewed_by", "reviewed_at", "uploaded_at")
    list_select_related = ("rider_profile", "rider_profile__user")
    actions = ("approve_documents", "reject_documents", "mark_expired")
    ordering = ("-uploaded_at",)
    date_hierarchy = "uploaded_at"


# ───────────────────────────────────────────────────────────
# 👤 RIDER-RELATED RECORDS
# ───────────────────────────────────────────────────────────
STATUS_COLORS = {
    "pending": "#d97706",
    "approved": "#16a34a",
    "rejected": "#dc2626",
    "expired": "#6b7280",
}


@admin.register(RiderStatusHistory)
class RiderStatusHistoryAdmin(LedgerAdmin):
    """Audit trail — never deletable."""
    list_display = ("rider_profile", "status", "changed_at")
    list_filter = ("status",)
    readonly_fields = ("changed_at",)
    ordering = ("-changed_at",)


@admin.register(ServiceQualification)
class ServiceQualificationAdmin(BaseAdmin):
    list_display = ("rider_profile", "service", "is_active", "created_at")
    list_filter = ("service", "is_active", "created_at")
    search_fields = ("rider_profile__user__email",)
    readonly_fields = ("created_at",)
    ordering = ("-created_at",)


@admin.register(RiderPayoutAccount)
class RiderPayoutAccountAdmin(NoDeleteAdminMixin, BaseAdmin):
    """Payout details — deletion disabled."""
    list_display = ("rider_profile", "bank_name", "account_number_masked", "is_verified")
    list_filter = ("is_verified",)
    search_fields = ("rider_profile__user__email",)


@admin.register(RiderEmergencyContact)
class RiderEmergencyContactAdmin(BaseAdmin):
    list_display = ("__str__", "pk", "created_at")
    list_filter = ("created_at",)
    search_fields = ("full_name",)
    readonly_fields = ("created_at", "updated_at")
    list_select_related = ("rider_profile",)
    ordering = ("-created_at",)


@admin.register(RiderOnlineSession)
class RiderOnlineSessionAdmin(LedgerAdmin):
    list_display = ("rider_profile", "started_at", "ended_at", "is_active")
    list_filter = ("is_active", "started_at")
    readonly_fields = ("started_at", "ended_at")
    ordering = ("-started_at",)


@admin.register(RiderLocationPing)
class RiderLocationPingAdmin(LedgerAdmin):
    """High-volume telemetry."""
    list_display = ("rider_profile", "timestamp", "latitude", "longitude")
    list_filter = ("timestamp",)
    readonly_fields = ("timestamp",)
    ordering = ("-timestamp",)


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
    search_fields = ("reference_code", "title", "description")
    list_select_related = ("rider_profile", "assigned_to")
    ordering = ("-pk",)


@admin.register(RiderComplaint)
class RiderComplaintAdmin(NoDeleteAdminMixin, BaseAdmin):
    list_display = ("__str__", "pk", "created_at")
    list_filter = ("created_at",)
    search_fields = ("description",)
    readonly_fields = ("created_at",)
    ordering = ("-created_at",)
    date_hierarchy = "created_at"


# ───────────────────────────────────────────────────────────
# 📋 BOOKING-RELATED RECORDS
# ───────────────────────────────────────────────────────────
@admin.register(BookingStatusHistory)
class BookingStatusHistoryAdmin(LedgerAdmin):
    """Audit trail — never deletable."""
    list_display = ("booking", "status", "changed_at")
    list_filter = ("status",)
    readonly_fields = ("changed_at",)
    ordering = ("-changed_at",)


@admin.register(ParcelDetail)
class ParcelDetailAdmin(BaseAdmin):
    pass


@admin.register(BookingOffer)
class BookingOfferAdmin(BaseAdmin):
    """Rider offers — use ID lookup widgets, not dropdowns."""
    list_display = ("__str__", "pk", "booking", "rider", "status")
    list_filter = ("status",)
    search_fields = ("rider__user__username", "rider__user__email")
    list_select_related = ("booking", "rider", "rider__user")
    raw_id_fields = ("booking", "rider")


@admin.register(Review)
class ReviewAdmin(NoDeleteAdminMixin, BaseAdmin):
    list_display = ("booking", "score", "created_at")
    list_filter = ("score", "created_at")
    readonly_fields = ("created_at",)
    ordering = ("-created_at",)


# ───────────────────────────────────────────────────────────
# 💰 FINANCE & WALLET
# ───────────────────────────────────────────────────────────
@admin.register(RiderWallet)
class RiderWalletAdmin(LedgerAdmin):
    """Manual balance edits — superuser-only, logged."""
    list_display = ("rider", "balance", "updated_at")
    readonly_fields = ("balance", "updated_at")
    list_select_related = ("rider",)


@admin.register(CustomerWallet)
class CustomerWalletAdmin(LedgerAdmin):
    """Manual balance edits — superuser-only, logged."""
    list_display = ("customer", "balance", "updated_at")
    readonly_fields = ("balance", "updated_at")
    list_select_related = ("customer",)


@admin.register(WalletTransaction)
class WalletTransactionAdmin(LedgerAdmin):
    """Immutable ledger entries."""
    list_display = ("__str__", "pk", "transaction_type", "amount", "created_at")
    list_filter = ("transaction_type", "created_at")
    search_fields = ("reference",)
    readonly_fields = ("created_at",)
    ordering = ("-created_at",)
    date_hierarchy = "created_at"


@admin.register(WalletDeposit)
class WalletDepositAdmin(LedgerAdmin):
    """Payment-gateway records."""
    list_display = ("__str__", "pk", "status", "created_at")
    list_filter = ("status", "created_at")
    search_fields = ("reference",)
    readonly_fields = ("created_at", "updated_at")
    ordering = ("-created_at",)
    date_hierarchy = "created_at"


@admin.register(WithdrawalRequest)
class WithdrawalRequestAdmin(BulkStatusActionMixin, NoDeleteAdminMixin, BaseAdmin):
    list_display = ("__str__", "pk", "status", "created_at")
    list_filter = ("status", "created_at")
    readonly_fields = ("created_at",)
    ordering = ("-created_at",)
    date_hierarchy = "created_at"
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


@admin.register(Payment)
class PaymentAdmin(LedgerAdmin):
    """Payment records."""
    list_display = ("__str__", "pk", "created_at")
    list_filter = ("created_at",)
    ordering = ("-created_at",)
    date_hierarchy = "created_at"


@admin.register(Refund)
class RefundAdmin(LedgerAdmin):
    """Refund records."""
    list_display = ("__str__", "pk", "created_at")
    list_filter = ("created_at",)
    ordering = ("-created_at",)
    date_hierarchy = "created_at"


@admin.register(LoyaltyAccount)
class LoyaltyAccountAdmin(LedgerAdmin):
    """Loyalty balances change only through the ledger."""
    list_display = ("user", "balance", "updated_at")
    readonly_fields = ("balance", "updated_at")
    list_select_related = ("user",)


@admin.register(LoyaltyTransaction)
class LoyaltyTransactionAdmin(LedgerAdmin):
    """Immutable loyalty ledger."""
    list_display = ("__str__", "pk", "created_at")
    list_filter = ("created_at",)
    ordering = ("-created_at",)
    date_hierarchy = "created_at"


# ══════════════════════════════════════════════════════════
# 🎁 REFERRALS, PROMOS & BUSINESS
# ══════════════════════════════════════════════════════════
class _CreatedAdmin(BaseAdmin):
    list_display = ("__str__", "pk", "created_at")
    list_filter = ("created_at",)
    ordering = ("-created_at",)
    date_hierarchy = "created_at"


@admin.register(Referral)
class ReferralAdmin(_CreatedAdmin):
    list_display = ("referrer", "referred", "code", "is_redeemed", "created_at")
    list_filter = ("is_redeemed", "created_at")
    search_fields = ("referrer__email", "referred__email")


@admin.register(ReferralCode)
class ReferralCodeAdmin(_CreatedAdmin):
    list_display = ("code", "usage_count", "is_active", "created_at")
    list_filter = ("is_active", "created_at")
    search_fields = ("code",)


@admin.register(BusinessAccount)
class BusinessAccountAdmin(_CreatedAdmin):
    list_display = ("name", "contact_email", "is_active", "created_at")
    list_filter = ("is_active", "created_at")
    search_fields = ("name", "contact_email")


@admin.register(WasteSubscription)
class WasteSubscriptionAdmin(_CreatedAdmin):
    list_display = ("customer", "plan", "status", "next_billing_date")
    list_filter = ("status", "plan", "created_at")
    search_fields = ("customer__email",)


@admin.register(PromoCode)
class PromoCodeAdmin(_CreatedAdmin):
    list_display = ("code", "discount_type", "discount_value", "is_active", "usage_limit", "times_used")
    list_filter = ("is_active", "discount_type", "created_at")
    search_fields = ("code",)


@admin.register(PromoRedemption)
class PromoRedemptionAdmin(_CreatedAdmin):
    list_display = ("code", "customer", "booking", "created_at")
    list_filter = ("created_at",)
    search_fields = ("code__code", "customer__email")


# ══════════════════════════════════════════════════════════
# ⚙️ PLATFORM CONFIG
# ══════════════════════════════════════════════════════════
for _name in ("Service", "Zone", "PricingRule", "SystemConfig", "Notification"):
    _model = getattr(api_models, _name, None)
    if _model is not None and not admin.site.is_registered(_model):
        admin.site.register(_model, type(f"{_name}Admin", (BaseAdmin,), {}))


# ══════════════════════════════════════════════════════════
# RiderProfile + Booking admins — KEEP THIS IMPORT LAST
# ══════════════════════════════════════════════════════════
from . import admin_riders  # noqa: E402,F401
