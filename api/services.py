"""
RYDNOW automation engine - the "no admin needed" brain.

Customer flow  : create_booking -> payment confirmed -> dispatch_booking
Rider flow     : accept_offer / decline_offer -> arrive -> start (OTP) -> complete
Self-healing   : run_automation_cycle() (every ~15s) expires stale offers,
                 re-dispatches, replaces no-show riders, cancels unpaid bookings,
                 releases earnings, retries refunds, revokes expired documents.

Admin can switch any of this off (SystemConfig) or override by hand;
everything is recorded in AuditLog.
"""
import importlib
import logging
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.db.models import Exists, OuterRef, Q
from django.utils import timezone

from .models import (
    ACTIVE_RIDER_STATUSES, Booking, BookingError, CustomerWallet, DailySummary, DispatchOffer,
    Notification, Refund, Result, Rider, RiderDocument, RiderWallet, Service, SystemAlert,
    SystemConfig, AuditLog, haversine_km, has_coordinates,
)

logger = logging.getLogger(__name__)
MAX_REFUND_ATTEMPTS = 5


# ==========================================================
#  Plumbing: audit, notify, push, task queue
# ==========================================================
def audit(action, obj=None, actor=None, **details):
    try:
        AuditLog.objects.create(
            actor=actor if getattr(actor, 'pk', None) else None,
            actor_label=(getattr(actor, 'get_username', lambda: 'admin')() if actor else 'system'),
            action=action,
            object_type=obj.__class__.__name__ if obj is not None else '',
            object_id=str(getattr(obj, 'pk', '')) if obj is not None else '',
            details={k: str(v) for k, v in details.items()},
        )
    except Exception:
        logger.exception("audit failed: %s", action)


def _load(dotted):
    module, _, attr = dotted.rpartition('.')
    return getattr(importlib.import_module(module), attr)


def send_push(user, title, body, data):
    """Plug in FCM/OneSignal: settings.RYDNOW_PUSH_BACKEND = 'myapp.push.send' -> fn(user, title, body, data)."""
    backend = getattr(settings, 'RYDNOW_PUSH_BACKEND', None)
    if not backend:
        logger.info("PUSH -> user=%s | %s | %s", getattr(user, 'pk', user), title, body)
        return
    try:
        _load(backend)(user, title, body, data)
    except Exception:
        logger.exception("push failed")


def notify(user, title, body='', kind='info', data=None):
    if user is None:
        return None
    n = Notification.objects.create(user=user, title=title, body=body, kind=kind, data=data or {})
    transaction.on_commit(lambda: send_push(user, title, body, data or {}))
    return n


def enqueue(task_name, *args, countdown=None):
    from . import tasks
    getattr(tasks, task_name).apply_async(args=args, countdown=countdown)


# ==========================================================
#  Customer: quote & create booking
# ==========================================================
def quote_fare(service_key, pickup_lat=None, pickup_lng=None, dest_lat=None, dest_lng=None):
    service = Service.objects.filter(pk=service_key, is_active=True).first()
    if not service:
        return Result(False, 'Service unavailable')
    b = Booking(service=service, pickup_lat=pickup_lat, pickup_lng=pickup_lng,
                destination_lat=dest_lat, destination_lng=dest_lng)
    b.estimate_trip()
    fare = service.calculate_fare(b.estimated_distance_km, b.estimated_duration_min)
    return Result(True, 'Quote', {'fare': fare, 'distance_km': b.estimated_distance_km,
                                  'duration_min': b.estimated_duration_min})


