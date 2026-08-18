import logging
import time
from celery import shared_task
from django.conf import settings
from django.core.cache import cache
from django.core.mail import get_connection
from django.template import Context
import smtplib

from client.models import Client
from mailings.models import MailLog, SenderEmail
from mailings.services.email_sender import send_email
from mailings.services.context_builder import build_email_context
from templates.models import EmailTemplate

logger = logging.getLogger(__name__)

SMTP_THROTTLE_SECONDS = settings.MAILING_SETTINGS.get('SMTP_THROTTLE_SECONDS', 0)
CIRCUIT_BREAKER_THRESHOLD = settings.MAILING_SETTINGS.get('CIRCUIT_BREAKER_THRESHOLD', 50)
BULK_CREATE_BATCH_SIZE = settings.MAILING_SETTINGS.get('BULK_CREATE_BATCH_SIZE', 500)
TASK_TIME_LIMIT = settings.MAILING_SETTINGS.get('TASK_TIME_LIMIT', 300)
TASK_SOFT_TIME_LIMIT = settings.MAILING_SETTINGS.get('TASK_SOFT_TIME_LIMIT', 270)


@shared_task(
    bind=True,
    max_retries=1,
    time_limit=TASK_TIME_LIMIT,
    soft_time_limit=TASK_SOFT_TIME_LIMIT,
    rate_limit='50/m',  # Natural spacing between task executions
    name="campaigns.tasks.send_bulk_mails"
)
def send_bulk_mails(
    self,
    client_ids: list,
    mail_type_id: int,
    email_template_id: int,
    sender_id: int,
    subject: str,
    attachments_cache_key: str = None,
    inline_images_cache_key: str = None,
    user_id: int = None,
    campaign_name: str = '',
    dynamic_vars: dict = None,
    test_mailing_id: int = None,
    chunk_index: int = None,
):
    """
    Worker task that sends emails to a chunk of recipients.
    
    This is the campaigns equivalent of mailings.tasks.send_mailing_chunk.
    It retrieves cached resources, builds emails, and creates MailLog entries.
    """
    task_id = self.request.id
    dynamic_vars = dynamic_vars or {}

    # ─── 1. Load resources ─────────────────────────────────────────
    try:
        template = EmailTemplate.objects.select_related('mail_type').get(id=email_template_id)
        mail_type = template.mail_type
        sender = SenderEmail.objects.get(id=sender_id) if sender_id else None
    except EmailTemplate.DoesNotExist:
        logger.error("Template %d not found for campaign chunk.", email_template_id)
        return {"status": "error", "error": "template_not_found"}
    except SenderEmail.DoesNotExist:
        logger.error("Sender %d not found for campaign chunk.", sender_id)
        return {"status": "error", "error": "sender_not_found"}

    # ─── 2. Retrieve cached data ───────────────────────────────────
    inline_images = cache.get(inline_images_cache_key) if inline_images_cache_key else {}
    attachments = cache.get(attachments_cache_key) if attachments_cache_key else []

    # ─── 3. Build sender info ──────────────────────────────────────
    if sender:
        sender_info = {
            'name': sender.name,
            'email': sender.email,
            'from_email': f"{sender.name} <{sender.email}>"
        }
    else:
        sender_info = {
            'name': 'Team',
            'email': 'no-reply@example.com',
            'from_email': 'no-reply@example.com'
        }

    # ─── 4. Prepare context variables ──────────────────────────────
    # Inject CID placeholders for inline images
    ctx_vars = dynamic_vars.copy()
    for cid in inline_images:
        ctx_vars[cid] = f"cid:{cid}"

    # Merge with template defaults if any
    template_defaults = getattr(template, 'default_context', None) or {}
    ctx_vars = {**template_defaults, **ctx_vars}

    # ─── 5. Load clients ───────────────────────────────────────────
    clients = list(
        Client.objects.select_related('user')
        .only('id', 'contact_email', 'user__username', 'company_name')
        .filter(id__in=client_ids, is_active=True)
    )

    if not clients:
        logger.warning("No active clients found for campaign chunk.")
        return {"status": "completed", "success": 0, "failed": 0}

    # ─── 6. Compile template once ──────────────────────────────────
    try:
        compiled_template = template.compiled_template
    except Exception as e:
        logger.error("Template compilation failed: %s", repr(e)[:200])
        logs_to_create = [
            _create_mail_log(
                client=client, mail_type=mail_type, template=template,
                sender=sender, user_id=user_id, task_id=task_id,
                campaign_name=campaign_name, subject=subject,
                status='FAILED',
                error_message=f"Template compilation failed: {repr(e)[:100]}"
            )
            for client in clients
        ]
        _bulk_create_logs(logs_to_create)
        return {"status": "completed", "success": 0, "failed": len(clients)}

    # ─── 7. Send emails ────────────────────────────────────────────
    success_count = 0
    failure_count = 0
    logs_to_create = []
    processed_count = 0

    connection = get_connection()
    try:
        connection.open()

        for client in clients:
            processed_count += 1
            html = None

            try:
                ctx = build_email_context(
                    client=client,
                    sender=sender,
                    message="",
                    request_data=ctx_vars
                )
                html = compiled_template.render(Context(ctx))

                send_email(
                    subject=subject,
                    html_body=html,
                    from_email=sender_info['from_email'],
                    to_email=client.contact_email,
                    inline_images=inline_images,
                    attachments=attachments,
                    connection=connection
                )

                logs_to_create.append(_create_mail_log(
                    client=client, mail_type=mail_type, template=template,
                    sender=sender, user_id=user_id, task_id=task_id,
                    campaign_name=campaign_name, subject=subject,
                    status='SENT'
                ))
                success_count += 1
                time.sleep(SMTP_THROTTLE_SECONDS)

            except (smtplib.SMTPServerDisconnected, TimeoutError, 
                    ConnectionResetError, BrokenPipeError, OSError):
                logger.warning(
                    "SMTP network error, attempting reconnect...",
                    extra={"task_id": task_id}
                )
                try:
                    if connection:
                        connection.close()
                    connection = get_connection()
                    connection.open()

                    if not html:
                        ctx = build_email_context(
                            client=client, sender=sender,
                            message="", request_data=ctx_vars
                        )
                        html = compiled_template.render(Context(ctx))

                    send_email(
                        subject=subject, html_body=html,
                        from_email=sender_info['from_email'],
                        to_email=client.contact_email,
                        inline_images=inline_images,
                        attachments=attachments,
                        connection=connection
                    )

                    logs_to_create.append(_create_mail_log(
                        client=client, mail_type=mail_type, template=template,
                        sender=sender, user_id=user_id, task_id=task_id,
                        campaign_name=campaign_name, subject=subject,
                        status='SENT'
                    ))
                    success_count += 1
                    time.sleep(SMTP_THROTTLE_SECONDS)

                except Exception as retry_error:
                    failure_count += 1
                    logs_to_create.append(_create_mail_log(
                        client=client, mail_type=mail_type, template=template,
                        sender=sender, user_id=user_id, task_id=task_id,
                        campaign_name=campaign_name, subject=subject,
                        status='FAILED',
                        error_message=repr(retry_error)[:300]
                    ))

            except Exception as e:
                failure_count += 1
                logs_to_create.append(_create_mail_log(
                    client=client, mail_type=mail_type, template=template,
                    sender=sender, user_id=user_id, task_id=task_id,
                    campaign_name=campaign_name, subject=subject,
                    status='FAILED',
                    error_message=repr(e)[:300]
                ))

            # Circuit breaker
            if failure_count >= CIRCUIT_BREAKER_THRESHOLD:
                logger.error("Circuit breaker triggered for task %s", task_id)
                for remaining_client in clients[processed_count:]:
                    logs_to_create.append(_create_mail_log(
                        client=remaining_client, mail_type=mail_type,
                        template=template, sender=sender, user_id=user_id,
                        task_id=task_id, campaign_name=campaign_name,
                        subject=subject, status='FAILED',
                        error_message='Circuit breaker triggered'
                    ))
                    failure_count += 1
                break

    except Exception as catastrophic_error:
        logger.exception(
            "Catastrophic error in campaign chunk",
            extra={"task_id": task_id}
        )
        for remaining_client in clients[processed_count - 1:]:
            logs_to_create.append(_create_mail_log(
                client=remaining_client, mail_type=mail_type,
                template=template, sender=sender, user_id=user_id,
                task_id=task_id, campaign_name=campaign_name,
                subject=subject, status='FAILED',
                error_message=repr(catastrophic_error)[:300]
            ))
            failure_count += 1

    finally:
        try:
            connection.close()
        except Exception:
            pass

    # ─── 8. Bulk create logs ───────────────────────────────────────
    _bulk_create_logs(logs_to_create)

    logger.info(
        "Campaign chunk completed: %d success, %d failed",
        success_count, failure_count
    )

    return {"status": "completed", "success": success_count, "failed": failure_count}


def _create_mail_log(
    client,
    mail_type,
    template,
    sender,
    user_id,
    task_id,
    campaign_name,
    subject,
    status,
    error_message=''
) -> MailLog:
    """Create a MailLog instance for campaign execution."""
    return MailLog(
        client=client,
        mail_type=mail_type,
        template_used=template,
        sender_email=sender,
        created_by_id=user_id,
        task_id=task_id,
        campaign_name=campaign_name,
        subject=subject,
        status=status,
        error_message=error_message
    )


def _bulk_create_logs(logs: list):
    """Bulk create MailLog entries in batches."""
    if not logs:
        return
    for i in range(0, len(logs), BULK_CREATE_BATCH_SIZE):
        MailLog.objects.bulk_create(logs[i:i + BULK_CREATE_BATCH_SIZE])