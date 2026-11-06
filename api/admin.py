"""
Central admin configuration.

Design goals
————
* No more commenting/uncommenting: models that don't exist yet are skipped
  automatically and registered as soon as they are created.
* Admin classes never crash startup because of a field that doesn't exist
  yet (SafeModelAdmin prunes unknown fields from its options).
* Financial records (transactions) are read-only; deletes are blocked on
  money-related models.
"""
from __future__ import annotations

import logging
from typing import Iterable, Optional

from django.apps import apps
from django.contrib import admin, messages
from django.utils import timezone

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════
def resolve_model(names: Iterable[str], app_label: Optional[str] = None):
    """Return the first installed model matching any of `names`, else None."""
    for name in names:
        matches = [
            m
            for m in apps.get_models()
            if m.__name__ == name and (app_label is None or m._meta.app_label == app_label)
        ]
        if len(matches) > 1:
            logger.warning(
                "Ambiguous model name %r (%s); pass app_label to disambiguate.",
                name,
                ", ".join(m._meta.label for m in matches),
            )
        if matches:
            return matches[0]
    return None


def register(names: Iterable[str], admin_class, app_label: Optional[str] = None) -> bool:
    """
    Register the first existing model from `names` with `admin_class`.
    Returns True if registered; silently skips missing/already-registered models.
    """
    names = tuple(names)
    model = resolve_model(names, app_label)
    if model is None:
        logger.debug("Admin: none of %s exist yet, skipping.", names)
        return False
    if admin.site.is_registered(model):
        logger.debug("Admin: %s already registered, skipping.", model._meta.label)
        return False
    admin.site.register(model, admin_class)
    return True


# ═══════════════════════════════════════════════════════════════
# Base classes
# ═══════════════════════════════════════════════════════════════
class SafeModelAdmin(admin.ModelAdmin):
    """ModelAdmin that drops options referencing fields the model lacks."""

    list_per_page = 50
    save_on_top = True

    def __init__(self, model, admin_site):
        super().__init__(model, admin_site)
        fields = {f.name for f in model._meta.get_fields()}

        def exists(item) -> bool:
            if callable(item):
                return True
            name = str(item).lstrip("^=@")
            return (
                name == "__str__"
                or name.split("__")[0] in fields
                or hasattr(self, name)
                or hasattr(model, name)
            )

        self.list_display = tuple(i for i in self.list_display if exists(i)) or ("__str__",)
        self.list_filter = tuple(
            i for i in self.list_filter if not isinstance(i, str) or i.split("__")[0] in fields
        )
        self.search_fields = tuple(i for i in self.search_fields if exists(i))
        self.readonly_fields = tuple(i for i in self.readonly_fields if exists(i))
        self.raw_id_fields = tuple(i for i in self.raw_id_fields if i in fields)
        if self.date_hierarchy and self.date_hierarchy not in fields:
            self.date_hierarchy = None


class ReadOnlyAdminMixin:
    """Ledger-style records: viewable, never editable or deletable from admin."""

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


class NoDeleteAdminMixin:
    def has_delete_permission(self, request, obj=None):
        return False


class TimestampedAdminMixin:
    readonly_fields = ("created_at", "updated_at")
    date_hierarchy = "created_at"


# ═══════════════════════════════════════════════════════════════
# Admin classes
# ═══════════════════════════════════════════════════════════════
class RiderAdmin(TimestampedAdminMixin, SafeModelAdmin):
    list_display = ("id", "__str__", "phone", "email", "is_active", "created_at")
    list_filter = ("is_active", "created_at")
    search_fields = ("name", "first_name", "last_name", "email", "phone", "user__username")
    raw_id_fields = ("user",)


class ServiceAdmin(TimestampedAdminMixin, SafeModelAdmin):
    list_display = ("id", "name", "price", "is_active", "created_at")
    list_filter = ("is_active",)
    search_fields = ("name", "description")


