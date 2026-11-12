"""
Django admin configuration for RYDNOW (django-unfold theme).

Design principles
─────────────────
* Every registered model has an "Add" page and can be operated manually.
* Financial and audit records (wallets, ledgers, payments, histories) can be
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

from django.contrib import admin, messages
from django.contrib.admin.models import CHANGE, LogEntry
from django.contrib.auth import get_user_model
from django.contrib.auth.admin import GroupAdmin as BaseGroupAdmin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.contrib.auth.models import Group
from django.contrib.contenttypes.models import ContentType
from django.db import transaction
from unfold.admin import ModelAdmin as UnfoldModelAdmin
from unfold.forms import AdminPasswordChangeForm, UserChangeForm, UserCreationForm
from django.utils import timezone
from datetime import timedelta


from . import models as api_models
from .models import (
    # Customer
    CustomerProfile,
    SavedAddress,
    CustomerWallet,
    CustomerRiderPreference,
    # Rider (RiderProfile itself lives in admin_riders.py)
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
    # Bookings (Booking itself lives in admin_riders.py)
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

admin.site.site_header = "RYDNOW Operations"
admin.site.site_title = "RYDNOW Admin"
admin.site.index_title = "Dashboard"


# ══════════════════════════════════════════════════════════
# 🧩 SHARED MIXINS & BASE CLASSES
# ══════════════════════════════════════════════════════════
class ReadOnlyAdminMixin:
    """View-only admin: no add, edit or delete for anyone. Apply to lock a model down."""

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
    """

    def __init__(self, model, admin_site):
        if not self.raw_id_fields:
            self.raw_id_fields = tuple(
                field.name
                for field in model._meta.get_fields()
                if field.is_relation and field.concrete and (field.many_to_one or field.one_to_one)
            )
        super().__init__(model, admin_site)


