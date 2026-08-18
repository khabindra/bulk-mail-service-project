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

from mailings.models import Mailing, MailingAttachment, MailingProcessedChunk, MailLog
from mailings.services.mailing_adapter import MailingAdapter

logger = get_task_logger(__name__)

CHUNK_SIZE = settings.MAILING_SETTINGS['CHUNK_SIZE']
MAX_RETRIES = settings.MAILING_SETTINGS['MAX_RETRIES']
TASK_TIME_LIMIT = settings.MAILING_SETTINGS['TASK_TIME_LIMIT']
TASK_SOFT_TIME_LIMIT = settings.MAILING_SETTINGS['TASK_SOFT_TIME_LIMIT']
STUCK_THRESHOLD_HOURS = settings.MAILING_SETTINGS.get('STUCK_CHUNK_THRESHOLD_HOURS', 2)


@shared_task(bind=True, name="mailings.tasks.run_mailing", acks_late=True, reject_on_worker_lost=True,
             max_retries=MAX_RETRIES, retry_backoff=30, time_limit=TASK_TIME_LIMIT, soft_time_limit=TASK_SOFT_TIME_LIMIT)
def run_mailing(self, mailing_id: int):
    task_id = self.request.id
    retry_count = self.request.retries

    try:
        mailing = Mailing.objects.select_related('email_template__mail_type', 'sender_email').prefetch_related('attachments').get(id=mailing_id)
        adapter = MailingAdapter.from_mailing(mailing)
        init_result = adapter.initialize_processing(task_id, retry_count=retry_count)
        
        if init_result.get("status") == "skipped":
            return init_result

        dispatch_result = adapter.dispatch_chunks(CHUNK_SIZE)
        return dispatch_result

    except Mailing.DoesNotExist:
        return {"status": "skipped", "reason": "not_found"}
    except Exception as e:
        logger.exception("run_mailing_critical_failure", extra={"task_id": task_id, "mailing_id": mailing_id})
        if self.request.retries >= self.max_retries:
            Mailing.objects.filter(id=mailing_id).update(status='FAILED', error_message=repr(e)[:300], updated_at=timezone.now())
            return {"status": "failed"}
        raise self.retry(exc=e)


@shared_task(bind=True, name="mailings.tasks.send_mailing_chunk", acks_late=True, max_retries=1,
             time_limit=TASK_TIME_LIMIT, soft_time_limit=TASK_SOFT_TIME_LIMIT)
def send_mailing_chunk(self, client_ids: list, mailing_id: int, chunk_index: int, is_retry: bool = False,
                       attachment_ids: list | None = None, attachment_cache_prefix: str = 'mailing_attachments'):
    task_id = self.request.id
    
    try:
        mailing = Mailing.objects.select_related('email_template__mail_type', 'sender_email').get(id=mailing_id)
        adapter = MailingAdapter.from_mailing(mailing, attachment_ids=attachment_ids, attachment_cache_prefix=attachment_cache_prefix)
        
        return adapter.execute_chunk(client_ids=client_ids, chunk_index=chunk_index, task_id=task_id,
                                    is_retry=is_retry, attachment_ids=attachment_ids, attachment_cache_prefix=attachment_cache_prefix)
    except Exception:
        logger.exception("chunk_failed", extra={"task_id": task_id})
        raise


@shared_task(bind=True, name="mailings.tasks.on_mailing_chunk_completed", acks_late=True, reject_on_worker_lost=True,
             time_limit=60, soft_time_limit=50)
def on_mailing_chunk_completed(self, mailing_id: int):
    try:
        mailing = Mailing.objects.get(id=mailing_id)
        adapter = MailingAdapter.from_mailing(mailing)
        return adapter.aggregate_chunk_completion()
    except Mailing.DoesNotExist:
        return {"status": "skipped"}
    except Exception:
        logger.exception("aggregator_failed", extra={"mailing_id": mailing_id})
        return {"status": "error"}


@shared_task(name="mailings.tasks.reconcile_stuck_mailings_immediate", reject_on_worker_lost=True,
             time_limit=300, soft_time_limit=270)
