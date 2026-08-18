from rest_framework import generics, status, filters, permissions
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.viewsets import ModelViewSet
from django_filters.rest_framework import DjangoFilterBackend
from django.db import IntegrityError, transaction
from django.http import HttpResponse
from django.utils.html import escape
from django.views.decorators.clickjacking import xframe_options_sameorigin
from rest_framework.exceptions import ValidationError

# ✅ ADDED: Swagger Documentation Tools
from drf_spectacular.utils import extend_schema, extend_schema_view, OpenApiParameter, OpenApiResponse, OpenApiExample
from drf_spectacular.types import OpenApiTypes

from .models import MailType, EmailTemplate, InlineImage
from .serializers import (
    MailTypeSerializer, 
    EmailTemplateSerializer, 
    EmailTemplateDetailSerializer,
    InlineImageSerializer
)
from rest_framework.pagination import PageNumberPagination


class StandardResultsSetPagination(PageNumberPagination):
    page_size = 20
    page_size_query_param = 'page_size'
    max_page_size = 100


# --- 1. MAIL TYPE VIEWS (Converted to ViewSet for clean docs) ---

# ✅ ADDED: Class-level documentation
@extend_schema_view(
    list=extend_schema(
        description="Retrieve a list of all email categories (Mail Types).",
        responses={200: MailTypeSerializer(many=True)}
    ),
    create=extend_schema(
        description="Create a new email category.",
        request=MailTypeSerializer,
        responses={201: MailTypeSerializer}
    ),
    retrieve=extend_schema(
        description="Retrieve details of a specific Mail Type including its active template.",
        responses={200: MailTypeSerializer}
    ),
    update=extend_schema(
        description="Update a Mail Type's name.",
        request=MailTypeSerializer,
        responses={200: MailTypeSerializer}
    ),
    destroy=extend_schema(
        description="Delete a Mail Type. Note: This will cascade delete all associated templates and images.",
        responses={204: None}
    )
)
class MailTypeViewSet(ModelViewSet):
    """
    API endpoint to manage Email Categories (Mail Types). 
    Each Mail Type acts as a container for Templates and Inline Images.
    """
    queryset = MailType.objects.all().select_related('template').order_by('name')
    serializer_class = MailTypeSerializer
    permission_classes = [permissions.IsAuthenticated]
    pagination_class = StandardResultsSetPagination
    filter_backends = [filters.SearchFilter]
    search_fields = ['name']


# --- 2. EMAIL TEMPLATE VIEWS ---

# ✅ ADDED: Explicit mapping for dynamic serializer swapping
@extend_schema_view(
    list=extend_schema(
        description="List all email templates. Uses a lightweight serializer.",
        responses={200: EmailTemplateSerializer(many=True)}
    ),
    retrieve=extend_schema(
        description="Retrieve full template details including the mail type name.",
        responses={200: EmailTemplateDetailSerializer}
    ),
    create=extend_schema(
        description="Create a new email template.",
        request=EmailTemplateSerializer,
        responses={201: EmailTemplateSerializer}
    ),
    update=extend_schema(
        description="Update a template. IMMUTABILITY RULE: Updating content automatically creates a new version (v+1) and deactivates the old one.",
        request=EmailTemplateSerializer,
        responses={200: EmailTemplateSerializer}
    ),
    destroy=extend_schema(
        description="Delete an email template.",
        responses={204: None}
    )
)
class EmailTemplateViewSet(ModelViewSet):
    """
    API endpoint for managing Email Templates.
    Supports versioning: updating an active template automatically creates a new version 
    and deactivates the previous one to preserve history.
    """
    queryset = EmailTemplate.objects.select_related('mail_type')
    permission_classes = [permissions.IsAuthenticated]
    pagination_class = StandardResultsSetPagination
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ['mail_type', 'is_active', 'version']
    search_fields = ['subject', 'description']
    ordering_fields = ['created_at', 'version']

    def get_serializer_class(self):
        if self.action == 'retrieve':
            return EmailTemplateDetailSerializer
        return EmailTemplateSerializer

    @transaction.atomic
    def perform_update(self, serializer):
        old = self.get_object()

        new_instance = EmailTemplate.objects.create(
            mail_type=old.mail_type,
            subject=serializer.validated_data.get('subject', old.subject),
            template_name=serializer.validated_data.get('template_name', old.template_name),
            template_content=serializer.validated_data.get('template_content', old.template_content),
            description=serializer.validated_data.get('description', old.description),
            available_variables=serializer.validated_data.get('available_variables', old.available_variables),
            version=old.version + 1,
            is_active=True,
        )

        old.is_active = False
        old.save(update_fields=['is_active'])

    # ✅ ADDED: Explicit Schema for HTML Preview
    @extend_schema(
        methods=['GET'],
        operation_id="preview_email_template",
        description="Returns the raw HTML rendering of the template in the browser. Replaces CID image tags with Cloudinary URLs. Pass variables as query parameters.",
        parameters=[
            OpenApiParameter(name='company_name', type=str, location='query', description="Company name"),
            OpenApiParameter(name='message', type=str, location='query', description="Custom message"),
            OpenApiParameter(name='sender_name', type=str, location='query', description="Sender name"),
            OpenApiParameter(name='sender_email', type=str, location='query', description="Sender email"),
        ],
        responses={
            200: OpenApiResponse(
                description="Raw HTML string rendered for browser viewing.",
                response=OpenApiTypes.STR  # ✅ This is all you need for HTML responses
            )
        }
    )
    @action(detail=True, methods=['get'], url_path='preview')
    @xframe_options_sameorigin
    def preview_browser(self, request, pk=None):
        template = self.get_object()

        context_data = {
            "company_name": request.GET.get("company_name", "Demo Company"),
            "message": request.GET.get("message", "Congratulations on your success!"),
            "sender_name": request.GET.get("sender_name", "CorpolaTech Team"),
            "sender_email": request.GET.get("sender_email", "no-reply@corpola.com"),
            "congrats_image_cid": "congrats_image_cid",
        }

        html = template.render_template(context_data)

        images = InlineImage.objects.filter(mail_type=template.mail_type, is_active=True)
        for img in images:
            if img.image:
                html = html.replace(
                    f"cid:{img.content_id}",
                    img.image.build_url(secure=True, transformation=[{'width': 600, 'crop': 'limit'}, {'quality': 'auto'}, {'fetch_format': 'png'}])
                )

        return HttpResponse(html)


