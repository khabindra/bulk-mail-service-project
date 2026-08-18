import logging
from mailings.services.inline_image_service import load_inline_images

logger = logging.getLogger(__name__)

def load_inline_images_safe(mail_type, task_id):
    try:
        return load_inline_images(mail_type)
    except Exception:
        logger.exception("inline_images_load_failed", extra={"task_id": task_id})
        return {}