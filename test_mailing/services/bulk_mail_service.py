# import base64
# import logging
# import time
# from django.template import Context
# from django.db import transaction, IntegrityError
# from django.core.mail import get_connection
# from django.core.cache import cache as django_cache
# from django.conf import settings
# import smtplib

# from mailings.models import MailLog
# from test_mailing.services.email_sender import send_email
# from mailings.services.context_builder import build_email_context
# from mailings.services.inline_image_service import load_inline_images
# from client.models import Client
# from templates.models import MailType, EmailTemplate
# from mailings.models import SenderEmail
# from test_mailing.models import MailingProcessedChunk
# from celery import shared_task

# from django.db.models import F

# logger = logging.getLogger(__name__)

# # Fix: Read all constants from centralized settings
# BULK_CREATE_BATCH_SIZE = settings.MAILING_SETTINGS['BULK_CREATE_BATCH_SIZE']
# CIRCUIT_BREAKER_THRESHOLD = settings.MAILING_SETTINGS['CIRCUIT_BREAKER_THRESHOLD']
# SMTP_THROTTLE_SECONDS = settings.MAILING_SETTINGS['SMTP_THROTTLE_SECONDS']


# def _get_cached_inline_images(mail_type_id):
#     cache_key = f"inline_images_{mail_type_id}"
#     result = django_cache.get(cache_key)
#     if result is None:
#         try:
#             mail_type = MailType.objects.get(id=mail_type_id)
#             result = load_inline_images(mail_type)
#             django_cache.set(cache_key, result, timeout=3600)
#         except Exception as e:
#             logger.error("Failed to load inline images for mail_type %s: %s", mail_type_id, e)
#             result = {}
#     return result


# def _get_cached_attachments(attachment_ids):
#     """Fix: Cache attachments to prevent memory explosion across concurrent chunks."""
#     if not attachment_ids:
#         return []
    
#     cache_key = f"mailing_attachments_{hash(tuple(sorted(attachment_ids)))}"
#     cached = django_cache.get(cache_key)
    
#     # Decode base64 back to bytes if returning from cache
#     if cached is not None:
#         return [{**att, 'content': base64.b64decode(att['content'])} for att in cached]
    
#     attachments = _load_attachments_from_db(attachment_ids)
#     if attachments:
#         # Encode bytes to base64 for JSON-serializable cache backends
#         cacheable = [{**att, 'content': base64.b64encode(att['content']).decode('utf-8')} for att in attachments]
#         django_cache.set(cache_key, cacheable, timeout=3600)
#     return attachments


# def _load_attachments_from_db(attachment_ids):
#     if not attachment_ids:
#         return []
#     from test_mailing.models import MailingAttachment
#     attachments = []
#     for att in MailingAttachment.objects.filter(id__in=attachment_ids):
#         if not att.file:
#             continue
#         try:
#             att.file.open('rb')
#             content = att.file.read()
#             attachments.append({
#                 'filename': att.file.name.split('/')[-1],
#                 'content': content,
#                 'content_type': getattr(att.file, 'content_type', 'application/octet-stream')
#             })
#         except Exception as e:
#             logger.exception("Failed to read attachment %s", att.id)
#         finally:
#             try:
#                 att.file.close()
#             except Exception:
#                 pass
#     return attachments


# @shared_task(
#     bind=True,
#     rate_limit=settings.MAILING_SETTINGS['RATE_LIMIT'],
#     max_retries=1,
#     acks_late=True,                # PRODUCTION GATE: Prevents silent task loss on worker crash
#     reject_on_worker_lost=True,    # PRODUCTION GATE: Requeues if worker is OOM-killed
#     time_limit=settings.MAILING_SETTINGS['TASK_TIME_LIMIT'],
#     soft_time_limit=settings.MAILING_SETTINGS['TASK_SOFT_TIME_LIMIT'],
# )
# def send_bulk_mails(
#     self, client_ids, mail_type_id, email_template_id, sender_id,
#     subject, attachment_ids, user_id=None, campaign_name=None,
#     dynamic_vars=None, test_mailing_id=None, chunk_index=1, is_retry=False
# ):
#     task_id = self.request.id
#     log_extra = {"task_id": task_id, "mailing_id": test_mailing_id, "chunk_index": chunk_index}

