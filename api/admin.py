"""
RYDNOW — Django Admin Configuration
Production-ready — no duplicate classes, no wrong imports, all field references verified
"""
from __future__ import annotations

import logging
from datetime import timedelta

from django.contrib import admin, messages
from django.contrib.admin.models import LogEntry, CHANGE
from django.contrib.auth import get_user_model
from django.contrib.auth.admin import GroupAdmin as BaseGroupAdmin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.contrib.auth.models import Group
from django.contrib.contenttypes.models import ContentType
from django.db import transaction
from django.utils import timezone

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
    # Marketing
    Referral,
    ReferralCode,
    BusinessAccount,
    WasteSubscription,
    PromoCode,
    PromoRedemption,
)


logger = logging.getLogger(__name__)
User = get_user_model()

admin.site.site_header = "RYDNOW Operations"
admin.site.site_title = "RYDNOW Admin"
admin.site.index_title = "Dashboard"


# ══════════════════════════════════════════════════════════
# BASE CLASSES
# ══════════════════════════════════════════════════════════
class ReadOnlyAdminMixin:
    def has_add_permission(self, request):
        return False
    def has_change_permission(self, request, obj=None):
        return False
    def has_delete_permission(self, request, obj=None):
        return False


class NoDeleteAdminMixin:
    def has_delete_permission(self, request, obj=None):
        return False


class BulkStatusActionMixin:
    final_statuses: tuple[str, ...] = ()

    def _apply_status(self, request, queryset, new_status):
        ct = ContentType.objects.get_for_model(queryset.model)
        with transaction.atomic():
            for obj in queryset.select_for_update():
                old_status = getattr(obj, 'status', None)
                obj.status = new_status
                obj.save(update_fields=['status'])
                LogEntry.objects.log_action(
                    request.user, obj.pk, ct, str(obj), CHANGE,
                    f"Status: {old_status} → {new_status}"
                )
        self.message_user(request, f"{queryset.count()} updated.", messages.SUCCESS)


class BaseAdmin(UnfoldModelAdmin):
    list_display = ("__str__", "pk")
    ordering = ("-pk",)
    list_per_page = 50


class LedgerAdmin(NoDeleteAdminMixin, BaseAdmin):
    list_per_page = 100
    readonly_fields = ()

    def has_delete_permission(self, request, obj=None):
        return False


# ══════════════════════════════════════════════════════════
# USER & AUTH
# ══════════════════════════════════════════════════════════
for _model in (User, Group):
    if admin.site.is_registered(_model):
        admin.site.unregister(_model)


@admin.register(User)
class CustomUserAdmin(BaseUserAdmin, UnfoldModelAdmin):
    form = UserChangeForm
    add_form = UserCreationForm
    change_password_form = AdminPasswordChangeForm
    list_display = ("username", "email", "is_staff", "is_active")
    list_filter = ("is_staff", "is_active", "is_superuser")
    search_fields = ("username", "email")
    ordering = ("-pk",)


@admin.register(Group)
class CustomGroupAdmin(BaseGroupAdmin, UnfoldModelAdmin):
    pass


# ══════════════════════════════════════════════════════════
# CUSTOMERS
# ══════════════════════════════════════════════════════════
@admin.register(CustomerProfile)
class CustomerProfileAdmin(BaseAdmin):
    list_display = ("__str__", "user", "created_at")
    search_fields = ("user__email", "user__username")
    readonly_fields = ("created_at", "updated_at")
    list_select_related = ("user",)
    date_hierarchy = "created_at"


@admin.register(SavedAddress)
class SavedAddressAdmin(BaseAdmin):
    list_display = ("__str__", "customer")
    search_fields = ("customer__user__email",)
    list_select_related = ("customer",)


@admin.register(CustomerWallet)
class CustomerWalletAdmin(NoDeleteAdminMixin, BaseAdmin):
    list_display = ("__str__", "customer")
    search_fields = ("customer__user__email",)
    list_select_related = ("customer",)


@admin.register(CustomerRiderPreference)
class CustomerRiderPreferenceAdmin(BaseAdmin):
    list_display = ("__str__", "customer", "rider")
    search_fields = ("customer__user__email", "rider__user__email")
    list_select_related = ("customer", "rider")


