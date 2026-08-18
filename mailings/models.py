import logging
import mimetypes
import os

from django.conf import settings
from django.db import models

from client.models import Client
from templates.models import MailType, EmailTemplate
# REMOVED: from test_mailing.models import TestMailing
# String FK reference avoids circular import - Django resolves it lazily after all apps load

logger = logging.getLogger(__name__)

class SenderEmail(models.Model):
    name  = models.CharField(max_length=100)
    email = models.EmailField()

    class Meta:
        verbose_name        = "Sender Email"
        verbose_name_plural = "Sender Emails"

    def __str__(self):
        return f"{self.name} <{self.email}>"

class Mailing(models.Model):
    class StatusChoices(models.TextChoices):
        DRAFT       = 'DRAFT',       'Draft'
        PROCESSING  = 'PROCESSING',  'Processing'
        DISPATCHED  = 'DISPATCHED',  'Dispatched'
        COMPLETED   = 'COMPLETED',   'Completed'
        FAILED      = 'FAILED',      'Failed'
        CANCELLED   = 'CANCELLED',   'Cancelled'

    name        = models.CharField(max_length=255, db_index=True)
    description = models.TextField(blank=True)

    mail_type      = models.ForeignKey(MailType,      on_delete=models.PROTECT, related_name='mailings')
    email_template = models.ForeignKey(EmailTemplate, on_delete=models.PROTECT, related_name='mailings')
    sender_email   = models.ForeignKey(SenderEmail,   on_delete=models.PROTECT, related_name='mailings')
    subject        = models.CharField(max_length=255, blank=True)

    recipients = models.ManyToManyField(Client, blank=True, related_name='mailings')
    context_variables = models.JSONField(default=dict, blank=True)

    status = models.CharField(max_length=20, choices=StatusChoices.choices, default=StatusChoices.DRAFT, db_index=True)

    total_recipients = models.PositiveIntegerField(default=0)
    total_chunks     = models.PositiveIntegerField(default=0)
    completed_chunks = models.PositiveIntegerField(default=0)
    successful_sends = models.PositiveIntegerField(default=0)
    failed_sends     = models.PositiveIntegerField(default=0)

    created_by    = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='created_mailings')
    error_message = models.TextField(blank=True, null=True)
    completed_at  = models.DateTimeField(null=True, blank=True)
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name        = "Mailing"
        verbose_name_plural = "Mailings"
        indexes = [
            models.Index(fields=['status', '-created_at']),
            models.Index(fields=['created_by', 'status']),
            models.Index(fields=['id', 'status']),
        ]

    def __str__(self):
        return f"{self.name} ({self.status})"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._initial_status = self.status

    def save(self, *args, **kwargs):
        if self.context_variables is None:
            self.context_variables = {}
        super().save(*args, **kwargs)

    @property
    def is_dispatchable(self) -> bool:
        return self.status == 'DRAFT'

    def get_progress_percentage(self) -> int:
        if not self.total_chunks: return 0
        return int((self.completed_chunks / self.total_chunks) * 100)

class MailingAttachment(models.Model):
    mailing      = models.ForeignKey(Mailing, on_delete=models.CASCADE, related_name='attachments')
    file         = models.FileField(upload_to='mailings/attachments/%Y/%m/')
    filename     = models.CharField(max_length=255, blank=True, editable=False)
    content_type = models.CharField(max_length=100, default='application/octet-stream', editable=False)
    file_size    = models.PositiveBigIntegerField(default=0, editable=False)
    uploaded_at  = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name        = "Mailing Attachment"
        verbose_name_plural = "Mailing Attachments"
        indexes = [models.Index(fields=['mailing'])]

    def __str__(self):
        return self.filename or str(self.id)

    def save(self, *args, **kwargs):
        if self.file:
            if not self.filename:
                self.filename = os.path.basename(self.file.name)
            if not self.file_size and hasattr(self.file, 'size'):
                self.file_size = self.file.size
            if self.content_type == 'application/octet-stream':
                ct = getattr(self.file, 'content_type', None)
                if ct:
                    self.content_type = ct
                elif self.filename:
                    guessed, _ = mimetypes.guess_type(self.filename)
                    if guessed: self.content_type = guessed
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        if self.file:
            try: self.file.delete(save=False)
            except Exception: logger.warning("Failed to delete file for MailingAttachment %s", self.id)
        super().delete(*args, **kwargs)

