import logging
from django.db.models.signals import pre_save
from django.dispatch import receiver
from .models import Mailing

logger = logging.getLogger(__name__)

@receiver(pre_save, sender=Mailing)
def log_mailing_status_transition(sender, instance, **kwargs):
    if not instance.pk:
        return
    
    # FIX: Leverages _initial_status set natively in Mailing.__init__
    old_status = getattr(instance, '_initial_status', None)
    if old_status and old_status != instance.status:
        logger.info(
            "Mailing status transition",
            extra={"mailing_id": instance.pk, "old": old_status, "new": instance.status}
        )