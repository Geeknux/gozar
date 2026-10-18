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
