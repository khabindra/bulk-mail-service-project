from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import CampaignViewSet, CampaignAttachmentViewSet

router = DefaultRouter()

# Main campaigns routes: /api/campaigns/
router.register(r'campaigns', CampaignViewSet, basename='campaign')

# Nested attachments routes: /api/campaigns/<pk>/attachments/
campaigns_router = DefaultRouter()
campaigns_router.register(r'attachments', CampaignAttachmentViewSet, basename='campaign-attachment')

# Wire them together
urlpatterns = [
    path('', include(router.urls)),
    path('campaigns/<int:campaign_pk>/', include(campaigns_router.urls)),
]