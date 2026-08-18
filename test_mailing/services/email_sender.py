# import logging
# from email.mime.image import MIMEImage
# from email.mime.multipart import MIMEMultipart
# from email.mime.text import MIMEText
# from email.mime.application import MIMEApplication
# from email.utils import make_msgid, formatdate

# logger = logging.getLogger(__name__)

# _EXTENSION_TO_MIME = {
#     'png': 'png', 'jpg': 'jpeg', 'jpeg': 'jpeg',
#     'gif': 'gif', 'svg': 'svg+xml', 'webp': 'webp',
# }


# class _SMTPSafeMessage(MIMEMultipart):
#     """
#     Python 3.14 removed the 'linesep' parameter from Message.as_bytes().
#     Django's SMTPBackend._send() calls message.as_bytes(linesep="\r\n").

#     This subclass accepts the linesep kwarg (preventing TypeError) and
#     performs the \\n → \\r\\n conversion that Django expects.
#     """
#     def as_bytes(self, unixfrom=False, linesep=None):
#         raw = super().as_bytes(unixfrom=unixfrom)
#         if linesep == "\r\n":
#             # Normalize first (handles any mixed \\r\\n), then convert
#             raw = raw.replace(b'\r\n', b'\n').replace(b'\n', b'\r\n')
#         return raw


# class _RawMessageWrapper:
#     """
#     Adapts a raw email.mime Message for Django's SMTPBackend.send_messages().

#     Django's _send() accesses exactly 4 things on the message object:
#       - .recipients()  → list of recipient emails
#       - .from_email    → sender string
#       - .encoding      → charset string
#       - .message()     → email.message.Message (calls .as_bytes() on it)
#     """
#     def __init__(self, msg, from_email, to_emails):
#         self._msg = msg
#         self._from = from_email
#         self._to = list(to_emails)

#     def message(self):
#         return self._msg

#     def recipients(self):
#         return self._to

#     @property
#     def from_email(self):
#         return self._from

#     @property
#     def encoding(self):
#         return 'utf-8'


# def send_email(*, subject, html_body, from_email, to_email,
#                inline_images=None, attachments=None,
#                plain_text=None, connection=None):
#     """
#     Send email with inline images and attachments.

#     Builds the entire MIME tree manually using email.mime.
#     Compatible with Python 3.14 (handles removed linesep parameter).

#     Structure produced:

#         multipart/mixed
#         ├── multipart/related
#         │   ├── multipart/alternative
#         │   │   ├── text/plain
#         │   │   └── text/html          ← cid: references live here
#         │   └── image/png (Content-ID) ← resolved against HTML
#         └── application/pdf            ← regular downloadable attachment
#     """
#     if isinstance(to_email, str):
#         to_email = [to_email]
#     if not to_email:
#         logger.warning("no_recipients_specified", extra={"subject": subject})
#         return False

#     if not plain_text:
#         plain_text = "Please view this email in an HTML compatible client."

#     # ── ROOT: multipart/mixed ──────────────────────────────────
#     msg_root = _SMTPSafeMessage('mixed')
#     msg_root['Subject'] = subject
#     msg_root['From'] = from_email
#     msg_root['To'] = ', '.join(to_email)
#     msg_root['Message-ID'] = make_msgid()
#     msg_root['Date'] = formatdate(localtime=True)

#     # ── RELATED: HTML + inline images ─────────────────────────
#     related = MIMEMultipart('related')

#     alternative = MIMEMultipart('alternative')
#     alternative.attach(MIMEText(plain_text, 'plain', 'utf-8'))
#     alternative.attach(MIMEText(html_body, 'html', 'utf-8'))
#     related.attach(alternative)

#     for cid, img_data in (inline_images or {}).items():
#         try:
#             filename, file_bytes, mime_subtype = _unpack_image_data(cid, img_data)
#             if not file_bytes:
#                 continue

#             image = MIMEImage(file_bytes, _subtype=mime_subtype)
#             image.add_header('Content-ID', f'<{cid}>')
#             image.add_header('Content-Disposition', 'inline', filename=filename)
#             related.attach(image)

#         except Exception:
#             logger.exception("inline_image_attach_failed", extra={"cid": cid})

