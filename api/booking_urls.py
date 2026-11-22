"""
URLs for the booking API (api/booking_urls.py).

This module is included ONCE, by api/urls.py:

    path("", include("api.booking_urls"))

and api/urls.py itself is mounted under "api/" in the project urls.py:

    path("api/", include("api.urls"))

Do NOT include this module again anywhere else (for example directly in the project urls.py):
every route would be published twice.

The frontend must call these exact paths (relative to /api/). Paths end with "/" because
APPEND_SLASH redirects a POST that lacks one, and the request body is lost.

Ordering: literal paths come before booking/<booking_ref>/ so "create", "webhook" and "admin"
are never read as a booking reference.
"""
from django.urls import path

from . import booking_views as v

urlpatterns = [
    # Pricing
    path("booking/calculate-price/", v.QuoteView.as_view(), name="calculate-price"),
    # Short alias of the route above for older clients. Remove once nothing calls it.
    path("quote/", v.QuoteView.as_view(), name="booking-quote"),
    # Deprecated quote route (kept so LegacyQuoteView keeps working).
    path("bookings/quote/", v.LegacyQuoteView.as_view(), name="legacy-quote"),

    # Booking collection and provider/staff endpoints (literals first)
    path("booking/create/", v.BookingViewSet.as_view({"post": "create"}), name="booking-create"),
    path("booking/webhook/", v.BookingWebhookView.as_view(), name="booking-webhook"),
    path("booking/admin/revenue/", v.RevenueReportView.as_view(), name="booking-revenue"),
    path("booking/", v.BookingViewSet.as_view({"get": "list"}), name="booking-list"),

    # One booking, by reference
    path("booking/<str:booking_ref>/status/", v.BookingViewSet.as_view({"patch": "update_status"}), name="booking-status"),
    path("booking/<str:booking_ref>/cancel/", v.BookingViewSet.as_view({"post": "cancel"}), name="booking-cancel"),
    path("booking/<str:booking_ref>/release/", v.BookingViewSet.as_view({"post": "release"}), name="booking-release"),
    path("booking/<str:booking_ref>/", v.BookingViewSet.as_view({"get": "retrieve"}), name="booking-detail"),

    # Riders near a point
    path("dispatch/rider/nearby/", v.NearbyRidersView.as_view(), name="riders-nearby"),
]

