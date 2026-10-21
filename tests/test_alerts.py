"""Alerts: the pipeline (dedup, rate limits, quiet hours, mutes, resolved notices),
scrubbing, every source, the outbox with retries, the Telegram channel with its
allow-list (sending and incoming updates), the bot commands, the management
commands and the System page API.

The Telegram Bot API is faked (``telegram._http``); nothing reaches the network.
The token and the ids below are obviously fake.
"""

import datetime as dt
import logging
from io import StringIO

import pytest
from django.contrib.auth.signals import user_login_failed
from django.core import checks
from django.core.management import CommandError, call_command
from django.db import DEFAULT_DB_ALIAS
from django.http import HttpResponse
from django.test import override_settings
from django.urls import path
from django.utils import timezone

from bi_admin import alerts
from bi_admin.alerts import (
    bot,
    handlers,
    hooks,
    messages,
    monitors,
    outbox,
    pipeline,
    scrub,
    telegram,
)
from bi_admin.models import (
    Alert,
    AlertDelivery,
    AlertSource,
    AlertState,
    ReportJob,
    WorkerHeartbeat,
)

pytestmark = pytest.mark.django_db

API = "/gozar/api/system"
TOKEN = "123456:FAKE-test-token-not-a-real-one-xxxxxxxxxx"  # bot-token shaped, not real
ALLOWED = [11, 22]  # fake Telegram user ids
STRANGER = 99
NOW = dt.datetime(2026, 10, 21, 12, 0, tzinfo=dt.UTC)


class FakeTelegram:
    """Stands in for ``telegram._http``: records calls, answers like the Bot API."""

    def __init__(self):
        self.calls = []
        self.responses = []  # queued (status, body) answers or exceptions
        self.updates = []  # queued getUpdates results

    def __call__(self, url, payload, timeout):
        assert url.startswith("https://api.telegram.org/bot")
        method = url.rsplit("/", 1)[1]
        self.calls.append((method, payload))
        if self.responses:
            answer = self.responses.pop(0)
            if isinstance(answer, Exception):
                raise answer
            return answer
        if method == "getMe":
            return 200, {"ok": True, "result": {"username": "gozar_test_bot", "first_name": "G"}}
        if method == "getUpdates":
            return 200, {"ok": True, "result": self.updates.pop(0) if self.updates else []}
        return 200, {"ok": True, "result": {"message_id": len(self.calls)}}

    def sent(self):
        return [p for method, p in self.calls if method == "sendMessage"]


def configure(settings, telegram_options=None, **options):
    gozar = dict(settings.GOZAR)
    current = dict(gozar.get("ALERTS") or {})
    if telegram_options is not None:
        current["TELEGRAM"] = {**(current.get("TELEGRAM") or {}), **telegram_options}
    current.update(options)
    gozar["ALERTS"] = current
    settings.GOZAR = gozar


@pytest.fixture
def fake(monkeypatch):
    api = FakeTelegram()
    monkeypatch.setattr(telegram, "_http", api)
    return api


@pytest.fixture
def channel(settings, monkeypatch, fake):
    """Telegram configured with the fake token and two allowed ids."""
    monkeypatch.setenv("GOZAR_TELEGRAM_BOT_TOKEN", TOKEN)
    configure(settings, {"ALLOWED_CHAT_IDS": "11, 22"})
    return fake


@pytest.fixture
def interactive(settings, channel):
    configure(settings, {"INTERACTIVE": True})
    return channel


def alert_rows(source=None):
    qs = Alert.objects.order_by("id")
    return list(qs.filter(source=source) if source else qs)


def later(seconds):
    return NOW + dt.timedelta(seconds=seconds)


# --- scrubbing ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        "Authorization: Bearer abcdefghijklmnop1234",
        "password=hunter2-secret",
        '{"csrfmiddlewaretoken": "csrf-value-xyz"}',
        "Cookie: sessionid=cookie-value-xyz; csrftoken=csrf-value-xyz",
        "postgres://gozar:db-password-xyz@db.local/gozar",
        "sk-abcdefghijklmnopqrstuvwxyz",
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U",
        "api_key: AbCdEf0123456789",
        "session key 0123456789abcdef0123456789abcdef",
    ],
)
def test_secrets_are_masked(raw):
    out = scrub.text(raw)
    assert scrub.MASK in out
    for secret in (
        "abcdefghijklmnop1234",
        "hunter2",
        "csrf-value",
        "cookie-value",
        "db-password",
        "sk-abc",
        "dozjgNry",
        "AbCdEf0123",
        "0123456789abcdef0123",
    ):
        assert secret not in out


def test_known_secret_values_and_emails_are_masked(monkeypatch, settings):
    monkeypatch.setenv("GOZAR_TELEGRAM_BOT_TOKEN", TOKEN)
    out = scrub.text(f"token {TOKEN} mail ali@example.com key {settings.SECRET_KEY}")
    assert TOKEN not in out and "ali@example.com" not in out and settings.SECRET_KEY not in out


def test_paths_and_identifiers_survive_scrubbing():
    line = 'File "bi_admin/engine/runner.py", line 12, in run_report_with_a_long_function_name'
    assert scrub.text(line) == line


def test_scrubbed_mappings_drop_secret_keys():
    data = scrub.data(
        {
            "password": "x",
            "HTTP_AUTHORIZATION": "y",
            "nested": {"api_key": "z"},
            "path": "/a",
            "n": 3,
        }
    )
    assert data == {
        "password": scrub.MASK,
        "HTTP_AUTHORIZATION": scrub.MASK,
        "nested": {"api_key": scrub.MASK},
        "path": "/a",
        "n": 3,
    }


# --- the pipeline: dedup, windows, severities ----------------------------------------------


def test_repeats_inside_the_window_fold_into_one_alert(channel):
    first = pipeline.raise_alert("jobs", "k", "error", "Boom", now=NOW)
    for i in range(3):
        again = pipeline.raise_alert("jobs", "k", "error", "Boom", now=later(10 * (i + 1)))
        assert again.pk == first.pk
    assert len(alert_rows()) == 1
    first.refresh_from_db()
    assert first.repeats == 3 and first.key == "jobs:k"
    assert AlertDelivery.objects.count() == len(ALLOWED)  # one message per allowed id
    assert AlertState.objects.get(key="jobs:k").suppressed == 3


def test_after_the_window_the_next_message_counts_the_held_back_repeats(channel):
    pipeline.raise_alert("jobs", "k", "error", "Boom", now=NOW)
    pipeline.raise_alert("jobs", "k", "error", "Boom", now=later(60))
    pipeline.raise_alert("jobs", "k", "error", "Boom", now=later(120))
    second = pipeline.raise_alert("jobs", "k", "error", "Boom", now=later(901))  # window: 900 s
    assert second.suppressed_before == 2 and len(alert_rows()) == 2
    assert "2 more alerts like this were held back" in messages.render(second)
    assert AlertState.objects.get(key="jobs:k").suppressed == 0


