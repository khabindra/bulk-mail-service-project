import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional, Any
from django.db import transaction
from django.db.models import Count, F, Q, Sum
from django.db.models.functions import Coalesce
from django.utils import timezone
from django.core.cache import cache as django_cache
from django.conf import settings

from mailings.services.bulk_mail_executor import BulkMailExecutor
from mailings.services.inline_image_service import get_cached_inline_images
from mailings.utils.attachment_utils import get_cached_attachments_for_model
from mailings.utils.parsers import safe_dict_merge
from mailings.utils.helpers import load_inline_images_safe
from mailings.models import MailLog
from client.models import Client

logger = logging.getLogger(__name__)

# FIX #8: Load max recipients limit from settings
MAX_RECIPIENTS_LIMIT = settings.MAILING_SETTINGS.get('MAX_RECIPIENTS_LIMIT', 50000)


@dataclass(frozen=True)
class MailingConfig:
    """Immutable configuration for any mailing execution."""
    mailing_id: int
    template: Any
    mail_type: Any
    sender: Any
    subject: str
    context_variables: dict
    created_by_id: Optional[int]
    campaign_name: str
    attachment_model: Any
    attachment_ids: tuple
    attachment_cache_prefix: str
    scheduled_mailing_id: Optional[int] = None
    is_scheduled: bool = False


