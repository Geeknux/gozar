from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _


class ChangeLog(models.Model):
    """History of edits made through Gozar (the counterpart of admin's LogEntry)."""

    class Action(models.TextChoices):
        ADD = "add", _("Addition")
        CHANGE = "change", _("Change")
        DELETE = "delete", _("Deletion")

    timestamp = models.DateTimeField(_("time"), auto_now_add=True, db_index=True)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        verbose_name=_("user"),
    )
    model_label = models.CharField(_("model"), max_length=200, db_index=True)
    object_pk = models.CharField(_("object id"), max_length=255, db_index=True)
    object_repr = models.CharField(_("object repr"), max_length=255)
    action = models.CharField(_("action"), max_length=10, choices=Action.choices)
    changes = models.JSONField(_("changes"), default=dict, blank=True)

    class Meta:
        verbose_name = _("change log entry")
        verbose_name_plural = _("change log entries")
        ordering = ["-timestamp", "-id"]

    def __str__(self):
        return f"{self.get_action_display()} {self.model_label}#{self.object_pk}"


class CalendarDay(models.Model):
    """Calendar dimension table: one row per day, Gregorian and Jalali attributes.

    Report queries group by these columns (looked up by date in the database), so
    Jalali months/seasons/years are aggregated correctly. Populate it with
    ``manage.py gozar_calendar``. The ``*_start`` columns hold the Gregorian date
    on which the enclosing week/month/season/year begins.
    """

    date = models.DateField(_("date"), primary_key=True)
    weekday = models.PositiveSmallIntegerField(_("weekday (ISO, Monday=0)"))

    year = models.PositiveSmallIntegerField(_("Gregorian year"))
    quarter = models.PositiveSmallIntegerField(_("Gregorian quarter"))
    month = models.PositiveSmallIntegerField(_("Gregorian month"))
    day = models.PositiveSmallIntegerField(_("Gregorian day"))
    iso_year = models.PositiveSmallIntegerField(_("ISO year"))
    iso_week = models.PositiveSmallIntegerField(_("ISO week"))
    week_start = models.DateField(_("Gregorian week start"))
    month_start = models.DateField(_("Gregorian month start"))
    quarter_start = models.DateField(_("Gregorian quarter start"))
    year_start = models.DateField(_("Gregorian year start"))

    jalali_year = models.PositiveSmallIntegerField(_("Jalali year"), db_index=True)
    jalali_season = models.PositiveSmallIntegerField(_("Jalali season"))
    jalali_month = models.PositiveSmallIntegerField(_("Jalali month"))
    jalali_day = models.PositiveSmallIntegerField(_("Jalali day"))
    jalali_weekday = models.PositiveSmallIntegerField(_("Jalali weekday (Saturday=0)"))
    jalali_week_start = models.DateField(_("Jalali week start"))
    jalali_month_start = models.DateField(_("Jalali month start"))
    jalali_season_start = models.DateField(_("Jalali season start"))
    jalali_year_start = models.DateField(_("Jalali year start"))

    is_weekend = models.BooleanField(_("weekend"), default=False)
    is_holiday = models.BooleanField(_("public holiday"), default=False)
    holiday_name = models.CharField(_("occasion"), max_length=100, blank=True)

    class Meta:
        verbose_name = _("calendar day")
        verbose_name_plural = _("calendar days")
        ordering = ["date"]

    def __str__(self):
        return f"{self.jalali_year:04d}/{self.jalali_month:02d}/{self.jalali_day:02d}"

    @property
    def is_working_day(self) -> bool:
        return not (self.is_weekend or self.is_holiday)


LANGUAGE_CHOICES = [("en", "English"), ("fa", "فارسی")]  # each name in its own language


class UserPreferences(models.Model):
    """Per-user interface preferences. Missing rows/blank values fall back to
    ``GOZAR["DEFAULT_LANGUAGE"]`` / ``GOZAR["DEFAULT_CALENDAR"]``."""

    class Calendar(models.TextChoices):
        GREGORIAN = "gregorian", _("Gregorian")
        JALALI = "jalali", _("Jalali (Solar Hijri)")

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        primary_key=True,
        related_name="gozar_preferences",
        verbose_name=_("user"),
    )
    language = models.CharField(_("language"), max_length=10, choices=LANGUAGE_CHOICES, blank=True)
    calendar = models.CharField(_("calendar"), max_length=10, choices=Calendar.choices, blank=True)
    options = models.JSONField(_("other options"), default=dict, blank=True)
    updated_at = models.DateTimeField(_("updated at"), auto_now=True)

    class Meta:
        verbose_name = _("user preferences")
        verbose_name_plural = _("user preferences")

    def __str__(self):
        return str(self.user)