def test_the_window_is_configurable_per_source(channel, settings):
    configure(settings, SOURCES={"jobs": {"window": 30}})
    pipeline.raise_alert("jobs", "k", "error", "Boom", now=NOW)
    assert pipeline.raise_alert("jobs", "k", "error", "Boom", now=later(31)).suppressed_before == 0
    assert len(alert_rows()) == 2


def test_different_keys_are_independent(channel):
    pipeline.raise_alert("jobs", "a", "error", "A", now=NOW)
    pipeline.raise_alert("jobs", "b", "error", "B", now=NOW)
    assert len(alert_rows()) == 2


def test_a_higher_severity_breaks_through_the_window(channel):
    pipeline.raise_alert("jobs", "k", "warning", "Slow", now=NOW)
    worse = pipeline.raise_alert("jobs", "k", "critical", "Down", now=later(5))
    assert len(alert_rows()) == 2 and worse.delivery == "pending"


def test_below_the_minimum_severity_is_recorded_but_not_sent(channel):
    alert = pipeline.raise_alert("exceptions", "k", "warning", "Meh", now=NOW)  # minimum: error
    assert alert.delivery == "below_threshold" and not AlertDelivery.objects.exists()


def test_a_disabled_source_records_nothing(channel, settings):
    configure(settings, SOURCES={"jobs": {"enabled": False}})
    assert pipeline.raise_alert("jobs", "k", "error", "Boom") is None
    assert not Alert.objects.exists()


def test_system_page_overrides_beat_settings(channel, settings):
    configure(settings, SOURCES={"jobs": {"enabled": False}})
    AlertSource.objects.create(source="jobs", enabled=True, min_severity="critical")
    alert = pipeline.raise_alert("jobs", "k", "error", "Boom")
    assert alert is not None and alert.delivery == "below_threshold"


def test_without_a_channel_alerts_are_still_recorded():
    alert = pipeline.raise_alert("jobs", "k", "error", "Boom")
    assert alert.delivery == "no_channel" and not AlertDelivery.objects.exists()


def test_alerts_switched_off_record_nothing(settings):
    configure(settings, ENABLED=False)
    assert pipeline.raise_alert("jobs", "k", "error", "Boom") is None
    assert alerts.send_test() is None and not Alert.objects.exists()


def test_raising_never_raises(monkeypatch, caplog):
    def broken(*args, **kwargs):
        raise RuntimeError("database gone")

    monkeypatch.setattr(pipeline, "_record", broken)
    with caplog.at_level(logging.WARNING, logger="bi_admin.alerts"):
        assert pipeline.raise_alert("jobs", "k", "error", "Boom") is None
        assert pipeline.resolve("jobs", "k") is None
    assert "Could not record" in caplog.text


def test_title_message_and_details_are_scrubbed(channel):
    alert = pipeline.raise_alert(
        "jobs", "k", "error", f"token {TOKEN}", "password=hunter2", {"cookie": "abc", "n": 1}
    )
    assert TOKEN not in alert.title and "hunter2" not in alert.message
    assert alert.details == {"cookie": scrub.MASK, "n": 1}


# --- resolved notices ------------------------------------------------------------------------


def test_a_resolved_notice_follows_a_delivered_alert(channel):
    pipeline.raise_alert("preaggregations", "daily:failed", "error", "Refresh failed", now=NOW)
    resolved = pipeline.resolve("preaggregations", "daily:failed", "Refreshed again", now=later(30))
    assert resolved.kind == "resolved" and resolved.delivery == "pending"
    assert resolved.severity == "error"  # the condition's severity (quiet hours, display)
    assert "Resolved" in messages.render(resolved) and "✅" in messages.render(resolved)
    assert not AlertState.objects.get(key="preaggregations:daily:failed").firing
    # nothing firing: nothing to resolve, and the next failure is new at once
    assert pipeline.resolve("preaggregations", "daily:failed", now=later(40)) is None
    again = pipeline.raise_alert("preaggregations", "daily:failed", "error", "Again", now=later(50))
    assert again.delivery == "pending" and again.kind == "alert"


def test_no_resolved_message_for_a_condition_that_was_never_sent(channel):
    pipeline.raise_alert("exceptions", "k", "warning", "Meh", now=NOW)  # below the minimum
    resolved = pipeline.resolve("exceptions", "k", now=later(10))
    assert resolved.delivery == "below_threshold"
    assert AlertDelivery.objects.filter(alert=resolved).count() == 0


def test_resolving_an_unknown_key_does_nothing():
    assert pipeline.resolve("jobs", "never-raised") is None and not Alert.objects.exists()


# --- quiet hours and mutes ---------------------------------------------------------------------


def test_quiet_hours_hold_non_critical_alerts_until_they_end(channel, settings):
    configure(settings, QUIET_HOURS="23:00-07:00", QUIET_HOURS_TIMEZONE="UTC")
    night = dt.datetime(2026, 10, 21, 23, 30, tzinfo=dt.UTC)
    held = pipeline.raise_alert("jobs", "k", "error", "Boom", now=night)
    assert held.delivery == "deferred"
    morning = dt.datetime(2026, 10, 22, 7, 0, tzinfo=dt.UTC)
    assert {d.next_attempt_at for d in held.deliveries.all()} == {morning}
    assert outbox.deliver_due(night + dt.timedelta(hours=1)) == {"sent": 0, "retry": 0, "failed": 0}
    assert outbox.deliver_due(morning)["sent"] == len(ALLOWED)
    held.refresh_from_db()
    assert held.delivery == "sent"


def test_critical_alerts_ignore_quiet_hours(channel, settings):
    configure(settings, QUIET_HOURS="22:00-08:00", QUIET_HOURS_TIMEZONE="UTC")
    night = dt.datetime(2026, 10, 21, 3, 0, tzinfo=dt.UTC)
    alert = pipeline.raise_alert("worker", "crashed", "critical", "Down", now=night)
    assert alert.delivery == "pending"
    assert outbox.deliver_due(night)["sent"] == len(ALLOWED)


def test_quiet_hours_within_one_day_and_outside(settings):
    configure(settings, QUIET_HOURS="12:00-14:00", QUIET_HOURS_TIMEZONE="UTC")
    assert pipeline.quiet_until(dt.datetime(2026, 1, 1, 13, tzinfo=dt.UTC)) == dt.datetime(
        2026, 1, 1, 14, tzinfo=dt.UTC
    )
    assert pipeline.quiet_until(dt.datetime(2026, 1, 1, 15, tzinfo=dt.UTC)) is None
    configure(settings, QUIET_HOURS="nonsense")
    assert pipeline.quiet_until(NOW) is None


def test_a_muted_key_is_recorded_but_not_sent(channel):
    pipeline.mute("jobs:k", NOW + dt.timedelta(hours=1))
    assert pipeline.raise_alert("jobs", "k", "critical", "Boom", now=NOW).delivery == "muted"
    assert not AlertDelivery.objects.exists()
    pipeline.resolve("jobs", "k", now=later(10))
    later_alert = pipeline.raise_alert("jobs", "k", "error", "Boom", now=later(7200))
    assert later_alert.delivery == "pending"


