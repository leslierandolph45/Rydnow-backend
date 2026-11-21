"""
URL configuration for the `api` app (api/urls.py).


Booking, pricing, webhook, revenue and nearby-rider routes live in booking_urls.py and are
pulled in ONCE with include(). They are NOT repeated here:


    booking/calculate-price/   bookings/quote/   booking/create/   booking/
    booking/webhook/           booking/admin/revenue/
    booking/<ref>/  .../status/  .../cancel/  .../release/
    dispatch/rider/nearby/


Ordering rules
--------------
- Admin live-map routes come BEFORE `admin/`.
- include("api.booking_urls") comes BEFORE the legacy `bookings/<ref>/` routes, so
  `bookings/quote/` is never read as a booking reference.
- Do not include booking_urls under any other prefix: that would publish every booking route
  a second time (for example `bookings/booking/calculate-price/`).


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
- `admin/` is mounted inside this app. If this file is included under "api/", the admin lives at
  /api/admin/. Mount the admin in the project urls.py instead if that is not what you want.
- Dispatch offer ids are UUIDs, so offer routes use <str:offer_id>.
- Route names are global (no app_name), because LegacyQuoteView calls reverse("calculate-price").
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
from .booking_views import QuoteView


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
        path("bookings/", collection or viewset.as_view({"get": "list", "post": "create"}),
             name="legacy-bookings"),
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
    for label, found in (("bookings_collection", collection), ("booking_cancel", cancel),
                         ("booking_detail", detail)):
        if found is None:
            logger.warning("urls: old view '%s' not found; using the new BookingViewSet", label)
    return routes




# ---------------------------------------------------------------------------
# URL patterns
# ---------------------------------------------------------------------------
urlpatterns = [
    # Admin (custom pages FIRST, then the admin site)
    path("admin/live-map/", admin.site.admin_view(admin_dashboard.live_map_page), name="rn_live_map"),
    path("admin/live-map/data/", admin.site.admin_view(admin_dashboard.live_map_data), name="rn_live_map_data"),
    


    # Pricing, booking, webhook, revenue, nearby riders (see booking_urls.py). Included once.
    path("", include("api.booking_urls")),


    # Short alias of booking/calculate-price/ for older clients. Remove once nothing calls it.
    path("quote/", QuoteView.as_view(), name="booking-quote"),


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
    path("promos/validate/", _legacy("promo_validate"), name="promo-validate"),


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

