from django.apps import AppConfig

class TestMailingConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'test_mailing'
    verbose_name = 'Scheduled Mailings'

    def ready(self):
        # FIX: Canonical pattern - import signals module, not models
        import test_mailing.signals  # noqa: F401