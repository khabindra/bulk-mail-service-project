from rest_framework import serializers
from django.core.exceptions import ValidationError
from drf_spectacular.utils import extend_schema_field   # <-- ADD THIS

from .models import MailType, EmailTemplate, InlineImage


# EmailTemplate
class EmailTemplateSerializer(serializers.ModelSerializer):
    class Meta:
        model = EmailTemplate
        fields = [
            'id', 'mail_type', 'subject', 'description',
            'template_name', 'template_content',
            'available_variables', 'version',
            'is_active', 'created_at', 'updated_at'
        ]
        read_only_fields = ['version', 'created_at', 'updated_at']


class EmailTemplateDetailSerializer(serializers.ModelSerializer):
    mail_type_name = serializers.CharField(source='mail_type.name', read_only=True)

    class Meta:
        model = EmailTemplate
        fields = '__all__'


# MAILTYPE
class MailTypeSerializer(serializers.ModelSerializer):
    template = EmailTemplateSerializer(read_only=True)

    class Meta:
        model = MailType
        fields = ['id', 'name', 'template']


# InlineImageSerializer
class InlineImageSerializer(serializers.ModelSerializer):
    image = serializers.ImageField(write_only=True, required=False)
    image_url = serializers.SerializerMethodField()

    class Meta:
        model = InlineImage
        fields = [
            'id',
            'mail_type',
            'content_id',
            'image',        # upload
            'image_url',    # read
            'public_id',
            'version',
            'name',
            'alt_text',
            'display_order',
            'is_active',
        ]
        read_only_fields = ['public_id', 'version']

    @extend_schema_field(serializers.URLField())   # <-- ADD THIS DECORATOR
    def get_image_url(self, obj):
        if not obj.image:
            return None

        return obj.image.build_url(
            secure=True,
            transformation=[
                {'width': 600, 'crop': 'limit'},
                {'quality': 'auto'},
                {'fetch_format': 'png'},
            ]
        )

    def validate_content_id(self, value):
        if " " in value:
            raise serializers.ValidationError("CID must not contain spaces")
        return value