# ══════════════════════════════════════════════════════════
# RIDER DOCUMENTS
# ══════════════════════════════════════════════════════════
class ExpiryFilter(admin.SimpleListFilter):
    title = "expiry status"
    parameter_name = "expiry"

    def lookups(self, request, model_admin):
        return [
            ("valid", "Valid"),
            ("expired", "Expired"),
            ("none", "No expiry date"),
        ]

    def queryset(self, request, queryset):
        value = self.value()
        today = timezone.now().date()
        if value == "valid":
            return queryset.filter(expiry_date__isnull=False, expiry_date__gte=today)
        if value == "expired":
            return queryset.filter(expiry_date__isnull=False, expiry_date__lt=today)
        if value == "none":
            return queryset.filter(expiry_date__isnull=True)
        return queryset


@admin.register(RiderDocument)
class RiderDocumentAdmin(RiderDocumentPreviewMixin, BaseAdmin):
    list_display = ("__str__", "rider_profile", "document_type", "status", "uploaded_at")
    list_filter = ("status", "document_type", ExpiryFilter)
    search_fields = ("rider_profile__user__email",)
    readonly_fields = ("uploaded_at",)
    list_select_related = ("rider_profile",)
    date_hierarchy = "uploaded_at"


# ══════════════════════════════════════════════════════════
# RIDERS
# ══════════════════════════════════════════════════════════
@admin.register(RiderStatusHistory)
class RiderStatusHistoryAdmin(ReadOnlyAdminMixin, BaseAdmin):
    list_display = ("__str__", "rider_profile")
    search_fields = ("rider_profile__user__email",)
    list_select_related = ("rider_profile",)


@admin.register(ServiceQualification)
class ServiceQualificationAdmin(BaseAdmin):
    list_display = ("__str__", "rider_profile")
    search_fields = ("rider_profile__user__email",)
    list_select_related = ("rider_profile",)


@admin.register(RiderPayoutAccount)
class RiderPayoutAccountAdmin(NoDeleteAdminMixin, BaseAdmin):
    list_display = ("__str__", "rider_profile")
    search_fields = ("rider_profile__user__email",)
    list_select_related = ("rider_profile",)


@admin.register(RiderEmergencyContact)
class RiderEmergencyContactAdmin(BaseAdmin):
    list_display = ("__str__", "rider_profile")
    search_fields = ("rider_profile__user__email",)
    list_select_related = ("rider_profile",)


@admin.register(RiderOnlineSession)
class RiderOnlineSessionAdmin(ReadOnlyAdminMixin, BaseAdmin):
    list_display = ("__str__", "rider_profile")
    search_fields = ("rider_profile__user__email",)
    list_select_related = ("rider_profile",)


@admin.register(RiderLocationPing)
class RiderLocationPingAdmin(ReadOnlyAdminMixin, BaseAdmin):
    list_display = ("__str__", "rider_profile")
    search_fields = ("rider_profile__user__email",)
    list_select_related = ("rider_profile",)


@admin.register(RiderProfileAdminNote)
class RiderProfileAdminNoteAdmin(NoDeleteAdminMixin, BaseAdmin):
    list_display = ("__str__", "rider_profile", "created_at")
    search_fields = ("rider_profile__user__email", "note")
    readonly_fields = ("created_at",)
    list_select_related = ("rider_profile",)
    date_hierarchy = "created_at"

    def save_model(self, request, obj, form, change):
        if not change:
            obj.author = request.user
        super().save_model(request, obj, form, change)


@admin.register(RiderIncident)
class RiderIncidentAdmin(NoDeleteAdminMixin, BaseAdmin):
    list_display = ("__str__", "rider_profile")
    search_fields = ("rider_profile__user__email", "reference_code")
    list_select_related = ("rider_profile",)


@admin.register(RiderComplaint)
class RiderComplaintAdmin(NoDeleteAdminMixin, BaseAdmin):
    list_display = ("__str__", "rider_profile", "created_at")
    search_fields = ("rider_profile__user__email", "description")
    readonly_fields = ("created_at",)
    list_select_related = ("rider_profile",)
    date_hierarchy = "created_at"


# ══════════════════════════════════════════════════════════
# BOOKINGS
# ══════════════════════════════════════════════════════════
@admin.register(BookingStatusHistory)
class BookingStatusHistoryAdmin(ReadOnlyAdminMixin, BaseAdmin):
    list_display = ("__str__", "booking")
    search_fields = ("booking__id",)
    list_select_related = ("booking",)


@admin.register(ParcelDetail)
class ParcelDetailAdmin(BaseAdmin):
    list_display = ("__str__", "booking")
    search_fields = ("booking__id",)
    list_select_related = ("booking",)


@admin.register(BookingOffer)
class BookingOfferAdmin(BaseAdmin):
    list_display = ("__str__", "booking", "rider")
    search_fields = ("booking__id", "rider__user__email")
    list_select_related = ("booking", "rider")


