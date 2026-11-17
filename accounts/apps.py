from django.apps import AppConfig
from django.db.models.signals import post_migrate


def _seed_roles(sender, **kwargs):
    from .permissions import ensure_default_roles

    ensure_default_roles()


class AccountsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "accounts"

    def ready(self):
        post_migrate.connect(_seed_roles, sender=self)