def create_booking(customer, service_key, payment_method='wallet', **fields):
    """
    The ONE entry point for views. Wallet payments are charged and dispatched instantly;
    momo/card bookings wait for the gateway webhook -> booking.confirm_payment_verified().
    """
    if customer.is_blocked:
        return Result(False, 'Your account cannot create bookings')
    service = Service.objects.filter(pk=service_key, is_active=True).first()
    if not service:
        return Result(False, 'This service is currently unavailable')
    try:
        with transaction.atomic():
            booking = Booking(service=service, customer=customer, payment_method=payment_method, **fields)
            booking.full_clean(exclude=['booking_ref', 'pickup_otp'])
            booking.save()
            if payment_method == 'wallet':
                wallet, _ = CustomerWallet.objects.get_or_create(user=customer.user)
                res = wallet.debit(booking.total_fare, 'booking_payment', f"PAYB-{booking.booking_ref}",
                                   f"Booking {booking.booking_ref}", booking, use_bonus=True)
                if not res.ok:
                    raise BookingError(res.message)
                res = booking.confirm_payment_verified('wallet', f"WAL-{booking.booking_ref}")
                if not res.ok:
                    raise BookingError(res.message)
    except BookingError as e:
        return Result(False, str(e))
    audit('booking.created', booking, actor=customer.user)
    return Result(True, 'Booking created', booking)


# ==========================================================
#  Dispatch: find qualified riders and offer the job
# ==========================================================
def find_candidates(booking, radius_km, limit):
    """Qualified + verified + online + free riders near pickup, best first."""
    qs = (Rider.objects.available_for(booking.service)
          .nearby(booking.pickup_lat, booking.pickup_lng, radius_km)
          .exclude(dispatch_offers__booking=booking)
          .select_related('user'))
    scored = []
    for r in qs[:200]:
        d = haversine_km(r.current_lat, r.current_lng, booking.pickup_lat, booking.pickup_lng)
        if d is None or d > radius_km:
            continue
        # closer is better, higher rating nudges a rider up (~0.3 km per rating point above 4.5)
        scored.append((d - (r.rating_avg - 4.5) * 0.3, d, r))
    scored.sort(key=lambda t: t[0])
    return [(r, d) for _, d, r in scored[:limit]]


def dispatch_booking(booking_id):
    """Run one dispatch round. Safe to call repeatedly (idempotent per round)."""
    if not SystemConfig.get_bool('auto_dispatch_enabled', True):
        return Result(False, 'Auto-dispatch disabled by admin')
    now = timezone.now()
    offers, booking = [], None
    with transaction.atomic():
        booking = (Booking.objects.select_for_update().select_related('service', 'customer__user')
                   .filter(pk=booking_id).first())
        if not booking:
            return Result(False, 'Booking not found')
        if booking.status not in ('pending', 'searching_rider'):
            return Result(False, 'Booking not dispatchable')
        if SystemConfig.get_bool('require_payment_before_dispatch', True) and booking.payment_status != 'held':
            return Result(False, 'Awaiting payment')
        if booking.scheduled_at and booking.scheduled_at - now > timedelta(minutes=15):
            return Result(False, 'Scheduled for later')
        if DispatchOffer.objects.filter(booking=booking, status='offered', expires_at__gt=now).exists():
            return Result(True, 'Offers still open')
        service = booking.service
        if not booking.has_pickup_coordinates:
            SystemAlert.create_alert('error', 'Booking has no pickup coordinates',
                                     'Cannot auto-dispatch; assign manually.', booking.booking_ref, dedupe=True)
            return Result(False, 'Missing pickup coordinates')
        if booking.dispatch_round >= service.max_dispatch_rounds:
            return _expire_search_locked(booking)

        radius = service.dispatch_radius_km * (1 + 0.5 * booking.dispatch_round)
        candidates = find_candidates(booking, radius, service.offers_per_round)
        booking.dispatch_round += 1
        booking.last_dispatch_at = now
        booking.status = 'searching_rider'
        booking.save(update_fields=['dispatch_round', 'last_dispatch_at', 'status', 'updated_at'])
        expires = now + timedelta(seconds=service.offer_timeout_seconds)
        offers = DispatchOffer.objects.bulk_create([
            DispatchOffer(booking=booking, rider=r, round_number=booking.dispatch_round,
                          distance_km=d, expires_at=expires)
            for r, d in candidates
        ])
        riders = [(r, d) for r, d in candidates]

    for rider, dist in riders:
        notify(rider.user, 'New job nearby',
               f"{service.name} - {dist:.1f} km away - GHS {booking.rider_payout} payout",
               kind='job_offer', data={'booking': str(booking.pk), 'service': service.key,
                                       'expires_at': expires.isoformat()})
    enqueue('check_offers_task', countdown=service.offer_timeout_seconds + 2)
    return Result(True, f'Offered to {len(riders)} rider(s) in round {booking.dispatch_round}', booking)