class ServiceQualificationAdmin(TimestampedAdminMixin, SafeModelAdmin):
    list_display = ("rider", "service", "is_approved", "approved_by", "approved_at", "created_at")
    list_filter = ("is_approved", "service", "approved_at")
    search_fields = ("rider__name", "service__name", "approved_by__username")
    list_select_related = ("rider", "service", "approved_by")
    raw_id_fields = ("rider",)
    readonly_fields = ("approved_at", "approved_by", "created_at", "updated_at")
    actions = ("approve_selected", "revoke_selected")

    def save_model(self, request, obj, form, change):
        if not change or "is_approved" in form.changed_data:
            obj.approved_at = timezone.now() if obj.is_approved else None
            obj.approved_by = request.user if obj.is_approved else None
        super().save_model(request, obj, form, change)

    @admin.action(description="Approve selected qualifications")
    def approve_selected(self, request, queryset):
        n = queryset.filter(is_approved=False).update(
            is_approved=True, approved_at=timezone.now(), approved_by=request.user
        )
        self.message_user(request, f"{n} qualification(s) approved.", messages.SUCCESS)

    @admin.action(description="Revoke selected qualifications")
    def revoke_selected(self, request, queryset):
        n = queryset.filter(is_approved=True).update(
            is_approved=False, approved_at=None, approved_by=None
        )
        self.message_user(request, f"{n} qualification(s) revoked.", messages.WARNING)


class BookingAdmin(TimestampedAdminMixin, SafeModelAdmin):
    list_display = ("id", "rider", "service", "status", "total_amount", "created_at")
    list_filter = ("status", "service", "created_at")
    search_fields = ("id", "rider__name", "service__name")
    list_select_related = True
    raw_id_fields = ("rider",)


class WalletAdmin(NoDeleteAdminMixin, TimestampedAdminMixin, SafeModelAdmin):
    list_display = ("id", "rider", "balance", "updated_at")
    search_fields = ("rider__name", "rider__phone", "rider__email")
    raw_id_fields = ("rider",)
    readonly_fields = ("balance", "created_at", "updated_at")


class TransactionAdmin(ReadOnlyAdminMixin, TimestampedAdminMixin, SafeModelAdmin):
    list_display = ("id", "wallet", "transaction_type", "amount", "status", "created_at")
    list_filter = ("transaction_type", "status", "created_at")
    search_fields = ("id", "reference", "wallet__rider__name")
    list_select_related = True


class WithdrawalAdmin(NoDeleteAdminMixin, TimestampedAdminMixin, SafeModelAdmin):
    list_display = ("id", "rider", "amount", "status", "created_at", "processed_at")
    list_filter = ("status", "created_at")
    search_fields = ("id", "rider__name", "reference")
    list_select_related = True
    raw_id_fields = ("rider",)


class PromoAdmin(TimestampedAdminMixin, SafeModelAdmin):
    list_display = ("code", "discount", "is_active", "expires_at", "created_at")
    list_filter = ("is_active",)
    search_fields = ("code",)


class ReferralAdmin(TimestampedAdminMixin, SafeModelAdmin):
    list_display = ("id", "referrer", "referred", "status", "created_at")
    list_filter = ("status",)
    search_fields = ("referrer__name", "referred__name", "code")


# ═══════════════════════════════════════════════════════════════
# Registry — add a line here; missing models are skipped automatically
# ═══════════════════════════════════════════════════════════════
ADMIN_REGISTRY = (
    (("Rider", "RiderProfile"), RiderAdmin),
    (("Service",), ServiceAdmin),
    (("ServiceQualification",), ServiceQualificationAdmin),
    (("Booking",), BookingAdmin),
    (("Wallet", "RiderWallet"), WalletAdmin),
    (("Transaction", "WalletTransaction"), TransactionAdmin),
    (("Withdrawal", "WithdrawalRequest"), WithdrawalAdmin),
    # Not created yet — safe to leave enabled, skipped until the models exist.
    (("PromoCode",), PromoAdmin),
    (("Referral",), ReferralAdmin),
)

for _names, _admin_class in ADMIN_REGISTRY:
    register(_names, _admin_class)
