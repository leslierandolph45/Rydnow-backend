"""
URL routes for the Rydnow `api` app.

Mounted in rydnow/urls.py as:  path("api/", include("api.urls"))

Security notes:
- Authentication/permissions are enforced in the views (DRF default is
  IsAuthenticated). Public endpoints (register, login, send/verify code,
  Paystack webhook) must explicitly set permission_classes = [AllowAny].
- csrf_exempt belongs on the webhook VIEW (or use DRF's APIView, which is
  already exempt), not here.
"""

from django.urls import path
from . import views
from . import rider_views  # ✅ Added missing import


urlpatterns = [
    # ───────────────────────
    # Auth
    # ───────────────────────
    path("auth/register/", views.customer_register, name="register"),
    path("auth/login/", views.user_login, name="login"),
    path("auth/send-code/", views.send_verification_code, name="send-code"),
    path("auth/verify-code/", views.verify_code, name="verify-code"),

    # ───────────────────────
    # Rider onboarding
    # ───────────────────────
    path("api/rider-register/", rider_views.rider_register, name="rider-register"),
  
    path("rider-signup/", views.rider_register, name="rider-signup"),
    path("api/rider/submit-documents/", rider_views.rider_submit_verification, name="rider-verify"),

    # ───────────────────────
    # Profile
    # ───────────────────────
    path("customer/profile/", views.customer_profile, name="customer-profile"),

    # ───────────────────────
    # Location
    # ───────────────────────
    path("rider/update-location/", views.update_rider_location, name="update-rider-location"),
    path("riders/nearby/", views.get_nearby_riders, name="get-nearby-riders"),

    # ───────────────────────
    # Wallet
    # ───────────────────────
    path("rider/wallet/", views.get_rider_wallet, name="rider-wallet"),
    path("rider/deposit/", views.wallet_deposit, name="wallet-deposit"),
    path("rider/transactions/", views.rider_transactions, name="rider-transactions"),
    path("payment/withdraw/", views.request_withdrawal, name="request-withdraw"),

    # ───────────────────────
    # Paystack
    # ───────────────────────
    path("paystack/initialize/", views.initialize_payment, name="paystack-initialize"),
    path("paystack/verify/", views.verify_payment, name="paystack-verify"),
    path("paystack/webhook/", views.paystack_webhook, name="paystack-webhook"),
]
