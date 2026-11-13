from django.contrib import admin
from django.urls import path
from rest_framework.authtoken.views import obtain_auth_token

from api import admin_dashboard

from . import views
from .views import (
    customer_register,
    rider_register,
    rider_submit_documents,
)
from .profile_views import my_profile

# NOTE: rider document files are NOT served from a public /media/ URL on purpose
# (Ghana Cards and licences are private). Staff view them through the admin at
#   /admin/api/riderdocument/<id>/file/
# which is added automatically by RiderDocumentAdmin.get_urls() in admin.py.

urlpatterns = [
    # Auth
    path("auth/login/", obtain_auth_token, name="api-login"),
    path("auth/register/customer/", customer_register, name="customer-register"),
    path("auth/register/rider/", rider_register, name="rider-register"),
    path("rider/verify/", rider_submit_documents, name="rider-verify"),
    path("rider/verification-status/", views.rider_verification_status, name="rider-verification-status"),
    path("me/", my_profile, name="my_profile"),

    # === Rider Wallet ===
    path("rider/wallet/", views.get_rider_wallet, name="rider-wallet"),
    path("rider/deposit/", views.wallet_deposit, name="wallet-deposit"),
    path("rider/transactions/", views.get_transaction_history, name="transaction-history"),
    path("rider/withdraw/", views.request_withdrawal, name="request-withdrawal"),
    path("rider/deposits/", views.deposit_history, name="deposit-history"),
    path("deposit/transactions/", views.deposit_initiate, name="deposit_initiate"),
    path("rider/transactions/<str:reference>/verify/", views.deposit_verify, name="deposit-verify"),

    # === Location & Discovery ===
    path("rider/update-location/", views.update_rider_location, name="update-rider-location"),
    path("riders/nearby/", views.get_nearby_riders, name="get-nearby-riders"),

    # LIVE MAP (must stay ABOVE admin.site.urls)
    path("admin/live-map/", admin.site.admin_view(admin_dashboard.live_map_page), name="rn_live_map"),
    path("admin/live-map/data/", admin.site.admin_view(admin_dashboard.live_map_data), name="rn_live_map_data"),
    path("admin/", admin.site.urls),

    # === Paystack Payments ===
    path("payments/paystack/webhook/", views.paystack_webhook, name="paystack-webhook"),
    path("paystack/initialize/", views.initialize_payment, name="paystack-initialize"),
    path("paystack/verify/", views.verify_payment, name="paystack-verify"),
]

