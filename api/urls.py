from django.urls import path
from .views import (
    RiderViewSet,
    BookingViewSet,
    CustomerProfileViewSet,
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
    create_booking,
    list_bookings,
    accept_booking,
    start_ride,
    complete_ride,
    estimate_fare,
    calculate_fare_with_ai,
    send_verification_code,
    verify_verification_code,
)

urlpatterns = [
    # ─── AUTH ───
    path('auth/register/', customer_register, name='customer-register'),
    path('auth/login/', user_login, name='user-login'),
    
    # ─── OTP VERIFICATION ───
    path('auth/send-code/', send_verification_code, name='send-verification-code'),
    path('auth/verify-code/', verify_verification_code, name='verify-verification-code'),
    
    # ─── CUSTOMER PROFILE ───
    path('customer/profile/', customer_profile, name='customer-profile'),
    
    # ─── RIDERS ───
    path('riders/', RiderViewSet.as_view({'get': 'list'})),
    path('riders/<int:pk>/', RiderViewSet.as_view({'get': 'retrieve'})),
    path('rider/register/', rider_register, name='rider-register'),
    path('rider/documents/', rider_submit_documents, name='rider-submit-documents'),
    path('rider/location/', update_rider_location, name='update-rider-location'),
    path('riders/nearby/', get_nearby_riders, name='get-nearby-riders'),
    path('rider/wallet/', get_rider_wallet, name='get-rider-wallet'),
    path('rider/transactions/', rider_transactions, name='rider-transactions'),
    path('rider/withdraw/', request_withdrawal, name='request-withdrawal'),
    
    # ─── BOOKINGS ───
    path('bookings/create/', create_booking, name='create-booking'),
    path('bookings/', list_bookings, name='list-bookings'),
    path('bookings/<int:booking_id>/accept/', accept_booking, name='accept-booking'),
    path('bookings/<int:booking_id>/start/', start_ride, name='start-ride'),
    path('bookings/<int:booking_id>/complete/', complete_ride, name='complete-ride'),
    path('bookings/<int:booking_id>/payment/', record_job_payment, name='record-job-payment'),
    
    # ─── FARE ESTIMATE ───
    path('fare/estimate/', estimate_fare, name='estimate-fare'),
    path('fare/ai-calculate/', calculate_fare_with_ai, name='calculate-fare-with-ai'),
]
