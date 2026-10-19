"""M4: the report result cache — keys, hits, bypass and data-version invalidation."""

import pytest
from django.contrib.auth.models import Group
from django.db.models import Count

from bi_admin.engine import run
from bi_admin.execution import cache
from bi_admin.semantic import Dimension, Measure, Semantic, semantic_site
from shop.models import City, Order, Product

pytestmark = pytest.mark.django_db

API = "/gozar/api"
SPEC = {"semantic_model": "order_metrics", "measures": ["orders", "revenue"]}
BY_CITY = {**SPEC, "dimensions": ["city"]}


@pytest.fixture
def go(admin_client, orders, report_api):
    def call(spec=SPEC, **params):
        resp = report_api(admin_client, "run", spec, **params)
        assert resp.status_code == 200, resp.content
        return resp.json()

    return call


def test_second_run_is_a_hit(go):
    first = go()
    assert first["cache"] == {
        "enabled": True,
        "hit": False,
        "stored_at": first["cache"]["stored_at"],
    }
    assert first["cache"]["stored_at"]
    second = go()
    assert second["cache"]["hit"] is True
    assert second["cache"]["stored_at"] == first["cache"]["stored_at"]
    assert second["rows"] == first["rows"] and second["columns"] == first["columns"]


def test_bypass_runs_again_and_refreshes(go):
    go()
    Order.objects.filter(status="paid").update(amount=1)  # no signal: the cache can't know
    assert go()["cache"]["hit"] is True
    fresh = go(cache="false")
    assert fresh["cache"]["hit"] is False
    assert fresh["rows"][0]["revenue"] == Order.objects.filter(status="paid").count()
    assert go()["rows"] == fresh["rows"]  # the bypass stored the fresh result


def test_chart_changes_share_the_entry(go):
    go(BY_CITY)
    assert go({**BY_CITY, "chart": "bar"})["cache"]["hit"] is True


def test_different_specs_and_languages_do_not_share(go, admin_client):
    go(BY_CITY)
    assert go({**BY_CITY, "limit": 2})["cache"]["hit"] is False
    admin_client.patch(f"{API}/preferences", {"language": "fa"}, "application/json")
    fa = go(BY_CITY)
    assert fa["cache"]["hit"] is False and fa["columns"][0]["label"] == "شهر"


def test_saving_the_model_invalidates(go, orders):
    go()
    order = orders[2]
    order.status = "paid"
    order.save()
    after = go()
    assert after["cache"]["hit"] is False and after["rows"][0]["revenue"] == 2700 + 50


def test_deleting_invalidates(go, orders):
    go()
    orders[0].delete()
    assert go()["rows"][0]["orders"] == 8


def test_changes_to_joined_models_invalidate(go):
    before = go(BY_CITY)
    city = City.objects.get(name_en="Shiraz")
    city.name = "Shiraz!"
    city.save()
    after = go(BY_CITY)
    assert after["cache"]["hit"] is False
    assert "Shiraz!" in {r["city"] for r in after["rows"]} - {r["city"] for r in before["rows"]}


def test_unrelated_models_do_not_invalidate(go):
    go()
    Product.objects.create(name="Pen", category="books", price=1)
    assert go()["cache"]["hit"] is True


def test_crud_writes_and_actions_invalidate(go, admin_client, orders):
    go()
    url = f"{API}/crud/shop/order"
    admin_client.patch(f"{url}/{orders[2].pk}", {"amount": 55}, "application/json")
    assert go()["cache"]["hit"] is False
    go()
    # the action uses QuerySet.update(), which sends no signals; Gozar bumps itself
    admin_client.post(f"{url}/actions/mark_cancelled", {"pks": [orders[2].pk]}, "application/json")
    assert go()["cache"]["hit"] is False


def test_m2m_changes_invalidate(admin_client, admin_user, report_api, db):
    class GroupStats(Semantic):
        model = Group
        dimensions = {"name": Dimension("name")}
        measures = {"members": Measure(Count("user"))}

    semantic_site.register(GroupStats)
    try:
        group = Group.objects.create(name="g")
        spec = {"semantic_model": "group_stats", "measures": ["members"]}
        assert report_api(admin_client, "run", spec).json()["rows"] == [{"members": 0}]
        admin_user.groups.add(group)
        after = report_api(admin_client, "run", spec).json()
        assert after["cache"]["hit"] is False and after["rows"] == [{"members": 1}]
    finally:
        semantic_site.unregister("group_stats")


def test_versions_are_bumped_again_on_commit(orders, django_capture_on_commit_callbacks):
    before = cache.versions(["shop.order"])["shop.order"]
    with django_capture_on_commit_callbacks(execute=False) as callbacks:
        orders[0].save()
    during = cache.versions(["shop.order"])["shop.order"]
    assert during != before  # visible to the writer's own transaction at once
    for callback in callbacks:
        callback()
    assert cache.versions(["shop.order"])["shop.order"] != during


def test_an_evicted_version_never_resurrects_old_results(go):
    go()
    cache.backend().delete(cache.VERSION_PREFIX + "shop.order")
    assert go()["cache"]["hit"] is False


def test_bump_all_and_bump(go):
    go()
    cache.bump_all()
    assert go()["cache"]["hit"] is False
    go()
    cache.bump("shop.order")
    assert go()["cache"]["hit"] is False


def test_dependencies_come_from_the_sql(orders):
    from bi_admin.engine.compiler import compile_report
    from bi_admin.engine.validator import validate

    report = validate({**BY_CITY, "time_grain": "month", "calendar": "jalali"})
    sql = str(compile_report(report).queryset.query)
    deps = cache.dependencies(sql, "default", [])
    assert {"shop.order", "shop.customer", "shop.city", "bi_admin.calendarday"} <= set(deps)
    assert "shop.product" not in deps


def test_timeout_zero_disables_the_cache(go, settings):
    settings.GOZAR = {**settings.GOZAR, "REPORT_CACHE_TIMEOUT": 0}
    go()
    assert go()["cache"] == {"enabled": False, "hit": False, "stored_at": None}


def test_cache_only_and_direct_runs(orders):
    assert run(SPEC, cache_only=True) is None
    run(SPEC)
    hit = run(SPEC, cache_only=True)
    assert hit is not None and hit.cache["hit"] is True


def test_export_uses_and_bypasses_the_cache(admin_client, orders, report_api):
    from bi_admin.models import ReportLog

    report_api(admin_client, "run", SPEC)
    report_api(admin_client, "export", SPEC, format="csv")
    report_api(admin_client, "export", SPEC, format="csv", cache="false")
    assert [log.cache_hit for log in ReportLog.objects.order_by("id")] == [False, True, False]


def test_calendar_population_invalidates(admin_client, orders, report_api):
    spec = {**SPEC, "time_grain": "month", "calendar": "jalali"}
    assert report_api(admin_client, "run", spec).status_code == 409  # errors aren't cached
    import datetime as dt

    from bi_admin.calendar import table

    table.populate(dt.date(2025, 1, 1), dt.date(2026, 12, 31))
    assert report_api(admin_client, "run", spec).status_code == 200


def test_scope_keys_tell_apart_what_scope_texts_cannot(db):
    from django.db.models import Q

    from bi_admin.access.rules import scope_key, scope_text

    one = City.objects.filter(Q(name__in=["a, b"]))
    two = City.objects.filter(Q(name__in=["a", "b"]))
    assert scope_key(one) != scope_key(two)
    assert scope_key(City.objects.filter(pk__in=[])) == "FALSE"
    assert scope_text(City.objects.all()) == ""
    assert scope_key(City.objects.all()) == scope_key(City.objects.all()) != scope_key(two)
