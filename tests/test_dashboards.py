"""M6: dashboards — widgets, sharing, global filters and viewer-scope execution."""

import pytest
from conftest import API, SHOP_VIEW, TEHRAN_REGION
from django.db.models import Count
from django.test import Client

from bi_admin import dashboards
from bi_admin.models import Dashboard, SavedReport
from bi_admin.semantic import Dimension, Measure, Semantic, semantic_site
from shop.models import Product

pytestmark = pytest.mark.django_db

BOARDS = f"{API}/dashboards"
BY_CITY = {
    "semantic_model": "order_metrics",
    "measures": ["revenue"],
    "dimensions": ["city"],
    "relative_period": "last_month",
    "chart": "bar",
}
KPI = {"semantic_model": "order_metrics", "measures": ["orders"], "chart": "kpi"}
EAST_REGION = ["Mashhad", "Kerman"]


class ProductMetrics(Semantic):
    """A second model with no time dimension and no city: global filters skip it."""

    model = Product
    name = "product_metrics"
    dimensions = {"category": Dimension("category")}
    measures = {"products": Measure(Count("id"))}


@pytest.fixture
def products():
    semantic_site.register(ProductMetrics)
    yield
    semantic_site.unregister("product_metrics")


def login(user) -> Client:
    client = Client()
    client.force_login(user)
    return client


@pytest.fixture
def people(seeded, make_staff, regional):
    return {
        "owner": make_staff(*SHOP_VIEW, username="owner"),
        "tehran": regional("Sales: Tehran region"),
        "east": regional("Sales: East region", username="east"),
        "outsider": make_staff(username="outsider"),
        "third": make_staff(*SHOP_VIEW, username="third"),
    }


@pytest.fixture
def clients(people):
    return {name: login(user) for name, user in people.items()}


def post(client, url, body):
    return client.post(url, body, "application/json")


def create_board(client, widgets=None, **body):
    widgets = widgets if widgets is not None else [{"spec": BY_CITY, "w": 6, "h": 4}]
    return post(client, BOARDS, {"name": "Sales", "widgets": widgets, **body})


def saved(owner, name="By city", spec=BY_CITY, visibility="private"):
    return SavedReport.objects.create(
        owner=owner, name=name, semantic_model=spec["semantic_model"], spec=spec,
        visibility=visibility,
    )  # fmt: skip


# --- CRUD and validation ---------------------------------------------------------------


def test_create_with_saved_and_inline_widgets(clients, people):
    report = saved(people["owner"])
    resp = create_board(
        clients["owner"],
        widgets=[
            {"saved_report": report.pk, "chart": "pie", "x": 0, "y": 0, "w": 6, "h": 4},
            {"spec": KPI, "title": "Orders", "x": 6, "y": 0, "w": 6, "h": 2},
        ],
        filters={"period": "last_3_months", "dimensions": ["city", "status"]},
    )
    assert resp.status_code == 201, resp.content
    board = resp.json()
    first, second = board["widgets"]
    assert (first["title"], first["chart"], first["inline"]) == ("By city", "pie", False)
    assert first["spec"] == BY_CITY and first["saved_report"]["id"] == report.pk
    assert (second["title"], second["chart"], second["inline"]) == ("Orders", "kpi", True)
    assert second["layout"] == {"x": 6, "y": 0, "w": 6, "h": 2}
    assert board["filters"] == {"period": "last_3_months", "dimensions": ["city", "status"]}
    keys = [o["key"] for o in board["filter_options"]]
    assert {"city", "status", "province"} <= set(keys) and "created_at" not in keys
    status = next(o for o in board["filter_options"] if o["key"] == "status")
    assert {"value": "paid", "label": "Paid"} in status["choices"]


@pytest.mark.parametrize(
    ("widget", "field"),
    [
        ({"spec": BY_CITY, "x": 8, "w": 6}, "widgets[0].layout"),
        ({"spec": BY_CITY, "h": 0}, "widgets[0].layout"),
        ({"spec": BY_CITY, "chart": "radar"}, "widgets[0].chart"),
        ({}, "widgets[0]"),
        ({"spec": {**BY_CITY, "measures": ["revenu"]}}, "widgets[0].spec"),
        ({"saved_report": 999}, "widgets[0].saved_report"),
    ],
)
def test_widget_validation(clients, widget, field):
    resp = create_board(clients["owner"], widgets=[widget])
    assert resp.status_code == 400
    assert field in resp.json()["errors"]
    assert not Dashboard.objects.exists()


def test_bad_global_filters(clients):
    resp = create_board(clients["owner"], filters={"period": "last_forever"})
    assert resp.status_code == 400 and "filters" in resp.json()["errors"]
    resp = create_board(
        clients["owner"], filters={"period": "last_month", "range": ["2026-01-01", "2026-02-01"]}
    )
    assert resp.status_code == 400


