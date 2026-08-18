import logging
from datetime import timedelta
from celery import shared_task
from celery.utils.log import get_task_logger
from django.conf import settings
from django.core.cache import cache as django_cache
from django.db import transaction
from django.db.models import Count, F, Q, Sum
from django.db.models.functions import Coalesce
from django.utils import timezone
from django.core.mail import get_connection
from django.template import Context

from .models import TestMailing, TestMailingAttachment, TestMailingProcessedChunk
from .services.test_mailing_service import TestMailingService
from .services.scheduled_mailing_adapter import ScheduledMailingAdapter
from mailings.models import MailLog

logger = get_task_logger(__name__)

CHUNK_SIZE = settings.MAILING_SETTINGS['CHUNK_SIZE']
MAX_RETRIES = settings.MAILING_SETTINGS['MAX_RETRIES']
TASK_TIME_LIMIT = settings.MAILING_SETTINGS['TASK_TIME_LIMIT']
TASK_SOFT_TIME_LIMIT = settings.MAILING_SETTINGS['TASK_SOFT_TIME_LIMIT']
STUCK_THRESHOLD_HOURS = settings.MAILING_SETTINGS.get('STUCK_CHUNK_THRESHOLD_HOURS', 2)


def _execute_smoke_test(mailing, template, sender, inline_images, task_id) -> bool:
    """Execute smoke test by sending to test_email."""
    ctx = {
        "sender_name": getattr(sender, 'name', 'Team') if sender else 'Team',
        "sender_email": getattr(sender, 'email', '') if sender else '',
        "current_year": timezone.now().year,
        "client_name": "Test",
        "company_name": "Test",
        "contact_email": "test@example.com",
        **(mailing.context_variables or {})
    }
    for cid in inline_images:
        ctx[cid] = cid
    
    conn = None
    try:
        html = template.compiled_template.render(Context(ctx))
        from mailings.services.email_sender import send_email
        conn = get_connection()
        from_email = f"{sender.name} <{sender.email}>" if sender else "no-reply@example.com"
        send_email(
            subject=template.subject, html_body=html, from_email=from_email,
            to_email=mailing.test_email, inline_images=inline_images,
            attachments=[], connection=conn
        )
        return True
    except Exception:
        logger.exception("smoke_test_failed", extra={
            "test_mailing_id": mailing.id, "task_id": task_id, "test_email": mailing.test_email
        })
        return False
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


@shared_task(bind=True, name="test_mailing.tasks.run_test_mailing", acks_late=True,
             reject_on_worker_lost=True, max_retries=MAX_RETRIES, retry_backoff=30,
             time_limit=TASK_TIME_LIMIT, soft_time_limit=TASK_SOFT_TIME_LIMIT)
def run_test_mailing(self, test_mailing_id: int):
    """Main orchestrator for scheduled mailings."""
    task_id = self.request.id
    retry_count = self.request.retries

    try:
        mailing = TestMailing.objects.select_related(
            'email_template__mail_type', 'sender_email'
        ).prefetch_related('attachments').get(id=test_mailing_id)
        
        adapter = ScheduledMailingAdapter.from_test_mailing(mailing)
        init_result = adapter.initialize_processing(task_id, retry_count=retry_count)
        
        if init_result.get("status") == "skipped":
            return init_result

        inline_images = adapter.prepare_inline_images(task_id)

        if mailing.test_email:
            if not _execute_smoke_test(mailing, mailing.email_template, mailing.sender_email, inline_images, task_id):
                TestMailing.objects.filter(id=test_mailing_id).update(
                    status='FAILED', error_message='Smoke test failed.', updated_at=timezone.now()
                )
                return {"status": "failed", "reason": "smoke_test_failed"}

        dispatch_result = adapter.dispatch_chunks(CHUNK_SIZE)
        return dispatch_result

    except TestMailing.DoesNotExist:
        return {"status": "skipped", "reason": "not_found"}
    except Exception as e:
        logger.exception("run_test_mailing_critical_failure", extra={
            "task_id": task_id, "test_mailing_id": test_mailing_id
        })
        if self.request.retries >= self.max_retries:
            TestMailing.objects.filter(id=test_mailing_id).update(
                status='FAILED', error_message=repr(e)[:300], updated_at=timezone.now()
            )
            return {"status": "failed"}
        raise self.retry(exc=e)