#     if test_mailing_id and not is_retry:
#         try:
#             with transaction.atomic():
#                 chunk_record, created = MailingProcessedChunk.objects.select_for_update().get_or_create(
#                     test_mailing_id=test_mailing_id, chunk_index=chunk_index,
#                     defaults={'status': 'PROCESSING', 'original_task_id': task_id}
#                 )
#                 if not created and chunk_record.status == 'COMPLETED':
#                     return {"status": "skipped"}
#         except IntegrityError:
#             return {"status": "skipped"}

#     try:
#         clients = list(Client.objects.select_related('user').only(
#             'id', 'contact_email', 'user__username', 'company_name'
#         ).filter(id__in=client_ids, is_active=True))
#         mail_type = MailType.objects.get(id=mail_type_id)
#         email_template = EmailTemplate.objects.get(id=email_template_id)
#         sender = SenderEmail.objects.get(id=sender_id) if sender_id else None
#     except Exception:
#         logger.exception("bulk_mail_fetch_failed", extra=log_extra)
#         raise

#     compiled_template = email_template.compiled_template
#     inline_images = _get_cached_inline_images(mail_type_id)
#     prepared_attachments = _get_cached_attachments(attachment_ids)

#     already_sent_ids = set()
#     if test_mailing_id:
#         try:
#             chunk = MailingProcessedChunk.objects.get(test_mailing_id=test_mailing_id, chunk_index=chunk_index)
#             if is_retry or chunk.status == 'PROCESSING':
#                 original_task_id = chunk.original_task_id
#                 if original_task_id:
#                     already_sent_ids = set(MailLog.objects.filter(
#                         client_id__in=client_ids, task_id=original_task_id, status="SENT"
#                     ).values_list('client_id', flat=True))
#         except MailingProcessedChunk.DoesNotExist:
#             pass

#     connection = None
#     logs_to_create = []
#     success_count = 0
#     failure_count = 0
#     failed_client_ids = []
#     processed_count = 0
#     sender_info = _build_sender_info(sender)

#     try:
#         connection = get_connection()
#         connection.open()

#         for client in clients:
#             processed_count += 1

#             if client.id in already_sent_ids:
#                 success_count += 1
#                 continue

#             html = None
#             try:
#                 safe_sender = _create_safe_sender(sender_info)
#                 context_dict = build_email_context(client=client, sender=safe_sender, message="", request_data=dynamic_vars)
#                 for cid in inline_images:
#                     context_dict[cid] = cid

#                 html = compiled_template.render(Context(context_dict))

#                 send_email(
#                     subject=subject, html_body=html, from_email=sender_info['from_email'],
#                     to_email=client.contact_email, inline_images=inline_images,
#                     attachments=prepared_attachments, connection=connection
#                 )
#                 logs_to_create.append(_create_log_entry(client, mail_type, email_template, sender, user_id, task_id, campaign_name, subject, "SENT"))
#                 success_count += 1
                
#                 # Fix: Per-email SMTP throttle
#                 time.sleep(SMTP_THROTTLE_SECONDS)

#             except (smtplib.SMTPServerDisconnected, TimeoutError, ConnectionResetError, BrokenPipeError, OSError):
#                 logger.warning("smtp_network_error_attempting_reconnect", extra={**log_extra, "client_id": client.id})
#                 try:
#                     if connection:
#                         try: connection.close()
#                         except Exception: pass
#                     connection = get_connection()
#                     connection.open()

#                     if html is None:
#                         safe_sender = _create_safe_sender(sender_info)
#                         context_dict = build_email_context(client=client, sender=safe_sender, message="", request_data=dynamic_vars)
#                         for cid in inline_images:
#                             context_dict[cid] = cid
#                         html = compiled_template.render(Context(context_dict))

#                     send_email(
#                         subject=subject, html_body=html, from_email=sender_info['from_email'],
#                         to_email=client.contact_email, inline_images=inline_images,
#                         attachments=prepared_attachments, connection=connection
#                     )
#                     logs_to_create.append(_create_log_entry(client, mail_type, email_template, sender, user_id, task_id, campaign_name, subject, "SENT"))
#                     success_count += 1
#                     time.sleep(SMTP_THROTTLE_SECONDS)
#                 except Exception as e2:
#                     failure_count += 1
#                     failed_client_ids.append(client.id)
#                     logs_to_create.append(_create_log_entry(client, mail_type, email_template, sender, user_id, task_id, campaign_name, subject, "FAILED", repr(e2)[:300]))


