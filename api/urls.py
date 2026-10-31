from django.urls import path
from django.contrib import admin
from .views import(
    user_login,
    send_verification_code,
    customer_register,
    rider_register,
    rider_submit_documents,
    customer_profile,
    update_rider_location,
    get_nearby_riders,
    get_rider_wallet,
    wallet_deposit,
    wallet_transactions,
    request_withdrawal,
    initialize_payment,
    verify_payment,
)
from .views import user_login, send_verification_code
from . import views
from rest_framework.decorators import api_view, permission_classes
from django.views.decorators.csrf import csrf_exempt
# Create router for auto-generated Booking endpoints
urlpatterns = [
    path('admin/',admin.sitte.urls),
    # Auth endpoints — matching your actual function names
    path('auth/register/', views.customer_register, name='register'),
    path('auth/login/', user_login, name='login'),
    path('auth/send-code/', views.send_verification_code, name='send-code'),
    path('auth/verify-code/', send_verification_code, name='verify-code'),

    # Rider registration
    path('auth/rider-register/', views.rider_register, name='rider-register'),
    path('rider/submit-docs/', views.rider_submit_documents, name='rider-submit-docs'),

    # Profile
    path('customer/profile/', views.customer_profile, name='customer-profile'),

    # Location
    path('rider/update-location/', views.update_rider_location, name='update-rider-location'),
    path('riders/nearby/', views.get_nearby_riders, name='get-nearby-riders'),

    # Wallet & Payments
    path('rider/wallet/', views.get_rider_wallet, name='rider-wallet'),
    path('rider/waller/deposit/', views.wallet_deposit, name='rider-wallet-deposit'),
    path('rider/transactions/', views.rider_transactions, name='rider-transactions'),
    path('payment/record-job/', views.record_job_payment, name='record-job-payment'),
    path('payment/withdraw/', views.request_withdrawal, name='request-withdraw'),

    # Paystack
    #path('paystack/initialize/', views.initialize_payment, name='paystack-initialize'),
    #path('paystack/verify/', views.verify_payment, name='paystack-verify'),
    #path('paystack/webhook/', views.paystack_webhook, name='paystack-webhook'),

    # ✨ Auto-generated Booking endpoints: /api/bookings/
    #path('', include(router.urls)),
]