# --- the outbox -------------------------------------------------------------------------------


def test_the_outbox_sends_html_to_every_allowed_id(channel):
    alert = pipeline.raise_alert("jobs", "k", "error", "Report <b>x</b> & co", "1 < 2", now=NOW)
    assert outbox.deliver_due(NOW) == {"sent": 2, "retry": 0, "failed": 0}
    sent = channel.sent()
    assert sorted(p["chat_id"] for p in sent) == ALLOWED
    assert all(p["parse_mode"] == "HTML" for p in sent)
    text = sent[0]["text"]
    assert "Report &lt;b&gt;x&lt;/b&gt; &amp; co" in text and "1 &lt; 2" in text
    alert.refresh_from_db()
    assert alert.delivery == "sent" and alert.delivered_at is not None
    assert outbox.deliver_due(NOW)["sent"] == 0  # never twice


def test_transient_failures_are_retried_with_backoff(channel, settings):
    configure(settings, MAX_ATTEMPTS=3, RETRY_BACKOFF=30)
    configure(settings, {"ALLOWED_CHAT_IDS": "11"})
    alert = pipeline.raise_alert("jobs", "k", "error", "Boom", now=NOW)
    channel.responses = [
        OSError("network down"),
        (502, {"ok": False, "description": "Bad gateway"}),
    ]
    assert outbox.deliver_due(NOW) == {"sent": 0, "retry": 1, "failed": 0}
    row = AlertDelivery.objects.get()
    assert row.next_attempt_at == later(30) and "network down" in row.last_error
    assert outbox.deliver_due(later(29))["retry"] == 0  # not due yet
    assert outbox.deliver_due(later(30)) == {"sent": 0, "retry": 1, "failed": 0}
    row.refresh_from_db()
    assert row.next_attempt_at == later(30 + 60)  # doubled
    assert outbox.deliver_due(later(90)) == {"sent": 1, "retry": 0, "failed": 0}
    alert.refresh_from_db()
    assert alert.delivery == "sent" and AlertDelivery.objects.get().attempts == 3


def test_retries_end_after_max_attempts(channel, settings):
    configure(settings, MAX_ATTEMPTS=2, RETRY_BACKOFF=1)
    configure(settings, {"ALLOWED_CHAT_IDS": "11"})
    alert = pipeline.raise_alert("jobs", "k", "error", "Boom", now=NOW)
    channel.responses = [OSError("down"), OSError("still down")]
    outbox.deliver_due(NOW)
    assert outbox.deliver_due(later(5)) == {"sent": 0, "retry": 0, "failed": 1}
    alert.refresh_from_db()
    assert alert.delivery == "failed" and AlertDelivery.objects.get().status == "failed"


def test_permanent_errors_fail_at_once_and_429_waits(channel, settings):
    configure(settings, {"ALLOWED_CHAT_IDS": "11"})
    pipeline.raise_alert("jobs", "a", "error", "A", now=NOW)
    channel.responses = [
        (
            403,
            {
                "ok": False,
                "error_code": 403,
                "description": "Forbidden: bot was blocked by the user",
            },
        )
    ]
    assert outbox.deliver_due(NOW)["failed"] == 1
    pipeline.raise_alert("jobs", "b", "error", "B", now=NOW)
    channel.responses = [
        (
            429,
            {
                "ok": False,
                "error_code": 429,
                "description": "Too Many Requests",
                "parameters": {"retry_after": 120},
            },
        )
    ]
    assert outbox.deliver_due(NOW)["retry"] == 1
    assert AlertDelivery.objects.get(status="queued").next_attempt_at == later(120)


def test_a_message_stuck_in_sending_is_retried(channel, settings):
    configure(settings, {"ALLOWED_CHAT_IDS": "11"})
    pipeline.raise_alert("jobs", "k", "error", "Boom", now=NOW)
    AlertDelivery.objects.update(status="sending", lease="dead", leased_at=NOW, attempts=1)
    assert outbox.deliver_due(later(60))["sent"] == 0
    assert outbox.deliver_due(later(121))["sent"] == 1


def test_telegram_failures_are_logged_without_the_token(channel, caplog, settings):
    configure(settings, {"ALLOWED_CHAT_IDS": "11"})
    pipeline.raise_alert("jobs", "k", "error", "Boom", now=NOW)
    channel.responses = [OSError(f"cannot reach https://api.telegram.org/bot{TOKEN}/sendMessage")]
    with caplog.at_level(logging.DEBUG):
        assert outbox.deliver_due(NOW)["retry"] == 1  # logged, not raised
    assert "cannot reach" in caplog.text and TOKEN not in caplog.text
    assert TOKEN not in AlertDelivery.objects.get().last_error


def test_a_channel_switched_off_meanwhile_fails_its_messages(channel, monkeypatch):
    pipeline.raise_alert("jobs", "k", "error", "Boom", now=NOW)
    monkeypatch.delenv("GOZAR_TELEGRAM_BOT_TOKEN")
    assert outbox.deliver_due(NOW)["failed"] == 2 and not channel.calls


# --- the channel: configuration and the allow-list -------------------------------------------


def test_the_channel_is_off_without_token_and_ids():
    cfg = telegram.config()
    assert not cfg.enabled and cfg.problems == []
    assert pipeline.channels() == []


def test_the_channel_refuses_to_start_without_an_allow_list(settings, monkeypatch, fake):
    monkeypatch.setenv("GOZAR_TELEGRAM_BOT_TOKEN", TOKEN)
    cfg = telegram.config()
    assert not cfg.enabled and [p["code"] for p in cfg.problems] == ["no_allowed_ids"]
    assert [m.id for m in checks.run_checks() if m.id.startswith("bi_admin.W002")]
    with pytest.raises(telegram.NotAllowed):
        telegram.Client(cfg).send(11, "hi")
    assert not fake.calls


def test_the_channel_refuses_to_start_without_a_token(settings, fake):
    configure(settings, {"ALLOWED_CHAT_IDS": [11]})
    cfg = telegram.config()
    assert not cfg.enabled and [p["code"] for p in cfg.problems] == ["no_token"]
    configure(settings, {"ENABLED": True, "ALLOWED_CHAT_IDS": ""})
    assert {p["code"] for p in telegram.config().problems} == {"no_token", "no_allowed_ids"}


def test_invalid_ids_keep_the_channel_off(settings, channel):
    configure(settings, {"ALLOWED_CHAT_IDS": "11, @someone"})
    cfg = telegram.config()
    assert not cfg.enabled and cfg.allowed == [] and cfg.problems[0]["code"] == "invalid_ids"
    assert telegram.parse_ids([11, "22", " 33 ", True]) == ([11, 22, 33], ["True"])


