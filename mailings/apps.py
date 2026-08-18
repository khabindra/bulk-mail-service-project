from django.apps import AppConfig

class MailingsConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'mailings'

    def ready(self):
        # FIX: Import signals to register them
        import mailings.signals  # noqa: F401