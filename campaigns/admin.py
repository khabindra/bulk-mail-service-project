# campaigns/admin.py

from django.contrib import admin
from django.core.exceptions import ValidationError
from django.forms.models import BaseInlineFormSet
from django.urls import reverse
from django.utils.html import format_html
from django.db.models import Count, Q
from .models import Campaign, Attachment, CampaignRun


class AttachmentInline(admin.TabularInline):
    model = Attachment
    extra = 1
    fields = ('file', 'filename', 'content_type', 'size_display', 'uploaded_at')
    readonly_fields = ('filename', 'content_type', 'size_display', 'uploaded_at')
    can_delete = True

    def size_display(self, obj):
        if obj.size:
            size = float(obj.size)
            for unit in ['B', 'KB', 'MB', 'GB']:
                if size < 1024.0:
                    return f"{size:.1f} {unit}"
                size /= 1024.0
        return "0 B"
    size_display.short_description = "Size"

    def get_extra(self, request, obj=None, **kwargs):
        return 1 if obj is None else 0


# ═══════════════════════════════════════════════════════════════════
# FIXED: Custom formset that limits queryset AFTER parent FK filtering
#
# Why get_formset didn't work:
#   super().get_formset() returns a FormSet CLASS, not an instance.
#   Classes don't have .queryset — only instances do.
#
# Why this works:
#   1. BaseInlineFormSet.__init__ calls super().__init__() which sets
#      self.queryset from the model's default manager
#   2. BaseInlineFormSet.__init__ then filters: self.queryset = self.queryset.filter(fk=instance)
#   3. Our __init__ runs AFTER super(), so the FK filter already happened
#   4. We then slice to 10 — this is a list, but that's fine because
#      Django only uses len() and [] indexing on inline querysets
# ═══════════════════════════════════════════════════════════════════

class LimitedCampaignRunFormSet(BaseInlineFormSet):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # At this point, self.queryset is already filtered to the parent campaign
        self.queryset = self.queryset.order_by('-started_at')[:10]


class CampaignRunInline(admin.TabularInline):
    model = CampaignRun
    formset = LimitedCampaignRunFormSet  # Use the custom formset
    can_delete = False
    extra = 0
    readonly_fields = (
        'task_id_short', 'status_colored', 'recipient_count',
        'progress_display', 'success_rate_display', 'duration_display',
        'started_at', 'completed_at'
    )
    fields = readonly_fields
    show_change_link = False

    # REMOVED: get_formset override — replaced with custom formset class above

    def get_queryset(self, request):
        # Just set ordering — the actual limiting happens in the formset
        return super().get_queryset(request).order_by('-started_at')

    def task_id_short(self, obj):
        return obj.task_id[:12] + '...' if len(obj.task_id) > 12 else obj.task_id
    task_id_short.short_description = 'Task ID'

    def status_colored(self, obj):
        colors = {
            'DISPATCHED': 'blue', 'PROCESSING': 'orange',
            'COMPLETED': 'green', 'PARTIAL': '#d4a017', 'FAILED': 'red',
        }
        return format_html(
            '<span style="color: {}; font-weight: bold;">{}</span>',
            colors.get(obj.status, 'gray'), obj.get_status_display()
        )
    status_colored.short_description = 'Status'

    def progress_display(self, obj):
        if obj.chunk_count == 0:
            return "N/A"
        pct = int((obj.completed_chunks / obj.chunk_count) * 100)
        color = 'green' if pct == 100 else 'orange'
        return format_html(
            '<span title="{}/{} chunks">{}</span>',
            obj.completed_chunks, obj.chunk_count, f"{pct}%"
        )
    progress_display.short_description = 'Progress'

    def success_rate_display(self, obj):
        rate = obj.success_rate
        if rate is None:
            return "N/A"
        color = 'green' if rate >= 95 else ('orange' if rate >= 50 else 'red')
        return format_html('<span style="color: {};">{}</span>', color, f"{rate}%")
    success_rate_display.short_description = 'Success Rate'

    def duration_display(self, obj):
        seconds = obj.duration_seconds
        if seconds is None:
            return "-"
        if seconds < 60:
            return f"{seconds:.1f}s"
        return f"{int(seconds // 60)}m {int(seconds % 60)}s"
    duration_display.short_description = 'Duration'

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(Campaign)
class CampaignAdmin(admin.ModelAdmin):
    list_display = (
        'name', 'schedule_type', 'status', 'get_crontab_preview',
        'recipient_count_display', 'attachment_count_display',
        'execution_count', 'last_executed_at', 'created_at',
    )
    list_filter = ('status', 'schedule_type', 'created_at')
    search_fields = ('name', 'description')
    filter_horizontal = ('recipients',)
    readonly_fields = (
        'celery_periodic_task', 'get_crontab_preview',
        'execution_count', 'last_executed_at', 'created_at', 'updated_at',
    )
    inlines = [AttachmentInline, CampaignRunInline]

    def get_queryset(self, request):
        return super().get_queryset(request).annotate(
            _active_recipient_count=Count('recipients', filter=Q(recipients__is_active=True)),
            _attachment_count=Count('attachments'),
        )

    fieldsets = (
        ('Campaign Setup', {
            'fields': ('name', 'description', 'email_template', 'sender_email', 'recipients', 'context_variables')
        }),
        ('Schedule Configuration', {
            'fields': ('status', 'schedule_type', 'send_at_time', 'target_weekday', 'day_of_month', 'month_of_year'),
            'description': 'Irrelevant fields are ignored automatically.'
        }),
        ('Execution Tracking', {
            'fields': ('execution_count', 'last_executed_at'),
            'classes': ('collapse',)
        }),
        ('System Information', {
            'fields': ('get_crontab_preview', 'celery_periodic_task', 'created_at', 'updated_at'),
            'classes': ('collapse',)
        }),
    )

    @admin.display(description='Recipients', ordering='_active_recipient_count')
    def recipient_count_display(self, obj):
        count = obj._active_recipient_count
        if count:
            ids = ",".join(map(str, obj.recipients.values_list("id", flat=True)))
            url = reverse('admin:client_client_changelist') + f'?id__in={ids}'
            return format_html('<a href="{}">{}</a>', url, count)
        return '0'

    @admin.display(description='Attachments', ordering='_attachment_count')
    def attachment_count_display(self, obj):
        count = obj._attachment_count
        return format_html('<span style="font-weight:bold; color:#264f78;">{}</span>', count) if count else '0'

    @admin.display(description='Crontab')
    def get_crontab_preview(self, obj):
        expr = obj.get_crontab_expression()
        return format_html('<code style="background:#f8f9fa; padding:2px 6px; border-radius:4px;">{}</code>', expr) if expr else '-'

    actions = ['make_active', 'make_paused']

    @admin.action(description='Activate selected campaigns')
    def make_active(self, request, queryset):
        count, errors = 0, []
        for campaign in queryset:
            campaign.status = Campaign.StatusChoices.ACTIVE
            try:
                campaign.save()
                count += 1
            except ValidationError as e:
                errors.append(f"'{campaign.name}': {e.message_dict if hasattr(e, 'message_dict') else str(e)}")
            except Exception as e:
                errors.append(f"'{campaign.name}': {e}")
        if count:
            self.message_user(request, f'{count} campaign(s) activated.')
        for error in errors:
            self.message_user(request, error, level='ERROR')

    @admin.action(description='Pause selected campaigns')
    def make_paused(self, request, queryset):
        count = 0
        for campaign in queryset:
            campaign.status = Campaign.StatusChoices.PAUSED
            campaign.save()
            count += 1
        self.message_user(request, f'{count} campaign(s) paused.')