class BulkStatusActionMixin:
    """
    Safe bulk status transitions: one transaction, rows locked, objects already in a
    final state are skipped, and one admin LogEntry is written per changed object.

    NOTE: this changes the status column only. It does NOT run the model's lifecycle
    (wallet credits, refunds, notifications). Use it for housekeeping, not for settling money.
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
            self.message_user(request, f"{changed} record(s) marked as {new_status}.", messages.SUCCESS)
        if skipped:
            self.message_user(
                request, f"{skipped} record(s) skipped because they were already in a final state.",
                messages.WARNING,
            )


class BaseAdmin(AutoRawIdMixin, UnfoldModelAdmin):
    """Safe defaults for every model: Unfold look, fast FK widgets, sensible list."""

    list_display = ("__str__", "pk")
    ordering = ("-pk",)
    list_per_page = 50


class LedgerAdmin(SuperuserWriteAdminMixin, BaseAdmin):
    """Financial / audit records: superuser add + edit, never deletable."""

    list_per_page = 100


# ══════════════════════════════════════════════════════════
# 🔐 AUTH (re-registered so Users / Groups use the Unfold theme)
# ══════════════════════════════════════════════════════════
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


# ══════════════════════════════════════════════════════════
# 👥 CUSTOMERS
# ══════════════════════════════════════════════════════════
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


# ══════════════════════════════════════════════════════════
# 👤 RIDER-RELATED RECORDS  (RiderProfile → admin_riders.py)
# ══════════════════════════════════════════════════════════
@admin.register(RiderStatusHistory)
class RiderStatusHistoryAdmin(LedgerAdmin):
    """Audit trail: superuser add + edit, never deletable."""


@admin.register(ServiceQualification)
class ServiceQualificationAdmin(BaseAdmin):
    pass


class ExpiryFilter(admin.SimpleListFilter):
    title = "expiry"
    parameter_name = "expiry"

    def lookups(self, request, model_admin):
        return [
            ("expired", "Expired"),
            ("soon", "Expiring within 30 days"),
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
                expiry_date__lte=today + timedelta(days=30),
            )
        if value == "none":
            return queryset.filter(expiry_date__isnull=True)
        return queryset

STATUS_COLORS = {
    "pending": "#d97706",
    "approved": "#16a34a",
    "rejected": "#dc2626",
    "expired": "#6b7280",
}

@admin.register(RiderDocument)
class RiderDocumentAdmin(BaseAdmin):
    list_display = (
        "rider_name",
        "document_type",
        "status_badge",
        "expiry_display",
        "preview",
        "uploaded_at",
        "reviewed_by",
    )
    list_filter = ("status", "document_type", ExpiryFilter, "uploaded_at")
    search_fields = ("rider_profile__full_name", "notes")
    list_select_related = ("rider_profile", "reviewed_by")
    raw_id_fields = ("rider_profile",)
    date_hierarchy = "uploaded_at"
    ordering = ("-uploaded_at",)
    list_per_page = 25
    save_on_top = True
    actions = ("approve_documents", "reject_documents", "mark_expired")

    readonly_fields = ("id", "preview_large", "uploaded_at", "reviewed_at", "reviewed_by")
    fieldsets = (
        ("Document", {
            "fields": ("id", "rider_profile", "document_type", "file", "preview_large", "expiry_date"),
        }),
        ("Review", {
            "fields": ("status", "notes", "reviewed_by", "reviewed_at"),
        }),
        ("Timestamps", {
            "fields": ("uploaded_at",),
            "classes": ("collapse",),
        }),
    )

    # ── List columns ──────────────────────────────────────────────
    @admin.display(description="Rider", ordering="rider_profile__full_name")
    def rider_name(self, obj):
        return obj.rider_profile.full_name

    @admin.display(description="Status", ordering="status")
    def status_badge(self, obj):
        return format_html(
            '<span style="background:{};color:#fff;padding:2px 10px;'
            'border-radius:12px;font-size:11px;font-weight:600;">{}</span>',
            STATUS_COLORS.get(obj.status, "#6b7280"),
            obj.get_status_display(),
        )

    @admin.display(description="Expiry", ordering="expiry_date")
    def expiry_display(self, obj):
        if not obj.expiry_date:
            return "—"
        if obj.is_expired:
            return format_html(
                '<span style="color:#dc2626;font-weight:600;">{} (expired)</span>',
                obj.expiry_date,
            )
        return obj.expiry_date

    @admin.display(description="Preview")
    def preview(self, obj):
        return self._render_file(obj, size=48)

    @admin.display(description="Preview")
    def preview_large(self, obj):
        if not obj.pk:
            return "Save the document to see a preview."
        return self._render_file(obj, size=320)

    @staticmethod
    def _render_file(obj, size):
        """Never raises: a broken or missing file must not take down the page."""
        try:
            url, ext = get_file_info(obj.file)
        except Exception:  # storage backend errors (S3/GCS signing, etc.)
            logger.exception("Could not resolve file for RiderDocument %s", obj.pk)
            return "File unavailable"

        if not url:
            return "No file"
        if ext in IMAGE_EXTENSIONS:
            return format_html(
                '<a href="{0}" target="_blank" rel="noopener">'
                '<img src="{0}" alt="Document" loading="lazy" '
                'style="max-height:{1}px;max-width:{1}px;border-radius:6px;'
                'object-fit:cover;" /></a>',
                url,
                size,
            )
        label = "Open PDF" if ext in PDF_EXTENSIONS else "Open file"
        return format_html('<a href="{}" target="_blank" rel="noopener">{}</a>', url, label)

    # ── Saving ────────────────────────────────────────────────────
    def save_model(self, request, obj, form, change):
        reviewed = {"approved", "rejected"}
        if change and "status" in form.changed_data and obj.status in reviewed:
            obj.reviewed_by = request.user
            obj.reviewed_at = timezone.now()
        super().save_model(request, obj, form, change)

    # ── Bulk actions ──────────────────────────────────────────────
    @admin.action(description="Approve selected documents", permissions=["change"])
    def approve_documents(self, request, queryset):
        count = self._bulk_review(request, queryset, approve=True)
        self.message_user(
            request,
            ngettext("%d document approved.", "%d documents approved.", count) % count,
            messages.SUCCESS,
        )

    @admin.action(description="Reject selected documents", permissions=["change"])
    def reject_documents(self, request, queryset):
        count = self._bulk_review(request, queryset, approve=False)
        self.message_user(
            request,
            ngettext("%d document rejected.", "%d documents rejected.", count) % count,
            messages.WARNING,
        )

    @admin.action(description="Mark selected documents as expired", permissions=["change"])
    def mark_expired(self, request, queryset):
        count = 0
        with transaction.atomic():
            for document in queryset.select_for_update().exclude(status="expired"):
                document.status = "expired"
                document.save(update_fields=["status"])
                self.log_change(request, document, "Bulk action: marked as expired.")
                count += 1
        self.message_user(
            request,
            ngettext("%d document marked as expired.", "%d documents marked as expired.", count) % count,
            messages.INFO,
        )

    def _bulk_review(self, request, queryset, approve):
        """Atomic, row-locked, audited. Uses the model's review() so reviewer/time are stamped."""
        target = "approved" if approve else "rejected"
        count = 0
        with transaction.atomic():
            for document in queryset.select_for_update().exclude(status=target):
                document.review(by=request.user, approve=approve)
                self.log_change(request, document, f"Bulk action: status set to '{target}'.")
                count += 1
        return count



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


