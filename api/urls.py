"""
URL configuration for the `api` app.

Crash-proofing
--------------
Routes backed by the NEW code (booking.py) and by `views.py` auth/wallet/location views are
referenced directly: if they are missing it should fail loudly.

Routes backed by the OLD booking/payment function views (bookings_collection, accept_booking,
paystack_webhook, ...) are looked up by name with `_find()`, first in booking_views.py, then in
views.py:
  * found                  -> used as before.
  * missing, customer      -> served by the new BookingViewSet instead.
  * missing, anything else -> a stub that answers 501 and is listed in the startup log
    (search for "urls: missing view"), so one missing function can never stop the server booting.

Ordering rules
--------------
- Admin live-map routes come BEFORE `admin/`.
- Literal booking paths (create/, webhook/, calculate-price/, admin/revenue/) come BEFORE
  booking/<str:booking_ref>/, or "webhook" is read as a reference.
- `bookings/quote/` comes BEFORE `bookings/<ref>/`.

Audit changes
-------------
- rider/offers/<offer_id>/accept/ used <int:offer_id> but dispatch offers use UUID ids
  (str(offer.id) is what the app receives), so every accept call was a 404. It is now <str:...>.
- Added rider/offers/<offer_id>/decline/ (the dispatch engine had decline logic but no route).
- NOTE: `admin/` is inside this app's urls. If this file is included under "api/", the admin lives at
  /api/admin/. Keep it only if that is what you want; otherwise mount admin in the project urls.py.
"""
import logging

from django.contrib import admin
from django.urls import path
from rest_framework import status
from rest_framework.authtoken.views import obtain_auth_token
from rest_framework.decorators import api_view
from rest_framework.response import Response

from api import admin_dashboard

from . import booking
from . import booking_views as v
from . import views

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
_MISSING = []


def _find(name):
    """Return the old view called `name` from booking_views, then views; else None."""
    for module in (v, views):
        found = getattr(module, name, None)
        if found is not None:
            return found
    return None


def _not_implemented(name):
    @api_view(["GET", "POST", "PUT", "PATCH", "DELETE"])
    def stub(request, *args, **kwargs):
        return Response(
            {"detail": f"'{name}' is not available on this server."},
            status=status.HTTP_501_NOT_IMPLEMENTED,
        )

    stub.__name__ = f"missing_{name}"
    return stub


def _legacy(name):
    """Old function view, or a 501 stub (logged) if it does not exist."""
    found = _find(name)
    if found is not None:
        return found
    _MISSING.append(name)
    return _not_implemented(name)


# ---------------------------------------------------------------------------
# New booking ViewSet bindings
# ---------------------------------------------------------------------------
BookingViewSet = booking.BookingViewSet

booking_create = BookingViewSet.as_view({"post": "create"})
booking_detail = BookingViewSet.as_view({"get": "retrieve"})
booking_status = BookingViewSet.as_view({"patch": "update_status"})
booking_cancel = BookingViewSet.as_view({"post": "cancel"})
booking_stop = BookingViewSet.as_view({"post": "stop_event"})
booking_collection = BookingViewSet.as_view({"get": "list", "post": "create"})


# ---------------------------------------------------------------------------
# Customer "bookings/" routes: old views if they exist, new ViewSet otherwise
# ---------------------------------------------------------------------------
def _customer_booking_routes():
    routes = []

    collection = _find("bookings_collection")
    routes.append(path("bookings/", collection or booking_collection, name="bookings"))

    cancel = _find("booking_cancel")
    if cancel is not None:
        routes.append(path("bookings/<str:ref>/cancel/", cancel, name="booking-cancel"))
    else:
        routes.append(path("bookings/<str:booking_ref>/cancel/", booking_cancel, name="booking-cancel"))

    detail = _find("booking_detail")
    if detail is not None:
        routes.append(path("bookings/<str:ref>/", detail, name="booking-detail"))
    else:
        routes.append(path("bookings/<str:booking_ref>/", booking_detail, name="booking-detail"))

    for name, found in (("bookings_collection", collection), ("booking_cancel", cancel),
                        ("booking_detail", detail)):
        if found is None:
            logger.warning("urls: old view '%s' not found; using the new BookingViewSet", name)
    return routes


