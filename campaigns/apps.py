# campaigns/apps.py

from django.apps import AppConfig


class CampaignsConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'campaigns'
    verbose_name = 'Recurring Campaigns'

    def ready(self):
        # FIX #7: Import signals to register them
        import campaigns.signals  # noqa: F401