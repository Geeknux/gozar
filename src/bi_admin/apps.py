from django.apps import AppConfig


class BiAdminConfig(AppConfig):
    name = "bi_admin"
    verbose_name = "Gozar"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self):
        from django.utils.module_loading import autodiscover_modules

        # Like admin.py for contrib.admin: each app may ship a ``gozar.py`` that
        # registers ModelOptions (and, from M2, semantic models).
        autodiscover_modules("gozar")
