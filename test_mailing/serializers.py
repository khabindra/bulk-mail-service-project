# from rest_framework import serializers
# from django.utils import timezone
# from django.db import transaction
# from .models import TestMailing, MailingAttachment
# from .services.test_mailing_service import TestMailingService
# from client.models import Client

# MAX_RECIPIENTS_LIMIT = 50000  # FIX 7: Hard limit to prevent queue flooding

# class MailingAttachmentSerializer(serializers.ModelSerializer):
#     class Meta:
#         model = MailingAttachment
#         fields = ['id', 'file', 'file_size', 'uploaded_at']
#         read_only_fields = ['id', 'file_size', 'uploaded_at']

#     def validate_file(self, value):
#         max_size = 10 * 1024 * 1024  
#         if value.size > max_size:
#             raise serializers.ValidationError(f"File size cannot exceed {max_size // (1024*1024)}MB.")
#         return value


# class TestMailingSerializer(serializers.ModelSerializer):
#     attachments = MailingAttachmentSerializer(
#         source='mailing_attachments', many=True, read_only=True,
#         help_text="Read-only. Upload attachments via /api/mailings/{id}/attachments/"
#     )
#     recipient_ids = serializers.PrimaryKeyRelatedField(
#         source='recipients', 
#         queryset=Client.objects.none(), 
#         many=True, 
#         required=False, 
#         write_only=True,
#         help_text="List of Client IDs to send the mailing to."
#     )

#     class Meta:
#         model = TestMailing
#         fields = [
#             'id', 'name', 'description', 'status', 'scheduled_time',
#             'mail_type', 'email_template', 'sender_email', 'recipients',
#             'recipient_ids', 'test_email', 'context_variables',
#             'attachments', 'created_at', 'created_by', 'error_message',
#             'total_recipients', 'successful_sends', 'failed_sends',
#             'total_chunks', 'completed_chunks', 'completed_at'
#         ]
#         read_only_fields = (
#             'created_by', 'status', 'error_message', 'created_at',   # ✅ 'status' added
#             'total_recipients', 'successful_sends', 'failed_sends',
#             'total_chunks', 'completed_chunks', 'completed_at'
#         )

#     def __init__(self, *args, **kwargs):
#         super().__init__(*args, **kwargs)
#         if 'request' in self.context:
#             self.fields['recipient_ids'].queryset = Client.objects.filter(is_active=True)

#     def validate(self, data):
#         instance = self.instance
        
#         recipients = data.get('recipients')
#         if recipients is None and instance:
#             recipients = instance.recipients.all()
            
#         test_email = data.get('test_email')
#         if test_email is None and instance:
#             test_email = instance.test_email

#         # FIX 10: Strict length check to prevent QuerySet truthy edge cases
#         recipient_count = len(recipients) if recipients is not None else 0
#         if recipient_count == 0 and not test_email:
#             raise serializers.ValidationError(
#                 {"non_field_errors": ["You must provide at least one recipient or a test email."]}
#             )

#         # FIX 7: Hard limit on recipient count
#         if recipient_count > MAX_RECIPIENTS_LIMIT:
#             raise serializers.ValidationError(
#                 {"non_field_errors": [f"Cannot exceed {MAX_RECIPIENTS_LIMIT} recipients per mailing."]}
#             )

#         if instance and instance.is_terminal_state:
#             raise serializers.ValidationError(f"Cannot modify a mailing that is {instance.status}.")
            
#         if instance and instance.status in ('PROCESSING', 'DISPATCHED'):
#             raise serializers.ValidationError("Cannot modify a mailing currently being processed.")
            
#         return data

#     def validate_scheduled_time(self, value):
#         if not self.instance and value and value < timezone.now():
#             raise serializers.ValidationError("Cannot schedule a mailing in the past.")
#         return value

#     def validate_context_variables(self, value):
#         if value is not None:
#             if not isinstance(value, dict):
#                 raise serializers.ValidationError("Context variables must be a JSON object.")
#             for key, val in value.items():
#                 if not isinstance(val, (str, int, float, bool)) or val is None:
#                     raise serializers.ValidationError(
#                         f"Context value for '{key}' must be a primitive type (string, number, boolean)."
#                     )
#         return value or {}

#     def create(self, validated_data):
#         request = self.context.get('request')
#         if request and hasattr(request, 'user'):
#             validated_data['created_by'] = request.user
        
#         with transaction.atomic():
#             instance = super().create(validated_data)
#             if instance.status == 'SCHEDULED':
#                 TestMailingService.sync_schedule(instance)
#             return instance

#     def update(self, instance, validated_data):
#         recipients = validated_data.pop('recipients', None)

#         with transaction.atomic():
#             locked_instance = TestMailing.objects.select_for_update().get(pk=instance.pk)

#             # ✅ Track exactly which fields changed
#             updated_fields = []
#             for attr, value in validated_data.items():
#                 setattr(locked_instance, attr, value)
#                 updated_fields.append(attr)

