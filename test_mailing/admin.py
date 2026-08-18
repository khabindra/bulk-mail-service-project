from django.contrib import admin
from django.utils.html import format_html
from django.urls import path
from django.shortcuts import redirect
from django.contrib import messages
from django.views.decorators.http import require_POST  # Still import, but use differently

from .models import TestMailing, TestMailingAttachment, TestMailingProcessedChunk
from .services.test_mailing_service import TestMailingService, InvalidStateError


class TestMailingAttachmentInline(admin.TabularInline):
    model = TestMailingAttachment
    readonly_fields = ('filename', 'content_type', 'file_size', 'uploaded_at')
    extra = 0


@admin.register(TestMailing)
class TestMailingAdmin(admin.ModelAdmin):
    list_display = ('name', 'status_colored', 'scheduled_time', 'progress_bar', 'created_by', 'created_at')
    list_filter = ('status', 'mail_type', 'scheduled_time')
    search_fields = ('name', 'description')
    readonly_fields = (
        'status', 'total_recipients', 'total_chunks', 'completed_chunks',
        'successful_sends', 'failed_sends', 'completed_at', 'error_message',
        'celery_periodic_task'
    )
    inlines = [TestMailingAttachmentInline]
    actions = ['schedule_selected', 'cancel_selected', 'trigger_now']
    fieldsets = (
        ('Basic Info', {
            'fields': ('name', 'description', 'mail_type', 'email_template', 'sender_email')
        }),
        ('Scheduling', {
            'fields': ('status', 'scheduled_time', 'test_email'),
            'description': 'Set scheduled_time and click "Schedule" to queue for delivery.'
        }),
        ('Recipients', {'fields': ('recipients',)}),
        ('Configuration', {'fields': ('context_variables',)}),
        ('Stats', {
            'fields': (
                'total_recipients', 'total_chunks', 'completed_chunks',
                'successful_sends', 'failed_sends', 'completed_at', 'error_message'
            ),
            'classes': ('collapse',)
        }),
    )

    def get_urls(self):
        urls = super().get_urls()
        custom_urls = [
            # FIX: Wrap the BOUND method with require_POST at URL registration time
            # When Python accesses self.schedule_view, the descriptor protocol binds it,
            # so self.schedule_view is already a bound method. require_POST then wraps
            # the bound method correctly, preserving (self, request, object_id) signature.
            path(
                '<path:object_id>/schedule/',
                self.admin_site.admin_view(require_POST(self.schedule_view)),
                name='test_mailing_schedule'
            ),
            path(
                '<path:object_id>/cancel/',
                self.admin_site.admin_view(require_POST(self.cancel_view)),
                name='test_mailing_cancel'
            ),
            path(
                '<path:object_id>/trigger/',
                self.admin_site.admin_view(require_POST(self.trigger_view)),
                name='test_mailing_trigger'
            ),
        ]
        return custom_urls + urls

    # REMOVED: @require_POST from method definitions
    # These are now plain methods that get wrapped at URL registration time
    def schedule_view(self, request, object_id):
        mailing = self.get_object(request, object_id)
        try:
            TestMailingService.schedule_mailing(mailing)
            self.message_user(request, "Mailing scheduled successfully.")
        except InvalidStateError as e:
            self.message_user(request, str(e), level='ERROR')
        except ValueError as e:
            self.message_user(request, str(e), level='ERROR')
        except Exception as e:
            self.message_user(request, f"Error: {e}", level='ERROR')
        return redirect('../')

    def cancel_view(self, request, object_id):
        mailing = self.get_object(request, object_id)
        if TestMailingService.cancel_mailing(mailing):
            self.message_user(request, "Mailing cancelled.")
        else:
            self.message_user(request, "Cannot cancel in current status.", level='ERROR')
        return redirect('../')

    def trigger_view(self, request, object_id):
        mailing = self.get_object(request, object_id)
        if TestMailingService.trigger_immediately(mailing):
            self.message_user(request, "Mailing triggered for immediate execution.")
        else:
            self.message_user(
                request,
                "Cannot trigger. Check: 1) Template is valid, 2) Status is DRAFT or SCHEDULED",
                level='ERROR'
            )
        return redirect('../')

    def status_colored(self, obj):
        colors = {
            'DRAFT': 'gray', 'SCHEDULED': 'blue', 'PROCESSING': 'orange',
            'DISPATCHED': 'purple', 'SENT': 'green', 'FAILED': 'red', 'CANCELLED': 'black'
        }
        return format_html('<span style="color: {};">{}</span>', colors.get(obj.status, 'gray'), obj.status)
    status_colored.short_description = 'Status'

    def progress_bar(self, obj):
        if obj.total_chunks == 0:
            return "N/A"
        pct = int((obj.completed_chunks / obj.total_chunks) * 100)
        color = 'green' if pct == 100 else 'orange'
        return format_html(
            '<div style="width:100px;background:#eee;border-radius:5px;">'
            '<div style="width:{}%;background:{};height:10px;border-radius:5px;"></div>'
            '</div> {}%',
            pct, color, pct
        )
    progress_bar.short_description = 'Progress'

    @admin.action(description='Schedule selected (requires scheduled_time)')
    def schedule_selected(self, request, queryset):
        count = 0
        for obj in queryset:
            try:
                TestMailingService.schedule_mailing(obj)
                count += 1
            except (InvalidStateError, ValueError) as e:
                self.message_user(request, f"'{obj.name}': {e}", level='ERROR')
            except Exception as e:
                self.message_user(request, f"'{obj.name}': Error: {e}", level='ERROR')
        if count:
            self.message_user(request, f"Scheduled {count} mailings.")

    @admin.action(description='Cancel selected DRAFT/SCHEDULED mailings')
    def cancel_selected(self, request, queryset):
        count = sum(1 for obj in queryset if TestMailingService.cancel_mailing(obj))
        self.message_user(request, f"Cancelled {count} mailings.")

    @admin.action(description='Trigger selected immediately')
    def trigger_now(self, request, queryset):
        count = 0
        for obj in queryset:
            if TestMailingService.trigger_immediately(obj):
                count += 1
            else:
                self.message_user(
                    request,
                    f"'{obj.name}': Could not trigger. Check template validity and status.",
                    level='ERROR'
                )
        if count:
            self.message_user(request, f"Triggered {count} mailings.")


@admin.register(TestMailingProcessedChunk)
class TestMailingProcessedChunkAdmin(admin.ModelAdmin):
    list_display = ('test_mailing_name', 'chunk_index', 'status', 'success_count', 'failure_count')
    
    def test_mailing_name(self, obj):
        return obj.test_mailing.name