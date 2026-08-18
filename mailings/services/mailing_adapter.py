from typing import Optional
from mailings.services.base_mailing_processor import BaseMailingProcessor, MailingConfig
from mailings.models import Mailing, MailingAttachment, MailingProcessedChunk


class MailingAdapter(BaseMailingProcessor):
    """Adapter that makes Mailing work with BaseMailingProcessor."""
    
    def get_chunk_model(self):
        return MailingProcessedChunk

    def get_mailing_model(self):
        return Mailing

    def get_chunk_fk_field_name(self) -> str:
        return 'mailing'

    def get_lock_key_prefix(self) -> str:
        return 'mailing'

    def get_success_status(self) -> str:
        return 'COMPLETED'

    def get_dispatched_status(self) -> str:
        return 'DISPATCHED'

    def get_chunk_worker_task(self):
        from mailings.tasks import send_mailing_chunk
        return send_mailing_chunk

    def get_aggregator_task(self):
        from mailings.tasks import on_mailing_chunk_completed
        return on_mailing_chunk_completed

    @classmethod
    def from_mailing(
        cls, 
        mailing: Mailing,
        # FIX #6: Allow overriding attachment info (from task args)
        attachment_ids: Optional[list] = None,
        attachment_cache_prefix: str = 'mailing_attachments'
    ) -> 'MailingAdapter':
        """Factory method to create adapter from Mailing instance."""
        config = MailingConfig(
            mailing_id=mailing.id,
            template=mailing.email_template,
            mail_type=mailing.mail_type,
            sender=mailing.sender_email,
            subject=mailing.subject or mailing.email_template.subject,
            context_variables=mailing.context_variables or {},
            created_by_id=mailing.created_by_id,
            campaign_name=mailing.name,
            attachment_model=MailingAttachment,
            # FIX #6: Use provided IDs or fetch from mailing
            attachment_ids=tuple(attachment_ids) if attachment_ids is not None else tuple(mailing.attachments.values_list('id', flat=True)),
            attachment_cache_prefix=attachment_cache_prefix,
            scheduled_mailing_id=None,
            is_scheduled=False
        )
        return cls(config)