"""
api/dashboard.py - builds the `rn` context used by templates/admin/index.html.

Wire it in settings.py (django-unfold):
    UNFOLD = {..., "DASHBOARD_CALLBACK": "api.dashboard.dashboard_callback"}

Every section is isolated by _safe(): one failing query shows an empty card and logs the
error, it never takes the admin home page down. All fields used here exist in api/models.py.
"""
import logging
from datetime import timedelta

from django.db.models import Count, Sum
from django.db.models.functions import TruncDate
from django.urls import NoReverseMatch, reverse
from django.utils import timezone

from .models import (
    ZERO, Booking, DispatchOffer, Refund, RiderComplaint, RiderDocument, RiderOnlineSession, RiderProfile,
    SystemAlert, VerificationStatus, WasteCollection, WasteSubscription, WebhookEvent,
    WithdrawalRequest,
)

log = logging.getLogger(__RYDNOW__)
LIVE_MAP_URL_NAME = "admin:rydnow_live_map"   # change to your live-map route name
S = Booking.Status
COLORS = {"completed": "#22C55E", "in_progress": "#3B5BFF", "accepted": "#F5B82E",
          "pending": "#F06A0A", "cancelled": "#EF4444", "verified": "#22C55E",
          "under_review": "#F5B82E", "pending_docs": "#8EA0CC", "rejected": "#EF4444",
          "suspended": "#8B5CF6"}


def _url(name, *args):
    try:
        return reverse(name, args=args)
    except NoReverseMatch:
        return "#"


def _safe(fn, default, *a):
    try:
        return fn(*a)
    except Exception:
        log.exception("Dashboard section failed: %s", getattr(fn, "__name__", fn))
        return default


def _pct(a, b):
    return round(a * 100 / b) if b else 0


def _money(v):
    return f"{(v or ZERO):,.2f}"


def _delta(now, prev):
    if not prev:
        return ("new today", "up") if now else ("no change", "flat")
    d = round((now - prev) * 100 / prev, 1)
    return f"{d:+}% vs yesterday", "up" if d > 0 else "down" if d < 0 else "flat"


def _daily(qs, field, agg=None):
    start = timezone.localdate() - timedelta(days=13)
    rows = (qs.filter(**{f"{field}__date__gte": start}).annotate(d=TruncDate(field))
            .order_by().values("d").annotate(n=agg or Count("pk")))
    m = {r["d"]: r["n"] for r in rows}
    return [m.get(start + timedelta(days=i), 0) for i in range(14)]


def _paths(series, peak, w=640, top=8, bot=142):
    pts = [(round(i * w / (len(series) - 1), 1), round(bot - v / peak * (bot - top), 1))
           for i, v in enumerate(series)]
    line = "M" + " L".join(f"{x},{y}" for x, y in pts)
    return line, f"{line} L{w},{bot} L0,{bot} Z"


def _donut(rows):
    """rows: [(label, count, colour)] -> conic-gradient css + legend."""
    total = sum(n for __, n, __ in rows)
    css, at, legend = [], 0.0, []
    for label, n, colour in rows:
        share = n * 100 / total if total else 0
        css.append(f"{colour} {at:.2f}% {at + share:.2f}%")
        at += share
        legend.append({"label": label, "n": n, "pct": round(share, 1), "color": colour})
    return {"css": f"conic-gradient({', '.join(css)})" if total else "#1B2A55",
            "legend": legend, "total": total}


def _kpis(today):
    y = today - timedelta(days=1)
    B = Booking.objects
    done = lambda d: B.filter(status=S.COMPLETED, completed_at__date=d)
    val = lambda d: done(d).aggregate(s=Sum("total_price"))["s"] or ZERO
    riders = RiderProfile.objects
    online = riders.active().verified().filter(is_online=True).count()
    verified = riders.not_deleted().verified().count()
    waiting = B.filter(status=S.PENDING).count()
    out = []
    for label, now, prev, shown, icon, tone, note in (
        ("Bookings today", B.filter(created_at__date=today).count(), B.filter(created_at__date=y).count(),
         None, "shopping_bag", "blue", f"{waiting} waiting for a rider"),
        ("Completed today", done(today).count(), done(y).count(), None, "check_circle", "green", "finished trips"),
        ("Booking value", val(today), val(y), "GH₵ " + _money(val(today)), "payments", "pink", "completed bookings today"),
    ):
        pill, pt = _delta(now, prev)
        out.append({"label": label, "value": shown or f"{now:,}", "pill": pill, "pill_tone": pt,
                    "note": note, "icon": icon, "tone": tone})
    active = B.filter(status__in=[S.ACCEPTED, S.IN_PROGRESS]).count()
    out.insert(2, {"label": "In progress", "value": f"{active:,}", "pill": "live", "pill_tone": "up",
                   "note": "accepted or on the road", "icon": "two_wheeler", "tone": "orange"})
    out.insert(3, {"label": "Riders online", "value": f"{online:,}", "pill": f"{_pct(online, verified)}% of verified",
                   "pill_tone": "up" if online else "flat", "note": f"{verified:,} verified riders",
                   "icon": "sensors", "tone": "purple"})
    return out


