# campaigns/tasks.py

import logging
from datetime import timedelta
from celery import shared_task
from django.core.cache import cache
from django.db import transaction
from django.utils import timezone
from django.db import models
from django.conf import settings

from .models import Campaign, Attachment, CampaignRun
from client.models import Client
from mailings.models import MailLog, SenderEmail
from mailings.services.inline_image_service import get_cached_inline_images
from mailings.services.bulk_mail_executor import BulkMailExecutor
from mailings.utils.attachment_utils import get_cached_attachments_for_model
from templates.models import EmailTemplate

logger = logging.getLogger(__name__)

CHUNK_SIZE = settings.MAILING_SETTINGS.get('CHUNK_SIZE', 200)
LOCK_TIMEOUT = 3600


def _acquire_execution_lock(campaign_id, task_id):
    lock_key = f"campaign_exec_lock_{campaign_id}"
    if cache.add(lock_key, task_id, timeout=LOCK_TIMEOUT):
        return True
    existing = cache.get(lock_key)
    if existing == task_id:
        cache.set(lock_key, task_id, timeout=LOCK_TIMEOUT)
        return True
    return False


def _release_execution_lock(campaign_id):
    cache.delete(f"campaign_exec_lock_{campaign_id}")


@shared_task(
    bind=True,
    max_retries=2,
    retry_backoff=30,
    name="campaigns.tasks.execute_campaign"
)
def execute_campaign(self, campaign_id):
    """
    Orchestrator: validates campaign, dispatches chunked workers.
    """
    task_id = self.request.id
    is_retrying = False

    try:
        campaign = Campaign.objects.select_related(
            'email_template__mail_type',
            'sender_email'
        ).get(id=campaign_id)

        if campaign.status != Campaign.StatusChoices.ACTIVE:
            logger.warning(
                "Campaign '%s' is %s, skipping.",
                campaign.name, campaign.status
            )
            return "Skipped: Not active"

        if not _acquire_execution_lock(campaign_id, task_id):
            logger.warning(
                "Campaign '%s' locked by another execution, skipping.",
                campaign.name
            )
            return "Skipped: Already running"

        template = campaign.email_template

        # ═══════════════════════════════════════════════════════════
        # CHECK #1: Template validity check before creating CampaignRun
        # ═══════════════════════════════════════════════════════════
        if not template.is_template_valid:
            campaign_run, _ = CampaignRun.objects.get_or_create(
                campaign=campaign,
                task_id=task_id,
                defaults={
                    'status': CampaignRun.StatusChoices.FAILED,
                    'error_message': "Template contains syntax errors and cannot be compiled."
                }
            )
            logger.error(
                "Campaign '%s' template invalid, aborting.",
                campaign.name
            )
            return "Failed: invalid template"

        mail_type = template.mail_type
        sender = campaign.sender_email

        # ═══════════════════════════════════════════════════════════
        # CHECK #6: get_or_create for CampaignRun (idempotency)
        # ═══════════════════════════════════════════════════════════
        campaign_run, created = CampaignRun.objects.get_or_create(
            campaign=campaign,
            task_id=task_id,
            defaults={'status': CampaignRun.StatusChoices.DISPATCHED}
        )

        if not created:
            logger.info(
                "Reusing existing CampaignRun %d for task %s",
                campaign_run.id, task_id
            )
            # Don't release lock — original execution owns it
            is_retrying = True
            return "Skipped: run already exists"
        recipient_ids = list(
            campaign.recipients.filter(is_active=True).values_list('id', flat=True)
        )

        if not recipient_ids:
            logger.warning("Campaign '%s' has no active recipients.", campaign.name)
            campaign_run.status = CampaignRun.StatusChoices.COMPLETED
            campaign_run.recipient_count = 0
            campaign_run.save(update_fields=['status', 'recipient_count'])
            return "No recipients"

        attachment_ids = list(campaign.attachments.values_list('id', flat=True))

        # ═══════════════════════════════════════════════════════════
        # CHECK #3: _dispatch_chunks wrapped in try/except
        # ═══════════════════════════════════════════════════════════
        try:
            chunk_count = _dispatch_chunks(
                recipient_ids=recipient_ids,
                mail_type_id=mail_type.id,
                email_template_id=template.id,
                sender_id=sender.id if sender else None,
                subject=template.subject,
                attachment_ids=attachment_ids,
                campaign_name=campaign.name,
                dynamic_vars=campaign.context_variables,
                campaign_run_id=campaign_run.id,
            )
            campaign_run.recipient_count = len(recipient_ids)
            campaign_run.chunk_count = chunk_count
            campaign_run.save(update_fields=['recipient_count', 'chunk_count'])
        except Exception as e:
            campaign_run.status = CampaignRun.StatusChoices.FAILED
            campaign_run.error_message = f"Dispatch failed: {repr(e)[:400]}"
            campaign_run.save(update_fields=['status', 'error_message'])
            logger.exception(
                "Campaign '%s' dispatch failed, marking run FAILED.",
                campaign.name
            )
            raise

        Campaign.objects.filter(id=campaign_id).update(
            last_executed_at=timezone.now(),
            execution_count=models.F('execution_count') + 1
        )

        logger.info(
            "Campaign '%s': dispatched %d chunks (%d recipients).",
            campaign.name, chunk_count, len(recipient_ids)
        )
        return f"Dispatched {chunk_count} chunks"

    except Campaign.DoesNotExist:
        logger.error("Campaign %d does not exist.", campaign_id)
        return "Not Found"

    except Exception as e:
        if self.request.retries >= self.max_retries:
            is_retrying = False
            logger.error("Campaign %d exhausted all %d retries.", campaign_id, self.max_retries)
            try:
                CampaignRun.objects.filter(
                    campaign_id=campaign_id, task_id=task_id
                ).update(
                    status=CampaignRun.StatusChoices.FAILED,
                    error_message=f"Max retries exceeded: {repr(e)[:300]}"
                )
            except Exception:
                logger.exception("Failed to update CampaignRun status")
            return "Failed: max retries exceeded"
        
        is_retrying = True
        logger.exception("Campaign %d failed, retrying.", campaign_id)
        raise self.retry(exc=e)

    finally:
        if not is_retrying:
            _release_execution_lock(campaign_id)