@shared_task(bind=True, name="test_mailing.tasks.send_test_mailing_chunk", acks_late=True,
             max_retries=1, time_limit=TASK_TIME_LIMIT, soft_time_limit=TASK_SOFT_TIME_LIMIT)
def send_test_mailing_chunk(self, client_ids: list, mailing_id: int, chunk_index: int,
                             is_retry: bool = False, attachment_ids: list | None = None,
                             attachment_cache_prefix: str = 'test_mailing_attachments'):
    """Chunk worker - delegates to adapter."""
    task_id = self.request.id
    
    try:
        mailing = TestMailing.objects.select_related('email_template__mail_type', 'sender_email').get(id=mailing_id)
        adapter = ScheduledMailingAdapter.from_test_mailing(
            mailing, attachment_ids=attachment_ids, attachment_cache_prefix=attachment_cache_prefix
        )
        
        return adapter.execute_chunk(
            client_ids=client_ids, chunk_index=chunk_index, task_id=task_id,
            is_retry=is_retry, attachment_ids=attachment_ids, attachment_cache_prefix=attachment_cache_prefix
        )
    except Exception:
        logger.exception("test_chunk_failed", extra={"task_id": task_id})
        raise


@shared_task(bind=True, name="test_mailing.tasks.on_test_mailing_chunk_completed", acks_late=True,
             reject_on_worker_lost=True, time_limit=60, soft_time_limit=50)
def on_test_mailing_chunk_completed(self, test_mailing_id: int):
    """Aggregator - delegates to adapter and cleans up schedule."""
    try:
        mailing = TestMailing.objects.get(id=test_mailing_id)
        adapter = ScheduledMailingAdapter.from_test_mailing(mailing)
        result = adapter.aggregate_chunk_completion()
        
        if result.get("final_status") in ('SENT', 'FAILED'):
            try:
                fresh_mailing = TestMailing.objects.filter(id=test_mailing_id).first()
                if fresh_mailing:
                    TestMailingService.disable_schedule(fresh_mailing)
            except Exception:
                logger.warning("Failed to disable beat task after completion", exc_info=True)
        
        return result
    except TestMailing.DoesNotExist:
        return {"status": "skipped"}
    except Exception:
        logger.exception("aggregator_failed", extra={"test_mailing_id": test_mailing_id})
        return {"status": "error"}


@shared_task(name="test_mailing.tasks.reconcile_stuck_test_mailings", reject_on_worker_lost=True,
             time_limit=300, soft_time_limit=270)
