"""M2: report API — catalog, validate, run; staff auth and model view permissions."""

import pytest
from django.test import Client

API = "/gozar/api/reports"
SPEC = {"semantic_model": "order_metrics", "measures": ["revenue", "orders"]}

pytestmark = pytest.mark.django_db


def post(client, path, body):
    return client.post(f"{API}/{path}", body, "application/json")


def test_endpoints_require_a_staff_session(client, django_user_model):
    assert client.get(f"{API}/catalog").status_code == 401
    assert post(client, "run", SPEC).status_code == 401
    assert post(client, "validate", SPEC).status_code == 401
    client.force_login(django_user_model.objects.create_user("joe", password="pw-123456!"))
    assert client.get(f"{API}/catalog").status_code == 401
    assert post(client, "run", SPEC).status_code == 401


def test_run_requires_csrf_for_session_auth(admin_user):
    c = Client(enforce_csrf_checks=True)
    c.force_login(admin_user)
    assert post(c, "run", SPEC).status_code in (401, 403)


def test_staff_without_view_permission_is_forbidden(client, make_staff, orders):
    client.force_login(make_staff("shop.view_customer"))
    assert client.get(f"{API}/catalog").json() == {"semantic_models": []}
    resp = post(client, "run", SPEC)
    assert resp.status_code == 403
    assert resp.json()["code"] == "permission_denied"
    assert post(client, "validate", SPEC).status_code == 403


@pytest.mark.parametrize("perm", ["shop.view_order", "shop.change_order"])
def test_view_or_change_permission_allows_reports(client, make_staff, orders, perm):
    client.force_login(make_staff(perm))
    resp = post(client, "run", {**SPEC, "dimensions": ["city"]})
    assert resp.status_code == 200, resp.content
    body = resp.json()
    assert [(r["city"], r["revenue"], r["orders"]) for r in body["rows"]] == [
        ("تهران", 1800, 5),
        ("مشهد", 600, 2),
        ("شیراز", 300, 2),
    ]
    assert body["row_count"] == 3 and body["truncated"] is False
    assert [c["key"] for c in body["columns"]] == ["city", "revenue", "orders"]
    assert body["columns"][1] == {
        "key": "revenue",
        "role": "measure",
        "format": "currency",
        "label": {"fa": "درآمد", "en": "Revenue"},
    }
    assert body["spec"]["semantic_model"] == "order_metrics"


def test_catalog_endpoint(admin_client):
    body = admin_client.get(f"{API}/catalog", {"lang": "en"}).json()
    (entry,) = body["semantic_models"]
    assert entry["name"] == "order_metrics" and entry["label"] == "Orders"
    assert {"revenue", "orders", "aov"} <= {m["key"] for m in entry["measures"]}
    both = admin_client.get(f"{API}/catalog").json()["semantic_models"][0]
    assert both["label"] == {"fa": "سفارش‌ها", "en": "Orders"}


def test_invalid_spec_returns_structured_errors(admin_client, orders):
    resp = post(admin_client, "run", {**SPEC, "measures": ["فروش"], "dimensions": ["town"]})
    assert resp.status_code == 422
    errors = resp.json()["errors"]
    assert [(e["code"], e["path"], e["suggestions"]) for e in errors] == [
        ("unknown_measure", "measures[0]", ["revenue"]),
        ("unknown_dimension", "dimensions[0]", ["city"]),
    ]
    assert errors[0]["value"] == "فروش" and errors[0]["message"]


def test_schema_errors_are_rejected_by_pydantic(admin_client):
    assert post(admin_client, "run", {**SPEC, "time_grain": "hour"}).status_code == 422
    assert post(admin_client, "run", {**SPEC, "sql": "select 1"}).status_code == 422
    assert post(admin_client, "run", {"measures": ["revenue"]}).status_code == 422


def test_unknown_model_suggests_only_permitted_models(client, make_staff):
    client.force_login(make_staff("shop.view_customer"))
    resp = post(client, "run", {**SPEC, "semantic_model": "orders"})
    assert resp.status_code == 422
    assert resp.json()["errors"][0]["suggestions"] == []  # order_metrics is not revealed


def test_validate_endpoint(admin_client, frozen_today):
    ok = post(admin_client, "validate", {**SPEC, "relative_period": "last_season"}).json()
    assert ok["valid"] is True and ok["errors"] == []
    assert ok["period"]["start_jalali"] == "1405-04-01"
    assert ok["period"]["end_jalali"] == "1405-06-31"
    bad = post(admin_client, "validate", {**SPEC, "relative_period": "someday"}).json()
    assert bad["valid"] is False
    assert bad["errors"][0]["code"] == "unknown_relative_period"
    assert "last_3_months" in bad["errors"][0]["suggestions"]


def test_run_with_frozen_today_and_jalali_grain(admin_client, orders, calendar_table, frozen_today):
    resp = post(
        admin_client,
        "run",
        {**SPEC, "time_grain": "month", "relative_period": "last_3_months", "chart": "line"},
    )
    assert resp.status_code == 200, resp.content
    body = resp.json()
    assert body["chart"] == "line" and body["time_grain"] == "month"
    assert body["period"]["start"] == "2026-06-22" and body["period"]["end"] == "2026-09-22"
    assert [(r["created_at__label"], r["revenue"], r["orders"]) for r in body["rows"]] == [
        ("1405-05", None, 1),
        ("1405-06", 100, 2),
    ]


def test_missing_calendar_is_a_409(admin_client, orders):
    resp = post(admin_client, "run", {**SPEC, "time_grain": "season"})
    assert resp.status_code == 409
    assert resp.json()["code"] == "calendar_not_populated"


def test_custom_permission_hook(client, make_staff, orders, monkeypatch):
    from bi_admin.semantic import semantic_site

    client.force_login(make_staff("shop.view_order"))
    sem = semantic_site.get("order_metrics")
    monkeypatch.setattr(sem, "has_permission", lambda request: request.user.is_superuser)
    assert post(client, "run", SPEC).status_code == 403
