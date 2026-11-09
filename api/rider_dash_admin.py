"""
Rider admin - performance dashboard, coaching tools and review queues.
Requires the model additions in rider_performance_patch.py.
"""
import csv
from datetime import timedelta
from urllib.parse import quote

from django.contrib import admin, messages
from django.core.exceptions import ValidationError
from django.db.models import (
    Avg, Case, Count, ExpressionWrapper, F, FloatField, Q, When,
)
from django.http import HttpResponse
from django.urls import reverse
from django.utils import timezone
from django.utils.html import format_html, format_html_join
from django.core.validators import RegexValidator

from .rider_domain import (
    HIGH_CANCEL, HIGH_EXPIRY, LICENCE_WARNING_DAYS, LOW_ACCEPTANCE, LOW_RATING,
    MIN_SAMPLE, SLOW_RESPONSE,
    RiderProfile, RiderEmergencyContact, RiderIncident, RiderPerformanceCommit,
    RiderStatusHistory, VerificationStatus,
    RiderProfileAdminNote, RiderProfileAdminNoteSerializer, RiderDocuments
)

BRAND = "RYDNOW"
GREEN, ORANGE, RED, GREY = "#16a34a", "#d97706", "#dc2626", "#6b7280"

STATUS_COLORS = {
    VerificationStatus.VERIFIED: GREEN,
    VerificationStatus.UNDER_REVIEW: "#2563eb",
    VerificationStatus.PENDING: GREY,
    VerificationStatus.REJECTED: RED,
    VerificationStatus.SUSPENDED: RED,
}
GRADE_COLORS = {"A": GREEN, "B": "#65a30d", "C": ORANGE, "D": RED, "New": GREY}


# ————————————————————————— #
# Helpers
# ————————————————————————— #
def _pct(numerator, denominator):
    """DB-side percentage that is NULL (not an error) when the denominator is 0."""
    return Case(
        When(
            **{f"{denominator}__gt": 0},
            then=ExpressionWrapper(F(numerator) * 100.0 / F(denominator), output_field=FloatField()),
        ),
        default=None,
        output_field=FloatField(),
    )


def _tone(value, good, bad, higher_is_better=True):
    if value is None:
        return GREY
    if higher_is_better:
        return GREEN if value >= good else RED if value < bad else ORANGE
    return GREEN if value <= good else RED if value > bad else ORANGE


def _fmt_seconds(s):
    if s is None:
        return "-"
    s = int(s)
    return f"{s}s" if s < 60 else f"{s // 60}m {s % 60:02d}s"


def _fmt_pct(v):
    return "-" if v is None else f"{v:.1f}%"


def _digits(phone):
    return (phone or "").lstrip("+")


def performance_message(r):
    """Ready-to-send WhatsApp text summarising the rider's performance."""
    first = r.full_name.split()[0] if r.full_name else "Rider"
    score = getattr(r, 'performance_score', None)
    lines = [
        f"Hi {first}, this is {BRAND} support with your performance update.",
        "",
        f"Rating: {r.rating_average:.2f}/5 ({r.rating_count} reviews)",
        f"Jobs completed: {r.completed_trips}",
        f"Acceptance rate: {r.acceptance_rate}%",
        f"Cancellation rate: {r.cancellation_rate}%",
    ]
    if hasattr(r, 'avg_response_seconds'):
        lines.append(f"Avg response time: {_fmt_seconds(r.avg_response_seconds)}")
    grade = getattr(r, 'performance_grade', 'N/A')
    lines.append(f"Overall grade: {grade}" + (f" ({score}/100)" if score is not None else ""))
    lines.append("")
    if hasattr(r, 'attention_reasons') and r.attention_reasons:
        lines.append("Please work on: " + "; ".join(r.attention_reasons) + ".")
        lines.append("Reply here if you need any help.")
    else:
        lines.append("Great work - keep it up!")
    return "\n".join(lines)


def whatsapp_url(r, with_report=True):
    text = f"?text={quote(performance_message(r))}" if with_report else ""
    return f"https://wa.me/{_digits(r.phone)}{text}"


