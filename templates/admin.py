# from django.contrib import admin
# from .models import MailType, EmailTemplate, InlineImage

# @admin.register(MailType)
# class MailTypeAdmin(admin.ModelAdmin):
#     list_display = ('name',)
#     search_fields = ('name',)
#     ordering = ('name',)

# @admin.register(EmailTemplate)
# class EmailTemplateAdmin(admin.ModelAdmin):
#     list_display = ('mail_type', 'template_name', 'version', 'is_active', 'created_at', 'updated_at')
#     list_filter = ('mail_type', 'is_active', 'version')
#     search_fields = ('template_name',)
#     ordering = ('mail_type__name', '-version')
#     readonly_fields = ('created_at', 'updated_at')
#     fieldsets = (
#         (None, {
#             'fields': ('mail_type', 'template_name', 'description', 'template_content', 'available_variables', 'version', 'is_active')
#         }),
#         ('Metadata', {
#             'fields': ('created_at', 'updated_at'),
#             'classes': ('collapse',),
#         }),
#     )

# @admin.register(InlineImage)
# class InlineImageAdmin(admin.ModelAdmin):
#     list_display = ('content_id', 'mail_type', 'name', 'alt_text', 'display_order', 'is_active')
#     list_filter = ('mail_type', 'is_active')
#     search_fields = ('content_id', 'name', 'alt_text')
#     ordering = ('mail_type', 'display_order', 'content_id')
#     readonly_fields = ('public_id', 'version')
#     fieldsets = (
#         (None, {
#             'fields': ('mail_type', 'content_id', 'name', 'alt_text', 'display_order', 'is_active', 'image', 'public_id', 'version')
#         }),
#     )
#     # To display image previews in admin (optional)
#     def get_queryset(self, request):
#         qs = super().get_queryset(request)
#         return qs

#     # To show image preview (optional)
#     def thumbnail_preview(self, obj):
#         if obj.image:
#             return f'<img src="{obj.image.url}" style="max-height:100px; max-width:100px;" />'
#         return ""
#     thumbnail_preview.allow_tags = True
#     thumbnail_preview.short_description = 'Preview'


# +++++++++++++++++++++++++++++++++++++++++++++++++ latest +++++++++++++++++++++++++++=

from django.contrib import admin
from django.utils.html import format_html
from .models import MailType, EmailTemplate, InlineImage

@admin.register(MailType)
class MailTypeAdmin(admin.ModelAdmin):
    list_display = ('name',)
    search_fields = ('name',)
    ordering = ('name',)

@admin.register(EmailTemplate)
class EmailTemplateAdmin(admin.ModelAdmin):
    list_display = ('template_name', 'mail_type', 'subject', 'version', 'is_active', 'updated_at')
    list_filter = ('mail_type', 'is_active', 'version')
    search_fields = ('template_name', 'mail_type__name')
    ordering = ('-updated_at',)
    readonly_fields = ('created_at', 'updated_at')
    
    # PERFORMANCE: Eager load mail_type to prevent N+1 queries
    def get_queryset(self, request):
        qs = super().get_queryset(request)
        return qs.select_related('mail_type')

    fieldsets = (
        (None, {
            'fields': ('mail_type', 'template_name', 'subject', 'description', 'template_content','default_context', 'available_variables', 'version', 'is_active')
        }),
        ('Metadata', {
            'fields': ('created_at', 'updated_at'),
            'classes': ('collapse',),
        }),
    )

@admin.register(InlineImage)
class InlineImageAdmin(admin.ModelAdmin):
    list_display = ('content_id', 'mail_type', 'image_preview', 'display_order', 'is_active', 'version')
    list_filter = ('mail_type', 'is_active', 'version')
    search_fields = ('content_id', 'name')
    ordering = ('mail_type', 'display_order')
    readonly_fields = ('image_preview_large',)
    
    # PERFORMANCE: Eager load mail_type
    def get_queryset(self, request):
        qs = super().get_queryset(request)
        return qs.select_related('mail_type')

    def image_preview(self, obj):
        """Small preview for list view"""
        if obj.image:
            return format_html('<img src="{}" style="max-height:50px; max-width:50px;" />', obj.image.url)
        return "-"
    image_preview.short_description = 'Preview'

    def image_preview_large(self, obj):
        """Large preview for detail view"""
        if obj.image:
            return format_html('<img src="{}" style="max-height:300px;" />', obj.image.url)
        return "No Image"
    image_preview_large.short_description = 'Image Preview'