def _dispatch_chunks(
    recipient_ids,
    mail_type_id,
    email_template_id,
    sender_id,
    subject,
    attachment_ids,
    campaign_name,
    dynamic_vars,
    campaign_run_id,
):
    chunk_count = 0
    for i in range(0, len(recipient_ids), CHUNK_SIZE):
        chunk_ids = recipient_ids[i:i + CHUNK_SIZE]
        chunk_count += 1
        send_bulk_mails.apply_async(kwargs={
            'client_ids': chunk_ids,
            'mail_type_id': mail_type_id,
            'email_template_id': email_template_id,
            'sender_id': sender_id,
            'subject': subject,
            'attachment_ids': attachment_ids,
            'campaign_name': campaign_name,
            'dynamic_vars': dynamic_vars,
            'chunk_index': chunk_count,
            'campaign_run_id': campaign_run_id,
        })
    return chunk_count


@shared_task(
    bind=True,
    max_retries=1,
    name="campaigns.tasks.send_bulk_mails",
    rate_limit='50/m'
)
def send_bulk_mails(
    self,
    client_ids: list,
    mail_type_id: int,
    email_template_id: int,
    sender_id: int,
    subject: str,
    attachment_ids: list = None,
    campaign_name: str = '',
    dynamic_vars: dict = None,
    chunk_index: int = None,
    campaign_run_id: int = None,
):
    """Worker task that sends emails to a chunk of recipients."""
    task_id = self.request.id
    dynamic_vars = dynamic_vars or {}
    attachment_ids = attachment_ids or []

    try:
        template = EmailTemplate.objects.select_related('mail_type').get(id=email_template_id)
    except EmailTemplate.DoesNotExist:
        logger.error("Template %d not found.", email_template_id)
        _update_run_status_on_failure(campaign_run_id, "template_not_found")
        return {"status": "error", "error": "template_not_found"}

    mail_type = getattr(template, 'mail_type', None)
    if not mail_type:
        logger.error("Template %d has no mail_type.", email_template_id)
        _update_run_status_on_failure(campaign_run_id, "no_mail_type")
        return {"status": "error", "error": "no_mail_type"}

    try:
        sender = SenderEmail.objects.get(id=sender_id) if sender_id else None
    except SenderEmail.DoesNotExist:
        logger.error("Sender %d not found.", sender_id)
        _update_run_status_on_failure(campaign_run_id, "sender_not_found")
        return {"status": "error", "error": "sender_not_found"}

    inline_images = get_cached_inline_images(mail_type.id)

    attachments = []
    if attachment_ids:
        try:
            attachments = get_cached_attachments_for_model(
                Attachment, attachment_ids, cache_prefix='campaign_attachments'
            )
        except Exception as e:
            logger.error("Failed to load attachments: %s", repr(e)[:200])

    sender_info = (
        {'name': sender.name, 'email': sender.email, 'from_email': f"{sender.name} <{sender.email}>"}
        if sender else {'name': 'Team', 'email': 'no-reply@example.com', 'from_email': 'no-reply@example.com'}
    )

    ctx_vars = dynamic_vars.copy()
    for cid in inline_images:
        ctx_vars[cid] = f"cid:{cid}"
    ctx_vars = {**(getattr(template, 'default_context', None) or {}), **ctx_vars}

    executor = BulkMailExecutor(
        email_template=template,
        sender_info=sender_info,
        inline_images=inline_images,
        attachments=attachments,
        subject=subject,
        dynamic_vars=ctx_vars,
        sender_instance=sender,
        scheduled_mailing_id=None
    )

    clients = list(
        Client.objects.select_related('user')
        .only('id', 'contact_email', 'user__username', 'company_name')
        .filter(id__in=client_ids, is_active=True)
    )

    if not clients:
        _trigger_finalization_if_last_chunk(campaign_run_id)
        return {"status": "completed", "success": 0, "failed": 0}

    success_count, failure_count, failed_ids, skipped = executor.execute(
        clients=clients,
        task_id=task_id,
        campaign_name=campaign_name,
        user_id=None,
        mailing_id=None,
        already_sent_ids=[]
    )

    if campaign_run_id:
        try:
            CampaignRun.objects.filter(
                id=campaign_run_id,
                status=CampaignRun.StatusChoices.DISPATCHED
            ).update(status=CampaignRun.StatusChoices.PROCESSING)

            CampaignRun.objects.filter(id=campaign_run_id).update(
                completed_chunks=models.F('completed_chunks') + 1,
                successful_sends=models.F('successful_sends') + success_count,
                failed_sends=models.F('failed_sends') + failure_count
            )

            _trigger_finalization_if_last_chunk(campaign_run_id)

        except Exception:
            logger.exception("Failed to update CampaignRun chunk stats")

    logger.info(
        "Campaign '%s' chunk %d: %d success, %d failed",
        campaign_name, chunk_index, success_count, failure_count
    )
    return {"status": "completed", "success": success_count, "failed": failure_count}


