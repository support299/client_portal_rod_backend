from django.apps import AppConfig


class GhlConfig(AppConfig):
    name = 'ghl'

    def ready(self):
        from . import signals  # noqa: F401
