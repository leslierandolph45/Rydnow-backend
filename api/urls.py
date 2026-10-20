from django.contrib import admin
from django.urls import path, include
from django.conf import settings
from django.conf.urls.static import static
from django.http import HttpResponse
from .views import send_verification_code, verify_verification_code


def home(request):
    return HttpResponse("Rydnow API is running! 🚀 Go to /admin or /api/bookings")


urlpatterns = [
    path('', home),
    path('admin/', admin.site.urls),
    path('api/', include('api.urls')),
    
    # ✅ OTP VERIFICATION ENDPOINTS
    path('api/auth/send-code/', send_verification_code, name='send-verification-code'),
    path('api/auth/verify-code/', verify_verification_code, name='verify-verification-code'),
]


if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
