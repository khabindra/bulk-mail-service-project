import logging
from django.conf import settings
from rest_framework.response import Response
from rest_framework import status, views
from rest_framework.permissions import IsAuthenticated
from django.core.cache import cache

from drf_spectacular.utils import extend_schema, OpenApiExample, OpenApiResponse
from drf_spectacular.types import OpenApiTypes

from .serializers import BulkSendSerializer, EmailPreviewSerializer, EmailPreviewResponseSerializer
from .services.dispatch_service import prepare_and_dispatch_bulk_send
from .services.preview_service import render_preview_html
from .services.context_builder import build_email_context
from users.permissions import IsAdminUserRole

from client.models import Client
from templates.models import EmailTemplate
from mailings.models import SenderEmail

from django.shortcuts import get_object_or_404

logger = logging.getLogger(__name__)

IDEMPOTENCY_WINDOW = getattr(settings, 'IDEMPOTENCY_WINDOW', 30)


class BulkSendAPIView(views.APIView):
    permission_classes = [IsAuthenticated, IsAdminUserRole]
    throttle_scope = 'bulk_send_trigger'

    @extend_schema(
        tags=["Mailings"],
        operation_id="trigger_bulk_send",
        request={'multipart/form-data': BulkSendSerializer},
        responses={
            202: OpenApiResponse(
                response=OpenApiTypes.OBJECT,
                examples=[OpenApiExample("Success", value={
                    "success": True,
                    "message": "Email processing started.",
                    "total_recipients": 1000,
                    "mailing_id": 1
                })]
            ),
            400: OpenApiResponse(description="Validation Error."),
            409: OpenApiResponse(description="Duplicate request."),
        },
        description="Triggers an immediate bulk email send."
    )
    def post(self, request):
        serializer = BulkSendSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        idempotency_key = data.get('idempotency_key')
        lock_key = f"bulk_send_lock_{idempotency_key}"
        
        if idempotency_key:
            if not cache.add(lock_key, True, timeout=IDEMPOTENCY_WINDOW):
                return Response(
                    {"error": "Duplicate request detected."},
                    status=status.HTTP_409_CONFLICT
                )

        dynamic_vars = {
            key.replace("var_", "", 1): value 
            for key, value in request.data.items() 
            if key.startswith("var_")
        }

        try:
            result = prepare_and_dispatch_bulk_send(
                client_ids=data['client_ids'],
                mail_type_id=data['mail_type_id'],
                email_template_id=data['email_template_id'],
                sender_id=data['sender_id'],
                subject=data.get('subject'),
                uploaded_files=request.FILES.getlist('attachments'),
                user_id=request.user.id,
                campaign_name=data.get('campaign_name', 'Instant Bulk Send'),
                dynamic_vars=dynamic_vars,
            )

            if not result.get("success"):
                if idempotency_key:
                    cache.delete(lock_key)
                return Response(
                    {"error": result.get("error")}, 
                    status=status.HTTP_400_BAD_REQUEST
                )

            # FIX: Removed non-existent result['chunks_dispatched'] reference
            return Response({
                "success": True,
                "message": "Email processing started.",
                **result
            }, status=status.HTTP_202_ACCEPTED)

        except Exception as e:
            if idempotency_key:
                cache.delete(lock_key)
                
            logger.exception(
                "Instant Bulk Send failed unexpectedly",
                extra={"user_id": request.user.id}
            )
            return Response(
                {"error": f"An unexpected error occurred: {str(e)}"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )


class EmailPreviewAPIView(views.APIView):
    permission_classes = [IsAuthenticated, IsAdminUserRole]

    @extend_schema(
        tags=["Mailings"],
        operation_id="generate_email_preview",
        request=EmailPreviewSerializer,
        responses={
            200: EmailPreviewResponseSerializer,
            404: OpenApiResponse(description="Client, Template, or Sender not found."),
        },
        description="Generates browser-renderable HTML preview of an email."
    )
    def post(self, request):
        serializer = EmailPreviewSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        client = get_object_or_404(Client, id=data['client_id'])
        email_template = get_object_or_404(EmailTemplate.objects.select_related('mail_type'), id=data['email_template_id'])
        sender = get_object_or_404(SenderEmail, id=data['sender_id'])

        context = build_email_context(
            client=client, 
            sender=sender, 
            message=data.get('message', ''), 
            request_data={}
        )

        final_html = render_preview_html(email_template, context)

        response_serializer = EmailPreviewResponseSerializer({
            "subject": email_template.subject,
            "recipient_email": client.contact_email,
            "recipient_name": client.company_name,
            "html_content": final_html
        })
        
        return Response(response_serializer.data, status=status.HTTP_200_OK)