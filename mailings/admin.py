from django.contrib import admin
from django.utils.html import format_html
from django.urls import path
from .models import SenderEmail, Mailing, MailingAttachment, MailingProcessedChunk, MailLog
from mailings.services.dispatch_service import dispatch_existing_mailing
from mailings.admin_views import preview_mailing_html

@admin.register(SenderEmail)
class SenderEmailAdmin(admin.ModelAdmin):
    list_display = ('name', 'email')

class MailingAttachmentInline(admin.TabularInline):
    model = MailingAttachment
    readonly_fields = ('filename', 'content_type', 'file_size', 'uploaded_at')
    extra = 0

@admin.register(Mailing)
class MailingAdmin(admin.ModelAdmin):
    list_display = ('name', 'status_colored', 'progress_bar', 'created_by', 'created_at')
    list_filter = ('status', 'mail_type', 'created_at') # FIX: Removed 'mailing' to prevent slow FK JOINs
    search_fields = ('name', 'description')
    readonly_fields = ('status', 'total_recipients', 'total_chunks', 'completed_chunks', 
                       'successful_sends', 'failed_sends', 'completed_at', 'error_message')
    inlines = [MailingAttachmentInline]
    actions = ['dispatch_selected']
    change_form_template = "admin/mailings/mailing_change_form.html"

    def get_urls(self):
        urls = super().get_urls()
        custom_urls = [
            path('<path:object_id>/preview/', self.admin_site.admin_view(preview_mailing_html), name='mailing_preview'),
        ]
        return custom_urls + urls
    
    def status_colored(self, obj):
        colors = {'DRAFT': 'gray', 'PROCESSING': 'orange', 'DISPATCHED': 'blue', 'COMPLETED': 'green', 'FAILED': 'red', 'CANCELLED': 'black'}
        return format_html('<span style="color: {};">{}</span>', colors.get(obj.status, 'gray'), obj.status)
    status_colored.short_description = 'Status'

    def progress_bar(self, obj):
        if obj.total_chunks == 0: 
            return "N/A"
        pct = obj.get_progress_percentage()
        color = 'green' if pct == 100 else 'orange'
        return format_html(
            '<div style="width:100px;background:#eee;border-radius:5px;">'
            '<div style="width:{}%;background:{};height:10px;border-radius:5px;"></div>'
            '</div> {}%',
            pct, color, pct
        )
    progress_bar.short_description = 'Progress'

    @admin.action(description='Dispatch selected DRAFT mailings')
    def dispatch_selected(self, request, queryset):
        dispatched = 0
        for obj in queryset:
            if obj.is_dispatchable:
                result = dispatch_existing_mailing(obj.id)
                if result.get("success"):
                    dispatched += 1
                else:
                    self.message_user(request, f"Mailing {obj.id}: {result.get('error')}", level='ERROR')
        self.message_user(request, f"Dispatched {dispatched} mailings.")

@admin.register(MailingProcessedChunk)
class MailingProcessedChunkAdmin(admin.ModelAdmin):
    list_display = ('mailing_name', 'chunk_index', 'status', 'success_count', 'failure_count')
    def mailing_name(self, obj): return obj.mailing.name

@admin.register(MailLog)
class MailLogAdmin(admin.ModelAdmin):
    list_display = ('client_company', 'subject_short', 'status_colored', 'campaign_name', 'sent_at')
    list_filter = ('status', 'mail_type', 'campaign_name') # FIX: Filter by campaign_name instead of FK
    def client_company(self, obj): return obj.client.company_name
    def subject_short(self, obj): return obj.subject[:50] + '...' if len(obj.subject) > 50 else obj.subject
    def status_colored(self, obj):
        return format_html('<span style="color: {};">{}</span>', 'green' if obj.status == 'SENT' else 'red', obj.status)