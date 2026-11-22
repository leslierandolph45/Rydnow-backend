"""
URL configuration for the `api` app (api/urls.py).

Mount it ONCE in the project urls.py, under the "api/" prefix:

    # project/urls.py
    from django.contrib import admin
    from django.urls import include, path

    urlpatterns = [
        path("admin/", admin.site.urls),
        path("api/", include("api.urls")),
    ]

Every path below is relative to /api/. The frontend calls them through its proxy:
    /api/rydnow/<path>  ->  https://<backend>/api/<path>

Booking, pricing, webhook, revenue and nearby-rider routes live in booking_urls.py and are
pulled in ONCE with include(). They are NOT repeated here:

    booking/calculate-price/   quote/   bookings/quote/   booking/create/   booking/
    booking/webhook/           booking/admin/revenue/
    booking/<ref>/  .../status/  .../cancel/  .../release/
    dispatch/rider/nearby/

Quick checks after a deploy
---------------------------
- GET  /api/health/                     -> 200 {"status": "ok"}. A 404 here means this file is not
                                           mounted under "api/" (project urls.py) or the deploy is old.
- POST /api/booking/calculate-price/    -> 400 with field errors for an empty body (NOT 404).
- python manage.py shell -c "from django.urls import reverse; print(reverse('calculate-price'))"
                                        -> /api/booking/calculate-price/

Ordering rules
--------------
- Admin live-map routes come BEFORE any `admin/` route that the project mounts.
- include("api.booking_urls") comes BEFORE the legacy `bookings/<ref>/` routes, so
  `bookings/quote/` is never read as a booking reference.
- Do not include booking_urls under any other prefix, and do not include it again in the project
  urls.py: that would publish every booking route a second time
  (for example `bookings/booking/calculate-price/`).

Legacy function views
---------------------
Routes backed by the old function views (bookings_collection, accept_booking, paystack_webhook, ...)
are looked up by name with _find(), in the modules listed in _LEGACY_MODULES (first match wins):
  * found                  -> used as before.
  * missing, customer      -> served by the new BookingViewSet instead.
  * missing, anything else -> a stub that answers 501 and is listed in the startup log
    (search for "urls: missing view"), so one missing function never stops the server booting.

Notes
-----
- The Django admin site is NOT mounted here; mount it in the project urls.py (see above).
  Only the custom live-map pages are served from this file, at /api/admin/live-map/.
- Dispatch offer ids are UUIDs, so offer routes use <str:offer_id>.
- Route names are global (no app_name), because LegacyQuoteView calls reverse("calculate-price").
- Paths end with "/" because APPEND_SLASH redirects a POST without a trailing slash and the body
  is lost. The one exception is customer/wallet, kept without a slash for existing clients.
"""
import logging

from django.contrib import admin
from django.urls import include, path
from rest_framework import status
from rest_framework.authtoken.views import obtain_auth_token
from rest_framework.decorators import api_view, authentication_classes, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

from . import admin_dashboard
from . import booking_views as v
from . import views

logger = logging.getLogger(__name__)

# Where old function views are searched, in order. The first module that has the name wins.
# If views.py holds the CURRENT version of a function that booking_views.py also defines
# (accept_booking, rider_bookings, ...), swap the order to (views, v).
_LEGACY_MODULES = (v, views)

# ---------------------------------------------------------------------------
# Helpers for legacy function views
# ---------------------------------------------------------------------------
_MISSING = []


def _find(name):
    """Return the old view called `name` from _LEGACY_MODULES; else None."""
    for module in _LEGACY_MODULES:
        found = getattr(module, name, None)
        if found is not None:
            return found
    return None


def _not_implemented(name):
    """A view that always answers 501. Open to everyone, so a missing view reports 501 itself
    instead of a misleading 401 (and payment webhooks, which carry no token, get the same answer)."""

    @api_view(["GET", "POST", "PUT", "PATCH", "DELETE"])
    @authentication_classes([])
    @permission_classes([AllowAny])
    def stub(request, *args, **kwargs):
        return Response(
            {"detail": f"'{name}' is not available on this server."},
            status=status.HTTP_501_NOT_IMPLEMENTED,
        )

    stub.__name__ = f"missing_{name}"
    return stub


def _legacy(name):
    """Old function view, or a 501 stub (logged at startup) if it does not exist."""
    found = _find(name)
    if found is not None:
        return found
    _MISSING.append(name)
    return _not_implemented(name)


@api_view(["GET"])
@authentication_classes([])
@permission_classes([AllowAny])
def health(request):
    """Liveness probe. Tells "api.urls is not mounted" apart from "a booking route is missing"."""
    return Response({"status": "ok"})


