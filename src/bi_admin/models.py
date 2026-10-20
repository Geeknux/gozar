import uuid

from django.conf import settings
from django.contrib.auth.models import Group
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


class ReportJob(models.Model):
    """A report run in the background (``GOZAR["REPORT_ASYNC"]`` or ``?mode=async``).

    The submitting user's language and calendar are kept with the job, so the
    worker produces the result they would have seen; row rules and visibility
    are evaluated for that user when the job runs."""

    class Status(models.TextChoices):
        QUEUED = "queued", _("Queued")
        RUNNING = "running", _("Running")
        DONE = "done", _("Done")
        FAILED = "failed", _("Failed")
        CANCELLED = "cancelled", _("Cancelled")

    FINISHED = (Status.DONE, Status.FAILED, Status.CANCELLED)

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="+",
        verbose_name=_("user"),
    )
    semantic_model = models.CharField(_("semantic model"), max_length=100)
    spec = models.JSONField(_("report spec"))
    options = models.JSONField(_("options"), default=dict, blank=True)
    status = models.CharField(
        _("status"), max_length=10, choices=Status.choices, default=Status.QUEUED, db_index=True
    )
    created_at = models.DateTimeField(_("created at"), auto_now_add=True, db_index=True)
    started_at = models.DateTimeField(_("started at"), null=True, blank=True)
    finished_at = models.DateTimeField(_("finished at"), null=True, blank=True)
    # Lease: the claiming worker's token and its last heartbeat. A running job whose
    # heartbeat is older than GOZAR["JOB_LEASE_SECONDS"] is requeued (or failed).
    lease = models.CharField(_("lease"), max_length=64, blank=True)
    heartbeat_at = models.DateTimeField(_("heartbeat"), null=True, blank=True)
    attempts = models.PositiveSmallIntegerField(_("attempts"), default=0)
    result = models.JSONField(_("result"), null=True, blank=True)
    error = models.JSONField(_("error"), null=True, blank=True)
    error_status = models.PositiveSmallIntegerField(_("error status"), null=True, blank=True)

    class Meta:
        verbose_name = _("report job")
        verbose_name_plural = _("report jobs")
        ordering = ["-created_at"]
        default_permissions = ()  # jobs are only ever visible to their owner

    def __str__(self):
        return f"{self.semantic_model} ({self.get_status_display()})"

    @property
    def finished(self) -> bool:
        return self.status in self.FINISHED


class ReportLog(models.Model):
    """Audit log: one row per report execution (run, export, drill-down, job),
    including refused and failed ones. Readable with ``bi_admin.view_reportlog``."""

    class Kind(models.TextChoices):
        RUN = "run", _("Run")
        EXPORT = "export", _("Export")
        DRILLDOWN = "drilldown", _("Drill-down")
        EMAIL = "email", _("Scheduled email")

    class Status(models.TextChoices):
        OK = "ok", _("Succeeded")
        INVALID = "invalid", _("Invalid spec")
        DENIED = "denied", _("Denied")
        FAILED = "failed", _("Failed")

    timestamp = models.DateTimeField(_("time"), auto_now_add=True, db_index=True)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        verbose_name=_("user"),
    )
    username = models.CharField(_("username"), max_length=150, blank=True)
    kind = models.CharField(_("kind"), max_length=10, choices=Kind.choices)
    semantic_model = models.CharField(_("semantic model"), max_length=100, db_index=True)
    spec = models.JSONField(_("report spec"), default=dict)
    scope = models.TextField(_("row-level scope"), blank=True)
    status = models.CharField(_("status"), max_length=10, choices=Status.choices, db_index=True)
    error_code = models.CharField(_("error code"), max_length=64, blank=True)
    error = models.TextField(_("error"), blank=True)
    duration_ms = models.PositiveIntegerField(_("duration (ms)"), null=True, blank=True)
    row_count = models.PositiveIntegerField(_("rows"), null=True, blank=True)
    cache_hit = models.BooleanField(_("served from cache"), default=False)
    database = models.CharField(_("database"), max_length=100, blank=True)
    job = models.ForeignKey(
        ReportJob,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        verbose_name=_("job"),
    )

    class Meta:
        verbose_name = _("report log entry")
        verbose_name_plural = _("report log entries")
        ordering = ["-timestamp", "-id"]
        default_permissions = ("view",)

    def __str__(self):
        return f"{self.get_kind_display()} {self.semantic_model} ({self.status})"