def _expire_search_locked(booking):
    """No rider found after all rounds: close the booking and refund automatically."""
    booking.status = 'expired'
    booking.save(update_fields=['status', 'updated_at'])
    booking.request_refund(reason='No rider available')
    SystemAlert.create_alert('warning', 'No rider found',
                             f'{booking.booking_ref} ({booking.service_id}) expired after '
                             f'{booking.dispatch_round} rounds. Customer auto-refunded.', booking.booking_ref)
    notify(booking.customer_user, 'No rider available',
           'Sorry - no rider was available. You have been refunded automatically.', kind='booking_expired',
           data={'booking': str(booking.pk)})
    audit('booking.expired', booking, rounds=booking.dispatch_round)
    return Result(False, 'No rider found - booking expired and refunded', booking)


# ==========================================================
#  Rider: accept / decline
# ==========================================================
def accept_offer(offer_id, rider):
    """First qualified rider to accept wins. Lock order: booking -> offer -> rider (no deadlocks)."""
    with transaction.atomic():
        offer = DispatchOffer.objects.filter(pk=offer_id, rider=rider).first()
        if not offer:
            return Result(False, 'Offer not found')
        booking = Booking.objects.select_for_update().get(pk=offer.booking_id)
        offer = DispatchOffer.objects.select_for_update().get(pk=offer.pk)
        if offer.status != 'offered':
            return Result(False, 'Sorry, this job is no longer available')
        if offer.is_expired:
            offer.status, offer.responded_at = 'expired', timezone.now()
            offer.save(update_fields=['status', 'responded_at'])
            return Result(False, 'This offer has expired')
        res = booking._assign_locked(rider)
        if not res.ok:
            return res
    audit('booking.accepted', booking, actor=rider.user, rider=rider.pk)
    booking.refresh_from_db()
    return Result(True, 'Job accepted! Head to the pickup point.', booking)


def decline_offer(offer_id, rider):
    now = timezone.now()
    updated = DispatchOffer.objects.filter(pk=offer_id, rider=rider, status='offered').update(
        status='declined', responded_at=now)
    if not updated:
        return Result(False, 'Offer not found or already answered')
    offer = DispatchOffer.objects.get(pk=offer_id)
    still_open = DispatchOffer.objects.filter(booking_id=offer.booking_id, status='offered',
                                              expires_at__gt=now).exists()
    if not still_open:
        enqueue('dispatch_booking_task', str(offer.booking_id))
    return Result(True, 'Offer declined')


# ==========================================================
#  Documents / verification
# ==========================================================
def auto_review_document(doc):
    """
    Pluggable automatic document check. Set settings.RYDNOW_DOCUMENT_VERIFIER = 'myapp.kyc.verify'
    (fn(doc) -> (ok, reason)) to call a real KYC provider (Ghana Card / DVLA).
    The built-in fallback only checks file type & size - it is NOT identity verification,
    so `auto_approve_documents` is OFF by default.
    """
    verifier = getattr(settings, 'RYDNOW_DOCUMENT_VERIFIER', None)
    if verifier:
        return _load(verifier)(doc)
    name = (doc.document_file.name or '').lower()
    if not name.endswith(('.jpg', '.jpeg', '.png', '.pdf')):
        return False, 'Unsupported file type'
    try:
        if doc.document_file.size < 10 * 1024:
            return False, 'File too small to be a valid document'
    except Exception:
        return False, 'File unreadable'
    return True, 'Basic checks passed'


def on_document_saved(doc_id):
    doc = RiderDocument.objects.select_related('rider').filter(pk=doc_id).first()
    if not doc:
        return
    if doc.status == 'pending' and doc.is_current and SystemConfig.get_bool('auto_approve_documents', False):
        ok, reason = auto_review_document(doc)
        if ok:
            doc.status, doc.verified_at = 'approved', timezone.now()
            doc.save(update_fields=['status', 'verified_at'])
            audit('document.auto_approved', doc, reason=reason)
    doc.rider.refresh_verification()


