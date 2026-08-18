from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import (
    EmailTemplateViewSet,
    InlineImageViewSet,
    MailTypeViewSet # ✅ CHANGED: Now a ViewSet
)

router = DefaultRouter()

# ✅ All ViewSets registered in one place. 
# Results in clean URLs: /api/templates/types/, /api/templates/1/, etc.
router.register(r'types', MailTypeViewSet, basename='mail-type')
router.register(r'', EmailTemplateViewSet, basename='emailtemplate')
router.register(r'images', InlineImageViewSet, basename='inlineimage')

urlpatterns = [
    # Simply include the router
    path('', include(router.urls)),
]