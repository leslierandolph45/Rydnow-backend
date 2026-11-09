from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.http import JsonResponse
from django.urls import include, path


def health(request):
    return JsonResponse({"status": "ok"})


urlpatterns = [
    path("", health),
    path("health/", health),
    path(getattr(settings, "ADMIN_URL", "admin/"), admin.site.urls),
    path("api/", include("api.urls")),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
