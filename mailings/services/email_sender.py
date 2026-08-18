import sys
import logging
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.application import MIMEApplication
from email.utils import make_msgid, formatdate

logger = logging.getLogger(__name__)

_EXTENSION_TO_MIME = {
    'png': 'png', 'jpg': 'jpeg', 'jpeg': 'jpeg',
    'gif': 'gif', 'svg': 'svg+xml', 'webp': 'webp',
}

def _sanitize_header(value):
    if value: return value.replace('\r', '').replace('\n', '')
    return value

class _SMTPSafeMessage(MIMEMultipart):
    def as_bytes(self, unixfrom=False, linesep=None):
        raw = super().as_bytes(unixfrom=unixfrom)
        if sys.version_info >= (3, 14) and linesep == "\r\n":
            raw = raw.replace(b'\r\n', b'\n').replace(b'\n', b'\r\n')
        return raw

class _RawMessageWrapper:
    def __init__(self, msg, from_email, to_emails):
        self._msg, self._from, self._to = msg, from_email, list(to_emails)
    def message(self): return self._msg
    def recipients(self): return self._to
    @property
    def from_email(self): return self._from
    @property
    def encoding(self): return 'utf-8'

def send_email(*, subject, html_body, from_email, to_email, inline_images=None, attachments=None, plain_text=None, connection=None):
    if isinstance(to_email, str): to_email = [to_email]
    if not to_email:
        logger.warning("no_recipients_specified", extra={"subject": subject})
        return False

    if not plain_text: plain_text = "Please view this email in an HTML compatible client."

    msg_root = _SMTPSafeMessage('mixed')
    msg_root['Subject'] = _sanitize_header(subject)
    msg_root['From'] = _sanitize_header(from_email)
    msg_root['To'] = ', '.join([_sanitize_header(e) for e in to_email])
    msg_root['Message-ID'] = make_msgid()
    msg_root['Date'] = formatdate(localtime=True)

    related = MIMEMultipart('related')
    alternative = MIMEMultipart('alternative')
    alternative.attach(MIMEText(plain_text, 'plain', 'utf-8'))
    alternative.attach(MIMEText(html_body, 'html', 'utf-8'))
    related.attach(alternative)

    for cid, img_data in (inline_images or {}).items():
        try:
            filename, file_bytes, mime_subtype = _unpack_image_data(cid, img_data)
            if not file_bytes: continue
            image = MIMEImage(file_bytes, _subtype=mime_subtype)
            image.add_header('Content-ID', f'<{cid}>')
            image.add_header('Content-Disposition', 'inline', filename=filename)
            related.attach(image)
        except Exception:
            logger.exception("inline_image_attach_failed", extra={"cid": cid})

    msg_root.attach(related)

    for att in (attachments or []):
        try:
            part = _build_attachment_part(att)
            if part: msg_root.attach(part)
        except Exception:
            logger.exception("file_attach_failed")

    wrapper = _RawMessageWrapper(msg_root, from_email, to_email)
    connection.send_messages([wrapper])
    return True

def _unpack_image_data(cid, img_data):
    if isinstance(img_data, (list, tuple)) and len(img_data) == 3: return img_data
    if isinstance(img_data, (list, tuple)) and len(img_data) == 2:
        filename, file_bytes = img_data
        ext = filename.rsplit('.', 1)[-1].lower() if '.' in filename else ''
        return filename, file_bytes, _EXTENSION_TO_MIME.get(ext, 'octet-stream')
    if isinstance(img_data, dict):
        return img_data.get('filename', f'{cid}.bin'), img_data.get('content', b''), img_data.get('subtype', 'octet-stream')
    return None, None, None

def _build_attachment_part(att):
    if isinstance(att, dict):
        content, filename = att.get('content'), att.get('filename', 'attachment')
        content_type = att.get('content_type', 'application/octet-stream')
    elif hasattr(att, 'read'):
        att.seek(0)
        content, filename = att.read(), getattr(att, 'name', 'attachment')
        content_type = getattr(att, 'content_type', 'application/octet-stream')
    else: return None

    if not content: return None
    parts = content_type.split('/', 1)
    main_type, sub_type = parts[0] if len(parts) > 0 else 'application', parts[1] if len(parts) > 1 else 'octet-stream'

    part = MIMEImage(content, _subtype=sub_type) if main_type == 'image' else MIMEApplication(content, _subtype=sub_type)

    try: filename.encode('ascii'); part.add_header('Content-Disposition', 'attachment', filename=filename)
    except UnicodeEncodeError: part.add_header('Content-Disposition', 'attachment', filename=('utf-8', '', filename))
    return part