# ---------------------------------------------------------------------------
# URL patterns
# ---------------------------------------------------------------------------
urlpatterns = [
    # ─────────────────────────────────────
    # Admin (custom pages FIRST, then the admin site)
    # ─────────────────────────────────────
    path(
        "admin/live-map/",
        admin.site.admin_view(admin_dashboard.live_map_page),
        name="rn_live_map",
    ),
    path(
        "admin/live-map/data/",
        admin.site.admin_view(admin_dashboard.live_map_data),
        name="rn_live_map_data",
    ),
    path("admin/", admin.site.urls),

    # ─────────────────────────────────────
    # Pricing (signed quote)
    # ─────────────────────────────────────
    path("booking/calculate-price/", booking.QuoteView.as_view(), name="calculate-price"),

    # ─────────────────────────────────────
    # Booking & dispatch (literal paths first)
    # ─────────────────────────────────────
    path("booking/create/", booking_create, name="api-create-booking"),
    path("booking/webhook/", booking.BookingWebhookView.as_view(), name="booking-webhook"),
    path("booking/admin/revenue/", booking.RevenueReportView.as_view(), name="revenue-report"),
    path("dispatch/rider/nearby/", booking.NearbyRidersView.as_view(), name="nearby-riders"),
    path("booking/<str:booking_ref>/", booking_detail, name="api-booking-detail"),
    path("booking/<str:booking_ref>/status/", booking_status, name="update-booking-status"),
    path("booking/<str:booking_ref>/cancel/", booking_cancel, name="api-cancel-booking"),
    path("booking/<str:booking_ref>/stops/<int:sequence>/", booking_stop, name="booking-stop-event"),

    # ─────────────────────────────────────
    # Auth & login
    # ─────────────────────────────────────
    path("auth/login/", views.user_login, name="api-login"),
    path("auth/token/", obtain_auth_token, name="api-token-auth"),

    # ─────────────────────────────────────
    # Registration & phone verification
    # ─────────────────────────────────────
    path("auth/register/customer/", views.customer_register, name="customer-register"),
    path("auth/register/rider/", views.rider_register, name="rider-register"),
    path("auth/verify/send-code/", views.send_verification_code, name="send-verification-code"),
    path("auth/verify/", views.verify_code, name="verify-code"),

    # ─────────────────────────────────────
    # Rider onboarding & verification
    # ─────────────────────────────────────
    path("auth/rider/verify/", views.rider_submit_documents, name="rider-verify"),
    path("auth/rider/verification-status/", views.rider_verification_status, name="rider-verification-status"),
    path("auth/me/", views.my_profile, name="my-profile"),
    path("profile/", views.customer_profile, name="customer-profile"),

    # ─────────────────────────────────────
    # Jobs / offers (dispatch engine; offer ids are UUIDs, so <str:>)
    # ─────────────────────────────────────
    path("rider/offers/<str:offer_id>/accept/", views.accept_offer, name="accept-offer"),
    path("rider/offers/<str:offer_id>/decline/", views.decline_offer, name="decline-offer"),

    # ─────────────────────────────────────
    # Rider wallet & transactions
    # ─────────────────────────────────────
    path("rider/wallet/", views.get_rider_wallet, name="rider-wallet"),
    path("rider/transactions/", views.get_transaction_history, name="transaction-history"),
    path("rider/transactions/initiate/", views.deposit_initiate, name="deposit-initiate"),
    path("rider/deposit/", views.wallet_deposit, name="wallet-deposit"),
    path("rider/deposits/", views.deposit_history, name="deposit-history"),
    path("rider/deposits/<str:reference>/verify/", views.deposit_verify, name="deposit-verify"),
    path("rider/withdraw/", views.request_withdrawal, name="request-withdrawal"),

    # ─────────────────────────────────────
    # Customer wallet
    # (no trailing slash kept to avoid breaking existing clients)
    # ─────────────────────────────────────
    path("customer/wallet", views.customer_wallet, name="customer-wallet"),

    # ─────────────────────────────────────
    # Location & nearby riders
    # ─────────────────────────────────────
    path("rider/update-location/", views.update_rider_location, name="rider-location"),
    path("riders/nearby/", views.get_nearby_riders, name="get-nearby-riders"),

    # ─────────────────────────────────────
    # Customer bookings (legacy paths kept for existing app versions)
    # ─────────────────────────────────────
    path("bookings/quote/", booking.LegacyQuoteView.as_view(), name="booking-quote"),
    *_customer_booking_routes(),
    path("promos/validate/", _legacy("promo_validate"), name="promo-validate"),

    # ─────────────────────────────────────
    # Rider dashboard (list-bookings/ is the path it already calls)
    # ─────────────────────────────────────
    path("list-bookings/", _legacy("rider_bookings"), name="list-bookings"),
    path("rider/bookings/", _legacy("rider_bookings"), name="rider-bookings"),
    path("rider/presence/", _legacy("rider_presence"), name="rider-presence"),
    path("accept-booking/", _legacy("accept_booking"), name="accept-booking"),
    path("decline-booking/", _legacy("decline_booking"), name="decline-booking"),
    path("arrive-ride/", _legacy("arrive_ride"), name="arrive-ride"),
    path("start-ride/", _legacy("start_ride"), name="start-ride"),
    path("complete-ride/", _legacy("complete_ride"), name="complete-ride"),
    path("release-booking/", _legacy("release_booking"), name="release-booking"),

    # ─────────────────────────────────────
    # Payments - Paystack
    # ─────────────────────────────────────
    path("payments/paystack/webhook/", _legacy("paystack_webhook"), name="paystack-webhook"),
    path("payments/initialize/", _legacy("initialize_payment"), name="paystack-initialize"),
    path("payments/verify/", _legacy("verify_payment"), name="paystack-verify"),
]


if _MISSING:
    logger.error("urls: missing view(s) served as 501 stubs: %s", ", ".join(sorted(set(_MISSING))))