@admin.register(Review)
class ReviewAdmin(NoDeleteAdminMixin, BaseAdmin):
    list_display = ("__str__", "booking", "created_at")
    search_fields = ("booking__id",)
    readonly_fields = ("created_at",)
    date_hierarchy = "created_at"


# ══════════════════════════════════════════════════════════
# FINANCE
# ══════════════════════════════════════════════════════════
@admin.register(RiderWallet)
class RiderWalletAdmin(LedgerAdmin):
    list_display = ("__str__", "rider_profile")
    search_fields = ("rider_profile__user__email",)
    list_select_related = ("rider_profile",)


@admin.register(WalletTransaction)
class WalletTransactionAdmin(LedgerAdmin):
    list_display = ("__str__", "created_at")
    search_fields = ("reference",)
    readonly_fields = ("created_at",)
    date_hierarchy = "created_at"


@admin.register(WalletDeposit)
class WalletDepositAdmin(LedgerAdmin):
    list_display = ("__str__", "status", "created_at")
    list_filter = ("status",)
    search_fields = ("reference",)
    readonly_fields = ("created_at", "updated_at")
    date_hierarchy = "created_at"


@admin.register(WithdrawalRequest)
class WithdrawalRequestAdmin(BulkStatusActionMixin, NoDeleteAdminMixin, BaseAdmin):
    list_display = ("__str__", "status", "created_at")
    list_filter = ("status",)
    readonly_fields = ("created_at",)
    date_hierarchy = "created_at"
    final_statuses = ("completed", "failed")

    def has_add_permission(self, request):
        return False

    @admin.action(description="Mark selected as Completed")
    def mark_completed(self, request, queryset):
        self._apply_status(request, queryset, "completed")

    @admin.action(description="Mark selected as Failed")
    def mark_failed(self, request, queryset):
        self._apply_status(request, queryset, "failed")


@admin.register(Payment)
class PaymentAdmin(LedgerAdmin):
    list_display = ("__str__", "created_at")
    search_fields = ("reference",)
    date_hierarchy = "created_at"


@admin.register(Refund)
class RefundAdmin(LedgerAdmin):
    list_display = ("__str__", "created_at")
    date_hierarchy = "created_at"


@admin.register(LoyaltyAccount)
class LoyaltyAccountAdmin(LedgerAdmin):
    list_display = ("__str__", "user")
    search_fields = ("user__email",)
    list_select_related = ("user",)


@admin.register(LoyaltyTransaction)
class LoyaltyTransactionAdmin(LedgerAdmin):
    list_display = ("__str__", "created_at")
    date_hierarchy = "created_at"


# ══════════════════════════════════════════════════════════
# MARKETING
# ══════════════════════════════════════════════════════════
class _CreatedAdmin(BaseAdmin):
    list_display = ("__str__", "pk", "created_at")
    list_filter = ("created_at",)
    ordering = ("-pk",)
    date_hierarchy = "created_at"


@admin.register(Referral)
class ReferralAdmin(_CreatedAdmin):
    list_display = ("__str__", "created_at")
    search_fields = ()


@admin.register(ReferralCode)
class ReferralCodeAdmin(_CreatedAdmin):
    list_display = ("__str__", "created_at")
    search_fields = ("code",)


@admin.register(BusinessAccount)
class BusinessAccountAdmin(_CreatedAdmin):
    list_display = ("__str__", "created_at")
    search_fields = ("name",)


@admin.register(WasteSubscription)
class WasteSubscriptionAdmin(_CreatedAdmin):
    list_display = ("__str__", "created_at")
    search_fields = ()


@admin.register(PromoCode)
class PromoCodeAdmin(_CreatedAdmin):
    list_display = ("__str__", "created_at")
    search_fields = ("code",)


@admin.register(PromoRedemption)
class PromoRedemptionAdmin(_CreatedAdmin):
    list_display = ("__str__", "created_at")
    search_fields = ()


# ══════════════════════════════════════════════════════════
# OPTIONAL PLATFORM MODELS
# ══════════════════════════════════════════════════════════
for _name in ("Service", "Zone", "PricingRule", "SystemConfig", "Notification"):
    _model = getattr(api_models, _name, None)
    if _model is not None and not admin.site.is_registered(_model):
        admin.site.register(_model, type(f"{_name}Admin", (BaseAdmin,), {}))


# ══════════════════════════════════════════════════════════
# KEEP THIS IMPORT LAST — RiderProfile + Booking admins
# ══════════════════════════════════════════════════════════
from . import admin_riders  # noqa: E402,F401
