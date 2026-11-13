from django.contrib import admin
from django.urls import path
from rest_framework.authtoken.views import obtain_auth_token
from api import admin_dashboard
from . import views
from .views import (
    customer_register,
    rider_register,
    rider_submit_documents,
    send_verification_code,
    verify_code,
)

# Safely import profile_views — no error if file doesn't exist
try:
    from . import profile_views
    profile_urls = [
        path('profile/', profile_views.customer_profile, name='customer_profile'),
    ]
except (ImportError, AttributeError):
    profile_urls = []

# NOTE: Rider document files are NOT served from a public /media/ URL on production.
# /admin/api/riderdocument/<id>/file/ — Staff view them through the admin,
# which is added automatically by RiderDocumentAdmin.get_urls() in admin.py


urlpatterns = [
    # ─────────────────────────────────────
    # Auth & Login
    # ─────────────────────────────────────
    path("auth/login/", views.user_login, name="api-login"),
    path("auth/token/", obtain_auth_token, name="api-token-auth"),

    # ─────────────────────────────────────
    # Registration & Phone Verification
    # ─────────────────────────────────────
    path("auth/register/customer/", views.customer_register, name="customer-register"),
    path("auth/register/rider/", views.rider_register, name="rider-register"),
    path("auth/verify/send-code/", send_verification_code, name="send-verification-code"),
    path("auth/verify/", verify_code, name="verify-code"),

    # ─────────────────────────────────────
    # Rider Onboarding & Verification
    # ─────────────────────────────────────
    path("auth/rider/verify/", rider_submit_documents, name="rider-verify"),
    path("auth/rider/verification-status/", views.rider_verification_status, name="rider-verification-status"),
    path("auth/me/", views.my_profile, name="my-profile"),
    path("profile/", views.customer_profile, name="customer-profile"),

    # ─────────────────────────────────────
    # ✅ Accept Job Route
    # ─────────────────────────────────────
    #path("rider/accept-job/", views.accept_job, name="accept-job"),
    path("rider/offers/<int:offer_id>/accept/", views.accept_offer, name= "accept-offer"),

    # ─────────────────────────────────────
    # Rider Wallet & Transactions
    # ─────────────────────────────────────
    path("rider/wallet/", views.get_rider_wallet, name="rider-wallet"),
    path("rider/transactions/", views.get_transaction_history, name="transaction-history"),
    path("rider/deposit/", views.wallet_deposit, name="wallet-deposit"),
    path("rider/withdraw/", views.request_withdrawal, name="request-withdrawal"),
    path("rider/deposits/", views.deposit_history, name="deposit-history"),
    path("rider/transactions/initiate/", views.deposit_initiate, name="deposit-initiate"),
    path("rider/deposits/<str:reference>/verify/", views.deposit_verify, name="deposit-verify"),
    path("customer/wallet", views.customer_wallet, name="customer-wallet"),

    # ─────────────────────────────────────
    # Location & Nearby Riders
    # ─────────────────────────────────────
    path("rider/update-location/", views.update_rider_location, name="rider-location"),
    path("riders/nearby/", views.get_nearby_riders, name="get-nearby-riders"),

    # ─────────────────────────────────────
    # Payments — Paystack
    # ─────────────────────────────────────
    path("payments/paystack/webhook/", views.paystack_webhook, name="paystack-webhook"),
    path("payments/initialize/", views.initialize_payment, name="paystack-initialize"),
    path("payments/verify/", views.verify_payment, name="paystack-verify"),

    # ─────────────────────────────────────
    # Admin Live Map
    # ─────────────────────────────────────
    path("admin/live-map/", admin.site.admin_view(admin_dashboard.live_map_page), name="rn-live-map"),
    path("admin/", admin.site.urls),
]

# Add profile routes only if the file exists
urlpatterns += profile_urls