# ══════════════════════════════════════════════════════════
# 📋 BOOKING-RELATED RECORDS  (Booking → admin_riders.py)
# ══════════════════════════════════════════════════════════
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


@admin.register(Review)
class ReviewAdmin(NoDeleteAdminMixin, BaseAdmin):
    pass


# ══════════════════════════════════════════════════════════
# 💰 FINANCE & WALLET
# ══════════════════════════════════════════════════════════
@admin.register(RiderWallet)
class RiderWalletAdmin(LedgerAdmin):
    """Manual balance edits are superuser-only and logged by Django admin."""

    list_select_related = ("rider",)


@admin.register(CustomerWallet)
class CustomerWalletAdmin(LedgerAdmin):
    """Manual balance edits are superuser-only and logged by Django admin."""


@admin.register(WalletTransaction)
class WalletTransactionAdmin(LedgerAdmin):
    """Ledger entries: superuser add + edit, never deletable."""

    list_display = ("__str__", "pk", "created_at")
    list_filter = ("created_at",)
    readonly_fields = ("created_at",)
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


@admin.register(WalletDeposit)
class WalletDepositAdmin(LedgerAdmin):
    """Payment-gateway records: superuser add + edit, never deletable."""

    list_display = ("__str__", "pk", "status", "created_at")
    list_filter = ("status", "created_at")
    search_fields = ("reference",)
    readonly_fields = ("created_at", "updated_at")
    ordering = ("-created_at",)
    date_hierarchy = "created_at"


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
    pass


@admin.register(ReferralCode)
class ReferralCodeAdmin(_CreatedAdmin):
    pass


@admin.register(BusinessAccount)
class BusinessAccountAdmin(_CreatedAdmin):
    pass


@admin.register(WasteSubscription)
class WasteSubscriptionAdmin(_CreatedAdmin):
    pass


@admin.register(PromoCode)
class PromoCodeAdmin(_CreatedAdmin):
    pass


@admin.register(PromoRedemption)
class PromoRedemptionAdmin(_CreatedAdmin):
    pass


# ══════════════════════════════════════════════════════════
# ⚙️ PLATFORM CONFIG
# Your models.py uses these (Service, Zone, PricingRule, SystemConfig, Notification) but
# they had no admin, so you couldn't add services, zones or pricing from the console.
# Registered only if they exist, and only once.
# ══════════════════════════════════════════════════════════
for _name in ("Service", "Zone", "PricingRule", "SystemConfig", "Notification"):
    _model = getattr(api_models, _name, None)
    if _model is not None and not admin.site.is_registered(_model):
        admin.site.register(_model, type(f"{_name}Admin", (BaseAdmin,), {}))


# ══════════════════════════════════════════════════════════
# RiderProfile + Booking admins.
# Keep this import LAST: admin_riders imports BaseAdmin from this module, so importing it
# earlier causes a circular-import crash. Do not import it anywhere else in this file.
# ══════════════════════════════════════════════════════════
from . import admin_riders  # noqa: E402,F401
