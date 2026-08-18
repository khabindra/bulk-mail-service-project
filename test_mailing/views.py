# import logging
# from rest_framework import viewsets, filters, status
# from rest_framework.decorators import action
# from rest_framework.permissions import IsAuthenticated, IsAdminUser
# from rest_framework.response import Response
# from rest_framework.exceptions import NotFound, ValidationError
# from django_filters.rest_framework import DjangoFilterBackend
# from drf_spectacular.utils import extend_schema, extend_schema_view, OpenApiExample, OpenApiResponse
# from drf_spectacular.types import OpenApiTypes
# from django.utils import timezone
# from datetime import timedelta
# from django.core.cache import cache as django_cache


# from .models import TestMailing,MailingAttachment
# from .serializers import TestMailingSerializer, MailingAttachmentSerializer
# from .services.test_mailing_service import TestMailingService
# from .permissions import IsOwnerOrAdmin
# from .pagination import StandardCursorPagination

# logger = logging.getLogger(__name__)

# class TestMailingViewSet(viewsets.ModelViewSet):
#     serializer_class = TestMailingSerializer
#     throttle_scope = 'one-time-scheduled-mailing'
#     filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
#     filterset_fields = ['status', 'mail_type', 'created_by']
#     search_fields = ['name', 'description']
#     ordering_fields = ['scheduled_time', 'created_at', 'name']
    
#     # Fix: Pagination class wired to ViewSet
#     pagination_class = StandardCursorPagination

#     def get_queryset(self):
#         if getattr(self, 'swagger_fake_view', False): 
#             return TestMailing.objects.none()
#         queryset = TestMailing.objects.select_related('email_template__mail_type', 'sender_email', 'created_by')
#         if self.action == 'retrieve':
#             queryset = queryset.prefetch_related('recipients', 'mailing_attachments')
#         elif self.action == 'list':
#             queryset = queryset.prefetch_related('mailing_attachments')
#         if not self.request.user.is_staff: 
#             queryset = queryset.filter(created_by=self.request.user)
#         return queryset

#     def get_permissions(self):
#         if self.action in ['update', 'partial_update', 'destroy', 'cancel_schedule', 'send_now']:
#             self.permission_classes = [IsOwnerOrAdmin]
#         else:
#             self.permission_classes = [IsAuthenticated] 
#         return super().get_permissions()

#     @extend_schema(request=TestMailingSerializer, responses={201: TestMailingSerializer})
#     def create(self, request, *args, **kwargs):
#         return super().create(request, *args, **kwargs)

#     def destroy(self, request, *args, **kwargs):
#         instance = self.get_object()
#         if not instance.is_editable:
#             return Response(
#                 {"detail": f"Cannot delete mailing with status '{instance.status}'."},
#                 status=status.HTTP_400_BAD_REQUEST
#             )
#         TestMailingService._disable_schedule(instance)
#         return super().destroy(request, *args, **kwargs)

#     @extend_schema(
#         methods=['POST'], 
#         responses={200: OpenApiResponse(
#             response=OpenApiTypes.OBJECT, 
#             examples=[OpenApiExample('Success', value={'detail': 'Schedule cancelled successfully.'})]
#         )}
#     )
#     @action(detail=True, methods=['post'], url_path='cancel')
#     def cancel_schedule(self, request, pk=None):
#         mailing = self.get_object()
#         if TestMailingService.cancel_mailing(mailing):
#             return Response({"detail": "Schedule cancelled successfully."})
#         return Response(
#             {"detail": f"Cannot cancel mailing with status '{mailing.status}'."},
#             status=status.HTTP_400_BAD_REQUEST
#         )

#     @extend_schema(
#         methods=['POST'], 
#         responses={200: OpenApiResponse(
#             response=OpenApiTypes.OBJECT, 
#             examples=[OpenApiExample('Success', value={'detail': 'Mailing triggered immediately.'})]
#         )}
#     )
#     @action(detail=True, methods=['post'], url_path='send-now', throttle_scope='one-time-schedule-mailing-trigger')
#     def send_now(self, request, pk=None):
#         mailing = self.get_object()
#         if TestMailingService.trigger_immediately(mailing):
#             mailing.refresh_from_db()
#             return Response({"detail": "Mailing triggered immediately.", "status": mailing.status})
#         return Response(
#             {"detail": f"Cannot send mailing with status '{mailing.status}'."},
#             status=status.HTTP_400_BAD_REQUEST
#         )

