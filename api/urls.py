from django.urls import path
from . import views
from django.contrib import admin
from django.views.decorators.csrf import csrf_exempt
from api.models import RiderWallet
from django.urls import path, include

urlpatterns = [
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

    # === Paystack Payments ===
    path("payments/paystack/webhook/", views.paystack_webhook, name="paystack-webhook"),
    path("paystack/initialize/", views.initialize_payment, name="paystack-initialize"),
    path("paystack/verify/", views.verify_payment, name="paystack-verify"),
]



   