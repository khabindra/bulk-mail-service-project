import logging
from django.db.models import Count, Q
from rest_framework import viewsets, status, filters
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.exceptions import NotFound, ValidationError as DRFValidationError
from django_filters.rest_framework import DjangoFilterBackend

from drf_spectacular.utils import (
    extend_schema, extend_schema_view,
    OpenApiParameter, OpenApiExample, OpenApiResponse,
)
from drf_spectacular.types import OpenApiTypes

from .models import Campaign, Attachment
from .serializers import (
    CampaignListSerializer,
    CampaignDetailSerializer,
    CampaignActivateSerializer,
    CampaignTriggerSerializer,
    AttachmentSerializer,
)
from .permissions import IsStaffOrReadOnly
from .tasks import execute_campaign

logger = logging.getLogger(__name__)


class CampaignQuerySetMixin:
    """Shared annotated queryset logic."""

    def get_base_queryset(self):
        return Campaign.objects.select_related(
            'email_template', 'sender_email',
        ).annotate(
            active_recipient_count=Count(
                'recipients', filter=Q(recipients__is_active=True)
            ),
            attachment_count=Count('attachments'),
        )


@extend_schema_view(
    list=extend_schema(
        description="Lightweight list with annotated counts. No N+1 queries.",
        responses={200: CampaignListSerializer(many=True)},
    ),
    retrieve=extend_schema(
        description="Full details including recipients and attachments.",
        responses={200: CampaignDetailSerializer},
    ),
    create=extend_schema(
        description="Create as DRAFT. Use /activate/ to schedule.",
        request=CampaignDetailSerializer,
        responses={201: CampaignDetailSerializer},
    ),
    update=extend_schema(
        description="Update campaign. Status changes are ignored — use /activate/ or /pause/.",
        request=CampaignDetailSerializer,
        responses={200: CampaignDetailSerializer},
    ),
    partial_update=extend_schema(
        description="Partial update. Status changes are ignored.",
        request=CampaignDetailSerializer,
        responses={200: CampaignDetailSerializer},
    ),
    destroy=extend_schema(
        description="Delete draft/paused only. Active campaigns blocked.",
        responses={204: None, 400: OpenApiResponse(description="Campaign is active.")},
    ),
)
class CampaignViewSet(CampaignQuerySetMixin, viewsets.ModelViewSet):
    permission_classes = [IsAuthenticated, IsStaffOrReadOnly]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ['status', 'schedule_type']
    search_fields = ['name', 'description']
    ordering_fields = ['created_at', 'name', 'schedule_type', 'last_executed_at']
    throttle_scope = 'recurring-scheduled-mailing'

    def get_queryset(self):
        qs = self.get_base_queryset()
        if self.action != 'list':
            qs = qs.prefetch_related('attachments')
        return qs

    def get_serializer_class(self):
        if self.action == 'list':
            return CampaignListSerializer
        if self.action == 'activate':
            return CampaignActivateSerializer
        if self.action == 'trigger':
            return CampaignTriggerSerializer
        return CampaignDetailSerializer

    # ─── Lifecycle ─────────────────────────────────────────────────

    def perform_create(self, serializer):
        instance = serializer.save(status=Campaign.StatusChoices.DRAFT)
        logger.info("Campaign created: '%s' by user %s", instance.name, self.request.user.id)

    # C5 FIX: Removed perform_update entirely.
    # Status is now read-only in the serializer, so it's impossible
    # to change status via PUT/PATCH. No silent revert needed.

    def destroy(self, request, *args, **kwargs):
        instance = self.get_object()
        if instance.status == Campaign.StatusChoices.ACTIVE:  # Q1 FIX
            return Response(
                {"detail": "Cannot delete an active campaign. Pause it first."},
                status=status.HTTP_400_BAD_REQUEST
            )
        logger.info("Campaign deleted: '%s' by user %s", instance.name, request.user.id)
        return super().destroy(request, *args, **kwargs)

    # ─── Actions ───────────────────────────────────────────────────

    @extend_schema(
        methods=['POST'],
        request=CampaignActivateSerializer,
        responses={
            200: OpenApiResponse(
                response=OpenApiTypes.OBJECT,
                examples=[OpenApiExample('Success', value={
                    'detail': 'Campaign activated and scheduled.',
                    'crontab': '0 9 * * 1',
                })],
            ),
            409: OpenApiResponse(description="Already active."),
        },
        description="Activate and create Celery Beat schedule."
    )
    @action(detail=True, methods=['post'], url_path='activate', throttle_scope='recurring-schedule')
    def activate(self, request, pk=None):
        campaign = self.get_object()

        if campaign.status == Campaign.StatusChoices.ACTIVE:  # Q1 FIX
            return Response(
                {"detail": "Campaign is already active.", "crontab": campaign.get_crontab_expression()},
                status=status.HTTP_409_CONFLICT
            )

        serializer = CampaignActivateSerializer(data=request.data or {})
        serializer.is_valid(raise_exception=True)
        reset_count = serializer.validated_data.get('reset_execution_count', False)

        campaign.status = Campaign.StatusChoices.ACTIVE  # Q1 FIX
        try:
            campaign.save()

            if reset_count:
                campaign.execution_count = 0
                campaign.last_executed_at = None
                campaign.save(update_fields=['execution_count', 'last_executed_at'])

            logger.warning(
                "Campaign ACTIVATED: '%s' (id=%s) by user %s. Crontab: %s",
                campaign.name, campaign.id, request.user.id, campaign.get_crontab_expression()
            )

            return Response({
                "detail": "Campaign activated and scheduled.",
                "crontab": campaign.get_crontab_expression(),
                "schedule_type": campaign.get_schedule_type_display(),
                "execution_count": campaign.execution_count,
            })

        except Exception as e:
            raise DRFValidationError(str(e))

    @extend_schema(
        methods=['POST'],
        responses={
            200: OpenApiResponse(examples=[OpenApiExample('Success', value={'detail': 'Campaign paused.'})]),
            409: OpenApiResponse(description="Not active."),
        },
        request=None,
        description="Pause and disable schedule."
    )
    @action(detail=True, methods=['post'], url_path='pause')
    def pause(self, request, pk=None):
        campaign = self.get_object()

        if campaign.status != Campaign.StatusChoices.ACTIVE:  # Q1 FIX
            return Response(
                {"detail": "Only active campaigns can be paused."},
                status=status.HTTP_409_CONFLICT
            )

        campaign.status = Campaign.StatusChoices.PAUSED  # Q1 FIX
        campaign.save()
        logger.info("Campaign PAUSED: '%s' by user %s", campaign.name, request.user.id)
        return Response({"detail": "Campaign paused."})

    @extend_schema(
        methods=['POST'],
        request=CampaignTriggerSerializer,
        responses={
            200: OpenApiResponse(
                response=OpenApiTypes.OBJECT,
                examples=[
                    OpenApiExample('Triggered', value={'detail': 'Triggered.', 'task_id': '...'}),
                    OpenApiExample('Dry Run', value={'detail': 'Dry run passed.', 'recipient_count': 150}),
                ],
            ),
            409: OpenApiResponse(description="Not active."),
        },
        description="Manually trigger execution."
    )
    @action(detail=True, methods=['post'], url_path='trigger', throttle_scope='recurring-schedule')
    def trigger(self, request, pk=None):
        campaign = self.get_object()

        if campaign.status != Campaign.StatusChoices.ACTIVE:  # Q1 FIX
            return Response(
                {"detail": "Only active campaigns can be triggered."},
                status=status.HTTP_409_CONFLICT
            )

        serializer = CampaignTriggerSerializer(data=request.data or {})
        serializer.is_valid(raise_exception=True)

        dry_run = serializer.validated_data.get('dry_run', False)
        override_ids = serializer.validated_data.get('recipient_ids', None)

        recipient_count = (
            len(override_ids) if override_ids
            else getattr(campaign, 'active_recipient_count', 0)
            or campaign.recipients.filter(is_active=True).count()
        )

        if dry_run:
            if not hasattr(campaign.email_template, 'compiled_template'):
                raise DRFValidationError("Email template has no compiled version.")
            return Response({
                "detail": "Dry run passed.",
                "recipient_count": recipient_count,
                "attachment_count": campaign.attachments.count(),
            })

        async_result = execute_campaign.apply_async(
            args=[campaign.id],
            kwargs={'override_recipient_ids': override_ids} if override_ids else {}
        )

        logger.info(
            "Campaign TRIGGERED: '%s' by user %s. Task: %s",
            campaign.name, request.user.id, async_result.id
        )

        return Response({
            "detail": "Campaign execution triggered.",
            "task_id": async_result.id,
            "recipient_count": recipient_count,
        })


