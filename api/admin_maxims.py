from django.contrib import admin
from django.contrib.admin.models import CHANGE, LogEntry
from django.contrib import messages
from unfold.admin import ModelAdmin as UnfoldModelAdmin
from django.utils.html import format_html
from django.db import transaction
from django.contrib.contenttypes.models import ContentType
import logging


logger = logging.getLogger(__name__)


class AutoRawIdMixin:
    def __init__(self, model, admin_site):
        if not hasattr(self, 'raw_id_fields'):
            self.raw_id_fields = ()
        super().__init__(model, admin_site)


class SuperuserWriteAdminMixin:
    """Only superusers can add/edit/delete objects."""

    def has_add_permission(self, request):
        return request.user.is_superuser

    def has_change_permission(self, request, obj=None):
        return request.user.is_superuser

    def has_delete_permission(self, request, obj=None):
        return request.user.is_superuser


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
                objs = list(manager.filter(pk__in=eligible_ids))
                content_type = ContentType.objects.get_for_model(self.model)
                LogEntry.objects.bulk_create([
                    LogEntry(
                        user_id=request.user.pk,
                        content_type_id=content_type.pk,
                        object_id=str(obj.pk),
                        object_repr=str(obj)[:200],
                        action_flag=CHANGE,
                        change_message=f"Bulk action: status set to '{new_status}'.",
                    )
                    for obj in objs
                ])

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


class BaseAdmin(AutoRawIdMixin, UnfoldModelAdmin):
    """Safe defaults for every model: fast FK widgets, sensible list view."""

    list_display = ("__str__", "pk")
    ordering = ("-pk",)
    list_per_page = 50


class LedgerAdmin(SuperuserWriteAdminMixin, BaseAdmin):
    """Financial / audit records: superuser add + edit, never deletable."""

    list_per_page = 100
