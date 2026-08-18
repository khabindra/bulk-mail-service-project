import mimetypes
import logging
from django.core.mail import EmailMultiAlternatives
from email.mime.image import MIMEImage

logger = logging.getLogger(__name__)

# Supported inline image types
SUPPORTED_IMAGE_TYPES = {
    'png': 'png',
    'jpg': 'jpeg',
    'jpeg': 'jpeg',
    'gif': 'gif',
    'svg': 'svg+xml',
    'webp': 'webp',
}


def send_email(
    *,
    subject,
    html_body,
    from_email,
    to_email,
    inline_images=None,
    attachments=None,
    plain_text=None,
    connection=None,          # ← THIS WAS MISSING
):
    """
    Send an email with HTML content, inline images, and attachments.
    
    Args:
        subject: Email subject line
        html_body: HTML content of the email
        from_email: Sender email address (can include display name)
        to_email: Recipient email(s) - string or list
        inline_images: Dict of {cid: (filename, bytes)}
        attachments: List of dicts with {filename, content, content_type}
        plain_text: Plain text fallback
        connection: Optional SMTP connection to reuse (for bulk sending)
    
    Returns:
        bool: True if sent successfully
    
    Raises:
        Exception: If email fails to send
    """
    if isinstance(to_email, str):
        to_email = [to_email]
    
    if not to_email:
        logger.warning("No recipients specified for email: %s", subject)
        return False

    if not plain_text:
        plain_text = "Please view this email in an HTML compatible client."

    email = EmailMultiAlternatives(
        subject=subject,
        body=plain_text,
        from_email=from_email,
        to=to_email,
        connection=connection,       # ← THIS WAS MISSING
    )
    email.mixed_subtype = "mixed"

    email.attach_alternative(html_body, "text/html")

    _attach_inline_images(email, inline_images or {})
    _attach_files(email, attachments or [])

    email.send(fail_silently=False)
    logger.info("Email sent to %s (subject: %s)", to_email, subject[:50])
    
    return True


def _attach_inline_images(email, inline_images):
    """Attach inline images to email."""
    for cid, (filename, file_bytes, mime_subtype) in inline_images.items():  # ✅ unpack 3
        try:
            image = MIMEImage(file_bytes, _subtype=mime_subtype)  # ✅ use header-derived subtype
            
            image.add_header("Content-ID", f"<{cid}>")
            image.add_header(
                "Content-Disposition",
                "inline",
                filename=filename,
            )
            email.attach(image)
            
        except Exception as e:
            logger.warning("Failed to attach inline image %s: %s", cid, e)
            
# def _attach_inline_images(email, inline_images):
#     """Attach inline images to email."""
#     for cid, (filename, file_bytes) in inline_images.items():
#         try:
#             subtype = _get_image_subtype(filename)
#             image = MIMEImage(file_bytes, _subtype=subtype)
            
#             image.add_header("Content-ID", f"<{cid}>")
#             image.add_header(
#                 "Content-Disposition",
#                 "inline",
#                 filename=filename,
#             )
#             email.attach(image)
            
#         except Exception as e:
#             logger.warning("Failed to attach inline image %s: %s", cid, e)


def _get_image_subtype(filename):
    """Determine image MIME subtype from filename."""
    extension = filename.rsplit('.', 1)[-1].lower() if '.' in filename else ''
    return SUPPORTED_IMAGE_TYPES.get(extension, 'octet-stream')


def _attach_files(email, attachments):
    """Attach files to email."""
    for attachment in attachments:
        try:
            if isinstance(attachment, dict):
                content = attachment.get("content")
                filename = attachment.get("filename", "attachment")
                content_type = attachment.get("content_type", "application/octet-stream")
            elif hasattr(attachment, "read"):
                attachment.seek(0)
                content = attachment.read()
                filename = getattr(attachment, "name", "attachment")
                content_type = getattr(attachment, "content_type", "application/octet-stream")
            else:
                logger.warning("Unsupported attachment type: %s", type(attachment))
                continue

            if not content:
                logger.warning("Empty attachment skipped: %s", filename)
                continue

            email.attach(
                filename=filename,
                content=content,
                mimetype=content_type,
            )
            
        except Exception as e:
            logger.warning("Failed to attach file: %s", e)