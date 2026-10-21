"""Keep existing names, descriptions and widget titles as the default language's
text (``GOZAR["DEFAULT_LANGUAGE"]``) in the new ``*_i18n`` fields."""

from django.db import migrations

FIELDS = {
    "SavedReport": ("name", "description"),
    "Dashboard": ("name", "description"),
    "DashboardWidget": ("title",),
    "Schedule": ("name", "description"),
}


def default_language() -> str:
    from bi_admin.preferences import default_language

    return default_language()


def forwards(apps, schema_editor):
    lang = default_language()
    db = schema_editor.connection.alias
    for model_name, fields in FIELDS.items():
        model = apps.get_model("bi_admin", model_name)
        changed = []
        for obj in model.objects.using(db).iterator():
            dirty = False
            for field in fields:
                plain = getattr(obj, field)
                values = getattr(obj, f"{field}_i18n") or {}
                if plain and not values.get(lang):
                    setattr(obj, f"{field}_i18n", {**values, lang: plain})
                    dirty = True
            if dirty:
                changed.append(obj)
        model.objects.using(db).bulk_update(changed, [f"{f}_i18n" for f in fields], batch_size=500)


def backwards(apps, schema_editor):
    """The plain fields still hold the default language: nothing to undo."""


class Migration(migrations.Migration):
    dependencies = [("bi_admin", "0010_translations")]

    operations = [migrations.RunPython(forwards, backwards)]