#     msg_root.attach(related)

#     # ── ATTACHMENTS: at mixed level (downloadable files) ───────
#     for att in (attachments or []):
#         try:
#             part = _build_attachment_part(att)
#             if part:
#                 msg_root.attach(part)
#         except Exception:
#             logger.exception("file_attach_failed")

#     # ── SEND via Django's connection ───────────────────────────
#     wrapper = _RawMessageWrapper(msg_root, from_email, to_email)
#     connection.send_messages([wrapper])

#     logger.info("email_sent_success", extra={"to": to_email, "subject": subject[:50]})
#     return True


# def _unpack_image_data(cid, img_data):
#     """Unpack inline image into (filename, bytes, mime_subtype)."""
#     if isinstance(img_data, (list, tuple)) and len(img_data) == 3:
#         return img_data[0], img_data[1], img_data[2]

#     if isinstance(img_data, (list, tuple)) and len(img_data) == 2:
#         filename, file_bytes = img_data
#         ext = filename.rsplit('.', 1)[-1].lower() if '.' in filename else ''
#         return filename, file_bytes, _EXTENSION_TO_MIME.get(ext, 'octet-stream')

#     if isinstance(img_data, dict):
#         filename = img_data.get('filename', f'{cid}.bin')
#         file_bytes = img_data.get('content', b'')
#         mime_subtype = img_data.get('subtype', 'octet-stream')
#         return filename, file_bytes, mime_subtype

#     logger.error("inline_image_unexpected_format", extra={"cid": cid})
#     return None, None, None


# def _build_attachment_part(att):
#     """Build MIME part for a regular (non-inline) attachment."""
#     if isinstance(att, dict):
#         content = att.get('content')
#         filename = att.get('filename', 'attachment')
#         content_type = att.get('content_type', 'application/octet-stream')
#     elif hasattr(att, 'read'):
#         att.seek(0)
#         content = att.read()
#         filename = getattr(att, 'name', 'attachment')
#         content_type = getattr(att, 'content_type', 'application/octet-stream')
#     else:
#         return None

#     if not content:
#         return None

#     parts = content_type.split('/', 1)
#     main_type = parts[0] if len(parts) > 0 else 'application'
#     sub_type = parts[1] if len(parts) > 1 else 'octet-stream'

#     if main_type == 'image':
#         part = MIMEImage(content, _subtype=sub_type)
#     else:
#         part = MIMEApplication(content, _subtype=sub_type)

#     part.add_header('Content-Disposition', 'attachment', filename=filename)
#     return part


# ============================ moved to mailings/services/email_sender.py file: ===============


# import sys
# import logging
# from email.mime.image import MIMEImage
# from email.mime.multipart import MIMEMultipart
# from email.mime.text import MIMEText
# from email.mime.application import MIMEApplication
# from email.utils import make_msgid, formatdate

# logger = logging.getLogger(__name__)

# _EXTENSION_TO_MIME = {
#     'png': 'png', 'jpg': 'jpeg', 'jpeg': 'jpeg',
#     'gif': 'gif', 'svg': 'svg+xml', 'webp': 'webp',
# }

# def _sanitize_header(value):
#     """Fix: Prevent header injection via \r\n."""
#     if value:
#         return value.replace('\r', '').replace('\n', '')
#     return value

# class _SMTPSafeMessage(MIMEMultipart):
#     """
#     Python 3.14 removed the 'linesep' parameter from Message.as_bytes().
#     Fix: Version guard ensures conversion only applies on 3.14+ to prevent double-CRLF.
#     """
#     def as_bytes(self, unixfrom=False, linesep=None):
#         raw = super().as_bytes(unixfrom=unixfrom)
#         if sys.version_info >= (3, 14) and linesep == "\r\n":
#             raw = raw.replace(b'\r\n', b'\n').replace(b'\n', b'\r\n')
#         return raw

# class _RawMessageWrapper:
#     def __init__(self, msg, from_email, to_emails):
#         self._msg = msg
#         self._from = from_email
#         self._to = list(to_emails)

#     def message(self):
#         return self._msg

#     def recipients(self):
#         return self._to

#     @property
#     def from_email(self):
#         return self._from

