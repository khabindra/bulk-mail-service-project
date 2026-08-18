from typing import Optional
from mailings.services.base_mailing_processor import BaseMailingProcessor, MailingConfig
from test_mailing.models import TestMailing, TestMailingAttachment, TestMailingProcessedChunk


class ScheduledMailingAdapter(BaseMailingProcessor):
    """Adapter that makes TestMailing work with BaseMailingProcessor."""
    
    def get_chunk_model(self):
        return TestMailingProcessedChunk

    def get_mailing_model(self):
        return TestMailing

    def get_chunk_fk_field_name(self) -> str:
        return 'test_mailing'

    def get_lock_key_prefix(self) -> str:
        return 'test_mailing'

    def get_success_status(self) -> str:
        return 'SENT'

    def get_dispatched_status(self) -> str:
        return 'DISPATCHED'

    def get_chunk_worker_task(self):
        from test_mailing.tasks import send_test_mailing_chunk
        return send_test_mailing_chunk

    def get_aggregator_task(self):
        from test_mailing.tasks import on_test_mailing_chunk_completed
        return on_test_mailing_chunk_completed

    @classmethod
    def from_test_mailing(
        cls, 
        test_mailing: TestMailing,
        # FIX #6: Allow overriding attachment info
        attachment_ids: Optional[list] = None,
        attachment_cache_prefix: str = 'test_mailing_attachments'
    ) -> 'ScheduledMailingAdapter':
        """Factory method to create adapter from TestMailing instance."""
        config = MailingConfig(
            mailing_id=test_mailing.id,
            template=test_mailing.email_template,
            mail_type=test_mailing.mail_type,
            sender=test_mailing.sender_email,
            subject=test_mailing.email_template.subject,
            context_variables=test_mailing.context_variables or {},
            created_by_id=test_mailing.created_by_id,
            campaign_name=test_mailing.name,
            attachment_model=TestMailingAttachment,
            # FIX #6: Use provided IDs or fetch from mailing
            attachment_ids=tuple(attachment_ids) if attachment_ids is not None else tuple(test_mailing.attachments.values_list('id', flat=True)),
            attachment_cache_prefix=attachment_cache_prefix,
            scheduled_mailing_id=test_mailing.id,
            is_scheduled=True
        )
        return cls(config)