def expire_documents():
    now = timezone.now()
    n = 0
    for doc in RiderDocument.objects.filter(is_current=True, status='approved', expires_at__lte=now).select_related('rider'):
        doc.status = 'resubmit_required'
        doc.rejection_reason = 'Document expired'
        doc.save(update_fields=['status', 'rejection_reason'])
        doc.rider.refresh_verification()
        notify(doc.rider.user, 'Document expired',
               f'Please upload a new {doc.get_document_type_display()} to keep receiving jobs.', kind='document')
        n += 1
    return n


# ==========================================================
#  Refunds
# ==========================================================
class PaymentGateway:
    """Subclass and point settings.RYDNOW_PAYMENT_GATEWAY at it (Paystack/Hubtel/Flutterwave...)."""

    def refund(self, amount, original_ref, reference):
        """Return (ok: bool, provider_ref: str, error: str)."""
        raise NotImplementedError


def get_gateway():
    path = getattr(settings, 'RYDNOW_PAYMENT_GATEWAY', None)
    return _load(path)() if path else None


def process_refund(refund_id):
    with transaction.atomic():
        refund = Refund.objects.select_for_update().select_related('booking__customer__user').get(pk=refund_id)
        if refund.status != 'pending':
            return Result(True, 'Already handled')
        refund.attempts += 1
        refund.last_attempt_at = timezone.now()
        refund.save(update_fields=['attempts', 'last_attempt_at'])
        booking = refund.booking

    ok, provider_ref, error = False, '', ''
    if booking.payment_method == 'wallet':
        if not booking.customer_id:
            error = 'Booking has no customer wallet'
        else:
            wallet, _ = CustomerWallet.objects.get_or_create(user=booking.customer.user)
            res = wallet.credit(refund.amount, 'refund', f"REFW-{refund.reference}",
                                f"Refund {booking.booking_ref}", booking)
            ok, error = res.ok, '' if res.ok else res.message
    else:
        gateway = get_gateway()
        if not gateway:
            error = 'No payment gateway configured - refund needs manual processing'
        else:
            try:  # network call OUTSIDE any DB transaction
                ok, provider_ref, error = gateway.refund(refund.amount, booking.payment_ref, refund.reference)
            except Exception as e:
                ok, error = False, str(e)

    with transaction.atomic():
        refund = Refund.objects.select_for_update().get(pk=refund_id)
        b = Booking.objects.select_for_update().get(pk=refund.booking_id)
        if ok:
            refund.status, refund.completed_at, refund.provider_ref = 'completed', timezone.now(), provider_ref
            refund.save(update_fields=['status', 'completed_at', 'provider_ref'])
            full = b.refunded_total() >= b.total_fare
            b.payment_status = 'refunded' if full else 'released'
            b.save(update_fields=['payment_status', 'updated_at'])
        else:
            refund.last_error = error
            if refund.attempts >= MAX_REFUND_ATTEMPTS:
                refund.status = 'failed'
                SystemAlert.create_alert('critical', 'Refund failed - action needed',
                                         f'{refund.reference} GHS {refund.amount}: {error}', b.booking_ref)
            refund.save(update_fields=['last_error', 'status'])
    if ok:
        notify(booking.customer_user, 'Refund completed', f'GHS {refund.amount} refunded.', kind='refund',
               data={'booking': str(booking.pk)})
        audit('refund.completed', refund)
    return Result(ok, 'Refund completed' if ok else f'Refund attempt failed: {error}')


def retry_pending_refunds():
    cutoff = timezone.now() - timedelta(minutes=2)
    ids = list(Refund.objects.filter(status='pending', attempts__lt=MAX_REFUND_ATTEMPTS)
               .filter(Q(last_attempt_at__isnull=True) | Q(last_attempt_at__lte=cutoff))
               .values_list('pk', flat=True))
    for rid in ids:
        process_refund(rid)
    return len(ids)


