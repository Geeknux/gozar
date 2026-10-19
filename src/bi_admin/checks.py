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


@checks.register()
def check_nl_settings(app_configs=None, **kwargs):
    """The LLM API key comes from the environment only, never from settings."""
    from bi_admin.nl.conf import configured

    config = configured()
    errors = []
    if config.get("API_KEY"):
        errors.append(
            checks.Error(
                'Don\'t put the LLM API key in settings: set GOZAR["NL"]["API_KEY_ENV"] to the '
                "name of an environment variable (default GOZAR_NL_API_KEY) and export the key",
                id="bi_admin.E003",
            )
        )
    fmt = config.get("RESPONSE_FORMAT")
    if fmt and fmt not in ("json_schema", "json_object", "none"):
        errors.append(
            checks.Error(
                f'GOZAR["NL"]["RESPONSE_FORMAT"] = {fmt!r}: use json_schema, json_object or none',
                id="bi_admin.E004",
            )
        )
    return errors