# --- 3. INLINE IMAGE VIEWS ---

# ✅ ADDED: Documenting custom versioning behavior
@extend_schema_view(
    list=extend_schema(description="List active inline images."),
    create=extend_schema(
        description="Upload a new inline image. Automatically assigns a version (v1).",
        request=InlineImageSerializer,
        responses={201: InlineImageSerializer}
    ),
    retrieve=extend_schema(description="Retrieve details of a specific inline image."),
    update=extend_schema(
        description="Update an inline image. IMMUTABILITY RULE: Uploading a new file creates a new version (v+1). Updating metadata (name, alt_text) updates in-place. Changing content_id or mail_type is forbidden.",
        request=InlineImageSerializer,
        responses={200: InlineImageSerializer}
    ),
    destroy=extend_schema(
        description="Soft-delete an inline image (sets is_active=False).",
        responses={204: None}
    )
)
class InlineImageViewSet(ModelViewSet):
    """
    API endpoint for managing Inline Images associated with Mail Types.
    Supports file versioning: uploading a new file creates a new version.
    """
    queryset = InlineImage.objects.select_related('mail_type')
    serializer_class = InlineImageSerializer
    permission_classes = [permissions.IsAuthenticated]
    pagination_class = StandardResultsSetPagination

    filter_backends = [DjangoFilterBackend, filters.SearchFilter]
    filterset_fields = ['mail_type', 'is_active']
    search_fields = ['content_id', 'name']

    def get_queryset(self):
        return super().get_queryset().filter(is_active=True)
    
    def perform_create(self, serializer):
        instance = serializer.save()
        instance.public_id = instance.image.public_id
        instance.save(update_fields=['public_id'])

    def perform_update(self, serializer):
        old = self.get_object()
        validated_data = serializer.validated_data

        new_image = validated_data.get('image')

        if 'content_id' in validated_data and validated_data['content_id'] != old.content_id:
            raise ValidationError("content_id cannot be changed once created")

        if 'mail_type' in validated_data and validated_data['mail_type'] != old.mail_type:
            raise ValidationError("mail_type cannot be changed once created")

        # CASE 1: IMAGE + METADATA -> NEW VERSION
        if new_image:
            new_instance = InlineImage.objects.create(
                mail_type=old.mail_type,
                content_id=old.content_id,
                image=new_image,
                name=validated_data.get('name', old.name),
                alt_text=validated_data.get('alt_text', old.alt_text),
                display_order=validated_data.get('display_order', old.display_order),
                is_active=True,
                version=old.version + 1,
            )

            new_instance.public_id = new_instance.image.public_id
            new_instance.save(update_fields=['public_id'])

            old.is_active = False
            old.save(update_fields=['is_active'])
            return

        # CASE 2: METADATA-ONLY UPDATE (NO NEW VERSION)
        metadata_fields = ['name', 'alt_text', 'display_order', 'is_active']
        updated = False

        for field in metadata_fields:
            if field in validated_data:
                setattr(old, field, validated_data[field])
                updated = True

        if not updated:
            raise ValidationError("No valid fields provided for update")

        old.save()

    def perform_destroy(self, instance):
        instance.is_active = False
        instance.save(update_fields=['is_active'])