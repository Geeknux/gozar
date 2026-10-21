from django.apps import AppConfig


class BiAdminConfig(AppConfig):
    name = "bi_admin"
    verbose_name = "Gozar"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self):
        from django.utils.module_loading import autodiscover_modules

        # Like admin.py for contrib.admin: each app may ship a ``gozar.py`` that
        # registers ModelOptions and semantic models.
        autodiscover_modules("gozar")

        from bi_admin import checks  # noqa: F401  (registers system checks)
        from bi_admin.execution import cache

        cache.connect_signals()  # result-cache data versions

        from bi_admin.scale import state

        state.connect()  # change records for pre-aggregations and columnar copies

        from bi_admin.alerts import handlers, hooks
        from bi_admin.alerts.conf import alert_setting

        hooks.connect()  # failed logins
        if alert_setting("ENABLED") and alert_setting("LOG_HANDLER"):
            handlers.attach()  # unhandled exceptions and HTTP 5xx → alerts