def reconcile_stuck_test_mailings():
    """Reconciler for stuck mailings and chunks."""
    lock_id = "reconcile_stuck_test_mailings_lock"
    if not django_cache.add(lock_id, "1", timeout=300):
        return {"status": "skipped"}

    try:
        stuck_threshold = timezone.now() - timedelta(hours=STUCK_THRESHOLD_HOURS)

        # Scenario 1: All chunks completed but mailing still DISPATCHED
        stuck_mailings = TestMailing.objects.filter(status='DISPATCHED').annotate(
            total_completed=Count('processed_chunks', filter=Q(processed_chunks__status='COMPLETED'))
        ).filter(total_completed__gte=F('total_chunks'))

        for mailing in stuck_mailings:
            with transaction.atomic():
                locked = TestMailing.objects.select_for_update().filter(
                    id=mailing.id, status='DISPATCHED'
                ).first()
                if not locked:
                    continue
                
                stats = TestMailingProcessedChunk.objects.filter(
                    test_mailing_id=mailing.id, status='COMPLETED'
                ).aggregate(
                    total_success=Coalesce(Sum('success_count'), 0),
                    total_failures=Coalesce(Sum('failure_count'), 0)
                )
                
                final_status = 'FAILED' if stats['total_success'] == 0 and stats['total_failures'] > 0 else 'SENT'
                locked.status = final_status
                locked.completed_at = timezone.now()
                locked.completed_chunks = locked.total_chunks
                locked.successful_sends = stats['total_success']
                locked.failed_sends = stats['total_failures']
                locked.save()
                
                logger.warning("reconciler_fixed_stuck_test_mailing", extra={
                    "test_mailing_id": mailing.id, "final_status": final_status
                })
                try:
                    TestMailingService.disable_schedule(locked)
                except Exception:
                    logger.warning("reconciler_failed_to_disable_schedule", extra={"test_mailing_id": mailing.id}, exc_info=True)

        # Scenario 2: Stuck PROCESSING chunks
        stuck_chunks = TestMailingProcessedChunk.objects.filter(
            status='PROCESSING', created_at__lte=stuck_threshold
        ).select_related('test_mailing')

        for chunk in stuck_chunks:
            mailing = chunk.test_mailing
            if mailing.status != 'DISPATCHED':
                # FIX: Explicitly zero counts when marking stuck chunk COMPLETED
                # (same pattern as mailings/tasks.py)
                with transaction.atomic():
                    locked_chunk = TestMailingProcessedChunk.objects.select_for_update().get(id=chunk.id)
                    if locked_chunk.success_count != 0 or locked_chunk.failure_count != 0:
                        logger.warning(
                            "reconciler_stuck_chunk_had_nonzero_counts",
                            extra={"chunk_id": chunk.id, "success": locked_chunk.success_count, "failure": locked_chunk.failure_count}
                        )
                    locked_chunk.success_count = 0
                    locked_chunk.failure_count = 0
                    locked_chunk.status = 'COMPLETED'
                    locked_chunk.save(update_fields=['status', 'success_count', 'failure_count'])
                continue

            inferred_success = inferred_failure = 0
            if chunk.original_task_id:
                log_stats = MailLog.objects.filter(task_id=chunk.original_task_id).aggregate(
                    success=Count('id', filter=Q(status='SENT')),
                    failures=Count('id', filter=Q(status='FAILED')),
                )
                inferred_success = log_stats['success'] or 0
                inferred_failure = log_stats['failures'] or 0

            with transaction.atomic():
                locked = TestMailing.objects.select_for_update().filter(
                    id=mailing.id, status='DISPATCHED'
                ).first()
                if not locked:
                    continue

                locked_chunk = TestMailingProcessedChunk.objects.select_for_update().get(
                    id=chunk.id, status='PROCESSING'
                )
                locked_chunk.success_count = inferred_success
                locked_chunk.failure_count = inferred_failure
                locked_chunk.status = 'COMPLETED'
                locked_chunk.save(update_fields=['status', 'success_count', 'failure_count'])

                logger.warning("reconciler_fixed_stuck_chunk", extra={
                    "test_mailing_id": mailing.id, "chunk_index": locked_chunk.chunk_index,
                    "inferred_success": inferred_success, "inferred_failure": inferred_failure
                })

                remaining = TestMailingProcessedChunk.objects.filter(
                    test_mailing_id=mailing.id, status='PROCESSING'
                ).count()
                
                if remaining == 0:
                    stats = TestMailingProcessedChunk.objects.filter(
                        test_mailing_id=mailing.id, status='COMPLETED'
                    ).aggregate(
                        total_success=Coalesce(Sum('success_count'), 0),
                        total_failures=Coalesce(Sum('failure_count'), 0),
                    )
                    final_status = 'FAILED' if stats['total_success'] == 0 and stats['total_failures'] > 0 else 'SENT'
                    locked.status = final_status
                    locked.completed_at = timezone.now()
                    locked.completed_chunks = locked.total_chunks
                    locked.successful_sends = stats['total_success']
                    locked.failed_sends = stats['total_failures']
                    locked.save()
                    
                    logger.warning("reconciler_fixed_stuck_mailing_from_chunks", extra={
                        "test_mailing_id": mailing.id, "final_status": final_status
                    })
                    try:
                        TestMailingService.disable_schedule(locked)
                    except Exception:
                        logger.warning("reconciler_failed_to_disable_schedule", extra={"test_mailing_id": mailing.id}, exc_info=True)

    finally:
        django_cache.delete(lock_id)

    return {"status": "completed"}


@shared_task(name="test_mailing.tasks.cleanup_old_beat_tasks")
def cleanup_old_beat_tasks():
    """Scheduled task to clean up old beat tasks."""
    deleted_count = TestMailingService.cleanup_old_tasks(days=30)
    return {"deleted_count": deleted_count}