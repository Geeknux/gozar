"""M4: semantic visibility — measures and dimensions a user may not see behave like
unknown keys everywhere: catalog, validator, run, drill-down, export, values, jobs."""

import pytest
from django.db.models import Count, Sum
from django.test import Client

from bi_admin.engine import SpecValidationError, validate
from bi_admin.semantic import Dimension, Measure, Semantic, semantic_site
from shop.models import Order

pytestmark = pytest.mark.django_db

API = "/gozar/api/reports"
SPEC = {"semantic_model": "order_metrics", "measures": ["orders"]}


@pytest.fixture
def viewer(make_staff, orders):
    """May view orders, but not change them: the finance-only Refunds is hidden."""
    client = Client()
    client.force_login(make_staff("shop.view_order"))
    return client


@pytest.fixture
def finance(make_staff, orders):
    client = Client()
    client.force_login(make_staff("shop.change_order", username="finance"))
    return client


@pytest.fixture
def scratch():
    yield semantic_site.register
    for name in semantic_site.names():
        if name != "order_metrics":
            semantic_site.unregister(name)


def measures(client):
    (entry,) = client.get(f"{API}/catalog").json()["semantic_models"]
    return [m["key"] for m in entry["measures"]]


def test_catalog_hides_what_the_user_may_not_see(viewer, finance, admin_client):
    assert "refunds" in measures(finance)
    assert "refunds" in measures(admin_client)  # superusers have every permission


def test_catalog_for_a_viewer(viewer):
    assert "refunds" not in measures(viewer)


def test_hidden_measure_is_rejected_like_an_unknown_one(viewer, report_api):
    hidden = report_api(viewer, "run", {**SPEC, "measures": ["refunds"]})
    unknown = report_api(viewer, "run", {**SPEC, "measures": ["refundz"]})
    assert hidden.status_code == unknown.status_code == 422
    (h,) = hidden.json()["errors"]
    (u,) = unknown.json()["errors"]
    assert (h["code"], h["path"], h["message"]) == (
        "unknown_measure",
        "measures[0]",
        "Unknown measure 'refunds' on order_metrics",
    )
    assert u["code"] == "unknown_measure"
    assert "refunds" not in h["suggestions"] and "refunds" not in u["suggestions"]
    # its synonyms don't suggest it either
    syn = report_api(viewer, "validate", {**SPEC, "measures": ["refunded amount"]}).json()
    assert syn["valid"] is False and "refunds" not in syn["errors"][0]["suggestions"]


def test_hidden_measure_is_rejected_by_validate_and_sort(viewer, finance, report_api):
    body = report_api(viewer, "validate", {**SPEC, "measures": ["refunds"]}).json()
    assert body["valid"] is False and body["errors"][0]["code"] == "unknown_measure"
    sort = {**SPEC, "sort": [{"field": "refunds", "direction": "desc"}]}
    err = report_api(viewer, "validate", sort).json()["errors"][0]
    assert err["code"] == "unknown_field"  # not "sort_field_not_in_report"
    finance_err = report_api(finance, "validate", sort).json()["errors"][0]
    assert finance_err["code"] == "sort_field_not_in_report"


def test_finance_may_run_the_measure(finance, report_api):
    body = report_api(finance, "run", {**SPEC, "measures": ["refunds"]}).json()
    assert body["rows"] == [{"refunds": 400}]


def test_drilldown_and_export_reject_hidden_measures(viewer, report_api):
    spec = {**SPEC, "measures": ["orders", "refunds"]}
    drill = report_api(viewer, "drilldown", {"spec": spec, "cell": {}, "measure": "refunds"})
    assert drill.status_code == 422
    assert drill.json()["errors"][0]["code"] == "unknown_measure"
    export = report_api(viewer, "export", spec, format="csv")
    assert export.status_code == 422
    jobs = report_api(viewer, "jobs", spec)
    assert jobs.status_code == 422


