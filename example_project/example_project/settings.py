"""Demo project settings. Not for production use."""

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "dev-only-insecure-key")
DEBUG = os.environ.get("DJANGO_DEBUG", "1") == "1"
ALLOWED_HOSTS = ["*"] if DEBUG else []

INSTALLED_APPS = [
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "bi_admin",
    "shop",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    # Activates each user's Gozar language (browser language before login);
    # it stands in for django.middleware.locale.LocaleMiddleware.
    "bi_admin.middleware.PreferencesMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
]

ROOT_URLCONF = "example_project.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "example_project.wsgi.application"

# SQLite by default; set GOZAR_DB=postgres (plus POSTGRES_* vars) for PostgreSQL.
# "replica" stands in for a read replica: the same database in development (so
# GOZAR_REPORTS_DATABASE=replica works out of the box), a separate test database
# in tests, where the routing tests use it.
if os.environ.get("GOZAR_DB") == "postgres":
    _postgres = {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": os.environ.get("POSTGRES_DB", "gozar"),
        "USER": os.environ.get("POSTGRES_USER", "gozar"),
        "PASSWORD": os.environ.get("POSTGRES_PASSWORD", "gozar"),
        "HOST": os.environ.get("POSTGRES_HOST", "localhost"),
        "PORT": os.environ.get("POSTGRES_PORT", "5432"),
    }
    DATABASES = {
        "default": _postgres,
        "replica": {**_postgres, "TEST": {"NAME": "test_gozar_replica"}},
    }
else:
    # GOZAR_SQLITE_PATH points the demo at another file (`gozar_benchmark` uses it so
    # its large data set never lands in db.sqlite3).
    _sqlite = os.environ.get("GOZAR_SQLITE_PATH") or BASE_DIR / "db.sqlite3"
    DATABASES = {
        "default": {"ENGINE": "django.db.backends.sqlite3", "NAME": _sqlite},
        "replica": {"ENGINE": "django.db.backends.sqlite3", "NAME": _sqlite},
    }

# Reads made while a report runs go to GOZAR["REPORTS_DATABASE"] (when set).
DATABASE_ROUTERS = ["bi_admin.execution.router.ReportsRouter"]

# The report result cache must be shared by every process (web workers and
# `manage.py gozar_worker`), so not the per-process LocMemCache. Production:
# Redis or Memcached. Tests switch to LocMemCache (tests/conftest.py).
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.filebased.FileBasedCache",
        "LOCATION": os.environ.get("GOZAR_CACHE_DIR") or BASE_DIR / ".cache",
    }
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# Gozar settings (all optional; see bi_admin/conf.py for every key and default).
# VITE_DEV_SERVER serves the SPA from `npm run dev`.
GOZAR = {
    "VITE_DEV_SERVER": os.environ.get("GOZAR_VITE_DEV_SERVER") or None,
    "DEFAULT_LANGUAGE": os.environ.get("GOZAR_DEFAULT_LANGUAGE", "en"),
    "DEFAULT_CALENDAR": os.environ.get("GOZAR_DEFAULT_CALENDAR", "gregorian"),
    # M4: access control & execution
    "ROW_RULES_BYPASS_SUPERUSER": True,
    "REPORTS_DATABASE": os.environ.get("GOZAR_REPORTS_DATABASE") or None,
    "REPORT_STATEMENT_TIMEOUT_MS": 30_000,
    "REPORT_CACHE_TIMEOUT": 300,
    # "1": every report runs as a background job (start `manage.py gozar_worker`)
    "REPORT_ASYNC": os.environ.get("GOZAR_REPORT_ASYNC") == "1",
    # M6: scheduled e-mails link back to the app
    "BASE_URL": os.environ.get("GOZAR_BASE_URL", "http://localhost:8000/gozar"),
    # M7: OrderMetrics declares pre-aggregations (shop/gozar.py); the worker keeps them
    # fresh, and a stale one steps aside for the ORM (PREAGG_MAX_STALENESS = 0).
    # GOZAR_DUCKDB=1 answers the other OrderMetrics reports from a DuckDB copy
    # (pip install 'gozar[duckdb]'; `manage.py gozar_columnar_sync` or the worker).
    "PREAGG_REFRESH_EVERY": 60,
    "SEMANTIC_ENGINES": {"order_metrics": "duckdb"}
    if os.environ.get("GOZAR_DUCKDB") == "1"
    else {},
    "DUCKDB": {
        "PATH": os.environ.get("GOZAR_DUCKDB_PATH") or BASE_DIR / ".duckdb" / "gozar.duckdb",
        "SYNC_EVERY": 60,
    },
    # Alerts: always recorded (System page); sent to Telegram once both variables
    # below are exported, off otherwise. Never put the token or real ids in this file:
    #   export GOZAR_TELEGRAM_BOT_TOKEN=…  (read only from the environment)
    #   export GOZAR_TELEGRAM_ALLOWED_CHAT_IDS=…  (numeric ids, comma separated)
    "ALERTS": {
        "LANGUAGE": os.environ.get("GOZAR_ALERTS_LANGUAGE", "en"),
        "QUIET_HOURS": os.environ.get("GOZAR_ALERTS_QUIET_HOURS") or None,  # "23:00-07:00"
        "TELEGRAM": {
            "ALLOWED_CHAT_IDS": os.environ.get("GOZAR_TELEGRAM_ALLOWED_CHAT_IDS", ""),
            # "1": also answer /status, /alerts, /ack, /mute and /help (long polling)
            "INTERACTIVE": os.environ.get("GOZAR_TELEGRAM_INTERACTIVE") == "1",
        },
    },
}

# Scheduled e-mail reports (`manage.py gozar_send_schedules`, or `gozar_worker`) are
# written to example_project/.mail as files — no SMTP server needed. Use
# GOZAR_EMAIL_BACKEND=django.core.mail.backends.console.EmailBackend to print them.
EMAIL_BACKEND = os.environ.get(
    "GOZAR_EMAIL_BACKEND", "django.core.mail.backends.filebased.EmailBackend"
)
EMAIL_FILE_PATH = os.environ.get("GOZAR_EMAIL_DIR") or BASE_DIR / ".mail"
DEFAULT_FROM_EMAIL = "Gozar <gozar@example.com>"

LANGUAGE_CODE = "en"
LANGUAGES = [("en", "English"), ("fa", "فارسی")]
TIME_ZONE = "Asia/Tehran"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"

# Uploads (FileField/ImageField) go through Django's storage API: the default
# FileSystemStorage here, any configured STORAGES["default"] backend elsewhere.
MEDIA_URL = "/media/"
MEDIA_ROOT = os.environ.get("GOZAR_MEDIA_ROOT") or BASE_DIR / "media"
