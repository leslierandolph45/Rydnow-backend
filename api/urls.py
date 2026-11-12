from django.urls import path
from rest_framework.authtoken.views import obtain_auth_token
from . import views
from django.contrib import admin
from django.views.decorators.csrf import csrf_exempt
from api.models import RiderWallet
from django.urls import path, include
from .views import (
    customer_register,
    rider_register, 
    rider_submit_documents,
    customer_profile,
)
from api import admin_dashboard

from .views import MyRiderProfileView
urlpatterns = [
    # Other URLS
    path("auth/login/", obtain_auth_token, name="api-login"),

    path("auth/register/customer/", customer_register, name="customer-register"),
    path("auth/register/rider/", rider_register, name="rider-register"),
    path("auth/rider/verify", rider_submit_documents, name="rider-verify"),

    # === Rider Wallet ===
    path("rider/wallet/", views.get_rider_wallet, name="rider-wallet"),
    path("rider/deposit/", views.wallet_deposit, name="wallet-deposit"),
    path("rider/transactions/", views.get_transaction_history, name="transaction-history"),
    path("rider/withdraw/", views.request_withdrawal, name="request-withdrawal"),
    path("rider/deposits/", views.deposit_history, name="deposit-history"),
    path("rider/transactions/", views.get_transaction_history, name="transaction-history"),
    path("deposit/transactions/", views.deposit_initiate, name="deposit_initiate"),
    path("rider/transactions/<str:reference>/verify/", views.deposit_verify, name="deposit-verify"),


    # === Location & Discovery ===
    path("rider/update-location/", views.update_rider_location, name="update-rider-location"),
    path("riders/nearby/", views.get_nearby_riders, name="get-nearby-riders"),

    # LIVE MAP
    path("admin/live-map/", admin.site.admin_view(admin_dashboard.live_map_page), name="rn_live_map"),
    path("admin/live-map/data/", admin.site.admin_view(admin_dashboard.live_map_data), name="rn_live_map_data"),
    path("admin/", admin.site.urls),

    # === Paystack Payments ===
    path("payments/paystack/webhook/", views.paystack_webhook, name="paystack-webhook"),
    path("paystack/initialize/", views.initialize_payment, name="paystack-initialize"),
    path("paystack/verify/", views.verify_payment, name="paystack-verify"),
]



   