#             except Exception as e:
#                 failure_count += 1
#                 failed_client_ids.append(client.id)
#                 logs_to_create.append(_create_log_entry(client, mail_type, email_template, sender, user_id, task_id, campaign_name, subject, "FAILED", repr(e)[:300]))

#             # PRODUCTION GATE: Circuit breaker off-by-one fix
#             if failure_count >= CIRCUIT_BREAKER_THRESHOLD:
#                 logger.error("circuit_breaker_triggered", extra={**log_extra, "failure_count": failure_count})
#                 # Current client was already processed and counted if it failed.
#                 # We slice from processed_count to get the strictly REMAINING clients.
#                 remaining_clients = clients[processed_count:]
#                 for rem_client in remaining_clients:
#                     if rem_client.id not in already_sent_ids:
#                         failed_client_ids.append(rem_client.id)
#                         logs_to_create.append(_create_log_entry(rem_client, mail_type, email_template, sender, user_id, task_id, campaign_name, subject, "FAILED", "Circuit breaker triggered"))
#                 failure_count += len(remaining_clients)
#                 break

#     except Exception as e:
#         logger.exception("bulk_mail_catastrophic_error", extra=log_extra)
#         # PRODUCTION GATE: Catastrophic off-by-one fix
#         # processed_count was already incremented for the client that just failed,
#         # so we use processed_count - 1 to include it in the remaining list.
#         remaining = clients[processed_count - 1:]
#         for rem_client in remaining:
#             if rem_client.id not in already_sent_ids:
#                 failed_client_ids.append(rem_client.id)
#                 logs_to_create.append(_create_log_entry(rem_client, mail_type, email_template, sender, user_id, task_id, campaign_name, subject, "FAILED", repr(e)[:300]))
#         failure_count += len(remaining)
#     finally:
#         if connection:
#             try: connection.close()
#             except Exception: pass

#     _bulk_create_logs(logs_to_create)

#     if test_mailing_id and not is_retry:
#         MailingProcessedChunk.objects.filter(test_mailing_id=test_mailing_id, chunk_index=chunk_index).update(
#             success_count=success_count, failure_count=failure_count
#         )

#         if failed_client_ids:
#             # Fix: Removed unnecessary Celery hop (retry_failed_mails task). Direct async call.
#             send_bulk_mails.apply_async(kwargs={
#                 "client_ids": failed_client_ids,
#                 "mail_type_id": mail_type_id,
#                 "email_template_id": email_template_id,
#                 "sender_id": sender_id,
#                 "subject": subject,
#                 "attachment_ids": attachment_ids,
#                 "user_id": user_id,
#                 "campaign_name": campaign_name,
#                 "dynamic_vars": dynamic_vars,
#                 "test_mailing_id": test_mailing_id,
#                 "chunk_index": chunk_index,
#                 "is_retry": True
#             })
#         else:
#             MailingProcessedChunk.objects.filter(test_mailing_id=test_mailing_id, chunk_index=chunk_index).update(status='COMPLETED')
#             _notify_chunk_completion(test_mailing_id)

#     elif is_retry and test_mailing_id:
#         with transaction.atomic():
#             chunk = MailingProcessedChunk.objects.select_for_update().get(test_mailing_id=test_mailing_id, chunk_index=chunk_index)
#             chunk.success_count = F('success_count') + success_count
#             chunk.failure_count = F('failure_count') + failure_count
#             chunk.status = 'COMPLETED'
#             chunk.save(update_fields=['success_count', 'failure_count', 'status'])
#         _notify_chunk_completion(test_mailing_id)

#     logger.info("chunk_processing_finished", extra={**log_extra, "success": success_count, "failed": failure_count})
#     return {"success": success_count, "failed": failure_count}


# # ─── Helper functions ─────────────────────────────

# def _build_sender_info(sender):
#     if sender:
#         return {'name': sender.name, 'email': sender.email, 'from_email': f"{sender.name} <{sender.email}>"}
#     return {'name': 'Team', 'email': 'no-reply@example.com', 'from_email': 'no-reply@example.com'}

# def _create_safe_sender(sender_info):
#     from types import SimpleNamespace
#     return SimpleNamespace(name=sender_info['name'], email=sender_info['email'])