@pytest.fixture
def secret_semantic(scratch):
    """``amount`` only appears through items limited to superusers/staff named boss."""

    @scratch
    class Secret(Semantic):
        model = Order
        dimensions = {
            "status": Dimension("status"),
            "city": Dimension("customer__city__name"),
            "amount": Dimension("amount", permission=lambda user: user.username == "boss"),
            "paid_at": Dimension("paid_at", permission="shop.change_order"),
            "broken": Dimension("customer__name", permission=lambda user: 1 / 0),
        }
        time_dimension = "paid_at"
        measures = {
            "n": Measure(Count("id")),
            "total": Measure(Sum("amount"), permission=["shop.view_order", "shop.change_order"]),
        }

    return "secret"


def test_permission_forms_and_failing_predicates(viewer, secret_semantic, report_api):
    entry = next(
        m for m in viewer.get(f"{API}/catalog").json()["semantic_models"] if m["name"] == "secret"
    )
    assert [d["key"] for d in entry["dimensions"]] == ["status", "city"]
    assert [m["key"] for m in entry["measures"]] == ["n"]
    assert entry["time_dimension"] is None  # the default time dimension is hidden too
    spec = {"semantic_model": "secret", "measures": ["n"], "time_grain": "month"}
    err = report_api(viewer, "validate", spec).json()["errors"][0]
    assert err["code"] == "no_time_dimension"
    filt = {
        "semantic_model": "secret",
        "measures": ["n"],
        "filters": [{"field": "amount", "op": "gte", "value": 100}],
    }
    err = report_api(viewer, "validate", filt).json()["errors"][0]
    assert (err["code"], err["path"]) == ("unknown_dimension", "filters[0].field")
    values = viewer.get(f"{API}/values", {"semantic_model": "secret", "dimension": "amount"}).json()
    assert values["errors"][0]["code"] == "unknown_dimension"


def test_hidden_fields_never_leak_through_drilldown_rows(viewer, secret_semantic, report_api):
    body = report_api(
        viewer, "drilldown", {"spec": {"semantic_model": "secret", "measures": ["n"]}, "cell": {}}
    ).json()
    names = [c["name"] for c in body["columns"]]
    # amount is only exposed by items the viewer can't see; customer only by a broken one
    assert "amount" not in names and "customer" not in names
    assert "customer__city__name" in names and "status" in names
    order_metrics = report_api(viewer, "drilldown", {"spec": SPEC, "cell": {}}).json()
    assert "amount" in [c["name"] for c in order_metrics["columns"]]  # revenue shows it


def test_superusers_pass_permissions_but_not_predicates(
    admin_client, orders, secret_semantic, report_api
):
    spec = {"semantic_model": "secret", "measures": ["n", "total"], "dimensions": ["paid_at"]}
    assert report_api(admin_client, "run", spec).status_code == 200
    body = report_api(admin_client, "drilldown", {"spec": spec, "cell": {}}).json()
    assert "amount" in [c["name"] for c in body["columns"]]  # exposed by "total"
    amount = {**spec, "dimensions": ["amount"]}  # the predicate decides, even for root
    assert report_api(admin_client, "run", amount).json()["errors"][0]["code"] == (
        "unknown_dimension"
    )


def test_validate_without_request_sees_everything(orders):
    assert validate({**SPEC, "measures": ["refunds"]}).measures[0].key == "refunds"


def test_validate_with_request_hides(rf, make_staff, orders):
    request = rf.get("/")
    request.user = make_staff("shop.view_order")
    with pytest.raises(SpecValidationError) as exc:
        validate({**SPEC, "measures": ["refunds"]}, request=request)
    assert exc.value.codes == ["unknown_measure"]


def test_hidden_flag_still_wins_over_permission(scratch, orders):
    @scratch
    class Flagged(Semantic):
        model = Order
        dimensions = {"status": Dimension("status")}
        measures = {
            "n": Measure(Count("id")),
            "x": Measure(Count("id"), hidden=True, permission="shop.view_order"),
        }

    sem = semantic_site.get("flagged")
    assert list(sem.visible_measures()) == ["n"]