class NLLog(models.Model):
    """Audit log of natural-language questions (``/api/nl/ask``): who asked what,
    what spec came out, how many LLM attempts it took. Prompts and raw model
    outputs are kept in ``transcript`` only when ``GOZAR["NL"]["LOG_PROMPTS"]``
    is on (opt-in, for evaluation). Readable with ``bi_admin.view_nllog``."""

    class Status(models.TextChoices):
        OK = "ok", _("Answered")
        CLARIFICATION = "clarification", _("Clarification asked")
        INVALID = "invalid", _("No valid spec")
        DENIED = "denied", _("Denied")
        FAILED = "failed", _("Failed")

    timestamp = models.DateTimeField(_("time"), auto_now_add=True, db_index=True)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        verbose_name=_("user"),
    )
    username = models.CharField(_("username"), max_length=150, blank=True)
    question = models.TextField(_("question"))
    language = models.CharField(_("language"), max_length=10, blank=True)
    calendar = models.CharField(_("calendar"), max_length=10, blank=True)
    previous_spec = models.JSONField(_("previous spec"), null=True, blank=True)
    status = models.CharField(_("status"), max_length=16, choices=Status.choices, db_index=True)
    semantic_model = models.CharField(_("semantic model"), max_length=100, blank=True)
    spec = models.JSONField(_("report spec"), null=True, blank=True)
    interpretation = models.TextField(_("interpretation"), blank=True)
    clarification = models.TextField(_("clarification"), blank=True)
    provider = models.CharField(_("provider"), max_length=50, blank=True)
    model_name = models.CharField(_("model"), max_length=200, blank=True)
    attempts = models.PositiveSmallIntegerField(_("attempts"), default=0)
    cache_hit = models.BooleanField(_("served from cache"), default=False)
    duration_ms = models.PositiveIntegerField(_("duration (ms)"), null=True, blank=True)
    error_code = models.CharField(_("error code"), max_length=64, blank=True)
    error = models.TextField(_("error"), blank=True)
    transcript = models.JSONField(_("transcript"), default=list, blank=True)

    class Meta:
        verbose_name = _("question log entry")
        verbose_name_plural = _("question log entries")
        ordering = ["-timestamp", "-id"]
        default_permissions = ("view",)

    def __str__(self):
        return f"{self.username}: {self.question[:50]} ({self.status})"

    @property
    def retries(self) -> int:
        return max(0, self.attempts - 1)


# --- M6: saved reports, dashboards, scheduled emails ---------------------------------


class Shared(models.Model):
    """Something a user owns and may share: privately, with specific users and
    groups, or with everyone who may see its data. Whoever views it always does so
    with their *own* permissions, row-level scope and semantic visibility."""

    class Visibility(models.TextChoices):
        PRIVATE = "private", _("Only me")
        USERS = "users", _("Specific users and groups")
        EVERYONE = "everyone", _("Everyone who can see the data")

    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="+",
        verbose_name=_("owner"),
    )
    name = models.CharField(_("name"), max_length=200)
    description = models.TextField(_("description"), blank=True)
    visibility = models.CharField(
        _("shared with"), max_length=10, choices=Visibility.choices, default=Visibility.PRIVATE
    )
    shared_users = models.ManyToManyField(
        settings.AUTH_USER_MODEL, blank=True, related_name="+", verbose_name=_("users")
    )
    shared_groups = models.ManyToManyField(
        Group, blank=True, related_name="+", verbose_name=_("groups")
    )
    created_at = models.DateTimeField(_("created at"), auto_now_add=True)
    updated_at = models.DateTimeField(_("updated at"), auto_now=True)

    class Meta:
        abstract = True
        ordering = ["name", "pk"]

    def __str__(self):
        return self.name