#             if updated_fields:
#                 locked_instance.save(update_fields=updated_fields)

#             if recipients is not None:
#                 locked_instance.recipients.set(recipients)

#             TestMailingService.sync_schedule(locked_instance)
#             locked_instance.refresh_from_db()
#             return locked_instance


import re
from rest_framework import serializers
from django.utils import timezone
from django.db import transaction
from django.conf import settings
from .models import TestMailing, MailingAttachment
from .services.test_mailing_service import TestMailingService
from client.models import Client

MAX_RECIPIENTS_LIMIT = settings.MAILING_SETTINGS['MAX_RECIPIENTS_LIMIT']

# Fix: CONTEXT_KEY_REGEX properly defined and imported
CONTEXT_KEY_REGEX = re.compile(r'^[a-zA-Z_][a-zA-Z0-9_]*$')

class MailingAttachmentSerializer(serializers.ModelSerializer):
    class Meta:
        model = MailingAttachment
        fields = ['id', 'file', 'file_size', 'uploaded_at']
        read_only_fields = ['id', 'file_size', 'uploaded_at']

    def validate_file(self, value):
        max_size = 10 * 1024 * 1024  
        if value.size > max_size:
            raise serializers.ValidationError(f"File size cannot exceed {max_size // (1024*1024)}MB.")
        return value

class TestMailingSerializer(serializers.ModelSerializer):
    attachments = MailingAttachmentSerializer(source='mailing_attachments', many=True, read_only=True)
    recipient_ids = serializers.PrimaryKeyRelatedField(
        source='recipients', queryset=Client.objects.none(), many=True, required=False, write_only=True
    )

    class Meta:
        model = TestMailing
        fields = [
            'id', 'name', 'description', 'status', 'scheduled_time',
            'mail_type', 'email_template', 'sender_email', 'recipients',
            'recipient_ids', 'test_email', 'context_variables',
            'attachments', 'created_at', 'created_by', 'error_message',
            'total_recipients', 'successful_sends', 'failed_sends',
            'total_chunks', 'completed_chunks', 'completed_at'
        ]
        read_only_fields = (
            'created_by', 'status', 'error_message', 'created_at',
            'total_recipients', 'successful_sends', 'failed_sends',
            'total_chunks', 'completed_chunks', 'completed_at'
        )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if 'request' in self.context:
            self.fields['recipient_ids'].queryset = Client.objects.filter(is_active=True)

    def validate(self, data):
        instance = self.instance
        recipients = data.get('recipients') or (instance.recipients.all() if instance else [])
        test_email = data.get('test_email') or (instance.test_email if instance else None)

        recipient_count = len(recipients)
        if recipient_count == 0 and not test_email:
            raise serializers.ValidationError({"non_field_errors": ["You must provide at least one recipient or a test email."]})
        if recipient_count > MAX_RECIPIENTS_LIMIT:
            raise serializers.ValidationError({"non_field_errors": [f"Cannot exceed {MAX_RECIPIENTS_LIMIT} recipients per mailing."]})

        if instance and instance.is_terminal_state:
            raise serializers.ValidationError(f"Cannot modify a mailing that is {instance.status}.")
        if instance and instance.status in ('PROCESSING', 'DISPATCHED'):
            raise serializers.ValidationError("Cannot modify a mailing currently being processed.")
        return data

    def validate_scheduled_time(self, value):
        if not self.instance and value and value < timezone.now():
            raise serializers.ValidationError("Cannot schedule a mailing in the past.")
        return value

    def validate_context_variables(self, value):
        if value is not None:
            if not isinstance(value, dict):
                raise serializers.ValidationError("Context variables must be a JSON object.")
            for key, val in value.items():
                if not CONTEXT_KEY_REGEX.match(key):
                    raise serializers.ValidationError(f"Context key '{key}' is invalid. Use only letters, numbers, and underscores.")
                if not isinstance(val, (str, int, float, bool)) or val is None:
                    raise serializers.ValidationError(f"Context value for '{key}' must be a primitive type.")
        return value or {}

    def create(self, validated_data):
        request = self.context.get('request')
        if request and hasattr(request, 'user'):
            validated_data['created_by'] = request.user
        with transaction.atomic():
            instance = super().create(validated_data)
            if instance.status == 'SCHEDULED':
                TestMailingService.sync_schedule(instance)
            return instance

    def update(self, instance, validated_data):
        recipients = validated_data.pop('recipients', None)
        with transaction.atomic():
            locked_instance = TestMailing.objects.select_for_update().get(pk=instance.pk)
            updated_fields = []
            for attr, value in validated_data.items():
                setattr(locked_instance, attr, value)
                updated_fields.append(attr)
            if updated_fields:
                locked_instance.save(update_fields=updated_fields)
            if recipients is not None:
                locked_instance.recipients.set(recipients)
            TestMailingService.sync_schedule(locked_instance)
            locked_instance.refresh_from_db()
            return locked_instance