# ═══════════════════════════════════════════════════════════════════
# CHECK #2: select_for_update, NOT cache.add
# ═══════════════════════════════════════════════════════════════════

def _trigger_finalization_if_last_chunk(campaign_run_id: int):
    """
    Check if all chunks are complete and trigger finalization.
    
    Uses select_for_update() for atomic check — NOT cache.add().
    Matches BaseMailingProcessor.aggregate_chunk_completion pattern.
    """
    if not campaign_run_id:
        return

    try:
        with transaction.atomic():
            run = CampaignRun.objects.select_for_update().get(id=campaign_run_id)

            if run.chunk_count == 0:
                return

            if run.status in (
                CampaignRun.StatusChoices.COMPLETED,
                CampaignRun.StatusChoices.PARTIAL,
                CampaignRun.StatusChoices.FAILED,
            ):
                return

            if run.completed_chunks >= run.chunk_count:
                finalize_campaign_run.delay(campaign_run_id)

    except CampaignRun.DoesNotExist:
        pass
    except Exception:
        logger.exception(
            "Error checking finalization for CampaignRun %d",
            campaign_run_id
        )


def _update_run_status_on_failure(campaign_run_id: int, error_msg: str):
    if not campaign_run_id:
        return
    try:
        CampaignRun.objects.filter(id=campaign_run_id).update(
            status=CampaignRun.StatusChoices.FAILED,
            error_message=error_msg[:500]
        )
    except Exception:
        logger.exception("Failed to update CampaignRun status")


