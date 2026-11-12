"""
Professional Rider + Booking admin for RYDNOW (django-unfold).

Replaces the minimal RiderProfileAdmin and BookingAdmin in admin.py.
Every field name below comes from the models.py you pasted.
"""
from django.contrib import admin, messages
from django.core.exceptions import ValidationError
from django.db.models import Count, Q, Sum
from django.utils import timezone
from django.utils.html import format_html, format_html_join
from django.utils.safestring import mark_safe
from unfold.admin import TabularInline
from unfold.decorators import action, display
from .admin_maxims import AutoRawIdMixin, BulkStatusActionMixin, BaseAdmin


from .models import (
    Booking,
    BookingOffer,
    BookingStatusHistory,
    RiderDocument,
    RiderEmergencyContact,
    RiderPayoutAccount,
    RiderProfile,
    RiderProfileAdminNote,
    RiderStatusHistory,
    ServiceQualification,
)


# Styles for the cards below live in static/css/rydnow_admin.css


# Fields counted for the "profile completion" bar (real field names).
COMPLETION_FIELDS = (
    "full_name", "phone", "profile_photo", "date_of_birth", "gender", "ghana_card_number",
    "city", "address", "motorbike_model", "motorbike_plate_number", "motorbike_color",
    "license_number", "license_expiry_date",
)


def _num(v, suffix="", dash="—"):
    return dash if v is None else f"{v}{suffix}"


# ═══════════════════════════ INLINES ═══════════════════════════
class QualificationInline(TabularInline):
    model = ServiceQualification
    extra = 0
    tab = True
    verbose_name_plural = "Services"
    fields = ("service", "status", "status_reason", "approved_at")
    readonly_fields = ("approved_at",)


class DocumentInline(TabularInline):
    model = RiderDocument
    extra = 0
    tab = True
    verbose_name_plural = "Documents"
    fields = ("document_type", "preview", "file", "status", "expiry_date", "notes")
    readonly_fields = ("preview",)

    @admin.display(description="Preview")
    def preview(self, obj):
        if not obj.pk or not obj.file:
            return "—"
        if obj.file.name.lower().endswith(".pdf"):
            return format_html('<a href="{}" target="_blank">Open PDF</a>', obj.file.url)
        return format_html(
            '<a href="{0}" target="_blank"><img src="{0}" style="height:64px;border-radius:8px"></a>',
            obj.file.url,
        )


class PayoutInline(TabularInline):
    model = RiderPayoutAccount
    extra = 0
    tab = True
    verbose_name_plural = "Payout accounts"
    fields = ("network", "momo_number", "account_name", "is_verified", "is_primary")


class EmergencyContactInline(TabularInline):
    model = RiderEmergencyContact
    extra = 0
    tab = True
    verbose_name_plural = "Emergency contacts"
    fields = ("full_name", "relationship", "phone_number", "is_primary", "is_verified")


class StatusHistoryInline(TabularInline):
    model = RiderStatusHistory
    extra = 0
    tab = True
    verbose_name_plural = "Status history"
    fields = ("created_at", "from_status", "to_status", "changed_by", "reason")
    readonly_fields = fields
    can_delete = False

    def has_add_permission(self, request, obj=None):
        return False


class AdminNoteInline(TabularInline):
    model = RiderProfileAdminNote
    extra = 0
    tab = True
    verbose_name_plural = "Admin notes"
    fields = ("note", "author", "created_at")
    readonly_fields = ("author", "created_at")
    can_delete = False


