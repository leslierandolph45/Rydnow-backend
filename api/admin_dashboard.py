"""
RYDNOW admin dashboard + live map data.

Written against the models you pasted:
  RiderProfile (is_online, current_lat/lng, location_updated_at, verification_status, ...)
  Booking      (status, pickup_lat/lng, destination_lat/lng, assigned_rider, customer, ...)
  RiderDocument(status), ServiceQualification
"""
from datetime import timedelta
from decimal import Decimal

from django.contrib import admin
from django.db.models import Count, Q, Sum
from django.db.models.functions import TruncDate
from django.http import JsonResponse
from django.template.response import TemplateResponse
from django.urls import reverse
from django.utils import timezone

from .models import (
    Booking,
    BookingOffer,
    RiderDocument,
    RiderOnlineSession,
    RiderProfile,
    VerificationStatus,
)

try:  # defined somewhere in your models.py
    from .models import LOCATION_STALE_SECONDS
except ImportError:  # pragma: no cover
    LOCATION_STALE_SECONDS = 120

# Bookings shown on the live map (anything still in flight).
LIVE_BOOKING_STATUSES = ("pending", "searching", "searching_rider", "assigned", "in_progress")


# ─────────────────────────── helpers ───────────────────────────
def _valid_point(lat, lng):
    return lat is not None and lng is not None and not (lat == 0 and lng == 0)


def _customer_name(customer):
    if not customer:
        return "Walk-in customer"
    user = getattr(customer, "user", None)
    if user:
        return user.get_full_name() or user.get_username()
    return str(customer)


# ─────────────────────────── live map ───────────────────────────
def live_map_page(request):
    ctx = {
        **admin.site.each_context(request),
        "title": "Live map",
        "live_data_url": reverse("rn_live_map_data"),
    }
    return TemplateResponse(request, "admin/live_map.html", ctx)


def live_map_data(request):
    now = timezone.now()
    fresh_after = now - timedelta(seconds=LOCATION_STALE_SECONDS)
    service = (request.GET.get("service") or "").strip()

    riders_qs = (
        RiderProfile.objects.active()
        .filter(is_online=True, current_lat__isnull=False, current_lng__isnull=False)
        .select_related("zone")
    )
    bookings_qs = Booking.objects.filter(status__in=LIVE_BOOKING_STATUSES).select_related(
        "customer__user", "assigned_rider"
    )
    if service:
        riders_qs = riders_qs.filter(
            qualifications__service_id=service, qualifications__status="approved"
        ).distinct()
        bookings_qs = bookings_qs.filter(service_type=service)

    busy = {
        str(pk): ref
        for pk, ref in Booking.objects.filter(
            status__in=Booking.ACTIVE_STATUSES, assigned_rider__isnull=False
        ).values_list("assigned_rider_id", "booking_ref")
    }

    riders = []
    for r in riders_qs[:500]:
        if not _valid_point(r.current_lat, r.current_lng):
            continue
        stale = not r.location_updated_at or r.location_updated_at < fresh_after
        state = "stale" if stale else ("busy" if str(r.pk) in busy else "free")
        riders.append({
            "id": str(r.pk),
            "name": r.full_name,
            "code": r.rider_code,
            "phone": r.phone,
            "lat": r.current_lat,
            "lng": r.current_lng,
            "state": state,
            "rating": r.rating_average,
            "trips": r.completed_trips,
            "tier": r.get_tier_display(),
            "zone": r.zone.name if r.zone_id and hasattr(r.zone, "name") else "",
            "bike": f"{r.motorbike_model} {r.motorbike_plate_number}".strip(),
            "booking_ref": busy.get(str(r.pk), ""),
            "seen": r.location_updated_at.isoformat() if r.location_updated_at else "",
            "url": reverse("admin:api_riderprofile_change", args=[r.pk]),
        })

    bookings = []
    for b in bookings_qs.order_by("-created_at")[:300]:
        rider = b.assigned_rider
        bookings.append({
            "id": str(b.pk),
            "ref": b.booking_ref,
            "service": b.service_type,
            "service_label": b.get_service_type_display(),
            "status": b.status,
            "status_label": b.get_status_display(),
            "customer": _customer_name(b.customer),
            "phone": getattr(b.customer, "phone", "") if b.customer_id else "",
            "pickup": b.pickup,
            "destination": b.destination,
            "p": [b.pickup_lat, b.pickup_lng] if _valid_point(b.pickup_lat, b.pickup_lng) else None,
            "d": [b.destination_lat, b.destination_lng]
            if _valid_point(b.destination_lat, b.destination_lng) else None,
            "fare": str(b.total_fare),
            "rider_id": str(rider.pk) if rider else "",
            "rider_name": rider.full_name if rider else "",
            "url": reverse("admin:api_booking_change", args=[b.pk]),
        })

    return JsonResponse({
        "now": now.isoformat(),
        "riders": riders,
        "bookings": bookings,
        "services": [{"key": k, "label": str(v)} for k, v in Booking._meta.get_field("service_type").choices],
        "counts": {
            "online": len(riders),
            "free": sum(1 for r in riders if r["state"] == "free"),
            "busy": sum(1 for r in riders if r["state"] == "busy"),
            "stale": sum(1 for r in riders if r["state"] == "stale"),
            "bookings": len(bookings),
            "searching": sum(1 for b in bookings if b["status"] in ("pending", "searching", "searching_rider")),
        },
    })