# ═══════════════════════════════════════════════════════════════════
# CHECK #4: finalize_campaign_run uses select_for_update
# ═══════════════════════════════════════════════════════════════════

@shared_task(
    name="campaigns.tasks.finalize_campaign_run",
    time_limit=60,
    soft_time_limit=50
)
def finalize_campaign_run(campaign_run_id: int):
    """
    Aggregator task called after all chunks complete.
    
    Uses select_for_update() to prevent double-write race when
    the reconciler dispatches multiple finalizations simultaneously.
    """
    try:
        with transaction.atomic():
            run = CampaignRun.objects.select_for_update().get(id=campaign_run_id)

            if run.status not in (
                CampaignRun.StatusChoices.DISPATCHED,
                CampaignRun.StatusChoices.PROCESSING
            ):
                return {"status": "skipped", "reason": f"status={run.status}"}

            if run.chunk_count == 0:
                run.status = CampaignRun.StatusChoices.FAILED
                run.error_message = "No chunks were dispatched."
                run.completed_at = timezone.now()
                run.save(update_fields=['status', 'error_message', 'completed_at'])
                return {"status": "failed", "reason": "no_chunks"}

            if run.completed_chunks < run.chunk_count:
                return {
                    "status": "in_progress",
                    "completed": run.completed_chunks,
                    "total": run.chunk_count
                }

            if run.failed_sends == 0:
                final_status = CampaignRun.StatusChoices.COMPLETED
            elif run.successful_sends == 0:
                final_status = CampaignRun.StatusChoices.FAILED
            else:
                final_status = CampaignRun.StatusChoices.PARTIAL

            run.status = final_status
            run.completed_at = timezone.now()
            run.save(update_fields=['status', 'completed_at'])

        logger.info("CampaignRun %d finalized: %s", campaign_run_id, final_status)
        return {"status": final_status}

    except CampaignRun.DoesNotExist:
        return {"status": "not_found"}
    except Exception:
        logger.exception("Error finalizing CampaignRun %d", campaign_run_id)
        return {"status": "error"}


@shared_task(
    name="campaigns.tasks.reconcile_stuck_campaign_runs",
    reject_on_worker_lost=True,
    time_limit=300,
    soft_time_limit=270
)
def reconcile_stuck_campaign_runs():
    """Recover stuck CampaignRun records via async finalization dispatch."""
    from django.core.cache import cache as django_cache

    lock_id = "reconcile_stuck_campaign_runs_lock"
    if not django_cache.add(lock_id, "1", timeout=300):
        return {"status": "skipped", "reason": "lock_held"}

    try:
        stuck_threshold = timezone.now() - timedelta(hours=2)

        stuck_runs = CampaignRun.objects.filter(
            status__in=[
                CampaignRun.StatusChoices.DISPATCHED,
                CampaignRun.StatusChoices.PROCESSING,
            ],
            started_at__lte=stuck_threshold
        ).select_related('campaign')

        reconciled_count = 0
        for run in stuck_runs:
            finalize_campaign_run.delay(run.id)
            reconciled_count += 1
            logger.warning(
                "Dispatched finalization for stuck CampaignRun %d (status=%s, chunks=%d/%d)",
                run.id, run.status, run.completed_chunks, run.chunk_count
            )

        return {"status": "completed", "dispatched": reconciled_count}

    finally:
        django_cache.delete(lock_id)


@shared_task(
    name="campaigns.tasks.cleanup_old_campaign_runs",
    time_limit=300,
    soft_time_limit=270
)
def cleanup_old_campaign_runs(days: int = 90):
    """Periodic cleanup of old CampaignRun records."""
    cutoff = timezone.now() - timedelta(days=days)
    deleted, _ = CampaignRun.objects.filter(
        started_at__lte=cutoff,
        status__in=[
            CampaignRun.StatusChoices.COMPLETED,
            CampaignRun.StatusChoices.PARTIAL,
            CampaignRun.StatusChoices.FAILED,
        ]
    ).delete()

    if deleted:
        logger.info("Cleaned up %d old CampaignRun records", deleted)
    return {"deleted": deleted}