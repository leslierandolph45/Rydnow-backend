from django.urls import path
from .views import (
    RiderViewSet,
    BookingViewSet,
    customer_profile,
    customer_register,
    user_login,
    rider_register,
    rider_submit_documents,
    update_rider_location,
    get_nearby_riders,
    get_rider_wallet,
    rider_transactions,
    record_job_payment,
    request_withdrawal,
    initialize_payment,
    verify_payment,
    paystack_webhook,
    send_verification_code,
    verify_code,
)


urlpatterns = [
    # ─── VERIFICATION ───
    path('auth/send-code/', send_verification_code, name='send-verification-code'),
    path('auth/verify-code/', verify_code, name='verify-code'),

    # ─── AUTH ───
    path('auth/register/', customer_register, name='customer-register'),
    path('auth/login/', user_login, name='user-login'),
    path('rider/register/', rider_register, name='rider-register'),
    path('rider/documents/', rider_submit_documents, name='rider-submit-documents'),

    # ─── CUSTOMER PROFILE ───
    path('customer/profile/', customer_profile, name='customer-profile'),

    # ─── RIDERS ───
    path('riders/', RiderViewSet.as_view({'get': 'list'}), name='rider-list'),
    path('riders/<int:pk>/', RiderViewSet.as_view({'get': 'retrieve'}), name='rider-detail'),
    path('rider/location/', update_rider_location, name='update-rider-location'),
    path('riders/nearby/', get_nearby_riders, name='get-nearby-riders'),
    path('rider/wallet/', get_rider_wallet, name='get-rider-wallet'),
    path('rider/transactions/', rider_transactions, name='rider-transactions'),
    path('rider/withdraw/', request_withdrawal, name='request-withdrawal'),

    # ─── BOOKINGS ───
    path('bookings/', BookingViewSet.as_view({'get': 'list', 'post': 'create'}), name='booking-list'),
    path('bookings/<int:pk>/', BookingViewSet.as_view({'get': 'retrieve', 'put': 'update', 'patch': 'partial_update', 'delete': 'destroy'}), name='booking-detail'),
    path('bookings/payment/', record_job_payment, name='record-job-payment'),

    # ─── PAYSTACK PAYMENTS ───
    path('payment/initialize/', initialize_payment, name='initialize-payment'),
    path('payment/verify/', verify_payment, name='verify-payment'),
    path('payment/webhook/', paystack_webhook, name='paystack-webhook'),
]