class MailingProcessedChunk(models.Model):
    mailing          = models.ForeignKey(Mailing, on_delete=models.CASCADE, related_name='processed_chunks')
    chunk_index      = models.PositiveIntegerField()
    status           = models.CharField(max_length=20, choices=[('PROCESSING', 'Processing'), ('COMPLETED', 'Completed')], default='PROCESSING')
    original_task_id = models.CharField(max_length=255, null=True, blank=True, editable=False)
    success_count    = models.PositiveIntegerField(default=0)
    failure_count    = models.PositiveIntegerField(default=0)
    created_at       = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('mailing', 'chunk_index')
        indexes = [
            models.Index(fields=['mailing', 'chunk_index']),
            models.Index(fields=['mailing', 'status']),
            models.Index(fields=['original_task_id']),
        ]

    def __str__(self):
        return f"Mailing {self.mailing_id} | chunk {self.chunk_index} | {self.status}"

class MailLog(models.Model):
    class StatusChoices(models.TextChoices):
        SENT       = 'SENT',       'Sent'
        FAILED     = 'FAILED',     'Failed'
        PENDING    = 'PENDING',    'Pending'
        PROCESSING = 'PROCESSING', 'Processing'

    client         = models.ForeignKey(Client, on_delete=models.CASCADE, related_name='mail_logs')
    mailing        = models.ForeignKey(Mailing, on_delete=models.SET_NULL, null=True, blank=True, related_name='logs')
    # String FK reference - avoids circular import between mailings and test_mailing
    scheduled_mailing = models.ForeignKey(
        'test_mailing.TestMailing', 
        on_delete=models.SET_NULL, 
        null=True, 
        blank=True, 
        related_name='logs',
        help_text="Links log to TestMailing for admin drill-through."
    )
    mail_type      = models.ForeignKey(MailType, on_delete=models.PROTECT)
    template_used  = models.ForeignKey(EmailTemplate, on_delete=models.SET_NULL, null=True, blank=True, related_name='logs')
    sender_email   = models.ForeignKey(SenderEmail, on_delete=models.SET_NULL, null=True, blank=True)
    created_by     = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='sent_logs')

    task_id       = models.CharField(max_length=255, blank=True, null=True, db_index=True)
    campaign_name = models.CharField(max_length=255, blank=True, db_index=True, help_text="Used by test_mailings for traceability.")
    
    status        = models.CharField(max_length=20, choices=StatusChoices.choices, default=StatusChoices.PENDING, db_index=True)
    subject       = models.CharField(max_length=255)
    error_message = models.TextField(blank=True, null=True)
    sent_at       = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ['-sent_at']
        verbose_name        = "Mail Log"
        verbose_name_plural = "Mail Logs"
        indexes = [
            models.Index(fields=['client', '-sent_at']),
            models.Index(fields=['status', '-sent_at']),
            models.Index(fields=['mailing', '-sent_at']),
            models.Index(fields=['campaign_name']),
            models.Index(fields=['scheduled_mailing', '-sent_at']),
        ]
        constraints = [
            models.CheckConstraint(
                name='maillog_single_campaign_fk',
                check=(
                    models.Q(mailing__isnull=True) | models.Q(scheduled_mailing__isnull=True)
                ),
                violation_error_message='A log entry cannot belong to both a Mailing and a TestMailing.'
            )
        ]

    def __str__(self):
        return f"To: {self.client.company_name} | Status: {self.status} | {self.subject}"

    def clean(self):
        if self.mailing_id and self.scheduled_mailing_id:
            from django.core.exceptions import ValidationError
            raise ValidationError({
                'mailing': 'A log cannot belong to both a Mailing and a TestMailing.',
                'scheduled_mailing': 'A log cannot belong to both a Mailing and a TestMailing.'
            })