def _minis(today):
    wd = WithdrawalRequest.objects.filter(status__in=["pending", "processing"])
    wd_sum = wd.aggregate(s=Sum("amount"))["s"] or ZERO
    docs = RiderDocument.objects.filter(status="pending").count()
    review = RiderProfile.objects.not_deleted().filter(verification_status=VerificationStatus.UNDER_REVIEW).count()
    lic = RiderProfile.objects.active().filter(
        license_expiry_date__range=(today, today + timedelta(days=30))).count()
    cmp_open = RiderComplaint.objects.exclude(status__in=RiderComplaint.CLOSED_STATUSES).count()
    return [
        {"label": "Documents to review", "value": docs, "icon": "fact_check", "tone": "orange",
         "url": _url("admin:api_riderdocument_changelist") + "?status__exact=pending"},
        {"label": "Riders under review", "value": review, "icon": "hourglass_top", "tone": "teal",
         "url": _url("admin:api_riderprofile_changelist") + "?verification_status__exact=under_review"},
        {"label": "Licences expiring in 30 days", "value": lic, "icon": "badge", "tone": "slate",
         "url": _url("admin:api_riderprofile_changelist")},
        {"label": "Open complaints", "value": cmp_open, "icon": "report", "tone": "red",
         "url": _url("admin:api_ridercomplaint_changelist")},
        {"label": f"Withdrawals pending · GH₵ {_money(wd_sum)}", "value": wd.count(), "icon": "account_balance_wallet",
         "tone": "gold", "url": _url("admin:api_withdrawalrequest_changelist") + "?status__exact=pending"},
    ]


def _status_donut(today):
    since = today - timedelta(days=30)
    c = dict(Booking.objects.filter(created_at__date__gte=since).order_by()
             .values_list("status").annotate(n=Count("pk")))
    return _donut([(label, c.get(v, 0), COLORS[v]) for v, label in S.choices])


def _verification_donut():
    c = dict(RiderProfile.objects.not_deleted().order_by().values_list("verification_status")
             .annotate(n=Count("pk")))
    rows = [(label, c.get(v, 0), COLORS["pending_docs" if v == "pending" else v])
            for v, label in VerificationStatus.choices]
    d = _donut(rows)
    d["pct"] = _pct(c.get("verified", 0), d["total"])
    return d


def _charts(today):
    days = [today - timedelta(days=13 - i) for i in range(14)]
    B = Booking.objects
    made, done = _daily(B, "created_at"), _daily(B.filter(status=S.COMPLETED), "completed_at")
    active = _daily(RiderOnlineSession.objects, "started_at", Count("rider", distinct=True))
    joined = _daily(RiderProfile.objects.not_deleted(), "created_at")
    p1, p2 = max(made + done + [1]), max(active + joined + [1])
    bl, ba = _paths(made, p1)
    dl, __ = _paths(done, p1)
    al, aa = _paths(active, p2)
    nl, __ = _paths(joined, p2)
    return (
        {"line": bl, "area": ba, "done_line": dl, "total14": sum(made), "done14": sum(done),
         "rate": _pct(sum(done), sum(made)), "peak": p1},
        {"line": al, "area": aa, "new_line": nl, "first": days[0], "last": days[-1], "peak": p2},
    )


def _perf(today):
    since = today - timedelta(days=30)
    B = Booking.objects.filter(created_at__date__gte=since)
    ok, bad = B.filter(status=S.COMPLETED).count(), B.filter(status=S.CANCELLED).count()
    offers = DispatchOffer.objects.filter(offered_at__date=today)
    taken = offers.filter(status="accepted").count()
    riders = RiderProfile.objects.not_deleted()
    verified, total = riders.verified().count(), riders.count()
    online = riders.active().verified().filter(is_online=True).count()
    return [
        {"label": "Trip completion (30 days)", "pct": _pct(ok, ok + bad), "color": "#22C55E",
         "detail": f"{ok:,} completed · {bad:,} cancelled"},
        {"label": "Dispatch offers accepted today", "pct": _pct(taken, offers.count()), "color": "#3B5BFF",
         "detail": f"{taken:,} of {offers.count():,} offers"},
        {"label": "Verified riders online", "pct": _pct(online, verified), "color": "#F5B82E",
         "detail": f"{online:,} of {verified:,} verified"},
        {"label": "Riders verified", "pct": _pct(verified, total), "color": "#8B5CF6",
         "detail": f"{verified:,} of {total:,} sign-ups"},
    ]