class SavedReport(Shared):
    """A named ``ReportSpec``. The spec is stored as submitted (a ``null`` calendar
    stays null), so every viewer sees it in their own calendar and scope."""

    semantic_model = models.CharField(_("semantic model"), max_length=100, db_index=True)
    spec = models.JSONField(_("report spec"))

    class Meta(Shared.Meta):
        verbose_name = _("saved report")
        verbose_name_plural = _("saved reports")
        default_permissions = ()  # access follows ownership and sharing


class Dashboard(Shared):
    """A grid of widgets (saved reports or inline specs) with global filters.

    ``filters``: ``{"period": "last_3_months" | null, "range": [start, end] | null,
    "dimensions": ["city", …], "values": {"city": [...]}}`` — the filters the
    dashboard offers and their defaults."""

    filters = models.JSONField(_("global filters"), default=dict, blank=True)

    class Meta(Shared.Meta):
        verbose_name = _("dashboard")
        verbose_name_plural = _("dashboards")
        default_permissions = ()


class DashboardWidget(models.Model):
    """One tile of a dashboard: a saved report or an inline spec, its chart type
    and its place on a 12-column grid."""

    dashboard = models.ForeignKey(
        Dashboard, on_delete=models.CASCADE, related_name="widgets", verbose_name=_("dashboard")
    )
    title = models.CharField(_("title"), max_length=200, blank=True)
    saved_report = models.ForeignKey(
        SavedReport,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="widgets",
        verbose_name=_("saved report"),
    )
    spec = models.JSONField(_("report spec"), null=True, blank=True)
    chart = models.CharField(_("chart"), max_length=10, blank=True)
    x = models.PositiveSmallIntegerField(default=0)
    y = models.PositiveSmallIntegerField(default=0)
    w = models.PositiveSmallIntegerField(default=6)
    h = models.PositiveSmallIntegerField(default=4)

    class Meta:
        verbose_name = _("dashboard widget")
        verbose_name_plural = _("dashboard widgets")
        ordering = ["y", "x", "pk"]
        default_permissions = ()

    def __str__(self):
        return self.title or (self.saved_report.name if self.saved_report else str(self.pk))


class Schedule(models.Model):
    """A saved report or dashboard e-mailed to users of the system, each one with
    their own scope, language and calendar."""

    class Frequency(models.TextChoices):
        DAILY = "daily", _("Daily")
        WEEKLY = "weekly", _("Weekly")
        MONTHLY = "monthly", _("Monthly")

    class Attachment(models.TextChoices):
        NONE = "", _("No attachment")
        CSV = "csv", "CSV"
        XLSX = "xlsx", "Excel (XLSX)"

    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="+",
        verbose_name=_("owner"),
    )
    name = models.CharField(_("name"), max_length=200, blank=True)
    saved_report = models.ForeignKey(
        SavedReport,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="schedules",
        verbose_name=_("saved report"),
    )
    dashboard = models.ForeignKey(
        Dashboard,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="schedules",
        verbose_name=_("dashboard"),
    )
    frequency = models.CharField(_("frequency"), max_length=10, choices=Frequency.choices)
    time = models.TimeField(_("time of day"))
    weekday = models.PositiveSmallIntegerField(_("weekday (Monday=0)"), null=True, blank=True)
    day_of_month = models.PositiveSmallIntegerField(_("day of month"), null=True, blank=True)
    calendar = models.CharField(
        _("calendar"),
        max_length=10,
        choices=UserPreferences.Calendar.choices,
        default=UserPreferences.Calendar.GREGORIAN,
    )
    timezone = models.CharField(_("time zone"), max_length=64)
    recipients = models.ManyToManyField(
        settings.AUTH_USER_MODEL, related_name="+", verbose_name=_("recipients")
    )
    attachment = models.CharField(
        _("attachment"), max_length=5, choices=Attachment.choices, blank=True, default="xlsx"
    )
    enabled = models.BooleanField(_("enabled"), default=True)
    next_run_at = models.DateTimeField(_("next run"), null=True, blank=True, db_index=True)
    last_run_at = models.DateTimeField(_("last run"), null=True, blank=True)
    created_at = models.DateTimeField(_("created at"), auto_now_add=True)
    updated_at = models.DateTimeField(_("updated at"), auto_now=True)

    class Meta:
        verbose_name = _("email schedule")
        verbose_name_plural = _("email schedules")
        ordering = ["next_run_at", "pk"]
        default_permissions = ()
        constraints = [
            models.CheckConstraint(
                condition=models.Q(saved_report__isnull=False, dashboard__isnull=True)
                | models.Q(saved_report__isnull=True, dashboard__isnull=False),
                name="bi_admin_schedule_one_target",
            )
        ]

    def __str__(self):
        return self.name or str(self.target)

    @property
    def target(self):
        return self.saved_report or self.dashboard


