import json
import logging
from django.db import transaction, IntegrityError
from django.utils import timezone
from django_celery_beat.models import ClockedSchedule, PeriodicTask
from test_mailing.models import TestMailing

logger = logging.getLogger(__name__)


class TestMailingError(Exception):
    """Base exception for TestMailing service."""
    pass


class InvalidStateError(TestMailingError):
    """Raised when operation is not valid for current state."""
    pass


class TestMailingService:
    """Service for managing scheduled mailings."""

    VALID_SCHEDULE_STATES = ('DRAFT', 'CANCELLED')
    VALID_CANCEL_STATES = ('DRAFT', 'SCHEDULED')
    VALID_TRIGGER_STATES = ('DRAFT', 'SCHEDULED')

    @staticmethod
    def validate_template_compiled(test_mailing) -> bool:
        """
        Check if the mailing's template is valid and ready to use.
        
        FIXED: The old pattern `getattr(..., 'compiled_template', None) is None`
        silently broke when compiled_template changed to @property. Now uses
        is_template_valid which properly catches syntax errors.
        """
        template = getattr(test_mailing, 'email_template', None)
        if template is None:
            return False
        return bool(template.template_content) and template.is_template_valid

    @staticmethod
    @transaction.atomic
    def sync_schedule(test_mailing: TestMailing) -> None:
        """Sync celery beat schedule with mailing status."""
        if test_mailing.status == 'SCHEDULED':
            TestMailingService._enable_schedule(test_mailing)
        else:
            TestMailingService.disable_schedule(test_mailing)

    @staticmethod
    def _enable_schedule(test_mailing: TestMailing) -> None:
        """Create or update the celery beat periodic task."""
        scheduled_time = test_mailing.scheduled_time
        if not scheduled_time:
            logger.warning("Cannot schedule mailing %s without scheduled_time", test_mailing.id)
            raise InvalidStateError("scheduled_time is required")

        if timezone.is_naive(scheduled_time):
            scheduled_time = timezone.make_aware(scheduled_time)

        if scheduled_time <= timezone.now():
            logger.info("Scheduled time is in the past for mailing %s, triggering immediately", test_mailing.id)
            from test_mailing.tasks import run_test_mailing
            run_test_mailing.delay(test_mailing.id)
            return

        clocked, _ = ClockedSchedule.objects.get_or_create(clocked_time=scheduled_time)
        task_name = f"test-mailing-{test_mailing.id}"
        existing_task = test_mailing.celery_periodic_task
        
        if existing_task:
            existing_task.clocked = clocked
            existing_task.enabled = True
            existing_task.args = json.dumps([test_mailing.id])
            existing_task.save(update_fields=['clocked', 'enabled', 'args'])
        else:
            try:
                task = PeriodicTask.objects.create(
                    name=task_name,
                    task='test_mailing.tasks.run_test_mailing',
                    clocked=clocked,
                    one_off=True,
                    enabled=True,
                    args=json.dumps([test_mailing.id]),
                )
                test_mailing.celery_periodic_task = task
                test_mailing.save(update_fields=['celery_periodic_task'])
            except IntegrityError:
                logger.warning("Periodic task %s already exists, recovering linkage", task_name)
                task = PeriodicTask.objects.get(name=task_name)
                test_mailing.celery_periodic_task = task
                test_mailing.save(update_fields=['celery_periodic_task'])

    @staticmethod
    def disable_schedule(test_mailing: TestMailing) -> None:
        """Disable the celery beat periodic task."""
        task = test_mailing.celery_periodic_task
        if not task:
            return
        task.enabled = False
        task.save(update_fields=['enabled'])

    @staticmethod
    @transaction.atomic
    def schedule_mailing(test_mailing: TestMailing) -> TestMailing:
        """Schedule a mailing for future execution."""
        if not TestMailingService.validate_template_compiled(test_mailing):
            raise InvalidStateError("Template is invalid or empty. Fix the template before scheduling.")
        
        if test_mailing.status not in TestMailingService.VALID_SCHEDULE_STATES:
            raise InvalidStateError(
                f"Cannot schedule mailing in '{test_mailing.status}' state. "
                f"Valid states: {TestMailingService.VALID_SCHEDULE_STATES}"
            )
        
        if not test_mailing.scheduled_time:
            raise ValueError("scheduled_time is required")
        
        test_mailing.status = 'SCHEDULED'
        test_mailing.save(update_fields=['status'])
        TestMailingService.sync_schedule(test_mailing)
        
        return test_mailing

    @staticmethod
    @transaction.atomic
    def cancel_mailing(test_mailing: TestMailing) -> bool:
        """Cancel a scheduled mailing."""
        if test_mailing.status not in TestMailingService.VALID_CANCEL_STATES:
            return False
        
        test_mailing.status = 'CANCELLED'
        test_mailing.save(update_fields=['status'])
        TestMailingService.disable_schedule(test_mailing)
        return True

    @staticmethod
    def trigger_immediately(test_mailing: TestMailing) -> bool:
        """Trigger a mailing to execute immediately."""
        if not TestMailingService.validate_template_compiled(test_mailing):
            return False
        
        updated = TestMailing.objects.filter(
            id=test_mailing.id,
            status__in=TestMailingService.VALID_TRIGGER_STATES
        ).update(
            status='SCHEDULED',
            scheduled_time=timezone.now()
        )
        
        if not updated:
            return False
        
        test_mailing.refresh_from_db()
        TestMailingService.disable_schedule(test_mailing)
        
        from test_mailing.tasks import run_test_mailing
        run_test_mailing.delay(test_mailing.id)
        return True

    @staticmethod
    def cleanup_old_tasks(days: int = 30) -> int:
        """Clean up old completed/cancelled periodic tasks."""
        from datetime import timedelta
        cutoff = timezone.now() - timedelta(days=days)
        
        deleted_count, _ = PeriodicTask.objects.filter(
            enabled=False,
            one_off=True,
            last_run_at__isnull=False,
            last_run_at__lte=cutoff,
            test_mailing__status__in=('SENT', 'FAILED', 'CANCELLED')
        ).delete()
        
        return deleted_count

    @staticmethod
    def get_mailing_status_transitions() -> dict:
        """Get valid status transitions for documentation/UI."""
        return {
            'DRAFT': ['SCHEDULED', 'CANCELLED'],
            'SCHEDULED': ['PROCESSING', 'CANCELLED'],
            'PROCESSING': ['DISPATCHED', 'FAILED'],
            'DISPATCHED': ['SENT', 'FAILED'],
            'SENT': [],
            'FAILED': ['DRAFT'],
            'CANCELLED': ['SCHEDULED'],
        }