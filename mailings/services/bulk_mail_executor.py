import logging
import time
from django.template import Context
from django.core.mail import get_connection
from django.conf import settings
import smtplib

from mailings.services.email_sender import send_email
from mailings.services.context_builder import build_email_context
from mailings.models import MailLog

logger = logging.getLogger(__name__)

SMTP_THROTTLE_SECONDS = settings.MAILING_SETTINGS['SMTP_THROTTLE_SECONDS']
CIRCUIT_BREAKER_THRESHOLD = settings.MAILING_SETTINGS['CIRCUIT_BREAKER_THRESHOLD']
BULK_CREATE_BATCH_SIZE = settings.MAILING_SETTINGS['BULK_CREATE_BATCH_SIZE']

class BulkMailExecutor:
    def __init__(self, email_template, sender_info, inline_images, attachments, subject, dynamic_vars, sender_instance=None, scheduled_mailing_id=None):
        self.template = email_template
        self.sender_info = sender_info
        self.inline_images = inline_images
        self.attachments = attachments
        self.subject = subject
        self.dynamic_vars = dynamic_vars or {}
        self.sender_instance = sender_instance
        self.scheduled_mailing_id = scheduled_mailing_id

    def execute(self, clients, task_id, campaign_name, user_id, mailing_id=None, already_sent_ids=None):
        from types import SimpleNamespace
        safe_sender = SimpleNamespace(name=self.sender_info['name'], email=self.sender_info['email'])
        
        already_sent_ids = set(already_sent_ids or [])
        logs_to_create, failed_client_ids = [], []
        success_count = failure_count = skipped_count = processed_count = 0

        # OPTIMIZATION: Compile template once for all clients, not per-client
        # With @property (not @cached_property), each access would re-parse the template.
        # This is fast (microseconds) but pointless to repeat 200 times.
        try:
            compiled_template = self.template.compiled_template
        except Exception as e:
            # If template has syntax errors, fail fast before processing any clients
            logger.error("Template compilation failed at executor level", extra={"task_id": task_id, "error": repr(e)[:200]})
            for client in clients:
                if client.id not in already_sent_ids:
                    failed_client_ids.append(client.id)
                    logs_to_create.append(self._create_log(client, task_id, campaign_name, user_id, mailing_id, "FAILED", f"Template compilation failed: {repr(e)[:100]}"))
                    failure_count += 1
            # Bulk create logs and return immediately
            for i in range(0, len(logs_to_create), BULK_CREATE_BATCH_SIZE):
                MailLog.objects.bulk_create(logs_to_create[i:i + BULK_CREATE_BATCH_SIZE])
            return 0, failure_count, failed_client_ids, skipped_count

        connection = get_connection()
        try:
            connection.open()
            for client in clients:
                processed_count += 1
                if client.id in already_sent_ids:
                    skipped_count += 1
                    continue

                html = None
                try:
                    ctx = build_email_context(client=client, sender=safe_sender, message="", request_data=self.dynamic_vars)
                    # Use pre-compiled template
                    html = compiled_template.render(Context(ctx))

                    send_email(subject=self.subject, html_body=html, from_email=self.sender_info['from_email'], 
                               to_email=client.contact_email, inline_images=self.inline_images, 
                               attachments=self.attachments, connection=connection)
                    logs_to_create.append(self._create_log(client, task_id, campaign_name, user_id, mailing_id, "SENT"))
                    success_count += 1
                    time.sleep(SMTP_THROTTLE_SECONDS)

                except (smtplib.SMTPServerDisconnected, TimeoutError, ConnectionResetError, BrokenPipeError, OSError):
                    logger.warning("SMTP network error, attempting reconnect...", extra={"task_id": task_id})
                    try:
                        if connection: connection.close()
                        connection = get_connection(); connection.open()
                        if not html:
                            ctx = build_email_context(client=client, sender=safe_sender, message="", request_data=self.dynamic_vars)
                            html = compiled_template.render(Context(ctx))
                        
                        send_email(subject=self.subject, html_body=html, from_email=self.sender_info['from_email'], 
                                   to_email=client.contact_email, inline_images=self.inline_images, 
                                   attachments=self.attachments, connection=connection)
                        logs_to_create.append(self._create_log(client, task_id, campaign_name, user_id, mailing_id, "SENT"))
                        success_count += 1
                        time.sleep(SMTP_THROTTLE_SECONDS)
                    except Exception as e2:
                        failure_count += 1
                        failed_client_ids.append(client.id)
                        logs_to_create.append(self._create_log(client, task_id, campaign_name, user_id, mailing_id, "FAILED", repr(e2)[:300]))

                except Exception as e:
                    failure_count += 1
                    failed_client_ids.append(client.id)
                    logs_to_create.append(self._create_log(client, task_id, campaign_name, user_id, mailing_id, "FAILED", repr(e)[:300]))

                if failure_count >= CIRCUIT_BREAKER_THRESHOLD:
                    logger.error("Circuit breaker triggered for task %s", task_id)
                    for rem in clients[processed_count - 1:]:
                        if rem.id not in already_sent_ids:
                            failed_client_ids.append(rem.id)
                            logs_to_create.append(self._create_log(rem, task_id, campaign_name, user_id, mailing_id, "FAILED", "Circuit breaker triggered"))
                            failure_count += 1
                    break
        except Exception as e:
            logger.exception("Catastrophic error in executor", extra={"task_id": task_id})
            for rem in clients[processed_count - 1:]:
                if rem.id not in already_sent_ids:
                    failed_client_ids.append(rem.id)
                    logs_to_create.append(self._create_log(rem, task_id, campaign_name, user_id, mailing_id, "FAILED", repr(e)[:300]))
                    failure_count += 1
        finally:
            try: connection.close()
            except Exception: pass

        for i in range(0, len(logs_to_create), BULK_CREATE_BATCH_SIZE):
            MailLog.objects.bulk_create(logs_to_create[i:i + BULK_CREATE_BATCH_SIZE])

        return success_count, failure_count, failed_client_ids, skipped_count

    def _create_log(self, client, task_id, campaign_name, user_id, mailing_id, status, error_message=''):
        return MailLog(
            client=client, mail_type=self.template.mail_type, template_used=self.template,
            sender_email=self.sender_instance, created_by_id=user_id, task_id=task_id, 
            campaign_name=campaign_name or "", subject=self.subject, status=status, 
            error_message=error_message, mailing_id=mailing_id,
            scheduled_mailing_id=self.scheduled_mailing_id
        )