#     @extend_schema(
#         methods=['GET'], 
#         responses={200: OpenApiResponse(
#             response=OpenApiTypes.OBJECT, 
#             examples=[OpenApiExample('Progress', value={"progress": {"percentage": 50}})]
#         )}
#     )
#     @action(detail=True, methods=['get'], url_path='progress')
#     def get_progress(self, request, pk=None):
#         try:
#             mailing = TestMailing.objects.values(
#                 'id', 'name', 'status', 'total_recipients', 'total_chunks',
#                 'completed_chunks', 'successful_sends', 'failed_sends', 'completed_at',
#                 'created_by_id'
#             ).get(pk=pk)
#         except TestMailing.DoesNotExist:
#             raise NotFound("Mailing not found.")

#         if not request.user.is_staff and mailing['created_by_id'] != request.user.id:
#             raise NotFound("Mailing not found.")

#         completed_chunks = mailing['completed_chunks'] or 0
#         successful = mailing['successful_sends'] or 0
#         failed = mailing['failed_sends'] or 0
#         email_total = successful + failed

#         chunk_pct = min(100, int((completed_chunks / mailing['total_chunks']) * 100)) if mailing['total_chunks'] else 0
#         email_pct = int((successful / email_total) * 100) if email_total > 0 else 0

#         return Response({
#             "id": mailing['id'],
#             "status": mailing['status'],
#             "total_recipients": mailing['total_recipients'],
#             "progress": {
#                 "chunks": {"total": mailing['total_chunks'], "completed": completed_chunks, "percentage": chunk_pct},
#                 "emails": {"successful": successful, "failed": failed, "total": email_total, "percentage": email_pct}
#             },
#             "completed_at": mailing['completed_at']
#         })

#     # Fix: Health check restricted to IsAdminUser to prevent public data leak
#     @extend_schema(
#         methods=['GET'], 
#         responses={200: OpenApiResponse(response=OpenApiTypes.OBJECT, description="System Health Check")},
#         deprecated=True 
#     )
#     @action(detail=False, methods=['get'], permission_classes=[IsAdminUser], url_path='health')
#     def health_check(self, request):
#         checks = {'database': 'ok', 'redis': 'ok'}
#         try:
#             TestMailing.objects.exists()
#         except Exception:
#             checks['database'] = 'error'
#         try:
#             django_cache.set('_health_check', '1', 5)
#         except Exception:
#             checks['redis'] = 'error'
        
#         stuck_count = TestMailing.objects.filter(
#             status='PROCESSING', updated_at__lt=timezone.now() - timedelta(hours=2)
#         ).count()
#         checks['stuck_mailings'] = stuck_count

#         healthy = all(v == 'ok' for k, v in checks.items() if k != 'stuck_mailings')
#         return Response(
#             {"status": "healthy" if healthy else "degraded", "checks": checks}, 
#             status=200 if healthy else 503
#         )


# @extend_schema_view(
#     list=extend_schema(description="List attachments."),
#     create=extend_schema(description="Upload an attachment.", request={'multipart/form-data': MailingAttachmentSerializer}),
#     destroy=extend_schema(description="Delete an attachment.")
# )
# class MailingAttachmentViewSet(viewsets.ModelViewSet):
#     serializer_class = MailingAttachmentSerializer
#     filter_backends = [DjangoFilterBackend]
#     filterset_fields = ['test_mailing']

#     def get_queryset(self):
#         if getattr(self, 'swagger_fake_view', False): 
#             return MailingAttachment.objects.none()
#         qs = MailingAttachment.objects.select_related('test_mailing')
#         if not self.request.user.is_staff: 
#             qs = qs.filter(test_mailing__created_by=self.request.user)
#         return qs

#     def perform_create(self, serializer):
#         mailing_id = self.request.data.get('test_mailing')
#         if mailing_id:
#             try:
#                 mailing = TestMailing.objects.get(pk=mailing_id)
#                 if not self.request.user.is_staff and mailing.created_by_id != self.request.user.id:
#                     raise ValidationError("Permission denied.")
#                 if not mailing.is_editable:
#                     raise ValidationError(f"Cannot modify mailing in status '{mailing.status}'.")
#             except TestMailing.DoesNotExist:
#                 raise ValidationError("Mailing does not exist.")
#         serializer.save()