@extend_schema_view(
    list=extend_schema(
        parameters=[OpenApiParameter(name='campaign_pk', type=int, location='path')],
        responses={200: AttachmentSerializer(many=True)},
    ),
    create=extend_schema(
        parameters=[OpenApiParameter(name='campaign_pk', type=int, location='path')],
        request={'multipart/form-data': AttachmentSerializer},
        responses={201: AttachmentSerializer},
    ),
    destroy=extend_schema(
        parameters=[OpenApiParameter(name='campaign_pk', type=int, location='path')],
        responses={204: None},
    ),
)
class CampaignAttachmentViewSet(viewsets.ModelViewSet):
    serializer_class = AttachmentSerializer
    permission_classes = [IsAuthenticated, IsStaffOrReadOnly]
    filter_backends = [DjangoFilterBackend]
    filterset_fields = ['campaign']
    throttle_scope = 'file_upload'
    http_method_names = ['get', 'post', 'delete']

    def get_queryset(self):
        qs = Attachment.objects.select_related('campaign')
        campaign_id = self.kwargs.get('campaign_pk')
        if campaign_id:
            qs = qs.filter(campaign_id=campaign_id)
        return qs

    def perform_create(self, serializer):
        campaign_id = self.kwargs.get('campaign_pk')
        if not campaign_id:
            raise DRFValidationError("Campaign ID required in URL.")
        try:
            campaign = Campaign.objects.get(pk=campaign_id)
        except Campaign.DoesNotExist:
            raise NotFound("Campaign not found.")

        if campaign.status == Campaign.StatusChoices.ACTIVE:  # Q1 FIX
            raise DRFValidationError("Cannot modify attachments on active campaign.")

        serializer.save(campaign=campaign)

    def perform_destroy(self, instance):
        if instance.campaign.status == Campaign.StatusChoices.ACTIVE:  # Q1 FIX
            raise DRFValidationError("Cannot modify attachments on active campaign.")
        instance.delete()