from django.urls import path, include
from rest_framework.routers import DefaultRouter
from rest_framework.authtoken.views import obtain_auth_token
from .views import (
    RiderViewSet, BookingViewSet, customer_profile,
    customer_register, user_login, rider_register, rider_submit_documents,
    update_rider_location, get_nearby_riders,
    get_rider_wallet, rider_transactions, record_job_payment, request_withdrawal,
    initialize_payment, verify_payment, paystack_webhook
)


router = DefaultRouter()
router.register(r'riders', RiderViewSet)
router.register(r'bookings', BookingViewSet)


urlpatterns = [
    # AUTH ENDPOINTS
    path('auth/register/', customer_register, name='customer-register'),
    path('auth/login/', user_login, name='user-login'),
    path('auth/rider-register/', rider_register, name='rider-register'),
    path('auth/rider-submit/', rider_submit_documents, name='rider-submit'),

    # 📍 LIVE LOCATION
    path('rider/update-location/', update_rider_location, name='update-rider-location'),
    path('riders/nearby/', get_nearby_riders, name='nearby-riders'),

    # 💰 WALLET, COMMISSION & WITHDRAWALS
    path('wallet/balance/', get_rider_wallet, name='rider-wallet'),
    path('wallet/transactions/', rider_transactions, name='rider-transactions'),
    path('payments/record/', record_job_payment, name='record-payment'),
    path('withdrawals/request/', request_withdrawal, name='request-withdrawal'),

    # 💳 PAYSTACK PAYMENT GATEWAY — NEW!
    path('payments/initialize/', initialize_payment, name='initialize-payment'),
    path('payments/verify/', verify_payment, name='verify-payment'),
    path('payments/webhook/', paystack_webhook, name='paystack-webhook'),

    # EXISTING ENDPOINTS
    path('', include(router.urls)),
    path('auth/token/', obtain_auth_token, name='api_token_auth'),
    path('profile/', customer_profile, name='customer_profile'),
]