class ScheduleDelivery(models.Model):
    """Audit log of scheduled e-mails: one row per recipient per send. The reports
    run for it are in ``ReportLog`` (kind ``email``, user = the recipient)."""

    class Status(models.TextChoices):
        SENT = "sent", _("Sent")
        FAILED = "failed", _("Failed")
        SKIPPED = "skipped", _("Skipped")

    timestamp = models.DateTimeField(_("time"), auto_now_add=True, db_index=True)
    schedule = models.ForeignKey(
        Schedule,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="deliveries",
        verbose_name=_("schedule"),
    )
    schedule_name = models.CharField(_("schedule"), max_length=200, blank=True)
    recipient = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        verbose_name=_("recipient"),
    )
    username = models.CharField(_("username"), max_length=150, blank=True)
    email = models.CharField(_("e-mail address"), max_length=254, blank=True)
    status = models.CharField(_("status"), max_length=10, choices=Status.choices, db_index=True)
    trigger = models.CharField(_("trigger"), max_length=10, default="schedule")  # or "manual"
    language = models.CharField(_("language"), max_length=10, blank=True)
    calendar = models.CharField(_("calendar"), max_length=10, blank=True)
    reports = models.PositiveSmallIntegerField(_("reports"), default=0)
    attachments = models.PositiveSmallIntegerField(_("attachments"), default=0)
    error = models.TextField(_("error"), blank=True)
    duration_ms = models.PositiveIntegerField(_("duration (ms)"), null=True, blank=True)

    class Meta:
        verbose_name = _("email delivery")
        verbose_name_plural = _("email deliveries")
        ordering = ["-timestamp", "-id"]
        default_permissions = ()  # readable with bi_admin.view_reportlog

    def __str__(self):
        return f"{self.schedule_name} → {self.username} ({self.status})"


# --- M6.5: admin parity and the system dashboard -------------------------------------


class SavedListView(models.Model):
    """A user's named view of a CRUD list: search, filters, ordering and columns.

    ``params`` is the list's query string as a dict (``{"q": "…", "status": "paid",
    "ordering": "-created_at"}``); ``columns`` the chosen column names in order
    (empty: the model's ``list_display``). Views are private to their owner."""

    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="+",
        verbose_name=_("owner"),
    )
    model_label = models.CharField(_("model"), max_length=200)
    name = models.CharField(_("name"), max_length=100)
    params = models.JSONField(_("filters and search"), default=dict, blank=True)
    columns = models.JSONField(_("columns"), default=list, blank=True)
    is_default = models.BooleanField(_("default view"), default=False)
    created_at = models.DateTimeField(_("created at"), auto_now_add=True)
    updated_at = models.DateTimeField(_("updated at"), auto_now=True)

    class Meta:
        verbose_name = _("saved list view")
        verbose_name_plural = _("saved list views")
        ordering = ["name", "pk"]
        default_permissions = ()  # private to the owner
        constraints = [
            models.UniqueConstraint(
                fields=["owner", "model_label", "name"], name="bi_admin_listview_unique_name"
            )
        ]
        indexes = [models.Index(fields=["owner", "model_label"])]

    def __str__(self):
        return self.name


class SystemDashboard(models.Model):
    """No table: holds the ``bi_admin.view_system`` permission for the System page
    (database statistics, migrations, checks, job queue). Superusers have it
    implicitly; grant it to others like any permission."""

    class Meta:
        managed = False
        verbose_name = _("system dashboard")
        verbose_name_plural = _("system dashboard")
        default_permissions = ()
        permissions = [("view_system", _("Can view the system dashboard"))]

    def __str__(self):
        return str(self._meta.verbose_name)
