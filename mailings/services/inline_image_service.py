import logging
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from django.core.cache import cache as django_cache
from django.db.models.signals import post_save, post_delete
from django.dispatch import receiver

from templates.models import InlineImage

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT = 10
MAX_RETRIES = 2
IMAGE_CACHE_TIMEOUT = 3600 * 6  # 6 hours

_MIME_TO_EXT = {
    'png': 'png', 'jpeg': 'jpg', 'gif': 'gif', 
    'svg+xml': 'svg', 'webp': 'webp', 'octet-stream': 'bin'
}


def _get_image_cache_key(mail_type_id: int) -> str:
    return f"inline_images_bytes_{mail_type_id}"


def _get_image_url_cache_key(mail_type_id: int) -> str:
    return f"inline_image_urls_{mail_type_id}"


def load_inline_images(mail_type) -> dict[str, tuple[str, bytes, str]]:
    """Load inline images for a mail type."""
    cache_key = _get_image_cache_key(mail_type.id)
    cached = django_cache.get(cache_key)
    if cached is not None:
        return cached

    images = _fetch_inline_images_from_source(mail_type)
    django_cache.set(cache_key, images, timeout=IMAGE_CACHE_TIMEOUT)
    return images


def _fetch_inline_images_from_source(mail_type) -> dict[str, tuple[str, bytes, str]]:
    """Fetch inline images via HTTP (only called on cache miss)."""
    images = {}
    inline_images = InlineImage.objects.filter(
        mail_type=mail_type, is_active=True
    ).order_by('display_order')
    
    if not inline_images.exists():
        return images

    session = _create_session()
    for img in inline_images:
        if not img.image:
            continue
        try:
            url = img.image.build_url(secure=True)
            response = session.get(url, timeout=REQUEST_TIMEOUT)
            response.raise_for_status()
            
            content_type_header = response.headers.get('Content-Type', 'image/png')
            mime_subtype = content_type_header.split('/')[-1] if '/' in content_type_header else 'png'
            ext = _MIME_TO_EXT.get(mime_subtype, mime_subtype)
            filename = f"{img.content_id}.{ext}"
            
            images[img.content_id] = (filename, response.content, mime_subtype)
        except requests.RequestException as e:
            logger.warning(
                "Failed to load image %s: %s", 
                img.content_id, e,
                extra={"mail_type_id": mail_type.id, "content_id": img.content_id}
            )
    return images


def _create_session() -> requests.Session:
    """Create HTTP session with retry logic."""
    session = requests.Session()
    retry = Retry(
        total=MAX_RETRIES, 
        backoff_factor=0.5, 
        status_forcelist=[429, 500, 502, 503, 504]
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=5, pool_maxsize=5)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def get_inline_image_urls(mail_type) -> dict[str, str]:
    """Get public URLs for inline images (for browser preview)."""
    cache_key = _get_image_url_cache_key(mail_type.id)
    cached = django_cache.get(cache_key)
    if cached is not None:
        return cached

    urls = {
        img.content_id: img.image.build_url(secure=True)
        for img in InlineImage.objects.filter(mail_type=mail_type, is_active=True)
        if img.image
    }
    django_cache.set(cache_key, urls, timeout=IMAGE_CACHE_TIMEOUT)
    return urls


def get_cached_inline_images(mail_type_id: int) -> dict:
    """
    Cached wrapper for load_inline_images by ID.
    
    FIX #11: Check cache BEFORE fetching MailType object.
    This avoids the DB query on every chunk call when cache is warm.
    """
    cache_key = _get_image_cache_key(mail_type_id)
    cached = django_cache.get(cache_key)
    
    # Return immediately if cache hit - no DB query needed
    if cached is not None:
        return cached
    
    # Only fetch MailType on cache miss
    try:
        from templates.models import MailType
        mail_type = MailType.objects.get(id=mail_type_id)
        return load_inline_images(mail_type)
    except Exception as e:
        logger.error("Failed to load inline images for mail_type %s: %s", mail_type_id, e)
        return {}


def invalidate_inline_image_cache(mail_type_id: int):
    """Invalidate both bytes and URL caches for a mail type."""
    django_cache.delete(_get_image_cache_key(mail_type_id))
    django_cache.delete(_get_image_url_cache_key(mail_type_id))


@receiver([post_save, post_delete], sender=InlineImage)
def invalidate_inline_image_cache_on_change(sender, instance, **kwargs):
    if instance.mail_type_id:
        invalidate_inline_image_cache(instance.mail_type_id)