# def _create_log_entry(client, mail_type, email_template, sender, user_id, task_id, campaign_name, subject, status, error_message=''):
#     return MailLog(
#         client=client, mail_type=mail_type, template_used=email_template,
#         sender_email=sender, created_by_id=user_id, task_id=task_id,
#         campaign_name=campaign_name or "", subject=subject, status=status, error_message=error_message
#     )

# def _bulk_create_logs(logs):
#     if not logs:
#         return
#     for i in range(0, len(logs), BULK_CREATE_BATCH_SIZE):
#         MailLog.objects.bulk_create(logs[i:i + BULK_CREATE_BATCH_SIZE])

# def _notify_chunk_completion(test_mailing_id):
#     from test_mailing.tasks import on_chunk_completed
#     on_chunk_completed.delay(test_mailing_id)


import base64
import importlib
import logging
import time
from django.template import Context
from django.db import transaction, IntegrityError
from django.core.mail import get_connection
from django.core.cache import cache as django_cache
from django.conf import settings
import smtplib

from mailings.models import MailLog
from test_mailing.services.email_sender import send_email
from mailings.services.context_builder import build_email_context
from mailings.services.inline_image_service import load_inline_images
from client.models import Client
from templates.models import MailType, EmailTemplate
from mailings.models import SenderEmail
from celery import shared_task
from django.db.models import F

logger = logging.getLogger(__name__)

BULK_CREATE_BATCH_SIZE = settings.MAILING_SETTINGS['BULK_CREATE_BATCH_SIZE']
CIRCUIT_BREAKER_THRESHOLD = settings.MAILING_SETTINGS['CIRCUIT_BREAKER_THRESHOLD']
SMTP_THROTTLE_SECONDS = settings.MAILING_SETTINGS['SMTP_THROTTLE_SECONDS']

# Defaults ensure test_mailing works without passing dotted paths
_DEFAULT_ATTACHMENT_MODEL = 'test_mailing.models.TestMailingAttachment'
_DEFAULT_CHUNK_MODEL = 'test_mailing.models.TestMailingProcessedChunk'
_DEFAULT_CHUNK_FK_FIELD = 'test_mailing'
_DEFAULT_COMPLETION_TASK = 'test_mailing.tasks.on_chunk_completed'

def _import_from_path(dotted_path: str):
    module_path, attr = dotted_path.rsplit('.', 1)
    return getattr(importlib.import_module(module_path), attr)

def _get_cached_inline_images(mail_type_id: int) -> dict:
    cache_key = f"inline_images_{mail_type_id}"
    result = django_cache.get(cache_key)
    if result is None:
        try:
            result = load_inline_images(MailType.objects.get(id=mail_type_id))
            django_cache.set(cache_key, result, timeout=3600)
        except Exception as e:
            logger.error("Failed to load inline images: %s", e)
            result = {}
    return result

def _get_cached_attachments(attachment_ids: list, attachment_model_path: str) -> list:
    if not attachment_ids: return []
    cache_key = f"mailing_attachments_{attachment_model_path}_{hash(tuple(sorted(attachment_ids)))}"
    cached = django_cache.get(cache_key)
    if cached is not None:
        return [{**att, 'content': base64.b64decode(att['content'])} for att in cached]

    AttachmentModel = _import_from_path(attachment_model_path)
    attachments = []
    for att in AttachmentModel.objects.filter(id__in=attachment_ids):
        if not att.file: continue
        try:
            att.file.open('rb')
            attachments.append({'filename': att.file.name.split('/')[-1], 'content': att.file.read(), 'content_type': getattr(att, 'content_type', 'application/octet-stream')})
        except Exception: logger.exception("Failed to read attachment %s", att.id)
        finally:
            try: att.file.close()
            except Exception: pass

    if attachments:
        django_cache.set(cache_key, [{**a, 'content': base64.b64encode(a['content']).decode('utf-8')} for a in attachments], timeout=3600)
    return attachments

@shared_task(bind=True, rate_limit=settings.MAILING_SETTINGS['RATE_LIMIT'], max_retries=1, acks_late=True, reject_on_worker_lost=True,
             time_limit=settings.MAILING_SETTINGS['TASK_TIME_LIMIT'], soft_time_limit=settings.MAILING_SETTINGS['TASK_SOFT_TIME_LIMIT'])