# ═══════════════════════════ RIDER ═══════════════════════════
@admin.register(RiderProfile)
class RiderProfileAdmin(BaseAdmin):
    list_display = (
        "rider_card", "phone", "status_label", "live_label", "tier_label",
        "rating_stars", "completed_trips", "grade_label", "completion_bar", "created_at",
    )
    list_filter = ("verification_status", "is_online", "tier", "is_active", "zone", "created_at")
    search_fields = (
        "full_name", "phone", "rider_code", "ghana_card_number", "motorbike_plate_number",
        "license_number", "user__username", "user__email",
    )
    list_select_related = ("user", "zone")
    list_per_page = 40
    date_hierarchy = "created_at"
    ordering = ("-created_at",)
    warn_unsaved_form = True
    inlines = (
        QualificationInline, DocumentInline, PayoutInline, EmergencyContactInline,
        StatusHistoryInline, AdminNoteInline,
    )
    actions = (
        "approve_riders", "reject_riders", "suspend_riders", "reinstate_riders", "force_offline",
    )

    # Counters are updated with F() by the app: never edit them by hand here.
    readonly_fields = (
        "debrief_card", "id", "public_id", "rider_code", "verified_at", "verified_by", "submitted_at",
        "last_online_at", "online_since", "location_updated_at", "rating_average", "rating_count",
        "completed_trips", "cancelled_trips", "offers_received", "offers_accepted", "offers_declined",
        "offers_expired", "response_time_total_seconds", "response_count", "arrival_time_total_seconds",
        "arrival_count", "total_online_seconds", "strike_count", "last_job_at", "last_contacted_at",
        "created_at", "updated_at",
    )
    fieldsets = (
        (None, {"fields": ("debrief_card",)}),
        ("Profile", {"classes": ["tab"], "fields": (
            ("full_name", "phone"), "profile_photo", ("date_of_birth", "gender"),
            ("city", "address"), "preferred_language", "user", "rider_code", "referred_by", "zone",
        )}),
        ("Vehicle & licence", {"classes": ["tab"], "fields": (
            ("motorbike_model", "motorbike_plate_number", "motorbike_color"),
            ("ghana_card_number", "license_number", "license_expiry_date"),
        )}),
        ("Verification", {"classes": ["tab"], "fields": (
            "verification_status", "rejection_reason", ("submitted_at", "verified_at", "verified_by"),
        )}),
        ("Safety", {"classes": ["tab"], "fields": (
            ("suspended_at", "suspended_until"), "suspension_reason",
            ("is_restricted", "restricted_until"), "restriction_notes", "strike_count", "is_active",
        )}),
        ("Live & performance", {"classes": ["tab"], "fields": (
            ("is_online", "last_online_at", "online_since"),
            ("current_lat", "current_lng", "location_updated_at"),
            ("tier", "rating_average", "rating_count"),
            ("completed_trips", "cancelled_trips"),
            ("offers_received", "offers_accepted", "offers_declined", "offers_expired"),
            ("response_time_total_seconds", "response_count"),
            ("arrival_time_total_seconds", "arrival_count"),
            ("total_online_seconds", "last_job_at", "last_contacted_at"),
        )}),
        ("System", {"classes": ["tab"], "fields": (
            "id", "public_id", "terms_accepted_at", "terms_version", "deleted_at", "created_at", "updated_at",
        )}),
    )

    # ── list columns ──
    @admin.display(description="Rider", ordering="full_name")
    def rider_card(self, obj):
        if obj.profile_photo:
            av = format_html('<img class="rn-av" src="{}">', obj.profile_photo.url)
        else:
            av = format_html('<span class="rn-av">{}</span>', (obj.full_name or "?")[:1].upper())
        return format_html('<div class="rn-who">{}<div>{}<small>{}</small></div></div>',
                           av, obj.full_name, obj.rider_code or "no code")

    @display(description="Status", ordering="verification_status", label={
        "Verified": "success", "Pending Documents": "warning", "Under Review": "info",
        "Rejected": "danger", "Suspended": "danger",
    })
    def status_label(self, obj):
        return obj.get_verification_status_display()

    @display(description="Live", ordering="is_online", label={"Online": "success", "Offline": "info"})
    def live_label(self, obj):
        return "Online" if obj.is_online else "Offline"

    @display(description="Tier", ordering="tier", label={
        "Standard": "info", "Silver": "info", "Gold": "warning", "Platinum": "success"})
    def tier_label(self, obj):
        return obj.get_tier_display()

    @admin.display(description="Rating", ordering="rating_average")
    def rating_stars(self, obj):
        return f"★ {obj.rating_average:.1f} ({obj.rating_count})"

    @display(description="Grade", label={"A": "success", "B": "info", "C": "warning", "D": "danger"})
    def grade_label(self, obj):
        try:
            return obj.performance_grade
        except Exception:  # constants like MIN_SAMPLE missing → never break the list
            return "New"

    @admin.display(description="Profile")
    def completion_bar(self, obj):
        filled = sum(1 for f in COMPLETION_FIELDS if getattr(obj, f))
        pct = round(filled * 100 / len(COMPLETION_FIELDS))
        return format_html('<div class="rn-bar"><span style="width:{}%"></span></div><small>{}%</small>', pct, pct)

    # ── Driver-debrief card (today) ──
    @admin.display(description="")
    def debrief_card(self, obj):
        if not obj or not obj.pk:
            return "Save the rider to see today's debrief."
        today = timezone.localdate()
        a = Booking.objects.filter(assigned_rider=obj, created_at__date=today).order_by().aggregate(
            total=Count("pk"),
            done=Count("pk", filter=Q(status="completed")),
            cancelled=Count("pk", filter=Q(status="cancelled")),
            earned=Sum("rider_payout", filter=Q(status="completed")),
        )
        total, done = a["total"] or 0, a["done"] or 0
        pct = round(done * 100 / total) if total else 0
        try:
            flags = obj.attention_reasons
        except Exception:
            flags = []
        flag_html = format_html_join("", '<span class="rn-flag">{}</span>', ((f,) for f in flags)) \
            if flags else mark_safe('<span style="opacity:.6">All good — nothing needs attention.</span>')

        return format_html(
            '<div class="rn-deb">'
            '<div class="c"><h4>Today\'s stops</h4><div class="rn-top">'
            '<div class="rn-ring" style="--p:{pct}"><i>{pct}%</i></div>'
            '<div><div class="row"><span>Total&nbsp;</span><b>{total}</b></div>'
            '<div class="row"><span>Completed&nbsp;</span><b>{done}</b></div>'
            '<div class="row"><span>Cancelled&nbsp;</span><b>{cancelled}</b></div></div></div></div>'
            '<div class="c"><h4>Offers</h4>'
            '<div class="row"><span>Acceptance</span><b>{acc}%</b></div>'
            '<div class="row"><span>Declined</span><b>{dec}</b></div>'
            '<div class="row"><span>Timed out</span><b>{exp}</b></div>'
            '<div class="row"><span>Avg response</span><b>{resp}</b></div></div>'
            '<div class="c"><h4>Service quality</h4>'
            '<div class="row"><span>Rating</span><b>★ {rating}</b></div>'
            '<div class="row"><span>Completion rate</span><b>{comp}</b></div>'
            '<div class="row"><span>Avg arrival</span><b>{arr}</b></div>'
            '<div class="row"><span>Online (all time)</span><b>{hrs} h</b></div></div>'
            '<div class="c"><h4>Earned today</h4><div style="font-size:26px;font-weight:700">GH₵ {earned}</div>'
            '<div style="margin-top:10px">{flags}</div></div></div>',
            pct=pct, total=total, done=done, cancelled=a["cancelled"] or 0,
            acc=obj.acceptance_rate, dec=obj.offers_declined, exp=obj.offers_expired,
            resp=_num(obj.avg_response_seconds, "s"), rating=f"{obj.rating_average:.1f}",
            comp=_num(obj.completion_rate, "%"), arr=_num(obj.avg_arrival_minutes, " min"),
            hrs=obj.total_online_hours, earned=a["earned"] or "0.00", flags=flag_html,
        )

    # ── actions (use your audited model methods) ──
    def _each(self, request, queryset, fn, done_msg):
        ok = 0
        for rider in queryset:
            try:
                fn(rider)
                ok += 1
            except ValidationError as exc:
                self.message_user(request, f"{rider.full_name}: {' '.join(exc.messages)}", messages.WARNING)
        if ok:
            self.message_user(request, f"{ok} rider(s) {done_msg}.", messages.SUCCESS)

    @action(description="Approve riders", permissions=["change"])
    def approve_riders(self, request, queryset):
        self._each(request, queryset, lambda r: r.approve(request.user), "approved")

    @action(description="Reject (uses the rejection reason saved on the profile)", permissions=["change"])
    def reject_riders(self, request, queryset):
        self._each(request, queryset, lambda r: r.reject(request.user, r.rejection_reason), "rejected")

    @action(description="Suspend riders", permissions=["change"])
    def suspend_riders(self, request, queryset):
        self._each(
            request, queryset,
            lambda r: r.suspend(request.user, r.suspension_reason or "Suspended by admin"), "suspended",
        )

    @action(description="Reinstate riders", permissions=["change"])
    def reinstate_riders(self, request, queryset):
        self._each(request, queryset, lambda r: r.reinstate(request.user), "reinstated")

    @action(description="Force offline", permissions=["change"])
    def force_offline(self, request, queryset):
        self._each(request, queryset.filter(is_online=True), lambda r: r.go_offline(), "set offline")

    def save_formset(self, request, form, formset, change):
        instances = formset.save(commit=False)
        for obj in instances:
            if isinstance(obj, RiderProfileAdminNote) and not obj.author_id:
                obj.author = request.user
            obj.save()
        formset.save_m2m()
        for obj in formset.deleted_objects:
            obj.delete()