#     @property
#     def encoding(self):
#         return 'utf-8'

# def send_email(*, subject, html_body, from_email, to_email,
#                inline_images=None, attachments=None,
#                plain_text=None, connection=None):
#     if isinstance(to_email, str):
#         to_email = [to_email]
#     if not to_email:
#         logger.warning("no_recipients_specified", extra={"subject": subject})
#         return False

#     if not plain_text:
#         plain_text = "Please view this email in an HTML compatible client."

#     msg_root = _SMTPSafeMessage('mixed')
    
#     # Fix: Sanitize headers to prevent injection
#     msg_root['Subject'] = _sanitize_header(subject)
#     msg_root['From'] = _sanitize_header(from_email)
#     msg_root['To'] = ', '.join([_sanitize_header(e) for e in to_email])
#     msg_root['Message-ID'] = make_msgid()
#     msg_root['Date'] = formatdate(localtime=True)

#     related = MIMEMultipart('related')
#     alternative = MIMEMultipart('alternative')
#     alternative.attach(MIMEText(plain_text, 'plain', 'utf-8'))
#     alternative.attach(MIMEText(html_body, 'html', 'utf-8'))
#     related.attach(alternative)

#     for cid, img_data in (inline_images or {}).items():
#         try:
#             filename, file_bytes, mime_subtype = _unpack_image_data(cid, img_data)
#             if not file_bytes:
#                 continue
#             image = MIMEImage(file_bytes, _subtype=mime_subtype)
#             image.add_header('Content-ID', f'<{cid}>')
#             image.add_header('Content-Disposition', 'inline', filename=filename)
#             related.attach(image)
#         except Exception:
#             logger.exception("inline_image_attach_failed", extra={"cid": cid})

#     msg_root.attach(related)

#     for att in (attachments or []):
#         try:
#             part = _build_attachment_part(att)
#             if part:
#                 msg_root.attach(part)
#         except Exception:
#             logger.exception("file_attach_failed")

#     wrapper = _RawMessageWrapper(msg_root, from_email, to_email)
#     connection.send_messages([wrapper])
#     logger.info("email_sent_success", extra={"to": to_email, "subject": subject[:50]})
#     return True

# def _unpack_image_data(cid, img_data):
#     if isinstance(img_data, (list, tuple)) and len(img_data) == 3:
#         return img_data[0], img_data[1], img_data[2]
#     if isinstance(img_data, (list, tuple)) and len(img_data) == 2:
#         filename, file_bytes = img_data
#         ext = filename.rsplit('.', 1)[-1].lower() if '.' in filename else ''
#         return filename, file_bytes, _EXTENSION_TO_MIME.get(ext, 'octet-stream')
#     if isinstance(img_data, dict):
#         filename = img_data.get('filename', f'{cid}.bin')
#         file_bytes = img_data.get('content', b'')
#         mime_subtype = img_data.get('subtype', 'octet-stream')
#         return filename, file_bytes, mime_subtype
#     return None, None, None

# def _build_attachment_part(att):
#     if isinstance(att, dict):
#         content = att.get('content')
#         filename = att.get('filename', 'attachment')
#         content_type = att.get('content_type', 'application/octet-stream')
#     elif hasattr(att, 'read'):
#         att.seek(0)
#         content = att.read()
#         filename = getattr(att, 'name', 'attachment')
#         content_type = getattr(att, 'content_type', 'application/octet-stream')
#     else:
#         return None

#     if not content:
#         return None

#     parts = content_type.split('/', 1)
#     main_type = parts[0] if len(parts) > 0 else 'application'
#     sub_type = parts[1] if len(parts) > 1 else 'octet-stream'

#     if main_type == 'image':
#         part = MIMEImage(content, _subtype=sub_type)
#     else:
#         part = MIMEApplication(content, _subtype=sub_type)

#     # Fix: Correct RFC 2231 encoding for non-ASCII filenames using stdlib idiom
#     try:
#         filename.encode('ascii')
#         part.add_header('Content-Disposition', 'attachment', filename=filename)
#     except UnicodeEncodeError:
#         part.add_header('Content-Disposition', 'attachment', filename=('utf-8', '', filename))
        
#     return part