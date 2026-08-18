# campaigns/signals.py

import logging
from django.db.models.signals import post_delete
from django.dispatch import receiver

from .models import Campaign

logger = logging.getLogger(__name__)


@receiver(post_delete, sender=Campaign)
def cleanup_periodic_task_on_campaign_delete(sender, instance, **kwargs):
    """
    Clean up the Celery Beat PeriodicTask when a Campaign is deleted.
    
    This signal is necessary for bulk_delete() calls where the model's
    delete() method is NOT called. For single-object deletes, the model's
    delete() method also calls _cleanup_periodic_task(), resulting in
    redundant but harmless double-cleanup (the second attempt finds
    the task already deleted and exits cleanly).
    """
    if instance.celery_periodic_task_id:
        _cleanup_celery_task(instance.celery_periodic_task)


def _cleanup_celery_task(periodic_task):
    """Safely delete a PeriodicTask and its associated CrontabSchedule."""
    if not periodic_task:
        return
    
    try:
        from django_celery_beat.models import PeriodicTask, CrontabSchedule
        
        # Use filter().first() instead of get() because the task may
        # already be deleted if model.delete() ran first
        task = PeriodicTask.objects.filter(id=periodic_task.id).first()
        if not task:
            return
        
        crontab = task.crontab
        task.delete()
        
        # Only delete crontab if no other tasks use it
        if crontab and not PeriodicTask.objects.filter(crontab=crontab).exists():
            crontab.delete()
            
    except Exception:
        logger.exception("Failed to cleanup celery task")