# ==========================================================
#  Self-healing sweeps (run by run_automation_cycle)
# ==========================================================
def expire_stale_offers():
    now = timezone.now()
    stale = DispatchOffer.objects.filter(status='offered', expires_at__lte=now)
    booking_ids = list(stale.values_list('booking_id', flat=True).distinct())
    stale.update(status='expired', responded_at=now)
    for bid in booking_ids:
        dispatch_booking(bid)
    return len(booking_ids)


def redispatch_idle_searches():
    """Bookings waiting with no live offers (e.g. no riders were online) get the next round."""
    now = timezone.now()
    live = DispatchOffer.objects.filter(booking=OuterRef('pk'), status='offered', expires_at__gt=now)
    qs = (Booking.objects.filter(status__in=['pending', 'searching_rider']).exclude(Exists(live))
          .select_related('service'))
    n = 0
    for b in qs[:200]:
        wait = timedelta(seconds=b.service.offer_timeout_seconds)
        if b.last_dispatch_at is None or now - b.last_dispatch_at >= wait:
            if dispatch_booking(b.pk).ok:
                n += 1
    return n


def reassign_late_riders():
    """Rider accepted but never arrived within the deadline -> replace them (with a strike)."""
    now = timezone.now()
    late = Booking.objects.filter(status='assigned', arrival_deadline__lt=now, arrived_at__isnull=True)
    n = 0
    for b in late[:100]:
        rider = b.assigned_rider
        res = b.unassign_rider('Did not arrive in time', penalize=True)
        if res.ok:
            notify(rider.user, 'Job reassigned', 'You did not arrive in time and received a strike.', kind='strike')
            notify(b.customer_user, 'Finding you another rider',
                   'Your rider was delayed - we are finding someone closer.', kind='booking_update')
            audit('booking.rider_replaced', b, rider=rider.pk)
            dispatch_booking(b.pk)
            n += 1
    return n


def cancel_unpaid_bookings():
    minutes = SystemConfig.get_int('unpaid_booking_timeout_minutes', 15)
    cutoff = timezone.now() - timedelta(minutes=minutes)
    n = 0
    for b in Booking.objects.filter(status='pending', created_at__lt=cutoff,
                                    payment_status__in=['initiated', 'pending_confirmation', 'failed']):
        if b.cancel_booking(by_role='system', reason='Payment not completed in time').ok:
            n += 1
    return n


def flag_stuck_bookings():
    hours = SystemConfig.get_int('stuck_ride_alert_hours', 6)
    cutoff = timezone.now() - timedelta(hours=hours)
    n = 0
    for b in Booking.objects.filter(status='in_progress', started_at__lt=cutoff):
        if SystemAlert.create_alert('warning', 'Ride in progress for too long',
                                    f'{b.booking_ref} started {b.started_at:%Y-%m-%d %H:%M}.',
                                    b.booking_ref, dedupe=True):
            n += 1
    return n


def release_due_earnings():
    now = timezone.now()
    n = 0
    due = Booking.objects.filter(status='completed', earnings_released_at__isnull=True,
                                 earnings_release_at__lte=now, assigned_rider__isnull=False)
    for b in due[:200]:
        wallet = RiderWallet.objects.filter(rider_id=b.assigned_rider_id).first()
        if wallet and wallet.release_earnings(b).ok:
            n += 1
    return n


def run_automation_cycle():
    """The heartbeat. Call every ~15 seconds (Celery beat / cron)."""
    report = {}
    for name, fn in [
        ('offers_expired', expire_stale_offers),
        ('redispatched', redispatch_idle_searches),
        ('riders_replaced', reassign_late_riders),
        ('unpaid_cancelled', cancel_unpaid_bookings),
        ('earnings_released', release_due_earnings),
        ('refunds_retried', retry_pending_refunds),
        ('stuck_flagged', flag_stuck_bookings),
    ]:
        try:
            report[name] = fn()
        except Exception:
            logger.exception("automation step failed: %s", name)
            report[name] = 'error'
    return report


def generate_daily_summary(target_date=None):
    return DailySummary.generate_for_date(target_date)

