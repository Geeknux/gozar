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
