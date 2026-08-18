from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import path, include
from drf_spectacular.views import SpectacularAPIView, SpectacularRedocView, SpectacularSwaggerView

urlpatterns = [
    path('admin/', admin.site.urls),
    # path('recurring-scheduling/',include('campaigns.urls')),
    # path('one-time-scheduling/', include('test_mailing.urls')),
    # path('immediate-mail/',include('mailings.urls')),
    path('users/',include('users.urls')),
    path('templates/',include('templates.urls')),
    
    # API Documentation (Production-grade requirement)
    path('api/schema/', SpectacularAPIView.as_view(), name='schema'),
    path('api/docs/', SpectacularSwaggerView.as_view(url_name='schema'), name='swagger-ui'),
    path('api/redoc/', SpectacularRedocView.as_view(url_name='schema'), name='redoc'),
]+ static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