class BaseMailingProcessor(ABC):
    """
    Abstract base processor that handles chunk orchestration.
    """
    
    def __init__(self, config: MailingConfig):
        self._config = config

    @property
    def config(self) -> MailingConfig:
        return self._config

    @abstractmethod
    def get_chunk_model(self): pass

    @abstractmethod
    def get_mailing_model(self): pass

    @abstractmethod
    def get_chunk_fk_field_name(self) -> str: pass

    @abstractmethod
    def get_lock_key_prefix(self) -> str: pass

    @abstractmethod
    def get_success_status(self) -> str: pass

    @abstractmethod
    def get_dispatched_status(self) -> str: pass

    @abstractmethod
    def get_chunk_worker_task(self): pass

    @abstractmethod
    def get_aggregator_task(self): pass

    def build_sender_info(self, sender=None) -> dict:
        effective_sender = sender if sender is not None else self._config.sender
        if effective_sender:
            return {
                'name': effective_sender.name,
                'email': effective_sender.email,
                'from_email': f"{effective_sender.name} <{effective_sender.email}>"
            }
        return {'name': 'Team', 'email': 'no-reply@example.com', 'from_email': 'no-reply@example.com'}

    def prepare_inline_images(self, task_id: str) -> dict:
        return load_inline_images_safe(self._config.mail_type, task_id)

    def prepare_attachments(self) -> list:
        if not self._config.attachment_ids:
            return []
        return get_cached_attachments_for_model(
            self._config.attachment_model,
            list(self._config.attachment_ids),
            cache_prefix=self._config.attachment_cache_prefix
        )

    def inject_cid_placeholders(self, dynamic_vars: dict, inline_images: dict) -> dict:
        result = dynamic_vars.copy()
        for cid in inline_images:
            result[cid] = f"cid:{cid}"
        return result

    def create_executor(self, inline_images: dict, attachments: list,
                        template: Any = None, sender: Any = None,
                        subject: str = None, context_variables: dict = None) -> BulkMailExecutor:
        effective_template = template or self._config.template
        effective_sender = sender if sender is not None else self._config.sender
        effective_subject = subject if subject is not None else self._config.subject
        effective_context = context_variables if context_variables is not None else self._config.context_variables
        
        dynamic_vars = self.inject_cid_placeholders(
            safe_dict_merge(getattr(effective_template, 'default_context', None) or {}, effective_context), 
            inline_images
        )
        
        return BulkMailExecutor(
            email_template=effective_template,
            sender_info=self.build_sender_info(effective_sender),
            inline_images=inline_images,
            attachments=attachments,
            subject=effective_subject,
            dynamic_vars=dynamic_vars,
            sender_instance=effective_sender,
            scheduled_mailing_id=self._config.scheduled_mailing_id
        )

    def get_chunk_filter_kwargs(self) -> dict:
        fk_field = self.get_chunk_fk_field_name()
        return {f"{fk_field}_id": self._config.mailing_id}

    def initialize_processing(self, task_id: str, retry_count: int = 0) -> dict:
        ChunkModel = self.get_chunk_model()
        MailingModel = self.get_mailing_model()
        
        valid_initial_states = ['SCHEDULED'] if self._config.is_scheduled else ['DRAFT']
        
        with transaction.atomic():
            mailing = MailingModel.objects.select_for_update().get(id=self._config.mailing_id)
            
            is_retry_eligible = mailing.status == 'PROCESSING' and retry_count > 0
            
            if mailing.status not in valid_initial_states and not is_retry_eligible:
                return {"status": "skipped"}

            chunk_filter = self.get_chunk_filter_kwargs()
            
            stats = ChunkModel.objects.filter(**chunk_filter, status='COMPLETED').aggregate(
                total_success=Coalesce(Sum('success_count'), 0),
                total_failure=Coalesce(Sum('failure_count'), 0),
                completed_count=Count('id')
            )
            ChunkModel.objects.filter(**chunk_filter, status='PROCESSING').delete()

            mailing.status = 'PROCESSING'
            mailing.completed_chunks = stats['completed_count']
            mailing.successful_sends = stats['total_success']
            mailing.failed_sends = stats['total_failure']
            mailing.error_message = ''
            mailing.save(update_fields=['status', 'completed_chunks', 'successful_sends', 
                                         'failed_sends', 'error_message', 'updated_at'])
        
        return {"status": "initialized"}

    def dispatch_chunks(self, chunk_size: int) -> dict:
        MailingModel = self.get_mailing_model()
        chunk_worker = self.get_chunk_worker_task()
        mailing_id = self._config.mailing_id
        
        attachment_ids = list(self._config.attachment_ids)
        attachment_cache_prefix = self._config.attachment_cache_prefix
        
        def _do_dispatch(recipient_ids: list, chunk_count: int):
            for i in range(1, chunk_count + 1):
                start = (i - 1) * chunk_size
                chunk_worker.delay(
                    client_ids=recipient_ids[start:start + chunk_size],
                    mailing_id=mailing_id,
                    chunk_index=i,
                    attachment_ids=attachment_ids,
                    attachment_cache_prefix=attachment_cache_prefix
                )
        
        with transaction.atomic():
            locked = MailingModel.objects.select_for_update().get(id=mailing_id)
            
            if locked.status != 'PROCESSING':
                return {"status": "skipped"}
            
            recipient_ids = list(
                locked.recipients.filter(is_active=True).values_list('id', flat=True)
            )
            
            recipient_count = len(recipient_ids)
            
            # FIX #8: Enforce MAX_RECIPIENTS_LIMIT
            if recipient_count > MAX_RECIPIENTS_LIMIT:
                locked.status = 'FAILED'
                locked.error_message = f"Exceeds maximum recipient limit of {MAX_RECIPIENTS_LIMIT:,}"
                locked.total_recipients = recipient_count
                locked.save(update_fields=['status', 'error_message', 'total_recipients', 'updated_at'])
                logger.error(
                    "recipient_limit_exceeded",
                    extra={
                        "mailing_id": mailing_id,
                        "recipient_count": recipient_count,
                        "max_limit": MAX_RECIPIENTS_LIMIT
                    }
                )
                return {"status": "failed", "error": f"Exceeds maximum recipient limit of {MAX_RECIPIENTS_LIMIT:,}"}
            
            chunk_count = (recipient_count + chunk_size - 1) // chunk_size if recipient_count > 0 else 0

            if recipient_count == 0:
                locked.status = self.get_success_status()
                locked.total_recipients = 0
                locked.total_chunks = 0
                locked.completed_at = timezone.now()
                locked.save(update_fields=['status', 'total_recipients', 'total_chunks', 'completed_at', 'updated_at'])
                return {"status": "completed", "recipients": 0}

            locked.status = self.get_dispatched_status()
            locked.total_recipients = recipient_count
            locked.total_chunks = chunk_count
            locked.save(update_fields=['status', 'total_recipients', 'total_chunks', 'updated_at'])

            transaction.on_commit(lambda: _do_dispatch(recipient_ids, chunk_count))
        
        return {"status": "dispatched", "recipients": recipient_count}

    def execute_chunk(self, client_ids: list, chunk_index: int, task_id: str,
                      is_retry: bool = False, attachment_ids: Optional[list] = None,
                      attachment_cache_prefix: Optional[str] = None) -> dict:
        ChunkModel = self.get_chunk_model()
        MailingModel = self.get_mailing_model()
        chunk_worker = self.get_chunk_worker_task()
        aggregator = self.get_aggregator_task()
        
        chunk_filter = self.get_chunk_filter_kwargs()
        
        if not is_retry:
            try:
                with transaction.atomic():
                    chunk_record, created = ChunkModel.objects.select_for_update().get_or_create(
                        **chunk_filter, chunk_index=chunk_index,
                        defaults={'status': 'PROCESSING', 'original_task_id': task_id}
                    )
                    if not created and chunk_record.status == 'COMPLETED':
                        return {"status": "skipped"}
            except Exception:
                return {"status": "skipped"}

        try:
            mailing = MailingModel.objects.select_related('email_template__mail_type', 'sender_email').get(id=self._config.mailing_id)
            clients = list(Client.objects.select_related('user').only('id', 'contact_email', 'user__username', 'company_name').filter(id__in=client_ids, is_active=True))
        except Exception:
            logger.exception("chunk_fetch_failed", extra={"task_id": task_id})
            raise

        already_sent_ids = set()
        if is_retry:
            try:
                chunk = ChunkModel.objects.filter(**chunk_filter, chunk_index=chunk_index).first()
                if chunk and chunk.original_task_id:
                    already_sent_ids = set(MailLog.objects.filter(client_id__in=client_ids, task_id=chunk.original_task_id, status='SENT').values_list('client_id', flat=True))
            except Exception:
                logger.warning("Failed to fetch already_sent_ids", extra={"task_id": task_id})

        inline_images = get_cached_inline_images(mailing.mail_type_id)
        
        effective_attachment_ids = tuple(attachment_ids) if attachment_ids is not None else self._config.attachment_ids
        effective_cache_prefix = attachment_cache_prefix or self._config.attachment_cache_prefix
        
        attachments = get_cached_attachments_for_model(self._config.attachment_model, list(effective_attachment_ids), cache_prefix=effective_cache_prefix) if effective_attachment_ids else []
        
        local_template = mailing.email_template
        local_sender = mailing.sender_email
        local_subject = self._config.subject or mailing.email_template.subject
        local_context_vars = safe_dict_merge(getattr(mailing.email_template, 'default_context', None), mailing.context_variables)

        executor = self.create_executor(inline_images=inline_images, attachments=attachments, template=local_template, sender=local_sender, subject=local_subject, context_variables=local_context_vars)
        
        success_count, failure_count, failed_client_ids, skipped_count = executor.execute(
            clients=clients, task_id=task_id, campaign_name=self._config.campaign_name,
            user_id=self._config.created_by_id, mailing_id=self._config.mailing_id if not self._config.is_scheduled else None,
            already_sent_ids=already_sent_ids
        )

        chunk_update_filter = {**chunk_filter, 'chunk_index': chunk_index}
        
        if not is_retry:
            if failed_client_ids:
                ChunkModel.objects.filter(**chunk_update_filter).update(success_count=success_count, failure_count=failure_count)
                chunk_worker.apply_async(kwargs={"client_ids": failed_client_ids, "mailing_id": self._config.mailing_id, "chunk_index": chunk_index, "is_retry": True, "attachment_ids": list(effective_attachment_ids), "attachment_cache_prefix": effective_cache_prefix})
            else:
                ChunkModel.objects.filter(**chunk_update_filter).update(success_count=success_count, failure_count=failure_count, status='COMPLETED')
                aggregator.delay(self._config.mailing_id)
        else:
            with transaction.atomic():
                chunk = ChunkModel.objects.select_for_update().get(**chunk_update_filter)
                chunk.success_count = F('success_count') + success_count
                chunk.failure_count = F('failure_count') + failure_count
                chunk.status = 'COMPLETED'
                chunk.save(update_fields=['success_count', 'failure_count', 'status'])
            aggregator.delay(self._config.mailing_id)

        return {"success": success_count, "failed": failure_count}

    def aggregate_chunk_completion(self) -> dict:
        ChunkModel = self.get_chunk_model()
        MailingModel = self.get_mailing_model()
        
        lock_key = f"{self.get_lock_key_prefix()}_chunk_lock_{self._config.mailing_id}"
        if not django_cache.add(lock_key, "1", timeout=5):
            return {"status": "debounced"}

        chunk_filter = self.get_chunk_filter_kwargs()

        try:
            with transaction.atomic():
                mailing = MailingModel.objects.select_for_update().get(id=self._config.mailing_id)
                if mailing.status != self.get_dispatched_status():
                    return {"status": "skipped"}

                stats = ChunkModel.objects.filter(**chunk_filter, status='COMPLETED').aggregate(
                    completed_chunks=Count('id'), total_success=Coalesce(Sum('success_count'), 0),
                    total_failures=Coalesce(Sum('failure_count'), 0)
                )

                mailing.successful_sends = stats['total_success']
                mailing.failed_sends = stats['total_failures']
                mailing.completed_chunks = stats['completed_chunks']

                if stats['completed_chunks'] < mailing.total_chunks:
                    mailing.save(update_fields=['successful_sends', 'failed_sends', 'completed_chunks', 'updated_at'])
                    return {"status": "in_progress"}

                final_status = 'FAILED' if stats['total_success'] == 0 and stats['total_failures'] > 0 else self.get_success_status()
                mailing.status = final_status
                mailing.completed_at = timezone.now()
                mailing.save(update_fields=['status', 'completed_at', 'completed_chunks', 'successful_sends', 'failed_sends', 'updated_at'])
                
                return {"status": "completed", "final_status": final_status}
        finally:
            django_cache.delete(lock_key)