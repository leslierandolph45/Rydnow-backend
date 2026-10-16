from django.urls import path, include
from django.conf import settings
from django.conf.urls.static import static
from rest_framework.routers import DefaultRouter
from rest_framework.authtoken.views import obtain_auth_token
from .views import (
    RiderViewSet, BookingViewSet, customer_profile,
    customer_register, user_login, rider_register, rider_submit_documents,
    update_rider_location, get_nearby_riders,
    get_rider_wallet, rider_transactions, record_job_payment, request_withdrawal,
    create_booking, list_bookings, accept_booking, start_ride, complete_ride
)


router = DefaultRouter()
router.register(r'riders', RiderViewSet)
router.register(r'bookings', BookingViewSet)


urlpatterns = [
    # ✅ BOOKING ENDPOINTS
    path('booking/create/', create_booking, name='create-booking'),
    path('bookings/list/', list_bookings, name='list-bookings'),
    path('bookings/accept/', accept_booking, name='accept-booking'),
    path('bookings/start/', start_ride, name='start-ride'),
    path('bookings/complete/', complete_ride, name='complete-ride'),
    
    # ✅ AUTH ENDPOINTS
    path('auth/register/', customer_register, name='customer-register'),
    path('auth/login/', user_login, name='user-login'),
    path('auth/rider-register/', rider_register, name='rider-register'),
    path('auth/rider-submit/', rider_submit_documents, name='rider-submit'),
    
    # ✅ 📍 LIVE LOCATION
    path('rider/update-location/', update_rider_location, name='update-rider-location'),
    path('riders/nearby/', get_nearby_riders, name='nearby-riders'),
    
    # ✅ 💰 WALLET, COMMISSION & WITHDRAWALS
    path('wallet/balance/', get_rider_wallet, name='rider-wallet'),
    path('wallet/transactions/', rider_transactions, name='rider-transactions'),
    path('payments/record/', record_job_payment, name='record-payment'),
    path('withdrawals/request/', request_withdrawal, name='request-withdrawal'),
    
    # ✅ EXISTING ENDPOINTS
    path('', include(router.urls)),
    path('auth/token/', obtain_auth_token, name='api_token_auth'),
    path('profile/', customer_profile, name='customer_profile'),
]


# ✅ SERVE UPLOADED IMAGES & FILES
if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