def test_widgets_may_only_use_reports_the_editor_can_open(clients, people):
    private = saved(people["third"])
    resp = create_board(clients["owner"], widgets=[{"saved_report": private.pk}])
    assert resp.status_code == 400


def test_sharing_and_ownership(clients, people):
    pk = create_board(clients["owner"]).json()["id"]
    assert clients["tehran"].get(f"{BOARDS}/{pk}").status_code == 404
    url = f"{BOARDS}/{pk}"
    post_patch = clients["owner"].patch(
        url, {"sharing": {"visibility": "everyone"}}, "application/json"
    )
    assert post_patch.status_code == 200
    listed = {b["id"] for b in clients["tehran"].get(BOARDS).json()["dashboards"]}
    assert pk in listed
    # no access to any widget's data: not listed
    assert pk not in {b["id"] for b in clients["outsider"].get(BOARDS).json()["dashboards"]}
    assert clients["outsider"].get(url).status_code == 404
    # only the owner edits
    assert clients["tehran"].patch(url, {"name": "x"}, "application/json").status_code == 403
    assert clients["tehran"].delete(url).status_code == 403
    copy = clients["tehran"].post(f"{url}/duplicate").json()
    assert copy["name"] == "Copy of Sales" and copy["can_edit"]
    assert copy["sharing"]["visibility"] == "private" and copy["widget_count"] == 1
    assert clients["owner"].delete(url).json() == {"deleted": True}


def test_widget_reports_follow_the_dashboard_owner(clients, people):
    """A widget may show its dashboard owner's private report to the dashboard's
    viewers, but never a third person's report that wasn't shared with them."""
    own = saved(people["owner"], name="Owner's own")
    shared_with_owner = saved(people["third"], name="Third's", visibility="users")
    shared_with_owner.shared_users.add(people["owner"])
    pk = create_board(
        clients["owner"],
        widgets=[{"saved_report": own.pk}, {"saved_report": shared_with_owner.pk, "y": 4}],
        sharing={"visibility": "everyone"},
    ).json()["id"]
    first, second = clients["tehran"].get(f"{BOARDS}/{pk}").json()["widgets"]
    assert first["spec"] == BY_CITY and not first["problem"]
    assert second["spec"] is None
    assert second["problem"] == "This saved report is not shared with you."
    # the duplicate keeps only what the viewer could see
    copy = clients["tehran"].post(f"{BOARDS}/{pk}/duplicate").json()
    (widget,) = copy["widgets"]
    assert widget["inline"] and widget["spec"] == BY_CITY


def test_deleted_saved_report_leaves_an_explained_gap(clients, people):
    report = saved(people["owner"])
    pk = create_board(clients["owner"], widgets=[{"saved_report": report.pk}]).json()["id"]
    resp = clients["owner"].delete(f"{API}/saved-reports/{report.pk}")
    assert resp.json() == {"deleted": True, "widgets": 1}
    (widget,) = clients["owner"].get(f"{BOARDS}/{pk}").json()["widgets"]
    assert widget["spec"] is None and "deleted" in widget["problem"]


def test_pin_as_home(clients):
    pk = create_board(clients["owner"]).json()["id"]
    resp = clients["owner"].patch(
        f"{API}/preferences", {"options": {"home_dashboard": pk}}, "application/json"
    )
    assert resp.status_code == 200
    listing = clients["owner"].get(BOARDS).json()
    assert listing["home"] == pk
    assert [b["is_home"] for b in listing["dashboards"]] == [True]


# --- global filters --------------------------------------------------------------------


def test_period_replaces_the_widgets_own_period_and_date_filters(rf, admin_user):
    request = rf.get("/")
    request.user = admin_user
    spec = {
        **BY_CITY,
        "filters": [
            {"field": "created_at", "op": "between", "value": ["2026-01-01", "2026-02-01"]},
            {"field": "status", "op": "eq", "value": "paid"},
        ],
        "compare_to": "previous_period",
    }
    out, applied, ignored = dashboards.apply_filters(spec, {"period": "this_year"}, request)
    assert out["relative_period"] == "this_year"
    assert out["filters"] == [{"field": "status", "op": "eq", "value": "paid"}]
    assert out["compare_to"] == "previous_period"
    assert (applied, ignored) == (["period"], [])
    out, applied, _ignored = dashboards.apply_filters(
        BY_CITY, {"range": ["2026-01-01", "2026-03-31"]}, request
    )
    assert out["relative_period"] is None
    assert out["filters"] == [
        {"field": "created_at", "op": "between", "value": ["2026-01-01", "2026-03-31"]}
    ]


def test_category_filters_replace_the_widgets_own(rf, admin_user):
    request = rf.get("/")
    request.user = admin_user
    spec = {**BY_CITY, "filters": [{"field": "city", "op": "eq", "value": "Tehran"}]}
    out, applied, ignored = dashboards.apply_filters(
        spec, {"values": {"city": ["Mashhad", "Kerman"], "status": ["paid"]}}, request
    )
    assert out["filters"] == [
        {"field": "city", "op": "in", "value": ["Mashhad", "Kerman"]},
        {"field": "status", "op": "eq", "value": "paid"},
    ]
    assert (applied, ignored) == (["city", "status"], [])


