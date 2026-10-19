"""System checks: every registered semantic model must bind cleanly."""

from django.core import checks
from django.core.exceptions import ImproperlyConfigured


@checks.register()
def check_semantic_models(app_configs=None, **kwargs):
    from bi_admin.semantic import semantic_site

    errors = []
    for name in semantic_site.names():
        try:
            semantic_site.get(name)
        except ImproperlyConfigured as exc:
            errors.append(checks.Error(str(exc), obj=name, id="bi_admin.E001"))
    return errors


@checks.register(checks.Tags.database)
def check_reports_database(app_configs=None, **kwargs):
    from django.conf import settings

    from bi_admin.conf import setting

    alias = setting("REPORTS_DATABASE")
    if not alias:
        return []
    if alias not in settings.DATABASES:
        return [
            checks.Error(
                f'GOZAR["REPORTS_DATABASE"] = {alias!r} is not in DATABASES',
                id="bi_admin.E002",
            )
        ]
    router = "bi_admin.execution.router.ReportsRouter"
    if router not in getattr(settings, "DATABASE_ROUTERS", []):
        return [
            checks.Warning(
                f"Add {router!r} to DATABASE_ROUTERS so every read a report makes "
                "(lazy related objects, custom querysets) goes to the reports database",
                id="bi_admin.W001",
            )
        ]
    return []
