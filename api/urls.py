from django.urls import path
from . import views


urlpatterns = [
    path('auth/register/', views.register, name='register'),
    path('auth/login/', views.login, name='login'),
    path('auth/send-code/', views.send_verification_code, name='send-code'),
    path('auth/verify-code/', views.verify_code, name='verify-code'),
    # Add your other API routes here
]