# ————————————————————————— #
# Attention rule
# ————————————————————————— #
ATTENTION_Q = (
    Q(offers_received__gte=MIN_SAMPLE, acceptance_pct__lt=LOW_ACCEPTANCE)
    | Q(offers_received__gte=MIN_SAMPLE, expiry_pct__gt=HIGH_EXPIRY)
    | Q(attempts__gte=MIN_SAMPLE, cancel_pct__gt=HIGH_CANCEL)
    | Q(rating_count__gte=5, rating_average__lt=LOW_RATING)
    | Q(strike_count__gte=2)
)


# ————————————————————————— #
# Filters
# ————————————————————————— #
class AttentionFilter(admin.SimpleListFilter):
    title = "performance"
    parameter_name = "perf"

    def lookups(self, request, model_admin):
        return [
            ("attention", "Needs coaching"),
            ("low_accept", f"Acceptance below {LOW_ACCEPTANCE}%"),
            ("ignoring", f"Ignoring offers (>{HIGH_EXPIRY}%)"),
            ("cancels", f"High cancellations (>{HIGH_CANCEL}%)"),
            ("low_rating", f"Rating below {LOW_RATING}"),
            ("strikes", "Has strikes"),
            ("top", "Top performers (4.7+ rating, 90%+ acceptance)"),
        ]

    def queryset(self, request, qs):
        v = self.value()
        if v == "attention":
            return qs.filter(ATTENTION_Q)
        if v == "low_accept":
            return qs.filter(offers_received__gte=MIN_SAMPLE, acceptance_pct__lt=LOW_ACCEPTANCE)
        if v == "ignoring":
            return qs.filter(offers_received__gte=MIN_SAMPLE, expiry_pct__gt=HIGH_EXPIRY)
        if v == "cancels":
            return qs.filter(attempts__gte=MIN_SAMPLE, cancel_pct__gt=HIGH_CANCEL)
        if v == "low_rating":
            return qs.filter(rating_count__gte=5, rating_average__lt=LOW_RATING)
        if v == "strikes":
            return qs.filter(strike_count__gt=0)
        if v == "top":
            return qs.filter(
                offers_received__gte=MIN_SAMPLE, acceptance_pct__gte=90,
                rating_average__gte=4.7, rating_count__gte=5,
            )
        return qs


class ActivityFilter(admin.SimpleListFilter):
    title = "last job"
    parameter_name = "activity"

    def lookups(self, request, model_admin):
        return [
            ("today", "Worked today"),
            ("week", "Worked in last 7 days"),
            ("idle7", "No job in 7+ days"),
            ("idle30", "No job in 30+ days"),
            ("never", "Never completed a job"),
        ]

    def queryset(self, request, qs):
        now, v = timezone.now(), self.value()
        if v == "today":
            return qs.filter(created_at__date=timezone.localdate())
        if v == "week":
            return qs.filter(created_at__gte=now - timedelta(days=7))
        if v == "idle7":
            return qs.filter(is_active=True).filter(
                Q(created_at__lt=now - timedelta(days=7)) | Q(created_at__isnull=True))
        if v == "idle30":
            return qs.filter(is_active=True).filter(
                Q(created_at__lt=now - timedelta(days=30)) | Q(created_at__isnull=True))
        if v == "never":
            return qs.filter(completed_trips=0)
        return qs


# ————————————————————————— #
# Main admin — Register in admin.py
# ————————————————————————— #