# ─────────────────────────── dashboard ───────────────────────────
def _paths(values, top, width=640, height=150, pad=8):
    """SVG line + closed-area path for a series, scaled to a shared maximum."""
    if not values:
        return "", ""
    top = max(top, 1)
    step = (width - 2 * pad) / max(len(values) - 1, 1)
    pts = [(pad + i * step, height - pad - (v / top) * (height - 2 * pad)) for i, v in enumerate(values)]
    line = "M " + " L ".join(f"{x:.1f},{y:.1f}" for x, y in pts)
    area = f"{line} L {pts[-1][0]:.1f},{height} L {pts[0][0]:.1f},{height} Z"
    return line, area


def _pct(part, whole):
    return round(part * 100 / whole) if whole else 0


def _daily(qs, field, start, distinct=None):
    """{date: count} for the last 14 days."""
    agg = Count(distinct, distinct=True) if distinct else Count("pk")
    rows = (
        qs.filter(**{f"{field}__date__gte": start})
        .annotate(day=TruncDate(field)).order_by().values("day").annotate(n=agg)
    )
    return {r["day"]: r["n"] for r in rows}


def dashboard_callback(request, context):
    today = timezone.localdate()
    riders = RiderProfile.objects.not_deleted()

    # ── rider counts & percentages ──
    by_status = dict(riders.order_by().values_list("verification_status").annotate(n=Count("pk")))
    total = sum(by_status.values())
    verified = by_status.get(VerificationStatus.VERIFIED, 0)
    awaiting = by_status.get(VerificationStatus.PENDING, 0) + by_status.get(VerificationStatus.UNDER_REVIEW, 0)
    online = riders.filter(is_online=True).count()
    week_new = riders.filter(created_at__date__gt=today - timedelta(days=7)).count()
    prev_week = riders.filter(
        created_at__date__lte=today - timedelta(days=7), created_at__date__gt=today - timedelta(days=14)
    ).count()
    growth = (round((week_new - prev_week) * 100 / prev_week) if prev_week else (100 if week_new else 0))

    kpis = [
        {"label": "Total riders", "value": f"{total:,}", "icon": "groups", "tone": "slate",
         "note": f"+{week_new} this week", "pill": f"{growth:+d}% vs last week", "pill_tone": "up" if growth >= 0 else "down"},
        {"label": "Verified", "value": f"{verified:,}", "icon": "verified", "tone": "green",
         "note": "of all riders", "pill": f"{_pct(verified, total)}%", "pill_tone": "green"},
        {"label": "Pending", "value": f"{awaiting:,}", "icon": "schedule", "tone": "orange",
         "note": "awaiting verification", "pill": f"{_pct(awaiting, total)}%", "pill_tone": "orange"},
        {"label": "Online now", "value": f"{online:,}", "icon": "bolt", "tone": "teal",
         "note": "of verified riders", "pill": f"{_pct(online, verified)}%", "pill_tone": "teal"},
    ]

    # ── donut ──
    palette = [
        (VerificationStatus.VERIFIED, "Verified", "#1FA463"),
        (VerificationStatus.UNDER_REVIEW, "Under review", "#1E3FCE"),
        (VerificationStatus.PENDING, "Pending documents", "#F5B82E"),
        (VerificationStatus.REJECTED, "Rejected", "#D64545"),
        (VerificationStatus.SUSPENDED, "Suspended", "#7A8194"),
    ]
    legend, stops, acc = [], [], 0.0
    for key, label, color in palette:
        n = by_status.get(key, 0)
        pct = (n / total * 100) if total else 0
        legend.append({"label": label, "n": n, "pct": round(pct), "color": color})
        if n:
            stops.append(f"{color} {acc:.2f}% {acc + pct:.2f}%")
            acc += pct
    donut = f"conic-gradient({', '.join(stops)})" if stops else "conic-gradient(#c9cedd 0 100%)"

    # ── rider activity (14 days): active riders vs new sign-ups ──
    start = today - timedelta(days=13)
    days = [start + timedelta(days=i) for i in range(14)]
    active_by_day = _daily(RiderOnlineSession.objects.all(), "started_at", start, distinct="rider")
    new_by_day = _daily(riders, "created_at", start)
    active = [active_by_day.get(d, 0) for d in days]
    signups = [new_by_day.get(d, 0) for d in days]
    top = max(active + signups + [1])
    a_line, a_area = _paths(active, top)
    n_line, _ = _paths(signups, top)

    # ── booking activity (14 days) ──
    created = _daily(Booking.objects.all(), "created_at", start)
    done = _daily(Booking.objects.filter(status="completed"), "created_at", start)
    b_created = [created.get(d, 0) for d in days]
    b_done = [done.get(d, 0) for d in days]
    btop = max(b_created + [1])
    b_line, b_area = _paths(b_created, btop)
    bd_line, _ = _paths(b_done, btop)

    # ── today's performance in percentages ──
    t = Booking.objects.filter(created_at__date=today).order_by().aggregate(
        n=Count("pk"), done=Count("pk", filter=Q(status="completed")),
        cancelled=Count("pk", filter=Q(status="cancelled")),
        assigned=Count("pk", filter=Q(assigned_rider__isnull=False)),
    )
    o = BookingOffer.objects.filter(sent_at__date=today).order_by().aggregate(
        n=Count("pk"), ok=Count("pk", filter=Q(status="accepted")),
    )
    perf = [
        {"label": "Booking completion", "pct": _pct(t["done"], t["n"]), "detail": f'{t["done"]} of {t["n"]} bookings', "color": "#1FA463"},
        {"label": "Rider matched", "pct": _pct(t["assigned"], t["n"]), "detail": f'{t["assigned"]} of {t["n"]} bookings', "color": "#1E3FCE"},
        {"label": "Offer acceptance", "pct": _pct(o["ok"], o["n"]), "detail": f'{o["ok"]} of {o["n"]} offers', "color": "#F5B82E"},
        {"label": "Cancellations", "pct": _pct(t["cancelled"], t["n"]), "detail": f'{t["cancelled"]} of {t["n"]} bookings', "color": "#D64545"},
        {"label": "Riders online", "pct": _pct(online, verified), "detail": f"{online} of {verified} verified", "color": "#14B8C4"},
    ]

    revenue = (
        Booking.objects.filter(completed_at__date=today, status="completed")
        .aggregate(s=Sum("total_fare"))["s"] or Decimal("0.00")
    )

    recent_riders = []
    for r in riders.select_related("zone").order_by("-created_at")[:10]:
        recent_riders.append({
            "name": r.full_name, "code": r.rider_code,
            "photo": r.profile_photo.url if r.profile_photo else "",
            "status": r.get_verification_status_display(), "status_key": r.verification_status,
            "online": r.is_online, "location": r.city or "—",
            "seen": r.location_updated_at or r.last_online_at,
            "url": reverse("admin:api_riderprofile_change", args=[r.pk]),
        })

    recent_bookings = []
    for b in Booking.objects.select_related("customer__user", "assigned_rider").order_by("-created_at")[:8]:
        recent_bookings.append({
            "ref": b.booking_ref, "service": b.get_service_type_display(),
            "customer": _customer_name(b.customer),
            "rider": b.assigned_rider.full_name if b.assigned_rider_id else "—",
            "status": b.get_status_display(), "status_key": b.status, "fare": b.total_fare,
            "url": reverse("admin:api_booking_change", args=[b.pk]),
        })

    context.update({"rn": {
        "today": today,
        "kpis": kpis,
        "extra": {
            "active_bookings": Booking.objects.filter(status__in=LIVE_BOOKING_STATUSES).count(),
            "revenue": revenue,
            "docs": RiderDocument.objects.filter(status="pending").count(),
        },
        "verified_pct": _pct(verified, total), "donut": donut, "legend": legend,
        "activity": {"line": a_line, "area": a_area, "new_line": n_line,
                     "peak": top, "first": days[0], "last": days[-1]},
        "bookings_chart": {"line": b_line, "area": b_area, "done_line": bd_line,
                           "peak": btop, "total14": sum(b_created), "done14": sum(b_done),
                           "rate": _pct(sum(b_done), sum(b_created))},
        "perf": perf,
        "todo": {
            "docs": RiderDocument.objects.filter(status="pending").count(),
            "licences": riders.filter(
                license_expiry_date__isnull=False,
                license_expiry_date__lte=today + timedelta(days=30),
            ).count(),
            "under_review": by_status.get(VerificationStatus.UNDER_REVIEW, 0),
        },
        "recent_riders": recent_riders, "recent_bookings": recent_bookings,
        "live_data_url": reverse("rn_live_map_data"), "live_map_url": reverse("rn_live_map"),
        "riders_url": reverse("admin:api_riderprofile_changelist"),
    }})
    return context

