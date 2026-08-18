from rest_framework import serializers
from django.db.models import Count, Q
from drf_spectacular.utils import extend_schema_field

from .models import Campaign, Attachment
from client.models import Client


class AttachmentSerializer(serializers.ModelSerializer):
    size_display = serializers.SerializerMethodField()

    class Meta:
        model = Attachment
        fields = [
            'id', 'file', 'filename', 'content_type',
            'size', 'size_display', 'uploaded_at'
        ]
        read_only_fields = [
            'id', 'filename', 'content_type', 'size', 'size_display', 'uploaded_at'
        ]

    @extend_schema_field(serializers.CharField())
    def get_size_display(self, obj):
        if obj.size:
            size = float(obj.size)
            for unit in ['B', 'KB', 'MB', 'GB']:
                if size < 1024.0:
                    return f"{size:.1f} {unit}"
                size /= 1024.0
        return "0 B"

    def validate_file(self, value):
        max_size = 10 * 1024 * 1024
        if value.size > max_size:
            raise serializers.ValidationError(
                f"File size cannot exceed {max_size // (1024*1024)}MB."
            )
        return value


class AttachmentLimitedSerializer(AttachmentSerializer):
    class Meta(AttachmentSerializer.Meta):
        fields = ['id', 'filename', 'content_type', 'size', 'size_display']


class CampaignListSerializer(serializers.ModelSerializer):
    schedule_type_display = serializers.CharField(source='get_schedule_type_display', read_only=True)
    status_display = serializers.CharField(source='get_status_display', read_only=True)
    crontab_preview = serializers.CharField(source='get_crontab_expression', read_only=True)
    recipient_count = serializers.IntegerField(read_only=True)
    attachment_count = serializers.IntegerField(read_only=True)
    last_executed_at = serializers.DateTimeField(read_only=True)
    execution_count = serializers.IntegerField(read_only=True)

    class Meta:
        model = Campaign
        fields = [
            'id', 'name', 'description',
            'schedule_type', 'schedule_type_display',
            'status', 'status_display',
            'send_at_time', 'crontab_preview',
            'recipient_count', 'attachment_count',
            'last_executed_at', 'execution_count',
            'created_at',
        ]


class CampaignDetailSerializer(CampaignListSerializer):
    recipient_ids = serializers.PrimaryKeyRelatedField(
        source='recipients',
        queryset=Client.objects.none(),
        many=True,
        required=False,
        write_only=True,
    )
    recipients = serializers.PrimaryKeyRelatedField(many=True, read_only=True)
    attachments = AttachmentLimitedSerializer(many=True, read_only=True)

    class Meta(CampaignListSerializer.Meta):
        fields = CampaignListSerializer.Meta.fields + [
            'email_template',
            'sender_email',
            'recipient_ids',
            'recipients',
            'context_variables',
            'target_weekday',
            'day_of_month',
            'month_of_year',
            'attachments',
            'updated_at',
        ]
        # C5 FIX: Status is read-only. Cannot be changed via PUT/PATCH.
        # Must use /activate/ or /pause/ endpoints.
        read_only_fields = ['status']

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if 'request' in self.context and self.context['request'].method in ('POST', 'PUT', 'PATCH'):
            self.fields['recipient_ids'].queryset = Client.objects.filter(is_active=True)

    def validate_email_template(self, value):
        if hasattr(value, 'is_active') and not value.is_active:
            raise serializers.ValidationError("Cannot select an inactive template.")
        return value

    def validate_context_variables(self, value):
        if value is not None and not isinstance(value, dict):
            raise serializers.ValidationError("Context variables must be a JSON object.")
        return value if isinstance(value, dict) else {}

    def validate_sender_email(self, value):
        if value and hasattr(value, 'is_verified') and not value.is_verified:
            raise serializers.ValidationError("Cannot select an unverified sender email.")
        return value


class CampaignActivateSerializer(serializers.Serializer):
    reset_execution_count = serializers.BooleanField(
        default=False, write_only=True,
        help_text="Reset execution count to 0."
    )


class CampaignTriggerSerializer(serializers.Serializer):
    dry_run = serializers.BooleanField(
        default=False, write_only=True,
        help_text="Validate without sending."
    )
    recipient_ids = serializers.ListField(
        child=serializers.IntegerField(),
        required=False, write_only=True,
        help_text="Override recipients for this run only."
    )