@admin.register(RiderProfile)
class RiderProfileAdmin(admin.ModelAdmin):
    list_per_page = 50
    list_select_related = ("user",)
    date_hierarchy = "created_at"
    save_on_top = True
    empty_value_display = "-"

    list_display = (
        "full_name", "status_badge", "is_online",
        "get_acceptance", "get_lost", "get_cancelled",
        "rating_average", "completed_trips", "strike_count", "created_at",
    )
    list_display_links = ("full_name",)
    list_filter = (
        AttentionFilter, ActivityFilter,
        "verification_status", "is_online", "tier", "is_active",
    )
    search_fields = ("full_name", "phone", "user__username", "user__email")
    ordering = ("-created_at",)

    readonly_fields = (
        "full_name", "phone", "profile_photo",
        "verification_status", "is_online",
        "rating_average", "completed_trips", "cancelled_trips",
        "offers_received", "offers_accepted", "strike_count",
        "created_at", "updated_at",
    )

    actions = [
        "approve_riders", "mark_as_suspended", "reset_strikes",
        "send_whatsapp_report", "export_performance_csv",
    ]

    def get_queryset(self, request):
        return super().get_queryset(request).filter(deleted_at__isnull=True)

    # — Columns —
    @admin.display(description="Status")
    def status_badge(self, obj):
        return format_html('<b style="color:{}">{}</b>',
                           STATUS_COLORS.get(obj.verification_status, GREY),
                           obj.get_verification_status_display())

    @admin.display(description="Accepted %")
    def get_acceptance(self, obj):
        return f"{obj.acceptance_rate}%"

    @admin.display(description="Lost %")
    def get_lost(self, obj):
        lost = 100 - obj.acceptance_rate
        return f"🔴 {lost}%" if lost > 40 else f"{lost}%"

    @admin.display(description="Cancelled %")
    def get_cancelled(self, obj):
        return f"{obj.cancellation_rate}%"

    # — Actions —
    @admin.action(description="✅ Mark selected as Verified")
    def approve_riders(self, request, queryset):
        updated = 0
        for r in queryset:
            if r.verification_status != VerificationStatus.VERIFIED:
                r.approve(by=request.user)
                updated += 1
        self.message_user(request, f"{updated} riders approved.", messages.SUCCESS)

    @admin.action(description="⚠️ Suspend selected riders")
    def mark_as_suspended(self, request, queryset):
        updated = queryset.update(
            verification_status=VerificationStatus.SUSPENDED,
            is_online=False
        )
        self.message_user(request, f"{updated} riders suspended.", messages.WARNING)

    @admin.action(description="🔄 Reset strike count")
    def reset_strikes(self, request, queryset):
        updated = queryset.update(strike_count=0)
        self.message_user(request, f"Strikes reset for {updated} riders.", messages.INFO)

    @admin.action(description="📱 WhatsApp performance report")
    def send_whatsapp_report(self, request, queryset):
        for r in queryset[:10]:
            self.message_user(
                request,
                format_html('{}: <a href="{}" target="_blank"><b>Send Report</b></a>',
                            r.full_name, whatsapp_url(r)),
                messages.INFO
            )

    @admin.action(description="📊 Export performance CSV")
    def export_performance_csv(self, request, queryset):
        response = HttpResponse(content_type="text/csv")
        response["Content-Disposition"] = f'attachment; filename="rider_performance_{timezone.localdate()}.csv"'
        w = csv.writer(response)
        w.writerow(["Name", "Phone", "Status", "Accepted %", "Cancelled %", 
                    "Rating", "Trips", "Strikes", "Joined"])
        for r in queryset:
            w.writerow([
                r.full_name, r.phone, r.get_verification_status_display(),
                f"{r.acceptance_rate}%", f"{r.cancellation_rate}%",
                r.rating_average, r.completed_trips, r.strike_count, r.created_at.date()
            ])
        return response


# — Document review queue —
@admin.register(RiderDocument)
class RiderDocumentAdmin(admin.ModelAdmin):
    list_display = ("rider", "document_type", "status", "expiry_date", "uploaded_at")
    list_filter = ("status", "document_type")
    search_fields = ("rider__full_name", "rider__phone")
    actions = ["approve_documents"]

    @admin.action(description="✅ Approve selected documents")
    def approve_documents(self, request, queryset):
        updated = 0
        for d in queryset:
            if d.status != d.Status.APPROVED:
                d.approve(by=request.user)
                updated += 1
        self.message_user(request, f"{updated} document(s) approved.", messages.SUCCESS)







phone_validator = RegexValidator(
    regex=r"^\+?[0-9][0-9\s\-]{6,19}$",
    message="Enter a valid phone number (digits, optional leading +).",
)


