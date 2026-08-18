# campaigns/models.py

import json
import logging
import os
import mimetypes
import uuid
from django.core.exceptions import ValidationError
from django.db import models, transaction, IntegrityError
from django.utils import timezone
from django_celery_beat.models import CrontabSchedule, PeriodicTask

logger = logging.getLogger(__name__)

try:
    from magic import from_buffer
    HAS_MAGIC = True
except ImportError:
    HAS_MAGIC = False


class Attachment(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    campaign = models.ForeignKey('Campaign', on_delete=models.CASCADE, related_name='attachments')
    file = models.FileField(upload_to='campaign_attachments/%Y/%m/')
    filename = models.CharField(max_length=255, blank=True, editable=False)
    content_type = models.CharField(max_length=100, blank=True, editable=False)
    size = models.PositiveBigIntegerField(default=0, editable=False)
    uploaded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [models.Index(fields=['campaign'])]

    def save(self, *args, **kwargs):
        if self.file and not self.filename:
            self.filename = os.path.basename(self.file.name)
            try:
                if HAS_MAGIC:
                    self.content_type = from_buffer(self.file.read(1024), mime=True)
                    self.file.seek(0)
                else:
                    self.content_type = mimetypes.guess_type(self.filename)[0] or 'application/octet-stream'
            except Exception:
                self.content_type = 'application/octet-stream'
            if hasattr(self.file, 'size'):
                self.size = self.file.size
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        if self.file:
            try:
                self.file.delete(save=False)
            except Exception:
                logger.warning("Failed to delete file for Attachment %s", self.id)
        super().delete(*args, **kwargs)

    def __str__(self):
        return self.filename


class CampaignRun(models.Model):
    class StatusChoices(models.TextChoices):
        DISPATCHED = 'DISPATCHED', 'Dispatched'
        PROCESSING = 'PROCESSING', 'Processing'
        COMPLETED = 'COMPLETED', 'Completed'
        PARTIAL = 'PARTIAL', 'Partial'
        FAILED = 'FAILED', 'Failed'

    campaign = models.ForeignKey('Campaign', on_delete=models.CASCADE, related_name='runs')
    task_id = models.CharField(max_length=255, db_index=True)
    status = models.CharField(
        max_length=20,
        choices=StatusChoices.choices,
        default=StatusChoices.DISPATCHED,
        db_index=True
    )
    recipient_count = models.PositiveIntegerField(default=0)
    chunk_count = models.PositiveIntegerField(default=0)
    completed_chunks = models.PositiveIntegerField(default=0)
    successful_sends = models.PositiveIntegerField(default=0)
    failed_sends = models.PositiveIntegerField(default=0)
    error_message = models.TextField(blank=True)
    started_at = models.DateTimeField(auto_now_add=True, db_index=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-started_at']
        indexes = [
            models.Index(fields=['campaign', '-started_at']),
            models.Index(fields=['status', '-started_at']),
            models.Index(fields=['task_id']),
        ]
        # ═══════════════════════════════════════════════════════════════
        # FIX #5: UniqueConstraint prevents duplicate CampaignRun records
        # if celery-beat fires the same campaign twice (clock drift, 
        # broker retry). Combined with get_or_create in execute_campaign,
        # this ensures idempotency.
        # ═══════════════════════════════════════════════════════════════
        constraints = [
            models.UniqueConstraint(
                fields=['campaign', 'task_id'],
                name='unique_campaign_run_per_task',
                violation_error_message='A CampaignRun already exists for this campaign and task.'
            )
        ]

    def __str__(self):
        return f"{self.campaign.name} | {self.started_at.strftime('%Y-%m-%d %H:%M')} | {self.status}"

    @property
    def duration_seconds(self) -> float | None:
        if self.completed_at and self.started_at:
            return (self.completed_at - self.started_at).total_seconds()
        return None

    @property
    def success_rate(self) -> float | None:
        total = self.successful_sends + self.failed_sends
        if total == 0:
            return None
        return round((self.successful_sends / total) * 100, 1)


class Campaign(models.Model):
    class Weekday(models.IntegerChoices):
        SUNDAY = 0, 'Sunday'; MONDAY = 1, 'Monday'; TUESDAY = 2, 'Tuesday'
        WEDNESDAY = 3, 'Wednesday'; THURSDAY = 4, 'Thursday'; FRIDAY = 5, 'Friday'
        SATURDAY = 6, 'Saturday'

    class MonthOfYear(models.IntegerChoices):
        JANUARY = 1, 'January'; FEBRUARY = 2, 'February'; MARCH = 3, 'March'
        APRIL = 4, 'April'; MAY = 5, 'May'; JUNE = 6, 'June'; JULY = 7, 'July'
        AUGUST = 8, 'August'; SEPTEMBER = 9, 'September'; OCTOBER = 10, 'October'
        NOVEMBER = 11, 'November'; DECEMBER = 12, 'December'

    class DayOfMonth(models.IntegerChoices):
        _1 = 1, '1st'; _2 = 2, '2nd'; _3 = 3, '3rd'; _4 = 4, '4th'; _5 = 5, '5th'
        _6 = 6, '6th'; _7 = 7, '7th'; _8 = 8, '8th'; _9 = 9, '9th'; _10 = 10, '10th'
        _11 = 11, '11th'; _12 = 12, '12th'; _13 = 13, '13th'; _14 = 14, '14th'; _15 = 15, '15th'
        _16 = 16, '16th'; _17 = 17, '17th'; _18 = 18, '18th'; _19 = 19, '19th'; _20 = 20, '20th'
        _21 = 21, '21st'; _22 = 22, '22nd'; _23 = 23, '23rd'; _24 = 24, '24th'; _25 = 25, '25th'
        _26 = 26, '26th'; _27 = 27, '27th'; _28 = 28, '28th'; _29 = 29, '29th'; _30 = 30, '30th'
        _31 = 31, '31st'

    class ScheduleType(models.TextChoices):
        DAILY = 'DAILY', 'Daily'; WEEKLY = 'WEEKLY', 'Weekly'
        MONTHLY = 'MONTHLY', 'Monthly'; YEARLY = 'YEARLY', 'Yearly'

    class StatusChoices(models.TextChoices):
        DRAFT = 'DRAFT', 'Draft'; ACTIVE = 'ACTIVE', 'Active'; PAUSED = 'PAUSED', 'Paused'

    name = models.CharField(max_length=200, unique=True, db_index=True)
    description = models.TextField(blank=True)
    email_template = models.ForeignKey('templates.EmailTemplate', on_delete=models.PROTECT, related_name='campaigns')
    sender_email = models.ForeignKey('mailings.SenderEmail', on_delete=models.SET_NULL, null=True, blank=True)
    recipients = models.ManyToManyField('client.Client', related_name='campaigns', limit_choices_to={'is_active': True}, blank=True)
    status = models.CharField(max_length=20, choices=StatusChoices.choices, default=StatusChoices.DRAFT, db_index=True)
    schedule_type = models.CharField(max_length=20, choices=ScheduleType.choices, default='DAILY', db_index=True)
    send_at_time = models.TimeField(help_text="e.g., 09:00 AM")
    target_weekday = models.IntegerField(choices=Weekday.choices, null=True, blank=True)
    day_of_month = models.IntegerField(choices=DayOfMonth.choices, null=True, blank=True)
    month_of_year = models.IntegerField(choices=MonthOfYear.choices, null=True, blank=True)
    context_variables = models.JSONField(default=dict, blank=True)
    last_executed_at = models.DateTimeField(null=True, blank=True, editable=False, db_index=True)
    execution_count = models.PositiveIntegerField(default=0, editable=False)
    celery_periodic_task = models.OneToOneField(PeriodicTask, on_delete=models.SET_NULL, null=True, blank=True, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['status', 'schedule_type']),
            models.Index(fields=['email_template']),
            models.Index(fields=['sender_email']),
            models.Index(fields=['last_executed_at']),
        ]

    def __str__(self):
        return f"{self.name} ({self.get_schedule_type_display()})"

    def clean(self):
        super().clean()
        if self.schedule_type == self.ScheduleType.WEEKLY and self.target_weekday is None:
            raise ValidationError({'target_weekday': 'Required for weekly schedule.'})
        if self.schedule_type == self.ScheduleType.MONTHLY and self.day_of_month is None:
            raise ValidationError({'day_of_month': 'Required for monthly schedule.'})
        if self.schedule_type == self.ScheduleType.YEARLY:
            if self.day_of_month is None:
                raise ValidationError({'day_of_month': 'Required for yearly schedule.'})
            if self.month_of_year is None:
                raise ValidationError({'month_of_year': 'Required for yearly schedule.'})
        if self.schedule_type != self.ScheduleType.WEEKLY:
            self.target_weekday = None
        if self.schedule_type not in [self.ScheduleType.MONTHLY, self.ScheduleType.YEARLY]:
            self.day_of_month = None
        if self.schedule_type != self.ScheduleType.YEARLY:
            self.month_of_year = None

    def save(self, *args, **kwargs):
        update_fields = kwargs.get('update_fields')
        if self.status == 'ACTIVE' and not update_fields:
            self.full_clean()
            self._atomic_save_with_schedule(*args, **kwargs)
        else:
            super().save(*args, **kwargs)
            if not update_fields and self.celery_periodic_task:
                self.celery_periodic_task.enabled = False
                self.celery_periodic_task.save(update_fields=['enabled'])

    def _atomic_save_with_schedule(self, *args, **kwargs):
        minute = str(self.send_at_time.minute)
        hour = str(self.send_at_time.hour)
        day_of_week = '*'
        day_of_month_cron = '*'
        month_of_year_cron = '*'

        if self.schedule_type == self.ScheduleType.WEEKLY:
            day_of_week = str(self.target_weekday)
        elif self.schedule_type == self.ScheduleType.MONTHLY:
            day_of_month_cron = str(self.day_of_month)
        elif self.schedule_type == self.ScheduleType.YEARLY:
            day_of_month_cron = str(self.day_of_month)
            month_of_year_cron = str(self.month_of_year)

        try:
            with transaction.atomic():
                super().save(*args, **kwargs)
                locked_campaign = Campaign.objects.select_for_update().get(pk=self.pk)
                tz = timezone.get_current_timezone_name()
                schedule, _ = CrontabSchedule.objects.get_or_create(
                    minute=minute, hour=hour, day_of_week=day_of_week,
                    day_of_month=day_of_month_cron, month_of_year=month_of_year_cron,
                    timezone=tz
                )
                task_name = f"campaign-{self.name.lower().replace(' ', '-')}-{self.pk}"
                if locked_campaign.celery_periodic_task:
                    task = locked_campaign.celery_periodic_task
                    task.crontab = schedule
                    task.enabled = True
                    task.name = task_name
                    task.args = json.dumps([self.pk])
                    task.save(update_fields=['crontab', 'enabled', 'name', 'args'])
                else:
                    task = PeriodicTask.objects.create(
                        name=task_name, crontab=schedule,
                        task='campaigns.tasks.execute_campaign',
                        args=json.dumps([self.pk]), enabled=True
                    )
                    locked_campaign.celery_periodic_task = task
                    locked_campaign.save(update_fields=['celery_periodic_task'])
        except ValidationError:
            raise
        except (IntegrityError, ValueError):
            raise
        except Exception as e:
            logger.error("Schedule creation failed for campaign %s: %s", self.name, e)
            raise ValidationError(f"Scheduling failed: {str(e)}")

    def get_crontab_expression(self):
        if not self.send_at_time:
            return ""
        m, h = str(self.send_at_time.minute), str(self.send_at_time.hour)
        patterns = {
            self.ScheduleType.DAILY: f"{m} {h} * * *",
            self.ScheduleType.WEEKLY: f"{m} {h} * * {self.target_weekday or '*'}",
            self.ScheduleType.MONTHLY: f"{m} {h} {self.day_of_month or '*'} * *",
            self.ScheduleType.YEARLY: f"{m} {h} {self.day_of_month or '*'} {self.month_of_year or '*'} *",
        }
        return patterns.get(self.schedule_type, "")

    def get_next_run_display(self):
        task = self.celery_periodic_task
        if not task or not task.enabled:
            return "Not scheduled"
        if task.last_run_at:
            return f"Last run: {task.last_run_at.strftime('%Y-%m-%d %H:%M')}"
        return "Pending first run"

    def delete(self, *args, **kwargs):
        self._cleanup_periodic_task()
        super().delete(*args, **kwargs)

    def _cleanup_periodic_task(self):
        if not self.celery_periodic_task:
            return
        try:
            task = PeriodicTask.objects.filter(id=self.celery_periodic_task.id).first()
            if not task:
                return
            crontab = task.crontab
            task.delete()
            if crontab and not PeriodicTask.objects.filter(crontab=crontab).exists():
                crontab.delete()
        except Exception:
            logger.exception("Failed to cleanup periodic task for campaign %s", self.id)