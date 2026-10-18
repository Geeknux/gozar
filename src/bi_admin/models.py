from django.conf import settings
from django.db import models


class ChangeLog(models.Model):
    """History of edits made through Gozar (the counterpart of admin's LogEntry)."""

    class Action(models.TextChoices):
        ADD = "add", "افزودن"
        CHANGE = "change", "ویرایش"
        DELETE = "delete", "حذف"

    timestamp = models.DateTimeField("زمان", auto_now_add=True, db_index=True)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        verbose_name="کاربر",
    )
    model_label = models.CharField("مدل", max_length=200, db_index=True)
    object_pk = models.CharField("شناسه شیء", max_length=255, db_index=True)
    object_repr = models.CharField("نمایش شیء", max_length=255)
    action = models.CharField("عمل", max_length=10, choices=Action.choices)
    changes = models.JSONField("تغییرات", default=dict, blank=True)

    class Meta:
        verbose_name = "سابقه تغییر"
        verbose_name_plural = "سوابق تغییرات"
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

    date = models.DateField("تاریخ", primary_key=True)
    weekday = models.PositiveSmallIntegerField("روز هفته (ISO، دوشنبه=۰)")

    year = models.PositiveSmallIntegerField("سال میلادی")
    quarter = models.PositiveSmallIntegerField("فصل میلادی")
    month = models.PositiveSmallIntegerField("ماه میلادی")
    day = models.PositiveSmallIntegerField("روز میلادی")
    iso_year = models.PositiveSmallIntegerField("سال ISO")
    iso_week = models.PositiveSmallIntegerField("هفته ISO")
    week_start = models.DateField("آغاز هفته میلادی")
    month_start = models.DateField("آغاز ماه میلادی")
    quarter_start = models.DateField("آغاز فصل میلادی")
    year_start = models.DateField("آغاز سال میلادی")

    jalali_year = models.PositiveSmallIntegerField("سال شمسی", db_index=True)
    jalali_season = models.PositiveSmallIntegerField("فصل شمسی")
    jalali_month = models.PositiveSmallIntegerField("ماه شمسی")
    jalali_day = models.PositiveSmallIntegerField("روز شمسی")
    jalali_weekday = models.PositiveSmallIntegerField("روز هفته شمسی (شنبه=۰)")
    jalali_week_start = models.DateField("آغاز هفته شمسی")
    jalali_month_start = models.DateField("آغاز ماه شمسی")
    jalali_season_start = models.DateField("آغاز فصل شمسی")
    jalali_year_start = models.DateField("آغاز سال شمسی")

    is_weekend = models.BooleanField("آخر هفته", default=False)
    is_holiday = models.BooleanField("تعطیل رسمی", default=False)
    holiday_name = models.CharField("مناسبت", max_length=100, blank=True)

    class Meta:
        verbose_name = "روز تقویم"
        verbose_name_plural = "تقویم"
        ordering = ["date"]

    def __str__(self):
        return f"{self.jalali_year:04d}/{self.jalali_month:02d}/{self.jalali_day:02d}"

    @property
    def is_working_day(self) -> bool:
        return not (self.is_weekend or self.is_holiday)