# ---------------------------------------------------------------------------
# Legacy customer "bookings/" routes: old views if they exist, new ViewSet otherwise
# ---------------------------------------------------------------------------
def _customer_booking_routes():
    viewset = v.BookingViewSet
    collection = _find("bookings_collection")
    cancel = _find("booking_cancel")
    detail = _find("booking_detail")

    # The old views take <ref>; the ViewSet's lookup_url_kwarg is <booking_ref>.
    routes = [
        path(
            "bookings/",
            collection or viewset.as_view({"get": "list", "post": "create"}),
            name="legacy-bookings",
        ),
        path(
            "bookings/<str:ref>/cancel/" if cancel else "bookings/<str:booking_ref>/cancel/",
            cancel or viewset.as_view({"post": "cancel"}),
            name="legacy-booking-cancel",
        ),
        path(
            "bookings/<str:ref>/" if detail else "bookings/<str:booking_ref>/",
            detail or viewset.as_view({"get": "retrieve"}),
            name="legacy-booking-detail",
        ),
    ]
    for label, found in (
        ("bookings_collection", collection),
        ("booking_cancel", cancel),
        ("booking_detail", detail),
    ):
        if found is None:
            logger.warning("urls: old view '%s' not found; using the new BookingViewSet", label)
    return routes


# ---------------------------------------------------------------------------
# URL patterns
# ---------------------------------------------------------------------------
urlpatterns = [
    # Health check
    path("health/", health, name="api-health"),

    # Admin live map (custom pages; the admin site itself is mounted in the project urls.py)
    path("admin/live-map/", admin.site.admin_view(admin_dashboard.live_map_page), name="rn_live_map"),
    path("admin/live-map/data/", admin.site.admin_view(admin_dashboard.live_map_data), name="rn_live_map_data"),

    # Pricing, booking, webhook, revenue, nearby riders (see booking_urls.py). Included once.
    path("", include("api.booking_urls")),

    # Auth & login
    path("auth/login/", views.user_login, name="api-login"),
    path("auth/token/", obtain_auth_token, name="api-token-auth"),

    # Registration & phone verification
    path("auth/register/customer/", views.customer_register, name="customer-register"),
    path("auth/register/rider/", views.rider_register, name="rider-register"),
    path("auth/verify/send-code/", views.send_verification_code, name="send-verification-code"),
    path("auth/verify/", views.verify_code, name="verify-code"),

    # Rider onboarding & verification
    path("auth/rider/verify/", views.rider_submit_documents, name="rider-verify"),
    path("auth/rider/verification-status/", views.rider_verification_status, name="rider-verification-status"),
    path("auth/me/", views.my_profile, name="my-profile"),
    path("profile/", views.customer_profile, name="customer-profile"),

    # Dispatch offers
    path("rider/offers/<str:offer_id>/accept/", views.accept_offer, name="accept-offer"),
    path("rider/offers/<str:offer_id>/decline/", views.decline_offer, name="decline-offer"),

    # Rider wallet & transactions
    path("rider/wallet/", views.get_rider_wallet, name="rider-wallet"),
    path("rider/transactions/", views.get_transaction_history, name="transaction-history"),
    path("rider/transactions/initiate/", views.deposit_initiate, name="deposit-initiate"),
    path("rider/deposit/", views.wallet_deposit, name="wallet-deposit"),
    path("rider/deposits/", views.deposit_history, name="deposit-history"),
    path("rider/deposits/<str:reference>/verify/", views.deposit_verify, name="deposit-verify"),
    path("rider/withdraw/", views.request_withdrawal, name="request-withdrawal"),

    # Customer wallet (no trailing slash kept so existing clients keep working)
    path("customer/wallet", views.customer_wallet, name="customer-wallet"),

    # Location & nearby riders
    path("rider/update-location/", views.update_rider_location, name="rider-location"),
    path("riders/nearby/", views.get_nearby_riders, name="get-nearby-riders"),

    # Customer bookings (legacy paths kept for existing app versions; after booking_urls)
    *_customer_booking_routes(),

    # Promo validation. The current frontend does NOT call these: it sends promo_code inside the
    # quote request. Both paths share one view so old clients keep working either way.
    path("promos/validate/", _legacy("promo_validate"), name="promo-validate"),
    path("promo/verify/", _legacy("promo_validate"), name="promo-verify"),

    # Rider dashboard (list-bookings/ is the path it already calls)
    path("list-bookings/", _legacy("rider_bookings"), name="list-bookings"),
    path("rider/bookings/", _legacy("rider_bookings"), name="rider-bookings"),
    path("rider/presence/", _legacy("rider_presence"), name="rider-presence"),
    path("accept-booking/", _legacy("accept_booking"), name="accept-booking"),
    path("decline-booking/", _legacy("decline_booking"), name="decline-booking"),
    path("arrive-ride/", _legacy("arrive_ride"), name="arrive-ride"),
    path("start-ride/", _legacy("start_ride"), name="start-ride"),
    path("complete-ride/", _legacy("complete_ride"), name="complete-ride"),
    path("release-booking/", _legacy("release_booking"), name="release-booking"),

    # Payments - Paystack
    path("payments/paystack/webhook/", _legacy("paystack_webhook"), name="paystack-webhook"),
    path("payments/initialize/", _legacy("initialize_payment"), name="paystack-initialize"),
    path("payments/verify/", _legacy("verify_payment"), name="paystack-verify"),
]

if _MISSING:
    logger.error("urls: missing view(s) served as 501 stubs: %s", ", ".join(sorted(set(_MISSING))))