def test_filters_skip_widgets_without_the_dimension(rf, admin_user, products):
    request = rf.get("/")
    request.user = admin_user
    spec = {"semantic_model": "product_metrics", "measures": ["products"]}
    out, applied, ignored = dashboards.apply_filters(
        spec,
        {"period": "last_month", "values": {"city": ["Tehran"], "category": ["books"]}},
        request,
    )
    assert out == {**spec, "filters": [{"field": "category", "op": "eq", "value": "books"}]}
    assert (applied, ignored) == (["category"], ["period", "city"])


def test_filters_on_hidden_dimensions_are_ignored(rf, make_staff):
    class Secret(Semantic):
        model = Product
        name = "secret_products"
        dimensions = {"category": Dimension("category", permission="shop.change_product")}
        measures = {"products": Measure(Count("id"))}

    semantic_site.register(Secret)
    try:
        request = rf.get("/")
        request.user = make_staff("shop.view_product")
        spec = {"semantic_model": "secret_products", "measures": ["products"]}
        _out, applied, ignored = dashboards.apply_filters(
            spec, {"values": {"category": ["books"]}}, request
        )
        assert (applied, ignored) == ([], ["category"])
    finally:
        semantic_site.unregister("secret_products")


def test_clean_filters():
    assert dashboards.clean_filters(None) == {}
    assert dashboards.clean_filters(
        {"period": "", "range": ["1405/01/01", "1405/01/31"], "values": {"city": "Tehran", "x": []}}
    ) == {"range": ["2026-03-21", "2026-04-20"], "values": {"city": ["Tehran"]}}
    with pytest.raises(dashboards.FilterError):
        dashboards.clean_filters({"range": ["2026-02-01", "2026-01-01"]})


# --- resolve + run as the viewer ---------------------------------------------------------


def resolve(client, pk, filters):
    resp = post(client, f"{BOARDS}/{pk}/resolve", {"filters": filters})
    assert resp.status_code == 200, resp.content
    return resp.json()["widgets"]


def cities(client, spec) -> set[str]:
    resp = post(client, f"{API}/reports/run", spec)
    assert resp.status_code == 200, resp.content
    return {row["city"] for row in resp.json()["rows"]}


def test_resolved_widgets_run_with_the_viewers_scope(clients, products):
    spec = {**BY_CITY, "relative_period": None}
    pk = create_board(
        clients["owner"],
        widgets=[
            {"spec": spec},
            {"spec": {"semantic_model": "product_metrics", "measures": ["products"]}, "y": 4},
        ],
        sharing={"visibility": "everyone"},
    ).json()["id"]
    by_city, other = resolve(clients["tehran"], pk, {"period": "last_year"})
    assert by_city["spec"]["relative_period"] == "last_year"
    assert (by_city["applied"], other["applied"], other["ignored"]) == (["period"], [], ["period"])
    every = cities(clients["owner"], resolve(clients["owner"], pk, {})[0]["spec"])
    tehran = cities(clients["tehran"], resolve(clients["tehran"], pk, {})[0]["spec"])
    east = cities(clients["east"], resolve(clients["east"], pk, {})[0]["spec"])
    assert tehran <= set(TEHRAN_REGION) and east <= set(EAST_REGION)
    assert tehran and east and every > tehran | east


def test_a_global_city_filter_never_widens_the_scope(clients):
    pk = create_board(
        clients["owner"], widgets=[{"spec": {**BY_CITY, "relative_period": None}}],
        sharing={"visibility": "everyone"},
    ).json()["id"]  # fmt: skip
    (widget,) = resolve(clients["tehran"], pk, {"values": {"city": ["Mashhad"]}})
    assert widget["spec"]["filters"] == [{"field": "city", "op": "eq", "value": "Mashhad"}]
    assert cities(clients["tehran"], widget["spec"]) == set()
    (widget,) = resolve(clients["east"], pk, {"values": {"city": ["Mashhad"]}})
    assert cities(clients["east"], widget["spec"]) <= {"Mashhad"}


def test_resolve_keeps_the_widget_chart_and_reports_problems(clients, people):
    report = saved(people["owner"])
    pk = create_board(
        clients["owner"], widgets=[{"saved_report": report.pk, "chart": "pie"}]
    ).json()["id"]
    (widget,) = resolve(clients["owner"], pk, {})
    assert widget["spec"]["chart"] == "pie"
    report.delete()
    (widget,) = resolve(clients["owner"], pk, {})
    assert widget["spec"] is None and widget["problem"]
    resp = post(clients["owner"], f"{BOARDS}/{pk}/resolve", {"filters": {"period": "nope"}})
    assert resp.status_code == 400
