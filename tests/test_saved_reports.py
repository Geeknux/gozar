"""M6: saved reports — CRUD, sharing, and viewer-scope execution."""

import pytest
from conftest import API, SHOP_VIEW, TEHRAN_REGION
from django.contrib.auth.models import Group
from django.test import Client

from bi_admin.models import SavedReport

pytestmark = pytest.mark.django_db

SAVED = f"{API}/saved-reports"
BY_CITY = {
    "semantic_model": "order_metrics",
    "measures": ["revenue"],
    "dimensions": ["city"],
    "chart": "bar",
}
EAST_REGION = ["Mashhad", "Kerman"]


def login(user) -> Client:
    client = Client()
    client.force_login(user)
    return client


@pytest.fixture
def people(seeded, make_staff, regional):
    """owner (all shop data), tehran/east (row rules), outsider (no shop access)."""
    return {
        "owner": make_staff(*SHOP_VIEW, username="owner"),
        "tehran": regional("Sales: Tehran region"),
        "east": regional("Sales: East region", username="east"),
        "outsider": make_staff(username="outsider"),
        "finance": make_staff(*SHOP_VIEW, "shop.change_order", username="finance"),
    }


@pytest.fixture
def clients(people):
    return {name: login(user) for name, user in people.items()}


def create(client, **body):
    body = {"name": "Revenue by city", "spec": BY_CITY, **body}
    return client.post(SAVED, body, "application/json")


def ids(client, **params):
    resp = client.get(SAVED, params)
    assert resp.status_code == 200
    return {r["id"] for r in resp.json()["saved_reports"]}


def test_create_and_read(clients):
    resp = create(clients["owner"], description="  For the weekly meeting ")
    assert resp.status_code == 201
    data = resp.json()
    assert data["name"] == "Revenue by city"
    assert data["description"] == "For the weekly meeting"
    assert data["semantic_model"] == "order_metrics"
    assert data["spec"] == BY_CITY  # defaults dropped, calendar left to each viewer
    assert data["sharing"] == {"visibility": "private", "users": [], "groups": []}
    assert data["is_owner"] and data["can_edit"] and data["available"]
    assert clients["owner"].get(f"{SAVED}/{data['id']}").json()["id"] == data["id"]


def test_saving_validates_the_spec_for_the_owner(clients):
    resp = create(clients["owner"], spec={**BY_CITY, "measures": ["revenu"]})
    assert resp.status_code == 422
    assert resp.json()["errors"][0]["code"] == "unknown_measure"
    # a hidden measure is as unknown as a misspelt one
    resp = create(clients["owner"], spec={**BY_CITY, "measures": ["refunds"]})
    assert resp.status_code == 422
    assert create(clients["outsider"]).status_code == 403
    assert create(clients["owner"], name="  ").status_code == 400
    assert not SavedReport.objects.exists()


def test_private_reports_are_invisible_to_others(clients):
    pk = create(clients["owner"]).json()["id"]
    for name in ("tehran", "east", "finance", "outsider"):
        assert pk not in ids(clients[name])
        assert clients[name].get(f"{SAVED}/{pk}").status_code == 404
        assert clients[name].post(f"{SAVED}/{pk}/duplicate").status_code == 404


def test_sharing_with_users_and_groups(clients, people):
    group = Group.objects.get(name="Sales: East region")
    pk = create(
        clients["owner"],
        sharing={"visibility": "users", "users": [people["tehran"].pk], "groups": [group.pk]},
    ).json()["id"]
    assert pk in ids(clients["tehran"])  # by name
    assert pk in ids(clients["east"])  # by group
    assert pk not in ids(clients["finance"])
    shared = clients["tehran"].get(f"{SAVED}/{pk}").json()
    assert not shared["is_owner"] and not shared["can_edit"]
    assert shared["owner"]["username"] == "owner"
    assert [u["username"] for u in shared["sharing"]["users"]] == ["tehran"]


def test_everyone_means_everyone_who_may_see_the_data(clients):
    pk = create(clients["owner"], sharing={"visibility": "everyone"}).json()["id"]
    for name in ("tehran", "east", "finance"):
        assert pk in ids(clients[name])
    # no view permission on orders: not listed even when shared with everyone
    assert pk not in ids(clients["outsider"])
    assert clients["outsider"].get(f"{SAVED}/{pk}").status_code == 404


def test_only_staff_can_be_shared_with(clients, django_user_model):
    customer = django_user_model.objects.create_user("customer", password="x-pass-123!")
    resp = create(clients["owner"], sharing={"visibility": "users", "users": [customer.pk]})
    assert resp.status_code == 400
    assert "users" in resp.json()["errors"]


