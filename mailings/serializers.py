# mailings/serializers.py
from rest_framework import serializers
from .utils.parsers import parse_client_ids


class BulkSendSerializer(serializers.Serializer):
    idempotency_key = serializers.CharField(
        required=False, 
        help_text="UUID to prevent duplicate sends on double-click."
    )
    client_ids = serializers.CharField(
        required=True, 
        help_text="Comma-separated IDs (e.g., '1,2,3') or JSON array string."
    )
    mail_type_id = serializers.IntegerField()
    email_template_id = serializers.IntegerField()
    sender_id = serializers.IntegerField()
    subject = serializers.CharField(required=False, allow_blank=True)
    campaign_name = serializers.CharField(required=False, allow_blank=True)
    
    # ✅ ADD: Document attachments for Swagger/OpenAPI
    attachments = serializers.ListField(
        child=serializers.FileField(),
        required=False,
        write_only=True,
        help_text="Files to attach to emails (max 10MB each)."
    )

    def validate_client_ids(self, value):
        ids = parse_client_ids(value)
        if not ids:
            raise serializers.ValidationError("No valid client IDs provided.")
        if len(ids) > 50000: 
            raise serializers.ValidationError("Cannot exceed 50,000 recipients per request.")
        return ids
    
    def validate_attachments(self, value):
        """Validate attachment files if provided."""
        if not value:
            return value
        
        max_size = 10 * 1024 * 1024  # 10MB
        errors = []
        
        for f in value:
            if hasattr(f, 'size') and f.size > max_size:
                errors.append(f"{f.name}: exceeds 10MB limit")
        
        if errors:
            raise serializers.ValidationError(errors)
        
        return value


class EmailPreviewSerializer(serializers.Serializer):
    client_id = serializers.IntegerField()
    email_template_id = serializers.IntegerField()
    sender_id = serializers.IntegerField()
    message = serializers.CharField(required=False, allow_blank=True)


class EmailPreviewResponseSerializer(serializers.Serializer):
    subject = serializers.CharField()
    recipient_email = serializers.CharField()
    recipient_name = serializers.CharField()
    html_content = serializers.CharField()