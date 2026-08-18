from django.urls import path
from .views import BulkSendAPIView, EmailPreviewAPIView

app_name = 'mailings'
urlpatterns = [
    # Immediate Bulk Send
    path('bulk-send/', BulkSendAPIView.as_view(), name='bulk-send'),
    # HTML Preview
    path('preview/', EmailPreviewAPIView.as_view(), name='email-preview'),
]