def test_enabled_false_wins(settings, channel):
    configure(settings, {"ENABLED": False})
    assert not telegram.config().enabled and telegram.config().problems == []


def test_a_token_in_settings_is_a_check_error(settings):
    configure(settings, {"TOKEN": TOKEN})
    errors = [m for m in checks.run_checks() if m.id == "bi_admin.E006"]
    assert errors and TOKEN not in errors[0].msg
    configure(settings, {"TOKEN": TOKEN, "ENABLED": False})
    assert [m for m in checks.run_checks() if m.id == "bi_admin.E006"]


def test_the_allow_list_falls_back_to_the_environment(settings, monkeypatch, fake):
    gozar = dict(settings.GOZAR)
    gozar["ALERTS"] = {"TELEGRAM": {}}
    settings.GOZAR = gozar
    monkeypatch.setenv("GOZAR_TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("GOZAR_TELEGRAM_ALLOWED_CHAT_IDS", "11 22")
    assert telegram.config().allowed == ALLOWED


def test_sending_refuses_any_id_outside_the_allow_list(channel):
    client = telegram.Client()
    for target in (STRANGER, -100123, "11; 99", "abc", 0):
        with pytest.raises(telegram.NotAllowed):
            client.send(target, "hi")
    assert not channel.calls
    client.send(11, "hi")
    assert channel.sent() == [
        {"chat_id": 11, "text": "hi", "parse_mode": "HTML", "disable_web_page_preview": True}
    ]


def test_a_delivery_row_for_a_foreign_id_is_never_sent(channel):
    alert = pipeline.raise_alert("jobs", "k", "error", "Boom", now=NOW)
    AlertDelivery.objects.create(
        alert=alert, channel="telegram", target=str(STRANGER), next_attempt_at=NOW
    )
    counts = outbox.deliver_due(NOW)
    assert counts == {"sent": 2, "retry": 0, "failed": 1}
    assert STRANGER not in [p["chat_id"] for p in channel.sent()]


# --- messages ------------------------------------------------------------------------------


def test_messages_link_back_when_base_url_is_set(channel, settings):
    alert = pipeline.raise_alert("jobs", "k", "error", "Boom")
    gozar = dict(settings.GOZAR, BASE_URL="https://bi.example.com/gozar/")
    settings.GOZAR = gozar
    text = messages.render(alert)
    assert (
        f'<a href="https://bi.example.com/gozar/system?alert={alert.pk}">Open in Gozar</a>' in text
    )
    settings.GOZAR = dict(gozar, BASE_URL=None)
    assert "<a href" not in messages.render(alert)


def test_persian_messages_are_right_to_left(channel, settings):
    configure(settings, LANGUAGE="fa")
    alert = alerts.send_test()
    text = messages.render(alert)
    assert alert.language == "fa" and alert.title.startswith("هشدار آزمایشی")
    lines = [line for line in text.split("\n") if line]
    assert all(line.startswith(messages.RLM) for line in lines)
    assert messages.FSI in text and "#" + str(alert.pk).translate(messages.PERSIAN_DIGITS) in text


def test_long_messages_fit_telegrams_limit(channel):
    alert = pipeline.raise_alert(
        "jobs", "k", "error", "Boom", "x " * 1490, {"traceback": "line\n" * 400}
    )
    assert len(messages.render(alert)) <= telegram.MAX_MESSAGE


# --- source: unhandled exceptions and HTTP 500s ------------------------------------------------


def explode(request, pk):
    raise ValueError("password=hunter2 went wrong for ali@example.com")


def server_error(request):
    return HttpResponse("nope", status=503)


urlpatterns = [path("explode/<int:pk>/", explode), path("down/", server_error)]


@pytest.fixture
def boom_urls(settings):
    from example_project import urls

    settings.ROOT_URLCONF = __name__
    globals()["urlpatterns"] = [*urlpatterns, *urls.urlpatterns]
    return settings


def test_an_unhandled_exception_becomes_an_alert(boom_urls, client, admin_user):
    client.raise_request_exception = False
    client.force_login(admin_user)
    client.cookies["csrftoken"] = "csrf-cookie-value-xyz"
    resp = client.get("/explode/7/?q=secret-query", HTTP_AUTHORIZATION="Bearer abcdefghij123456789")
    assert resp.status_code == 500
    (alert,) = alert_rows("exceptions")
    assert alert.title == "Unhandled ValueError" and alert.severity == "error"
    d = alert.details
    assert (d["method"], d["path"], d["route"], d["user_id"]) == (
        "GET",
        "/explode/7/",
        "explode/<int:pk>/",
        admin_user.pk,
    )
    assert d["exception"] == "ValueError" and "explode" in d["traceback"]
    assert alert.release
    stored = str(alert.title) + alert.message + str(alert.details)
    for leaked in (
        "hunter2",
        "ali@example.com",
        "secret-query",
        "csrf-cookie-value",
        "abcdefghij123456789",
        "sessionid",
        admin_user.email,
    ):
        assert leaked not in stored
    # the same bug again is the same key, folded into the alert
    client.get("/explode/8/")
    assert len(alert_rows("exceptions")) == 1 and alert_rows("exceptions")[0].repeats == 1


def test_a_5xx_response_without_exception_is_an_alert(boom_urls, client):
    assert client.get("/down/").status_code == 503
    (alert,) = alert_rows("exceptions")
    assert alert.title == "HTTP 503 response" and alert.details["user_id"] is None


def test_the_handler_is_attached_once_and_never_raises(monkeypatch):
    attached = [
        h
        for h in logging.getLogger("django.request").handlers
        if isinstance(h, handlers.AlertHandler)
    ]
    assert len(attached) == 1
    handlers.attach()
    assert (
        len(
            [
                h
                for h in logging.getLogger("django.request").handlers
                if isinstance(h, handlers.AlertHandler)
            ]
        )
        == 1
    )

    def broken(*args, **kwargs):
        raise RuntimeError("no database")

    monkeypatch.setattr(handlers.AlertHandler, "report", broken)
    record = logging.LogRecord("django.request", logging.ERROR, __file__, 1, "x", (), None)
    attached[0].emit(record)  # no exception, nothing printed


def test_the_handlers_own_loggers_are_ignored():
    handler = handlers.AlertHandler()
    try:
        raise KeyError("x")
    except KeyError:
        import sys

        info = sys.exc_info()
    handler.emit(logging.LogRecord("bi_admin.alerts", logging.ERROR, __file__, 1, "x", (), info))
    assert not Alert.objects.exists()
    handler.emit(logging.LogRecord("myapp", logging.ERROR, __file__, 1, "x", (), info))
    assert alert_rows("exceptions")[0].title == "Unhandled KeyError"


def test_the_release_comes_from_settings_or_environment(settings, monkeypatch):
    from bi_admin.alerts.release import release

    monkeypatch.setenv("GOZAR_RELEASE", "2026.10.21-test")
    assert release() == "2026.10.21-test"
    configure(settings, RELEASE="v9")
    assert release() == "v9"


# --- source: jobs ----------------------------------------------------------------------------


def make_job(user, **kw):
    return ReportJob.objects.create(
        **{"user": user, "semantic_model": "order_metrics", "spec": {}, **kw}
    )


def test_a_failed_job_is_alerted(admin_user, monkeypatch):
    from bi_admin import engine
    from bi_admin.execution import jobs

    def boom(*args, **kwargs):
        raise RuntimeError("database exploded")

    monkeypatch.setattr(engine, "run", boom)
    job = jobs.claim(make_job(admin_user).pk)
    assert jobs.run_job(job, heartbeat=False).status == "failed"
    (alert,) = alert_rows("jobs")
    assert alert.key == "jobs:report:order_metrics:internal_error" and alert.severity == "error"


def test_refused_or_invalid_jobs_are_not_operational_alerts(admin_user):
    from bi_admin.execution import jobs

    job = make_job(admin_user, spec={"semantic_model": "nope", "measures": ["x"]})
    assert jobs.run_job(jobs.claim(job.pk), heartbeat=False).error_status in (403, 422)
    assert not alert_rows("jobs")


def test_a_lost_worker_is_alerted(admin_user, settings):
    from bi_admin.execution import jobs

    gozar = dict(settings.GOZAR, JOB_MAX_ATTEMPTS=1)
    settings.GOZAR = gozar
    job = jobs.claim(make_job(admin_user).pk)
    ReportJob.objects.filter(pk=job.pk).update(heartbeat_at=timezone.now() - dt.timedelta(hours=1))
    assert jobs.recover_stale()["failed"] == 1
    (alert,) = alert_rows("jobs")
    assert alert.key == "jobs:worker_lost:report" and alert.details["attempts"] == 1


# --- source: worker heartbeat ------------------------------------------------------------------


def test_a_stale_worker_heartbeat_is_alerted_and_resolved(channel):
    monitors.check_worker(NOW)
    assert not Alert.objects.exists()  # no worker ever ran: nothing to watch
    monitors.beat("host:1", started=True, now=NOW)
    monitors.check_worker(later(60))
    assert not Alert.objects.exists()
    monitors.check_worker(later(301))
    (alert,) = alert_rows("worker")
    assert alert.key == "worker:stale" and alert.severity == "error"
    monitors.beat("host:1", now=later(400))
    monitors.check_worker(later(410))
    assert alert_rows("worker")[-1].kind == "resolved"


def test_a_stopped_worker_is_not_stale():
    monitors.beat("host:1", started=True, now=NOW)
    monitors.stopped("host:1")
    monitors.check_worker(later(3600))
    assert not Alert.objects.exists()


def test_the_worker_beats_sends_the_outbox_and_stops_cleanly(channel, settings):
    configure(settings, SOURCES={"checks": {"enabled": False}})
    pipeline.raise_alert("jobs", "k", "error", "Boom")
    out = StringIO()
    call_command("gozar_worker", burst=True, stdout=out)
    assert len(channel.sent()) == 2 and "Alert messages: 2 sent" in out.getvalue()
    worker = WorkerHeartbeat.objects.get()
    assert worker.stopped_at is not None and worker.name == monitors.worker_name()


def test_the_worker_runs_the_system_checks_at_start(settings):
    settings.DEBUG = True
    call_command("gozar_worker", burst=True, stdout=StringIO())
    assert alert_rows("checks")


# --- source: schedules, pre-aggregations, columnar, NL --------------------------------------------


def test_schedule_failures_alert_once_per_send_and_resolve():
    from types import SimpleNamespace

    schedule = SimpleNamespace(pk=5)

    def delivery(status, user):
        return SimpleNamespace(
            status=status,
            schedule_name="Weekly",
            recipient_id=user,
            error="ConnectionRefusedError: SMTP down" if status == "failed" else "",
            trigger="schedule",
        )

    hooks.schedule_deliveries(
        schedule, [delivery("failed", 1), delivery("sent", 2), delivery("failed", 3)]
    )
    (alert,) = alert_rows("schedules")
    assert alert.key == "schedules:schedule:5" and alert.details["failed"] == 2
    assert "SMTP down" in alert.message
    hooks.schedule_deliveries(schedule, [delivery("sent", 1)])
    assert alert_rows("schedules")[-1].kind == "resolved"


def test_refresh_and_sync_failures_alert_and_resolve():
    hooks.refresh_failed("order_metrics.daily_sales", RuntimeError("disk full"), "rebuild")
    hooks.sync_failed("duckdb", RuntimeError("DuckDB isn't installed"))
    assert [a.key for a in alert_rows()] == [
        "preaggregations:order_metrics.daily_sales:failed",
        "columnar:duckdb:failed",
    ]
    hooks.refresh_succeeded("order_metrics.daily_sales")
    hooks.sync_succeeded("duckdb")
    assert [a.kind for a in alert_rows()][-2:] == ["resolved", "resolved"]


def test_a_duckdb_sync_failure_is_alerted(settings, monkeypatch):
    from bi_admin.scale.columnar.duckdb import DuckDBBackend

    monkeypatch.setattr(DuckDBBackend, "available", lambda self: False)
    with pytest.raises(RuntimeError):
        DuckDBBackend().sync()
    assert alert_rows("columnar")[0].key == "columnar:duckdb:failed"


def test_staleness_beyond_the_allowance_is_alerted(monkeypatch):
    from bi_admin.scale import state as states

    fresh = states.Freshness(False, NOW, 4000.0)
    monitors._stale("preaggregations", "order_metrics.daily_sales", fresh, 1200, NOW)
    (alert,) = alert_rows("preaggregations")
    assert alert.key == "preaggregations:order_metrics.daily_sales:stale"
    monitors._stale(
        "preaggregations", "order_metrics.daily_sales", states.Freshness(True), 1200, later(60)
    )
    assert alert_rows("preaggregations")[-1].kind == "resolved"
    monitors._stale("columnar", "duckdb", states.Freshness(False, NOW, 100.0), 1200, NOW)
    assert not alert_rows("columnar")  # still within the allowance


def test_nl_errors_alert_above_the_threshold_and_clear(settings):
    configure(settings, NL_ERRORS=3)
    for _i in range(2):
        hooks.nl_provider_error("openai", "provider_unreachable", True)
    assert not alert_rows("nl")
    hooks.nl_provider_error("openai", "provider_unreachable", True)
    (alert,) = alert_rows("nl")
    assert alert.details == {
        "provider": "openai",
        "code": "provider_unreachable",
        "fallback": True,
        "count": 3,
    }
    from django.core.cache import cache

    cache.delete("gozar:alerts:count:nl")
    monitors.check_nl()
    assert alert_rows("nl")[-1].kind == "resolved"


# --- source: system checks, database, disks ------------------------------------------------------


def test_system_checks_raise_one_alert_and_resolve(settings):
    settings.DEBUG = True  # deployment checks complain about DEBUG
    found = monitors.run_system_checks(NOW)
    assert any(m["id"] == "security.W018" for m in found)
    (alert,) = alert_rows("checks")
    assert "security.W018" in alert.message and alert.details["checks"]
    monitors.run_system_checks(later(60))  # unchanged: folded into the alert
    assert len(alert_rows("checks")) == 1

    @checks.register("gozar-test")
    def broken(app_configs=None, **kwargs):
        return [checks.Error("broken for the test", id="gozar_test.E001")]

    try:
        monitors.run_system_checks(later(120))  # changed: resolved and raised again
    finally:
        checks.registry.registry.registered_checks.discard(broken)
    kinds = [(a.kind, a.severity) for a in alert_rows("checks")]
    assert kinds[1:] == [("resolved", "warning"), ("alert", "error")]


def test_database_size_and_growth_thresholds(settings, monkeypatch):
    from bi_admin.system import db

    stats = {
        "total_size": 300 * 2**20,
        "tables": [{"name": "shop_order", "total_size": 100 * 2**20}],
    }
    monkeypatch.setattr(db, "database_stats", lambda: stats)
    configure(
        settings,
        DATABASE_MAX_BYTES=200 * 2**20,
        TABLE_GROWTH_PERCENT=50,
        TABLE_GROWTH_MIN_BYTES=10 * 2**20,
        DISK_USAGE_PERCENT=None,
    )
    monitors.check_database(NOW)
    assert [a.key for a in alert_rows()] == ["database:size"]
    stats["tables"][0]["total_size"] = 200 * 2**20
    monitors.check_database(later(3600))  # not a day yet
    assert len(alert_rows()) == 1
    monitors.check_database(later(86_401))
    assert [a.key for a in alert_rows()][-1] == "database:growth:shop_order"
    stats["total_size"] = 2**20
    monitors.check_database(later(2 * 86_402))
    assert {(a.key, a.kind) for a in alert_rows()} >= {
        ("database:size", "resolved"),
        ("database:growth:shop_order", "resolved"),
    }


def test_disk_usage_above_the_limit(settings, monkeypatch, tmp_path):
    from collections import namedtuple

    Usage = namedtuple("Usage", "total used free")
    monkeypatch.setattr(monitors.shutil, "disk_usage", lambda p: Usage(100, 95, 5))
    configure(settings, DISK_PATHS=[str(tmp_path)], DISK_USAGE_PERCENT=90)
    monitors.check_disks(NOW)
    (alert,) = alert_rows("database")
    assert alert.title.startswith("Disk 95% full") and alert.severity == "warning"
    monkeypatch.setattr(monitors.shutil, "disk_usage", lambda p: Usage(100, 50, 50))
    monitors.check_disks(later(60))
    assert alert_rows("database")[-1].kind == "resolved"


def test_monitors_run_every_check_even_when_one_fails(monkeypatch):
    calls = []
    monkeypatch.setattr(monitors, "check_worker", lambda now: calls.append("worker") or 1 / 0)
    monkeypatch.setattr(monitors, "check_staleness", lambda now: calls.append("stale"))
    monkeypatch.setattr(monitors, "check_nl", lambda now: calls.append("nl"))
    monkeypatch.setattr(monitors, "check_database", lambda now: calls.append("db"))
    monitors.run(NOW)
    assert calls == ["worker", "stale", "nl", "db"]


# --- source: failed logins ---------------------------------------------------------------------


def test_repeated_failed_logins_from_one_ip_are_alerted(client, settings, make_staff):
    configure(settings, FAILED_LOGINS=3)
    make_staff(username="ops")
    client.get("/gozar/api/auth/csrf")
    token = client.cookies["csrftoken"].value
    for _i in range(2):
        resp = client.post(
            "/gozar/api/auth/login",
            {"username": "ops", "password": "wrong-pass-1"},
            "application/json",
            HTTP_X_CSRFTOKEN=token,
        )
        assert resp.status_code == 401
    assert not alert_rows("logins")
    client.post(
        "/gozar/api/auth/login",
        {"username": "nobody", "password": "wrong-pass-2"},
        "application/json",
        HTTP_X_CSRFTOKEN=token,
    )
    (alert,) = alert_rows("logins")
    assert alert.key == "logins:127.0.0.1" and alert.details["count"] == 3
    stored = alert.title + alert.message + str(alert.details)
    assert "wrong-pass" not in stored and "nobody" not in stored and "ops" not in stored


def test_a_non_staff_login_counts_too(client, settings, django_user_model):
    configure(settings, FAILED_LOGINS=1)
    django_user_model.objects.create_user("plain", password="right-pass-1")
    client.get("/gozar/api/auth/csrf")
    resp = client.post(
        "/gozar/api/auth/login",
        {"username": "plain", "password": "right-pass-1"},
        "application/json",
        HTTP_X_CSRFTOKEN=client.cookies["csrftoken"].value,
    )
    assert resp.status_code == 401 and alert_rows("logins")


def test_the_client_ip_header_is_used_only_when_configured(settings, rf):
    request = rf.get("/", REMOTE_ADDR="10.0.0.1", HTTP_X_FORWARDED_FOR="203.0.113.9, 10.0.0.1")
    assert hooks.client_ip(request) == "10.0.0.1"
    configure(settings, CLIENT_IP_HEADER="HTTP_X_FORWARDED_FOR")
    assert hooks.client_ip(request) == "203.0.113.9"


def test_the_login_signal_is_connected(settings, rf):
    configure(settings, FAILED_LOGINS=1)
    user_login_failed.send(sender=None, credentials={"username": "x"}, request=rf.get("/"))
    assert alert_rows("logins")


# --- incoming updates: the allow-list -------------------------------------------------------------


def update(text="/help", *, sender=11, chat=None, chat_type="private", **message):
    body = {
        "message_id": 1,
        "from": {"id": sender, "is_bot": False, "first_name": "T"},
        "chat": {"id": sender if chat is None else chat, "type": chat_type},
        "date": 0,
        "text": text,
        **message,
    }
    return {"update_id": 500, "message": body}


def test_an_allowed_user_in_a_private_chat_gets_an_answer(interactive):
    assert bot.handle(update("/help"), telegram.Client()) == "ok"
    (sent,) = interactive.sent()
    assert sent["chat_id"] == 11 and "/status" in sent["text"]


@pytest.mark.parametrize(
    ("upd", "reason"),
    [
        (update(chat=-100555, chat_type="group"), "not_private"),
        (update(chat=-100555, chat_type="supergroup"), "not_private"),
        (update(chat=22), "chat_mismatch"),  # allowed sender, another (allowed) chat
        (update(forward_origin={"type": "user", "sender_user": {"id": 99}}), "forwarded"),
        (update(forward_from={"id": 11}), "forwarded"),
        (update(sender_chat={"id": -100777, "type": "channel"}), "on_behalf"),
        (update(via_bot={"id": 5, "is_bot": True}), "on_behalf"),
        (update(text=None), "no_text"),
        ({"update_id": 1, "edited_message": update()["message"]}, "not_a_message"),
    ],
)
def test_allowed_senders_outside_a_plain_private_message_are_ignored(interactive, upd, reason):
    assert bot.handle(upd, telegram.Client()) == reason
    assert not interactive.sent() and not Alert.objects.exists()


@pytest.mark.parametrize(
    "upd",
    [
        update(sender=STRANGER),
        update(sender=STRANGER, chat=11),  # spoofed chat id: an allowed chat, a stranger sender
        update(sender=STRANGER, chat=-100555, chat_type="group"),
        {
            "update_id": 2,
            "my_chat_member": {
                "from": {"id": STRANGER, "is_bot": False},
                "chat": {"id": -100555, "type": "group"},
            },
        },
        {
            "update_id": 3,
            "channel_post": {"chat": {"id": -100777, "type": "channel"}, "text": "/status"},
        },
        update(sender=True),  # not an integer id
    ],
)
def test_anyone_else_is_ignored_silently_and_alerted(interactive, upd):
    assert bot.handle(upd, telegram.Client()) == "unknown_sender"
    assert not interactive.sent()  # no reply to them
    (alert,) = alert_rows("telegram")
    assert alert.severity == "warning" and "allow-list" in alert.title
    assert "/status" not in alert.message  # their text isn't repeated


def test_a_bot_sender_is_refused(interactive):
    upd = update()
    upd["message"]["from"]["is_bot"] = True
    assert bot.handle(upd, telegram.Client()) == "bot_sender" and not interactive.sent()


def test_unauthorised_attempts_are_logged_at_most_once_a_minute(interactive, caplog, monkeypatch):
    monkeypatch.setattr(bot, "_log_state", {"at": 0.0, "count": 0})
    with caplog.at_level(logging.WARNING, logger="bi_admin.alerts.telegram"):
        for _i in range(5):
            bot.handle(update(sender=STRANGER), telegram.Client())
    assert caplog.text.count("Ignored") == 1
    (alert,) = alert_rows("telegram")
    assert alert.repeats == 4  # folded, one message to the allowed users


def test_the_bot_answers_only_operational_status(interactive, seeded, admin_user):
    from shop.models import City, Customer

    pipeline.raise_alert("jobs", "k", "error", "Boom")
    client = telegram.Client()
    for command in ("/status", "/alerts", "/help", "/status@gozar_test_bot", "/nonsense"):
        bot.handle(update(command), client)
    texts = "\n".join(p["text"] for p in interactive.sent())
    assert "Gozar status" in texts and "Latest alerts" in texts and "Boom" in texts
    assert "Unknown command" in texts
    for value in [
        *City.objects.values_list("name_en", flat=True)[:5],
        *Customer.objects.values_list("name", flat=True)[:5],
    ]:
        assert value not in texts


def test_ack_and_mute_commands(interactive):
    alert = pipeline.raise_alert("jobs", "k", "error", "Boom")
    client = telegram.Client()
    bot.handle(update(f"/ack {alert.pk}"), client)
    alert.refresh_from_db()
    assert alert.acknowledged_at and alert.acknowledged_via == "telegram:11"
    bot.handle(update(f"/ack {alert.pk}"), client)
    bot.handle(update("/ack 999999"), client)
    bot.handle(update("/mute jobs:k 2h"), client)
    state = AlertState.objects.get(key="jobs:k")
    assert state.muted_until and state.muted_until > timezone.now() + dt.timedelta(minutes=119)
    bot.handle(update("/mute jobs:k 30d"), client)  # capped at MAX_MUTE (7 days)
    state.refresh_from_db()
    assert state.muted_until < timezone.now() + dt.timedelta(days=7, minutes=1)
    bot.handle(update("/mute jobs:k soon"), client)
    bot.handle(update("/mute unknown:key 1h"), client)
    bot.handle(update("/unmute jobs:k"), client)
    state.refresh_from_db()
    assert state.muted_until <= timezone.now()
    replies = [p["text"] for p in interactive.sent()]
    assert "acknowledged." in replies[0] and "already acknowledged" in replies[1]
    assert "No alert" in replies[2] and "muted until" in replies[3]
    assert "Durations look like" in replies[5] and "Unknown alert key" in replies[6]
    assert "no longer muted" in replies[7]


def test_polling_keeps_the_offset_and_survives_bad_updates(interactive):
    interactive.updates = [
        [
            update("/help") | {"update_id": 7},
            {"update_id": 8, "message": "garbage"},
            update(sender=STRANGER) | {"update_id": 9},
        ],
        [],
    ]
    client = bot.require_interactive()
    assert bot.poll_once(client, timeout=0) == 3
    assert bot._offset() == 10
    bot.poll_once(client, timeout=0)
    offsets = [p.get("offset") for m, p in interactive.calls if m == "getUpdates"]
    assert offsets == [None, 10]
    assert len(interactive.sent()) == 1  # only the allowed user got an answer


def test_polling_refuses_to_start_unless_interactive_and_configured(channel, settings):
    with pytest.raises(RuntimeError, match="Interactive mode is off"):
        bot.require_interactive()
    with pytest.raises(CommandError, match="Interactive mode is off"):
        call_command("gozar_telegram_poll", once=True)
    configure(settings, {"INTERACTIVE": True, "ALLOWED_CHAT_IDS": ""})
    with pytest.raises(CommandError, match="No allowed chat ids"):
        call_command("gozar_telegram_poll", once=True)
    assert bot.start_thread() is None
    assert not channel.calls


def test_the_poll_command_runs_once(interactive):
    interactive.updates = [[update("/status")]]
    call_command("gozar_telegram_poll", once=True, stdout=StringIO())
    assert interactive.sent()[0]["chat_id"] == 11


# --- management commands ----------------------------------------------------------------------


def test_gozar_alert_test_sends_at_once(channel):
    out = StringIO()
    call_command("gozar_alert_test", message="Hello from the test", stdout=out)
    assert "Sent 2" in out.getvalue()
    assert all("Hello from the test" in p["text"] for p in channel.sent())
    assert alert_rows("test")[0].delivery == "sent"


def test_gozar_alert_test_without_a_channel_only_records():
    out = StringIO()
    call_command("gozar_alert_test", stdout=out)
    assert "nothing was sent" in out.getvalue() and alert_rows("test")


def test_gozar_telegram_whoami(channel):
    out = StringIO()
    call_command("gozar_telegram_whoami", stdout=out)
    assert "@gozar_test_bot" in out.getvalue() and TOKEN not in out.getvalue()


def test_gozar_telegram_whoami_without_a_token():
    with pytest.raises(CommandError, match="GOZAR_TELEGRAM_BOT_TOKEN"):
        call_command("gozar_telegram_whoami")


def test_gozar_telegram_whoami_reports_a_rejected_token(channel):
    channel.responses = [(401, {"ok": False, "error_code": 401, "description": "Unauthorized"})]
    with pytest.raises(CommandError, match="Unauthorized") as exc:
        call_command("gozar_telegram_whoami")
    assert TOKEN not in str(exc.value)


def test_gozar_alerts_watchdog_checks_and_delivers(channel):
    monitors.beat("host:1", started=True, now=timezone.now() - dt.timedelta(hours=1))
    out = StringIO()
    call_command("gozar_alerts", stdout=out)
    assert alert_rows("worker") and "2 sent" in out.getvalue()


# --- System page API ----------------------------------------------------------------------------


@pytest.fixture
def sysadmin(client, make_staff):
    client.force_login(make_staff("bi_admin.view_system", username="ops"))
    return client


@pytest.mark.parametrize(
    ("method", "url"),
    [
        ("get", "alerts"),
        ("get", "alerts/status"),
        ("post", "alerts/test"),
        ("post", "alerts/1/ack"),
        ("patch", "alerts/sources/jobs"),
    ],
)
def test_alerts_api_needs_the_system_permission(client, make_staff, method, url):
    def call():
        return getattr(client, method)(f"{API}/{url}", {}, content_type="application/json")

    assert call().status_code == 401
    client.force_login(make_staff("shop.view_order", "bi_admin.view_reportlog"))
    resp = call()
    assert resp.status_code == 403 and resp.json()["code"] == "permission_denied"


def test_alerts_api_lists_and_filters(sysadmin, channel):
    pipeline.raise_alert("jobs", "a", "error", "Job failed")
    pipeline.raise_alert("exceptions", "b", "critical", "Unhandled KeyError")
    pipeline.resolve("jobs", "a")
    data = sysadmin.get(f"{API}/alerts").json()
    assert data["count"] == 3 and data["entries"][0]["kind"] == "resolved"
    assert sysadmin.get(f"{API}/alerts", {"severity": "critical"}).json()["count"] == 1
    assert sysadmin.get(f"{API}/alerts", {"source": "jobs"}).json()["count"] == 2
    assert sysadmin.get(f"{API}/alerts", {"kind": "resolved"}).json()["count"] == 1
    assert sysadmin.get(f"{API}/alerts", {"q": "KeyError"}).json()["count"] == 1
    assert sysadmin.get(f"{API}/alerts", {"delivery": "pending"}).json()["count"] == 3
    entry = sysadmin.get(f"{API}/alerts", {"source": "exceptions"}).json()["entries"][0]
    assert [d["channel"] for d in entry["deliveries"]] == ["telegram", "telegram"]
    assert "target" not in entry["deliveries"][0]  # the chat ids stay on the server
    assert sysadmin.get(f"{API}/alerts", {"page": 9}).status_code == 404


def test_alerts_api_acknowledges(sysadmin, channel):
    alert = pipeline.raise_alert("jobs", "a", "error", "Job failed")
    data = sysadmin.post(f"{API}/alerts/{alert.pk}/ack").json()
    assert data["acknowledged_by"] == "ops" and data["acknowledged_via"] == "web"
    assert sysadmin.get(f"{API}/alerts", {"acknowledged": "true"}).json()["count"] == 1
    assert sysadmin.get(f"{API}/alerts", {"acknowledged": "false"}).json()["count"] == 0
    assert sysadmin.post(f"{API}/alerts/999999/ack").status_code == 404


def test_alerts_api_sends_a_test_alert_through_the_outbox(sysadmin, channel):
    resp = sysadmin.post(f"{API}/alerts/test")
    assert resp.status_code == 202 and resp.json()["alert"]["delivery"] == "pending"
    assert not channel.calls  # the request only queued it
    outbox.deliver_due()
    assert len(channel.sent()) == 2 and "(ops)" in channel.sent()[0]["text"]


def test_alerts_api_source_switches(sysadmin, settings):
    resp = sysadmin.patch(
        f"{API}/alerts/sources/jobs",
        {"enabled": False, "min_severity": "error"},
        content_type="application/json",
    )
    jobs_source = next(s for s in resp.json()["sources"] if s["name"] == "jobs")
    assert (jobs_source["enabled"], jobs_source["min_severity"], jobs_source["overridden"]) == (
        False,
        "error",
        True,
    )
    assert pipeline.raise_alert("jobs", "k", "error", "x") is None
    resp = sysadmin.patch(
        f"{API}/alerts/sources/jobs",
        {"enabled": None, "min_severity": None},
        content_type="application/json",
    )
    jobs_source = next(s for s in resp.json()["sources"] if s["name"] == "jobs")
    assert (jobs_source["enabled"], jobs_source["overridden"]) == (True, False)
    assert (
        sysadmin.patch(
            f"{API}/alerts/sources/nope", {}, content_type="application/json"
        ).status_code
        == 404
    )
    assert (
        sysadmin.patch(
            f"{API}/alerts/sources/jobs", {"min_severity": "loud"}, content_type="application/json"
        ).status_code
        == 400
    )
    assert AlertSource.objects.get(source="jobs").updated_by.username == "ops"


def test_alerts_api_test_button_when_test_alerts_are_off(sysadmin, settings):
    configure(settings, SOURCES={"test": {"enabled": False}})
    assert sysadmin.post(f"{API}/alerts/test").status_code == 409


def test_status_shows_configuration_but_never_the_token_or_ids(sysadmin, channel, admin_client):
    data = sysadmin.get(f"{API}/alerts/status").json()
    (tg,) = data["channels"]
    assert tg["enabled"] and tg["token_configured"] and tg["allowed_count"] == 2
    assert tg["token_env"] == "GOZAR_TELEGRAM_BOT_TOKEN" and tg["problems"] == []
    pipeline.raise_alert("jobs", "k", "error", "x")
    outbox.deliver_due()
    everything = (
        "".join(
            sysadmin.get(f"{API}/{u}").content.decode()
            for u in ("alerts/status", "alerts", "overview", "checks")
        )
        + admin_client.get("/gozar/api/auth/me").content.decode()
    )
    assert TOKEN not in everything and "FAKE-test-token" not in everything
    assert '"11"' not in everything and '"22"' not in everything


def test_status_warns_when_the_channel_cannot_start(sysadmin, settings, monkeypatch):
    monkeypatch.setenv("GOZAR_TELEGRAM_BOT_TOKEN", TOKEN)
    (tg,) = sysadmin.get(f"{API}/alerts/status").json()["channels"]
    assert not tg["enabled"] and tg["problems"][0]["code"] == "no_allowed_ids"


def test_alerts_are_never_written_to_another_database(channel):
    pipeline.raise_alert("jobs", "k", "error", "x")
    assert Alert.objects.using(DEFAULT_DB_ALIAS).count() == 1


@override_settings(LANGUAGE_CODE="fa")
def test_persian_api_labels(sysadmin, channel):
    pipeline.raise_alert("jobs", "k", "error", "x")
    entry = sysadmin.get(f"{API}/alerts", HTTP_ACCEPT_LANGUAGE="fa").json()["entries"][0]
    assert entry["severity_display"]