# ═══════════════════════════ BOOKINGS ═══════════════════════════
class BookingHistoryInline(TabularInline):
    model = BookingStatusHistory
    extra = 0
    tab = True
    verbose_name_plural = "Timeline"
    fields = ("created_at", "from_status", "to_status", "changed_by", "note")
    readonly_fields = fields
    can_delete = False

    def has_add_permission(self, request, obj=None):
        return False


class BookingOfferInline(TabularInline):
    model = BookingOffer
    extra = 0
    tab = True
    verbose_name_plural = "Dispatch offers"
    fields = ("rider", "status", "distance_to_pickup_km", "sent_at", "responded_at")
    readonly_fields = ("rider", "status", "distance_to_pickup_km", "sent_at", "responded_at")
    can_delete = False

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(Booking)
class BookingAdmin(BulkStatusActionMixin, BaseAdmin):
    list_display = (
        "booking_ref", "service_label", "customer_name", "rider_name", "status_label",
        "payment_label", "total_fare", "created_at",
    )
    list_filter = ("status", "service_type", "payment_status", "payment_method", "source", "created_at")
    search_fields = (
        "booking_ref", "pickup", "destination", "payment_ref", "customer__user__username",
        "assigned_rider__full_name", "assigned_rider__rider_code", "assigned_rider__phone",
    )
    list_select_related = ("customer__user", "assigned_rider")
    date_hierarchy = "created_at"
    ordering = ("-created_at",)
    list_per_page = 40
    inlines = (BookingHistoryInline, BookingOfferInline)
    readonly_fields = ("created_at", "booking_ref")
    actions = ("mark_completed", "mark_cancelled")
    final_statuses = ("completed", "cancelled")

    @admin.display(description="Service", ordering="service_type")
    def service_label(self, obj):
        return obj.get_service_type_display()

    @admin.display(description="Customer")
    def customer_name(self, obj):
        if not obj.customer_id:
            return "—"
        u = getattr(obj.customer, "user", None)
        return (u.get_full_name() or u.get_username()) if u else str(obj.customer)

    @admin.display(description="Rider")
    def rider_name(self, obj):
        return obj.assigned_rider.full_name if obj.assigned_rider_id else "—"

    @display(description="Status", ordering="status", label={
        "Completed": "success", "Cancelled": "danger", "In Progress": "info", "Assigned": "info",
        "Confirmed": "info", "Pending": "warning", "Searching": "warning", "No Rider Found": "danger",
    })
    def status_label(self, obj):
        return obj.get_status_display()

    @display(description="Payment", ordering="payment_status", label=True)
    def payment_label(self, obj):
        return obj.get_payment_status_display()

    @admin.action(description="Mark selected bookings as completed", permissions=["change"])
    def mark_completed(self, request, queryset):
        self._apply_status(request, queryset, "completed")

    @admin.action(description="Mark selected bookings as cancelled", permissions=["change"])
    def mark_cancelled(self, request, queryset):
        self._apply_status(request, queryset, "cancelled")