@admin.register(CampaignRun)
class CampaignRunAdmin(admin.ModelAdmin):
    list_display = ('campaign_name', 'started_at', 'status_colored', 'recipient_count', 'progress_display', 'success_rate_display', 'duration_display')
    list_filter = ('status', 'campaign')
    search_fields = ('campaign__name', 'task_id')
    readonly_fields = ('campaign', 'task_id', 'status', 'recipient_count', 'chunk_count', 'completed_chunks', 'successful_sends', 'failed_sends', 'error_message', 'started_at', 'completed_at')
    fieldsets = (
        ('Run Info', {'fields': ('campaign', 'task_id', 'status', 'started_at', 'completed_at')}),
        ('Statistics', {'fields': ('recipient_count', 'chunk_count', 'completed_chunks', 'successful_sends', 'failed_sends')}),
        ('Error Details', {'fields': ('error_message',), 'classes': ('collapse',)}),
    )

    def campaign_name(self, obj):
        return obj.campaign.name
    campaign_name.admin_order_field = 'campaign__name'

    def status_colored(self, obj):
        colors = {'DISPATCHED': 'blue', 'PROCESSING': 'orange', 'COMPLETED': 'green', 'PARTIAL': '#d4a017', 'FAILED': 'red'}
        return format_html('<span style="color: {}; font-weight: bold;">{}</span>', colors.get(obj.status, 'gray'), obj.get_status_display())
    status_colored.short_description = 'Status'

    def progress_display(self, obj):
        if obj.chunk_count == 0:
            return "N/A"
        pct = int((obj.completed_chunks / obj.chunk_count) * 100)
        color = 'green' if pct == 100 else 'orange'
        return format_html(
            '<div style="width:100px;background:#eee;border-radius:5px;">'
            '<div style="width:{}%;background:{};height:10px;border-radius:5px;"></div>'
            '</div> {}/{}',
            pct, color, obj.completed_chunks, obj.chunk_count
        )
    progress_display.short_description = 'Progress'

    def success_rate_display(self, obj):
        rate = obj.success_rate
        return format_html(
            '<span style="color: {};">{}</span>',
            'green' if rate and rate >= 95 else ('orange' if rate and rate >= 50 else 'red'),
            f"{rate}%"
        ) if rate else "N/A"
    success_rate_display.short_description = 'Success Rate'

    def duration_display(self, obj):
        seconds = obj.duration_seconds
        return "-" if seconds is None else (f"{seconds:.1f}s" if seconds < 60 else f"{int(seconds // 60)}m {int(seconds % 60)}s")
    duration_display.short_description = 'Duration'

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(Attachment)
class AttachmentAdmin(admin.ModelAdmin):
    list_display = ('filename', 'campaign', 'size_display', 'uploaded_at')
    list_filter = ('campaign',)
    search_fields = ('filename', 'campaign__name')
    readonly_fields = ('id', 'campaign', 'filename', 'content_type', 'size', 'uploaded_at')

    def has_add_permission(self, request):
        return False

    @admin.display(description='Size')
    def size_display(self, obj):
        if obj.size:
            size = float(obj.size)
            for unit in ['B', 'KB', 'MB', 'GB']:
                if size < 1024.0:
                    return f"{size:.1f} {unit}"
                size /= 1024.0
        return "0 B"