def test_only_the_owner_changes_or_deletes(clients):
    pk = create(clients["owner"], sharing={"visibility": "everyone"}).json()["id"]
    url = f"{SAVED}/{pk}"
    for name in ("tehran", "finance"):
        assert clients[name].patch(url, {"name": "Mine"}, "application/json").status_code == 403
        assert clients[name].delete(url).status_code == 403
    resp = clients["owner"].patch(
        url,
        {"name": "City revenue", "description": "new", "spec": {**BY_CITY, "chart": "pie"}},
        "application/json",
    )
    assert resp.status_code == 200
    assert (resp.json()["name"], resp.json()["spec"]["chart"]) == ("City revenue", "pie")
    resp = clients["owner"].patch(url, {"sharing": {"visibility": "private"}}, "application/json")
    assert resp.json()["sharing"]["visibility"] == "private"
    assert pk not in ids(clients["tehran"])
    assert clients["owner"].delete(url).json()["deleted"] is True
    assert not SavedReport.objects.filter(pk=pk).exists()


def test_duplicate_makes_a_private_copy(clients, people):
    pk = create(clients["owner"], sharing={"visibility": "everyone"}).json()["id"]
    resp = clients["tehran"].post(f"{SAVED}/{pk}/duplicate")
    assert resp.status_code == 201
    copy = resp.json()
    assert copy["name"] == "Copy of Revenue by city"
    assert copy["owner"]["username"] == "tehran" and copy["can_edit"]
    assert copy["sharing"]["visibility"] == "private"
    assert copy["spec"] == BY_CITY
    assert copy["id"] not in ids(clients["owner"])
    named = clients["tehran"].post(f"{SAVED}/{pk}/duplicate?name=Mine")
    assert named.json()["name"] == "Mine"


def run_cities(client, spec) -> set[str]:
    resp = client.post(f"{API}/reports/run", spec, "application/json")
    assert resp.status_code == 200, resp.content
    return {row["city"] for row in resp.json()["rows"]}


def test_a_shared_report_runs_with_the_viewers_scope(clients):
    pk = create(clients["owner"], sharing={"visibility": "everyone"}).json()["id"]
    every = run_cities(clients["owner"], clients["owner"].get(f"{SAVED}/{pk}").json()["spec"])
    tehran = run_cities(clients["tehran"], clients["tehran"].get(f"{SAVED}/{pk}").json()["spec"])
    east = run_cities(clients["east"], clients["east"].get(f"{SAVED}/{pk}").json()["spec"])
    assert tehran and tehran <= set(TEHRAN_REGION)
    assert east and east <= set(EAST_REGION)
    assert every > tehran | east


def test_a_report_shared_by_a_region_user_shows_the_viewers_rows(clients):
    """Sharing never carries the owner's scope along — narrower or wider."""
    pk = create(clients["tehran"], sharing={"visibility": "everyone"}).json()["id"]
    spec = clients["east"].get(f"{SAVED}/{pk}").json()["spec"]
    assert run_cities(clients["east"], spec) <= set(EAST_REGION)
    assert run_cities(clients["owner"], spec) - set(TEHRAN_REGION)


def test_hidden_measures_make_a_shared_report_unavailable(clients):
    spec = {**BY_CITY, "measures": ["revenue", "refunds"]}
    pk = create(clients["finance"], spec=spec, sharing={"visibility": "everyone"}).json()["id"]
    (entry,) = [r for r in clients["tehran"].get(SAVED).json()["saved_reports"] if r["id"] == pk]
    assert entry["available"] is False
    assert "refunds" in entry["problem"]
    resp = clients["tehran"].post(f"{API}/reports/run", entry["spec"], "application/json")
    assert resp.status_code == 422
    assert resp.json()["errors"][0]["code"] == "unknown_measure"


def test_listing_filters(clients):
    create(clients["owner"], sharing={"visibility": "everyone"})
    create(clients["tehran"], name="Tehran orders")
    assert len(ids(clients["tehran"])) == 2
    assert len(ids(clients["tehran"], mine="true")) == 1
    assert len(ids(clients["tehran"], q="orders")) == 1


def test_sharing_targets(clients):
    data = clients["owner"].get(f"{API}/sharing/targets", {"q": "ea"}).json()
    assert [u["username"] for u in data["users"]] == ["east"]
    assert [g["name"] for g in data["groups"]] == ["Sales: East region"]


def test_saved_reports_need_a_staff_session(people):
    assert Client().get(SAVED).status_code == 401
