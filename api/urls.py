"""
URLs for the booking API. In your project urls.py (or api/urls.py):

    from django.urls import include, path
    urlpatterns = [path("api/", include("api.booking_urls")), ...]

The frontend must call these exact paths (relative to wherever this is included).
Literal paths come before booking/<booking_ref>/ so "create" and "webhook" are never read as a reference.
"""
from django.urls import path

from . import booking_views as v

urlpatterns = [
    path("booking/calculate-price/", v.QuoteView.as_view(), name="calculate-price"),
    path("bookings/quote/", v.LegacyQuoteView.as_view(), name="legacy-quote"),
    path("booking/create/", v.BookingViewSet.as_view({"post": "create"}), name="booking-create"),
    path("booking/webhook/", v.BookingWebhookView.as_view(), name="booking-webhook"),
    path("booking/admin/revenue/", v.RevenueReportView.as_view(), name="booking-revenue"),
    path("booking/", v.BookingViewSet.as_view({"get": "list"}), name="booking-list"),
    path("booking/<str:booking_ref>/status/", v.BookingViewSet.as_view({"patch": "update_status"}), name="booking-status"),
    path("booking/<str:booking_ref>/cancel/", v.BookingViewSet.as_view({"post": "cancel"}), name="booking-cancel"),
    path("booking/<str:booking_ref>/release/", v.BookingViewSet.as_view({"post": "release"}), name="booking-release"),
    path("booking/<str:booking_ref>/", v.BookingViewSet.as_view({"get": "retrieve"}), name="booking-detail"),
    path("dispatch/rider/nearby/", v.NearbyRidersView.as_view(), name="riders-nearby"),
]

    