from django.urls import path
from . import views


urlpatterns = [
    # Auth endpoints — matching your actual function names
    path('auth/register/', views.customer_register, name='register'),
    path('auth/login/', views.user_login, name='login'),
    path('auth/send-code/', views.send_verification_code, name='send-code'),
    path('auth/verify-code/', views.verify_code, name='verify-code'),

    # Rider registration
    path('auth/rider-register/', views.rider_register, name='rider-register'),
    path('rider/submit-docs/', views.rider_submit_documents, name='rider-submit-docs'),

    # Profile
    path('customer/profile/', views.customer_profile, name='customer-profile'),

    # Location
    path('rider/update-location/', views.update_rider_location, name='update-rider-location'),
    path('riders/nearby/', views.get_nearby_riders, name='nearby-riders'),

    # Wallet & Payments
    path('rider/wallet/', views.get_rider_wallet, name='rider-wallet'),
    path('rider/transactions/', views.rider_transactions, name='rider-transactions'),
    path('payment/record-job/', views.record_job_payment, name='record-job-payment'),
    path('payment/withdraw/', views.request_withdrawal, name='request-withdrawal'),

    # Paystack
    path('paystack/initialize/', views.initialize_payment, name='paystack-initialize'),
    path('paystack/verify/', views.verify_payment, name='paystack-verify'),
    path('paystack/webhook/', views.paystack_webhook, name='paystack-webhook'),
]