def send_bulk_mails(self, client_ids, mail_type_id, email_template_id, sender_id, subject, attachment_ids,
                    user_id=None, campaign_name=None, dynamic_vars=None, test_mailing_id=None, 
                    mailing_id=None, chunk_index=1, is_retry=False,
                    attachment_model_path=_DEFAULT_ATTACHMENT_MODEL, chunk_model_path=_DEFAULT_CHUNK_MODEL,
                    chunk_fk_field=_DEFAULT_CHUNK_FK_FIELD, completion_task_path=_DEFAULT_COMPLETION_TASK):
    
    task_id = self.request.id
    log_extra = {"task_id": task_id, "mailing_id": test_mailing_id, "chunk_index": chunk_index}
    ChunkModel = _import_from_path(chunk_model_path)
    chunk_filter = {chunk_fk_field: test_mailing_id}

    # 1. Idempotency Check
    if test_mailing_id and not is_retry:
        try:
            with transaction.atomic():
                chunk_record, created = ChunkModel.objects.select_for_update().get_or_create(
                    **chunk_filter, chunk_index=chunk_index, defaults={'status': 'PROCESSING', 'original_task_id': task_id})
                if not created and chunk_record.status == 'COMPLETED': return {"status": "skipped"}
        except IntegrityError: return {"status": "skipped"}

    # 2. Fetch DB Objects
    try:
        clients = list(Client.objects.select_related('user').only('id', 'contact_email', 'user__username', 'company_name').filter(id__in=client_ids, is_active=True))
        mail_type = MailType.objects.get(id=mail_type_id)
        email_template = EmailTemplate.objects.get(id=email_template_id)
        sender = SenderEmail.objects.get(id=sender_id) if sender_id else None
    except Exception:
        logger.exception("bulk_mail_fetch_failed", extra=log_extra)
        raise

    inline_images = _get_cached_inline_images(mail_type_id)
    prepared_attachments = _get_cached_attachments(attachment_ids, attachment_model_path)

    # 3. Determine Already Sent
    already_sent_ids = set()
    if test_mailing_id:
        try:
            chunk = ChunkModel.objects.filter(**chunk_filter, chunk_index=chunk_index).first()
            if chunk and (is_retry or chunk.status == 'PROCESSING') and chunk.original_task_id:
                already_sent_ids = set(MailLog.objects.filter(client_id__in=client_ids, task_id=chunk.original_task_id, status="SENT").values_list('client_id', flat=True))
        except Exception: pass

    # 4. Sending Loop
    connection = None
    logs_to_create, failed_client_ids = [], []
    success_count = failure_count = processed_count = 0
    sender_info = {'name': sender.name, 'email': sender.email, 'from_email': f"{sender.name} <{sender.email}>"} if sender else {'name': 'Team', 'email': 'no-reply@example.com', 'from_email': 'no-reply@example.com'}

    try:
        connection = get_connection()
        connection.open()
        for client in clients:
            processed_count += 1
            if client.id in already_sent_ids: success_count += 1; continue

            html = None
            try:
                from types import SimpleNamespace
                safe_sender = SimpleNamespace(name=sender_info['name'], email=sender_info['email'])
                ctx = build_email_context(client=client, sender=safe_sender, message="", request_data=dynamic_vars)
                for cid in inline_images: ctx[cid] = cid
                html = email_template.compiled_template.render(Context(ctx))

                send_email(subject=subject, html_body=html, from_email=sender_info['from_email'], to_email=client.contact_email, inline_images=inline_images, attachments=prepared_attachments, connection=connection)
                logs_to_create.append(_create_log_entry(client, mail_type, email_template, sender, user_id, task_id, campaign_name, subject, "SENT", mailing_id=mailing_id))
                success_count += 1
                time.sleep(SMTP_THROTTLE_SECONDS)

            except (smtplib.SMTPServerDisconnected, TimeoutError, ConnectionResetError, BrokenPipeError, OSError) as net_err:
                logger.warning("smtp_network_error", extra={**log_extra, "client_id": client.id})
                try:
                    if connection: connection.close()
                    connection = get_connection(); connection.open()
                    if not html:
                        safe_sender = SimpleNamespace(name=sender_info['name'], email=sender_info['email'])
                        ctx = build_email_context(client=client, sender=safe_sender, message="", request_data=dynamic_vars)
                        for cid in inline_images: ctx[cid] = cid
                        html = email_template.compiled_template.render(Context(ctx))
                    send_email(subject=subject, html_body=html, from_email=sender_info['from_email'], to_email=client.contact_email, inline_images=inline_images, attachments=prepared_attachments, connection=connection)
                    logs_to_create.append(_create_log_entry(client, mail_type, email_template, sender, user_id, task_id, campaign_name, subject, "SENT", mailing_id=mailing_id))
                    success_count += 1
                    time.sleep(SMTP_THROTTLE_SECONDS)
                except Exception as e2:
                    failure_count += 1; failed_client_ids.append(client.id)
                    logs_to_create.append(_create_log_entry(client, mail_type, email_template, sender, user_id, task_id, campaign_name, subject, "FAILED", repr(e2)[:300], mailing_id=mailing_id))
            except Exception as e:
                failure_count += 1; failed_client_ids.append(client.id)
                logs_to_create.append(_create_log_entry(client, mail_type, email_template, sender, user_id, task_id, campaign_name, subject, "FAILED", repr(e)[:300], mailing_id=mailing_id))

            if failure_count >= CIRCUIT_BREAKER_THRESHOLD:
                logger.error("circuit_breaker_triggered", extra=log_extra)
                for rem in clients[processed_count:]:
                    if rem.id not in already_sent_ids:
                        failed_client_ids.append(rem.id)
                        logs_to_create.append(_create_log_entry(rem, mail_type, email_template, sender, user_id, task_id, campaign_name, subject, "FAILED", "Circuit breaker", mailing_id=mailing_id))
                failure_count += len(clients[processed_count:])
                break
    except Exception as e:
        logger.exception("bulk_mail_catastrophic_error", extra=log_extra)
        for rem in clients[processed_count - 1:]:
            if rem.id not in already_sent_ids:
                failed_client_ids.append(rem.id)
                logs_to_create.append(_create_log_entry(rem, mail_type, email_template, sender, user_id, task_id, campaign_name, subject, "FAILED", repr(e)[:300], mailing_id=mailing_id))
        failure_count += len(clients[processed_count - 1:])
    finally:
        if connection:
            try: connection.close()
            except Exception: pass

    for i in range(0, len(logs_to_create), BULK_CREATE_BATCH_SIZE):
        MailLog.objects.bulk_create(logs_to_create[i:i + BULK_CREATE_BATCH_SIZE])

    # 5. Post-Send Bookkeeping
    if test_mailing_id and not is_retry:
        ChunkModel.objects.filter(**chunk_filter, chunk_index=chunk_index).update(success_count=success_count, failure_count=failure_count)
        if failed_client_ids:
            send_bulk_mails.apply_async(kwargs={
                "client_ids": failed_client_ids, "mail_type_id": mail_type_id, "email_template_id": email_template_id,
                "sender_id": sender_id, "subject": subject, "attachment_ids": attachment_ids, "user_id": user_id,
                "campaign_name": campaign_name, "dynamic_vars": dynamic_vars, "test_mailing_id": test_mailing_id,
                "mailing_id": mailing_id, "chunk_index": chunk_index, "is_retry": True,
                "attachment_model_path": attachment_model_path, "chunk_model_path": chunk_model_path,
                "chunk_fk_field": chunk_fk_field, "completion_task_path": completion_task_path,
            })
        else:
            ChunkModel.objects.filter(**chunk_filter, chunk_index=chunk_index).update(status='COMPLETED')
            _import_from_path(completion_task_path).delay(test_mailing_id)

    elif is_retry and test_mailing_id:
        with transaction.atomic():
            chunk = ChunkModel.objects.select_for_update().get(**chunk_filter, chunk_index=chunk_index)
            chunk.success_count = F('success_count') + success_count
            chunk.failure_count = F('failure_count') + failure_count
            chunk.status = 'COMPLETED'
            chunk.save(update_fields=['success_count', 'failure_count', 'status'])
        _import_from_path(completion_task_path).delay(test_mailing_id)

    return {"success": success_count, "failed": failure_count}

def _create_log_entry(client, mail_type, email_template, sender, user_id, task_id, campaign_name, subject, status, error_message='', mailing_id=None):
    return MailLog(
        client=client, mail_type=mail_type, template_used=email_template, sender_email=sender,
        created_by_id=user_id, task_id=task_id, campaign_name=campaign_name or "", subject=subject,
        status=status, error_message=error_message, mailing_id=mailing_id
    )