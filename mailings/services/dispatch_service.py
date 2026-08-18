import logging
from django.conf import settings
from django.db import transaction

from client.models import Client
from mailings.models import Mailing, MailingAttachment, SenderEmail
from templates.models import EmailTemplate, MailType

logger = logging.getLogger(__name__)

MAX_ATTACHMENT_SIZE = getattr(settings, 'MAILING_SETTINGS', {}).get('MAX_ATTACHMENT_SIZE', 10 * 1024 * 1024)


def _validate_template(template) -> dict:
    """
    Validate that a template is ready for use.
    
    FIXED: The old pattern `getattr(template, 'compiled_template', None) is None`
    silently broke when compiled_template changed from @cached_property to @property,
    because @property never returns None - it either returns a Template or raises
    an exception (which getattr catches, not converts to None).
    
    The fix: Use template.is_template_valid which properly catches syntax errors.
    """
    if not template.template_content:
        return {"success": False, "error": "Template has no content."}
    
    if not template.is_template_valid:
        return {"success": False, "error": "Template contains syntax errors and cannot be compiled."}
    
    if not template.subject:
        return {"success": False, "error": "Template subject is required."}
    
    return {"success": True}


def prepare_and_dispatch_bulk_send(
    *, client_ids: list[int], mail_type_id: int, email_template_id: int,
    sender_id: int, subject: str = '', uploaded_files: list | None = None,
    user_id: int | None = None, campaign_name: str = 'Instant Bulk Send',
    dynamic_vars: dict | None = None,
) -> dict:
    deps = _validate_dependencies(mail_type_id, email_template_id, sender_id)
    if not deps["success"]: 
        return deps

    template_validation = _validate_template(deps["template"])
    if not template_validation["success"]:
        return template_validation

    if uploaded_files:
        for f in uploaded_files:
            if hasattr(f, 'size') and f.size > MAX_ATTACHMENT_SIZE:
                return {"success": False, "error": f"'{f.name}' exceeds {MAX_ATTACHMENT_SIZE // (1024*1024)}MB limit."}

    valid_ids = list(Client.objects.filter(id__in=client_ids, is_active=True).values_list('id', flat=True))
    if not valid_ids:
        return {"success": False, "error": "No active clients found for the given IDs."}

    try:
        mailing = _create_mailing_record(
            name=campaign_name, mail_type=deps["mail_type"], template=deps["template"],
            sender=deps["sender"], subject=subject or deps["template"].subject,
            dynamic_vars=dynamic_vars or {}, user_id=user_id,
            valid_client_ids=valid_ids, uploaded_files=uploaded_files or [],
        )
    except Exception as e:
        logger.exception("Failed to create Mailing record")
        return {"success": False, "error": f"Failed to create mailing record: {e}"}

    try:
        from mailings.tasks import run_mailing
        run_mailing.delay(mailing.id)
    except Exception as e:
        return {"success": True, "mailing_id": mailing.id, "warning": f"Mailing saved but dispatch failed: {e}"}

    return {"success": True, "mailing_id": mailing.id, "total_recipients": len(valid_ids)}


def dispatch_existing_mailing(mailing_id: int) -> dict:
    try:
        mailing = Mailing.objects.get(id=mailing_id)
    except Mailing.DoesNotExist:
        return {"success": False, "error": f"Mailing id={mailing_id} not found."}
    
    if not mailing.is_dispatchable:
        return {"success": False, "error": f"Status '{mailing.status}' cannot be dispatched."}
    
    template_validation = _validate_template(mailing.email_template)
    if not template_validation["success"]:
        return template_validation

    from mailings.tasks import run_mailing
    run_mailing.delay(mailing_id)
    return {"success": True, "mailing_id": mailing_id}


def _validate_dependencies(mail_type_id, email_template_id, sender_id):
    try: 
        mail_type = MailType.objects.get(id=mail_type_id)
    except MailType.DoesNotExist: 
        return {"success": False, "error": f"MailType id={mail_type_id} not found."}
    try: 
        template = EmailTemplate.objects.get(id=email_template_id)
    except EmailTemplate.DoesNotExist: 
        return {"success": False, "error": f"EmailTemplate id={email_template_id} not found."}
    try: 
        sender = SenderEmail.objects.get(id=sender_id)
    except SenderEmail.DoesNotExist: 
        return {"success": False, "error": f"SenderEmail id={sender_id} not found."}
    return {"success": True, "mail_type": mail_type, "template": template, "sender": sender}


@transaction.atomic
def _create_mailing_record(*, name, mail_type, template, sender, subject, dynamic_vars, user_id, valid_client_ids, uploaded_files) -> Mailing:
    mailing = Mailing.objects.create(
        name=name, mail_type=mail_type, email_template=template, sender_email=sender,
        subject=subject, context_variables=dynamic_vars, created_by_id=user_id,
        status='DRAFT', total_recipients=len(valid_client_ids),
    )
    mailing.recipients.set(valid_client_ids)

    for f in uploaded_files:
        try:
            MailingAttachment.objects.create(
                mailing=mailing, filename=f.name,
                content_type=getattr(f, 'content_type', 'application/octet-stream'),
                file_size=getattr(f, 'size', 0), file=f,
            )
        except Exception as e:
            logger.exception("Failed to save attachment for mailing %s", mailing.id)
            raise
    return mailing