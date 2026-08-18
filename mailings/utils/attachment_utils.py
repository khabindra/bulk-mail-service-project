import logging
from django.core.cache import cache

logger = logging.getLogger(__name__)

def get_cached_attachments_for_model(model_class, attachment_ids: list, cache_prefix: str = 'attachments') -> list:
    if not attachment_ids:
        return []
    
    cache_key = f"{cache_prefix}_{'_'.join(str(i) for i in sorted(attachment_ids))}"
    cached = cache.get(cache_key)
    if cached is not None:
        return cached
    
    attachments = []
    for att in model_class.objects.filter(id__in=attachment_ids).select_related(None):
        try:
            # FIX: Use context manager for safer file reading
            with att.file.open('rb') as f:
                content = f.read()
            attachments.append({
                'filename': att.filename,
                'content': content,
                'content_type': att.content_type,
            })
        except Exception as e:
            logger.error("Failed to read attachment %s: %s", att.id, e)
    
    cache.set(cache_key, attachments, timeout=7200)
    return attachments