def reconcile_stuck_mailings_immediate():
    lock_id = "reconcile_stuck_mailings_immediate_lock"
    if not django_cache.add(lock_id, "1", timeout=300):
        return {"status": "skipped"}

    try:
        stuck_threshold = timezone.now() - timedelta(hours=STUCK_THRESHOLD_HOURS)

        # Scenario 1
        stuck_mailings = Mailing.objects.filter(status='DISPATCHED').annotate(
            total_completed=Count('processed_chunks', filter=Q(processed_chunks__status='COMPLETED'))
        ).filter(total_completed__gte=F('total_chunks'))

        for mailing in stuck_mailings:
            with transaction.atomic():
                locked = Mailing.objects.select_for_update().filter(id=mailing.id, status='DISPATCHED').first()
                if not locked:
                    continue
                
                stats = MailingProcessedChunk.objects.filter(mailing_id=mailing.id, status='COMPLETED').aggregate(
                    total_success=Coalesce(Sum('success_count'), 0), total_failures=Coalesce(Sum('failure_count'), 0)
                )
                
                final_status = 'FAILED' if stats['total_success'] == 0 and stats['total_failures'] > 0 else 'COMPLETED'
                locked.status = final_status
                locked.completed_at = timezone.now()
                locked.completed_chunks = locked.total_chunks
                locked.successful_sends = stats['total_success']
                locked.failed_sends = stats['total_failures']
                locked.save()
                
                logger.warning("reconciler_fixed_stuck_mailing", extra={"mailing_id": mailing.id, "final_status": final_status})

        # Scenario 2
        stuck_chunks = MailingProcessedChunk.objects.filter(status='PROCESSING', created_at__lte=stuck_threshold).select_related('mailing')

        for chunk in stuck_chunks:
            mailing = chunk.mailing
            if mailing.status != 'DISPATCHED':
                # FIX #11: Set explicit zero counts when marking stuck chunk COMPLETED
                # Previously this left success_count and failure_count at their default (0),
                # but now we're explicit for clarity and to match the DISPATCHED path behavior.
                with transaction.atomic():
                    locked_chunk = MailingProcessedChunk.objects.select_for_update().get(id=chunk.id)
                    # Explicitly ensure counts are zero (they should be, but be defensive)
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
                    success=Count('id', filter=Q(status='SENT')), failures=Count('id', filter=Q(status='FAILED')),
                )
                inferred_success = log_stats['success'] or 0
                inferred_failure = log_stats['failures'] or 0

            with transaction.atomic():
                locked = Mailing.objects.select_for_update().filter(id=mailing.id, status='DISPATCHED').first()
                if not locked:
                    continue

                locked_chunk = MailingProcessedChunk.objects.select_for_update().get(id=chunk.id, status='PROCESSING')
                locked_chunk.success_count = inferred_success
                locked_chunk.failure_count = inferred_failure
                locked_chunk.status = 'COMPLETED'
                locked_chunk.save(update_fields=['status', 'success_count', 'failure_count'])

                logger.warning("reconciler_fixed_stuck_chunk", extra={
                    "mailing_id": mailing.id, "chunk_index": locked_chunk.chunk_index,
                    "inferred_success": inferred_success, "inferred_failure": inferred_failure
                })

                remaining = MailingProcessedChunk.objects.filter(mailing_id=mailing.id, status='PROCESSING').count()
                
                if remaining == 0:
                    stats = MailingProcessedChunk.objects.filter(mailing_id=mailing.id, status='COMPLETED').aggregate(
                        total_success=Coalesce(Sum('success_count'), 0), total_failures=Coalesce(Sum('failure_count'), 0),
                    )
                    final_status = 'FAILED' if stats['total_success'] == 0 and stats['total_failures'] > 0 else 'COMPLETED'
                    locked.status = final_status
                    locked.completed_at = timezone.now()
                    locked.completed_chunks = locked.total_chunks
                    locked.successful_sends = stats['total_success']
                    locked.failed_sends = stats['total_failures']
                    locked.save()
                    
                    logger.warning("reconciler_fixed_stuck_mailing_from_chunks", extra={"mailing_id": mailing.id, "final_status": final_status})

    finally:
        django_cache.delete(lock_id)

    return {"status": "completed"}