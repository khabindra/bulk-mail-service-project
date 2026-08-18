import logging
import mimetypes
from django.db import models
from django.utils import timezone
from django.db.models.signals import post_delete
from django.dispatch import receiver

logger = logging.getLogger(__name__)

def cleanup_celery_task_by_id(mailing_id, task_id):
    if not task_id: return
    try:
        from django_celery_beat.models import PeriodicTask, ClockedSchedule
        task = PeriodicTask.objects.filter(id=task_id).first()
        if not task: return
        clocked = task.clocked
        task.delete()
        if clocked and not PeriodicTask.objects.filter(clocked=clocked).exists(): clocked.delete()
    except Exception:
        logger.exception("failed_to_cleanup_celery_task")

class TestMailing(models.Model):
    class StatusChoices(models.TextChoices):
        DRAFT = 'DRAFT', 'Draft'; SCHEDULED = 'SCHEDULED', 'Scheduled'; PROCESSING = 'PROCESSING', 'Processing'
        DISPATCHED = 'DISPATCHED', 'Dispatched'; SENT = 'SENT', 'Sent'; FAILED = 'FAILED', 'Failed'; CANCELLED = 'CANCELLED', 'Cancelled'

    name = models.CharField(max_length=200, db_index=True)
    description = models.TextField(blank=True)
    status = models.CharField(max_length=20, choices=StatusChoices.choices, default=StatusChoices.DRAFT, db_index=True)
    scheduled_time = models.DateTimeField(db_index=True, null=True, blank=True)
    
    celery_periodic_task = models.OneToOneField('django_celery_beat.PeriodicTask', on_delete=models.SET_NULL, null=True, blank=True, editable=False, related_name='test_mailing')

    mail_type = models.ForeignKey('templates.MailType', on_delete=models.PROTECT, related_name='test_mailings')
    email_template = models.ForeignKey('templates.EmailTemplate', on_delete=models.PROTECT, related_name='test_mailings')
    sender_email = models.ForeignKey('mailings.SenderEmail', on_delete=models.SET_NULL, null=True, blank=True, related_name='test_mailings')

    recipients = models.ManyToManyField('client.Client', related_name='test_mailings', blank=True)
    test_email = models.EmailField(blank=True, null=True)
    context_variables = models.JSONField(default=dict, blank=True)

    total_recipients = models.PositiveIntegerField(default=0)
    total_chunks = models.PositiveIntegerField(default=0)
    completed_chunks = models.PositiveIntegerField(default=0)
    successful_sends = models.PositiveIntegerField(default=0)
    failed_sends = models.PositiveIntegerField(default=0)
    completed_at = models.DateTimeField(null=True, blank=True)

    created_by = models.ForeignKey('users.User', on_delete=models.SET_NULL, null=True, blank=True, related_name='created_test_mailings')
    error_message = models.TextField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-scheduled_time']
        indexes = [models.Index(fields=['status', 'scheduled_time']), models.Index(fields=['created_by', 'status'])]

    def __str__(self): return f"{self.name} ({self.status})"

    def save(self, *args, **kwargs):
        if self.context_variables is None: self.context_variables = {}
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        cleanup_celery_task_by_id(self.id, self.celery_periodic_task_id)
        super().delete(*args, **kwargs)

@receiver(post_delete, sender=TestMailing)
def cleanup_celery_task_on_bulk_delete(sender, instance, **kwargs):
    cleanup_celery_task_by_id(instance.id, instance.celery_periodic_task_id)

class TestMailingAttachment(models.Model):
    test_mailing = models.ForeignKey(TestMailing, on_delete=models.CASCADE, related_name='attachments')
    file = models.FileField(upload_to='test_mailing_attachments/%Y/%m/')
    filename = models.CharField(max_length=255, editable=False) # Already has editable=False
    content_type = models.CharField(max_length=100, default='application/octet-stream')
    file_size = models.PositiveBigIntegerField(default=0, editable=False)
    uploaded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [models.Index(fields=['test_mailing'])]

    def save(self, *args, **kwargs):
        if self.file:
            if not self.filename: self.filename = self.file.name.split('/')[-1]
            if hasattr(self.file, 'size') and not self.file_size: self.file_size = self.file.size
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
            except Exception: pass
        super().delete(*args, **kwargs)

class TestMailingProcessedChunk(models.Model):
    test_mailing = models.ForeignKey(TestMailing, on_delete=models.CASCADE, related_name='processed_chunks')
    chunk_index = models.PositiveIntegerField()
    status = models.CharField(max_length=20, choices=[('PROCESSING', 'Processing'), ('COMPLETED', 'Completed')], default='PROCESSING')
    original_task_id = models.CharField(max_length=255, null=True, blank=True, editable=False)
    success_count = models.PositiveIntegerField(default=0)
    failure_count = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('test_mailing', 'chunk_index')
        indexes = [
            models.Index(fields=['test_mailing', 'chunk_index']),
            models.Index(fields=['test_mailing', 'status']),
            models.Index(fields=['original_task_id']),
        ]