def _top_riders():
    out = []
    qs = RiderProfile.objects.active().verified().filter(completed_trips__gt=0)
    for r in qs.order_by("-completed_trips", "-rating_average")[:5]:
        try:
            photo = r.profile_photo.url if r.profile_photo else ""
        except Exception:
            photo = ""
        out.append({"name": r.full_name, "rating": r.rating_average, "trips": r.completed_trips,
                    "tier": r.get_tier_display(), "photo": photo, "url": _url("admin:api_riderprofile_change", r.pk)})
    return out


def _feed():
    qs = Booking.objects.select_related("customer", "rider").order_by("-updated_at")[:6]
    tone = {"completed": "green", "cancelled": "red", "pending": "orange"}
    out = []
    for b in qs:
        who = (b.rider.get_full_name() or b.rider.get_username()) if b.rider_id else "no rider yet"
        out.append({"title": f"Booking {b.reference}", "sub": f"{b.get_status_display()} · {who}",
                    "at": b.updated_at, "tone": tone.get(b.status, "blue"), "url": _url("admin:api_booking_change", b.pk)})
    return out


def _health():
    failed = WebhookEvent.objects.filter(status="failed").count()
    offers = DispatchOffer.objects.filter(status="pending").count()
    due = WasteSubscription.objects.due().count()
    norider = WasteCollection.objects.needs_rider().count()
    alerts = SystemAlert.objects.live().count()
    refunds = Refund.objects.awaiting_review().count()
    row = lambda t, s, bad, icon: {"title": t, "sub": s, "state": "Check" if bad else "OK",
                                   "tone": "warn" if bad else "ok", "icon": icon}
    return [
        row("Payment webhooks", f"{failed} failed, retrying" if failed else "All delivered", failed, "webhook"),
        row("Dispatch", f"{offers} offers waiting for a reply", False, "alt_route"),
        row("Waste collections", f"{due} subscriptions due · {norider} without a rider", norider, "recycling"),
        row("Refunds", f"{refunds} awaiting review" if refunds else "Nothing waiting", refunds, "undo"),
        row("System alerts", f"{alerts} live", False, "campaign"),
    ]


def _recent_riders():
    out = []
    for r in RiderProfile.objects.not_deleted().select_related("zone").order_by("-created_at")[:8]:
        try:
            photo = r.profile_photo.url if r.profile_photo else ""
        except Exception:
            photo = ""
        out.append({"name": r.full_name, "code": r.rider_code, "photo": photo, "online": r.is_online,
                    "status": r.get_verification_status_display(), "status_key": r.verification_status,
                    "location": (r.zone.name if r.zone_id else r.city) or "—", "seen": r.last_online_at,
                    "url": _url("admin:api_riderprofile_change", r.pk)})
    return out


def _recent_bookings():
    out = []
    for b in Booking.objects.select_related("customer", "rider").order_by("-created_at")[:8]:
        name = lambda u: (u.get_full_name() or u.get_username()) if u else "—"
        out.append({"ref": b.reference, "service": b.get_service_type_display(), "customer": name(b.customer),
                    "rider": name(b.rider), "status": b.get_status_display(), "status_key": b.status,
                    "fare": _money(b.total_price), "url": _url("admin:api_booking_change", b.pk)})
    return out


def build_dashboard(request=None):
    today = timezone.localdate()
    hour = timezone.localtime().hour
    user = getattr(request, "user", None)
    bc, ac = _safe(_charts, ({}, {}), today)
    return {
        "today": today,
        "greeting": "Good morning" if hour < 12 else "Good afternoon" if hour < 18 else "Good evening",
        "name": (user.first_name or user.get_username()) if user else "",
        "live_map_url": _url(LIVE_MAP_URL_NAME),
        "riders_url": _url("admin:api_riderprofile_changelist"),
        "kpis": _safe(_kpis, [], today),
        "minis": _safe(_minis, [], today),
        "status_donut": _safe(_status_donut, {"legend": [], "total": 0, "css": "#1B2A55"}, today),
        "ver_donut": _safe(_verification_donut, {"legend": [], "total": 0, "css": "#1B2A55", "pct": 0}),
        "bookings_chart": bc, "activity": ac,
        "perf": _safe(_perf, [], today),
        "top_riders": _safe(_top_riders, []),
        "feed": _safe(_feed, []),
        "health": _safe(_health, []),
        "recent_riders": _safe(_recent_riders, []),
        "recent_bookings": _safe(_recent_bookings, []),
        "actions": [
            {"label": "Review documents", "icon": "fact_check", "tone": "blue",
             "url": _url("admin:api_riderdocument_changelist") + "?status__exact=pending"},
            {"label": "Add promo code", "icon": "sell", "tone": "green", "url": _url("admin:api_promocode_add")},
            {"label": "Withdrawals", "icon": "account_balance_wallet", "tone": "orange",
             "url": _url("admin:api_withdrawalrequest_changelist")},
            {"label": "New business", "icon": "storefront", "tone": "purple",
             "url": _url("admin:api_businessaccount_add")},
        ],
    }


def dashboard_callback(request, context):
    context["rn"] = build_dashboard(request)
    return context

