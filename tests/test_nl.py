"""M5: the natural-language interface — text normalisation, value matching, retrieval,
providers, the pipeline (validation + retries, clarification, refinement, cache,
audit), privacy and the /api/nl endpoints."""

import datetime as dt
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from django.contrib.auth.models import Permission
from django.core import checks
from django.db.models import Count, Sum
from django.test import Client, RequestFactory, override_settings
from django.utils import translation

from bi_admin.models import NLLog
from bi_admin.nl import dates, prompt
from bi_admin.nl.catalog import retrieve, visible_catalog
from bi_admin.nl.conf import nl_setting
from bi_admin.nl.heuristic import HeuristicProvider
from bi_admin.nl.interpret import describe
from bi_admin.nl.pipeline import ask, redact, restore
from bi_admin.nl.providers import (
    OpenAICompatibleProvider,
    ProviderError,
    StubProvider,
    get_provider,
    parse_json,
)
from bi_admin.nl.text import normalise, phonetic_keys, phonetic_similarity, tokens
from bi_admin.nl.values import ValueMatcher
from bi_admin.semantic import Dimension, Measure, Semantic, semantic_site
from shop.models import City, Customer, Order

pytestmark = pytest.mark.django_db

SHOP_VIEW = [f"shop.view_{m}" for m in ("city", "customer", "product", "order", "orderitem")]

API = "/gozar/api"
TODAY = dt.date(2026, 10, 18)
SPEC = {"semantic_model": "order_metrics", "measures": ["revenue"]}


def as_request(user):
    request = RequestFactory().get("/")
    request.user = user
    return request


def nl(**options):
    """``override_settings`` for GOZAR["NL"] (keeps the other GOZAR keys)."""
    from django.conf import settings

    return override_settings(GOZAR={**settings.GOZAR, "NL": options})


@pytest.fixture
def admin_request(admin_user):
    return as_request(admin_user)


@pytest.fixture
def viewer(make_staff):
    """May view shop data but not change orders: the finance-only Refunds is hidden."""
    return make_staff(*SHOP_VIEW, username="viewer")


@pytest.fixture
def scratch():
    yield semantic_site.register
    for name in semantic_site.names():
        if name != "order_metrics":
            semantic_site.unregister(name)


def prompt_text(stub: StubProvider) -> str:
    return "\n".join(m["content"] for messages in stub.prompts for m in messages)


# --- text ------------------------------------------------------------------------------


def test_normalise_letters_digits_and_zwnj():
    assert normalise("سفارش‌های كيك ۱۴۰۵ آمار") == "سفارش های کیک 1405 امار"
    assert tokens("فروش ۱٬۰۰۰ در 1405/01/01") == ["فروش", "1,000", "در", "1405/01/01"]


@pytest.mark.parametrize(
    "persian,latin",
    [
        ("تهران", "Tehran"),
        ("مشهد", "Mashhad"),
        ("اصفهان", "Isfahan"),
        ("شیراز", "Shiraz"),
        ("اهواز", "Ahvaz"),
        ("قم", "Qom"),
        ("خراسان رضوی", "Razavi Khorasan"),
        ("آذربایجان شرقی", "East Azerbaijan"),
        ("علی کریمی", "Ali Karimi"),
    ],
)
def test_transliteration_keys_meet_across_scripts(persian, latin):
    assert phonetic_keys(persian) & phonetic_keys(latin)
    assert phonetic_similarity(persian, latin) == 1.0


def test_transliteration_keeps_different_names_apart():
    assert phonetic_similarity("تهران", "Tabriz") < 0.6
    assert phonetic_similarity("مشهد", "Tehran") < 0.6


def test_partial_dates_expand_in_both_calendars():
    assert dates.partial_bounds("1403") == (dt.date(2024, 3, 20), dt.date(2025, 3, 20))  # leap
    assert dates.partial_bounds("۱۴۰۵/۰۷") == (dt.date(2026, 9, 23), dt.date(2026, 10, 22))
    assert dates.partial_bounds("2024-02") == (dt.date(2024, 2, 1), dt.date(2024, 2, 29))
    assert dates.expand_filter("eq", "1404") == ("between", ["2025-03-21", "2026-03-20"])
    assert dates.expand_filter("gte", "1405/01/01") == ("gte", "2026-03-21")
    assert dates.expand_filter("between", ["1405/01/01", "1405/01"]) == (
        "between",
        ["2026-03-21", "2026-04-20"],
    )
    assert dates.partial_bounds("Tehran") is None


def test_parse_json_tolerates_fences_and_chatter():
    assert parse_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert parse_json('Here you go: {"a": 1} hope it helps') == {"a": 1}
    assert parse_json("no json") is None
    assert parse_json("[1, 2]") is None


# --- value matching ----------------------------------------------------------------------


def test_value_index_matches_persian_to_english_values(seeded, admin_request):
    from bi_admin.semantic import semantic_site

    matcher = ValueMatcher(semantic_site.get("order_metrics"), admin_request)
    best, _ = matcher.index("city").resolve("تهران")
    assert best.value == "Tehran" and best.how == "phonetic"
    best, _ = matcher.index("province").resolve("خراسان رضوی")
    assert best.value == "Razavi Khorasan"
    best, _ = matcher.index("city").resolve("Tehrn")  # same skeleton as Tehran
    assert best.value == "Tehran" and best.how == "phonetic"
    best, _ = matcher.index("city").resolve("Tehrsn")
    assert best.value == "Tehran" and best.how == "fuzzy"
    assert matcher.index("city").resolve("Gotham")[0] is None


def test_value_index_matches_english_to_persian_values(seeded_fa, admin_request):
    matcher = ValueMatcher(semantic_site.get("order_metrics"), admin_request)
    assert matcher.index("city").resolve("Tehran")[0].value == "تهران"
    # Arabic yeh/kaf in the question still find the Persian spelling
    assert matcher.index("city").resolve("كرمان")[0].value == "کرمان"


def test_value_index_lists_related_rows_without_report_rows(seeded, admin_request):
    """customer__city__name is indexed from City, so a city without orders matches."""
    assert not Order.objects.filter(customer__city__name="Rasht").exists()
    matcher = ValueMatcher(semantic_site.get("order_metrics"), admin_request)
    assert matcher.index("city").resolve("رشت")[0].value == "Rasht"


def test_value_index_respects_row_rules(seeded, regional):
    user = regional()  # Tehran region: Tehran, Karaj, Qom
    matcher = ValueMatcher(semantic_site.get("order_metrics"), as_request(user))
    labels = {label for _value, label in matcher.index("city").entries}
    assert labels == {"Tehran", "Karaj", "Qom"}
    assert matcher.index("city").resolve("Mashhad")[0] is None
    assert matcher.index("city").search("Mashhad") == [] or all(
        m.label in labels for m in matcher.index("city").search("Mashhad")
    )
    customers = {label for _v, label in matcher.index("customer").entries}
    in_scope = set(
        Customer.objects.filter(city__name_en__in=["Tehran", "Karaj", "Qom"]).values_list(
            "name", flat=True
        )
    )
    assert customers <= in_scope


def test_value_index_cache_follows_data_changes(seeded, admin_request):
    sem = semantic_site.get("order_metrics")
    assert ValueMatcher(sem, admin_request).index("city").resolve("Yazd")[0] is None
    City.objects.create(name="Yazd", name_en="Yazd", province="Yazd")
    assert ValueMatcher(sem, admin_request).index("city").resolve("یزد")[0].value == "Yazd"


def test_value_index_hides_invisible_dimensions(scratch, seeded, viewer):
    @scratch
    class SecretMetrics(Semantic):
        model = Order
        dimensions = {
            "city": Dimension("customer__city__name", permission="shop.change_order"),
            "status": Dimension("status"),
        }
        measures = {"orders": Measure(Count("id"))}

    matcher = ValueMatcher(semantic_site.get("secret_metrics"), as_request(viewer))
    assert matcher.index("city") is None
    assert "city" not in matcher.indexes()


# --- retrieval & catalog -----------------------------------------------------------------


def test_visible_catalog_respects_visibility(viewer, admin_user):
    (entry,) = visible_catalog(as_request(viewer))
    assert "refunds" not in entry.measures
    (admin_entry,) = visible_catalog(as_request(admin_user))
    assert "refunds" in admin_entry.measures


def test_visible_catalog_needs_model_permission(make_staff):
    assert visible_catalog(as_request(make_staff("shop.view_city"))) == []


def test_retrieve_ranks_models_and_limits_items(scratch, admin_user):
    @scratch
    class CityMetrics(Semantic):
        model = City
        label = {"en": "Cities", "fa": "شهرها"}
        dimensions = {"province": Dimension("province", label={"en": "Province"})}
        measures = {"cities": Measure(Count("id"), label={"en": "Number of cities"})}

    entries = visible_catalog(as_request(admin_user))
    ranked = retrieve("number of cities by province", entries, top_k=20, max_models=1)
    assert [e.name for e in ranked] == ["city_metrics"]
    ranked = retrieve("revenue by city", entries, top_k=2, max_models=3)
    orders = next(e for e in ranked if e.name == "order_metrics")
    # top-k items plus the default time dimension
    assert "revenue" in orders.measures and "city" in orders.dimensions
    assert set(orders.dimensions) <= {"city", "created_at", "customer", "province"}
    assert len(orders.measures) + len(orders.dimensions) <= 3
    previous = {"semantic_model": "order_metrics", "measures": ["aov"], "dimensions": ["status"]}
    kept = retrieve("now monthly", entries, previous=previous, top_k=1, max_models=1)
    assert kept[0].name == "order_metrics"
    assert "aov" in kept[0].measures and "status" in kept[0].dimensions


def test_heuristic_asks_which_model_when_two_fit(scratch, seeded, admin_request):
    @scratch
    class OrderCounts(Semantic):
        model = Order
        label = {"en": "Order counts"}
        dimensions = {"city": Dimension("customer__city__name", label={"en": "City"})}
        measures = {"orders": Measure(Count("id"), label={"en": "Orders"})}

    answer = ask("orders by city", admin_request, today=TODAY, provider=HeuristicProvider())
    assert answer.status == "clarification"
    assert "Which data" in answer.clarification


# --- the pipeline with the heuristic -------------------------------------------------------


def test_ask_heuristic_end_to_end(seeded, admin_request):
    answer = ask("revenue by city last 3 months", admin_request, today=TODAY)
    assert answer.status == "ok"
    assert answer.provider["name"] == "heuristic"
    assert answer.spec["measures"] == ["revenue"] and answer.spec["dimensions"] == ["city"]
    assert answer.spec["relative_period"] == "last_3_months"
    assert answer.period == {
        "name": "last_3_months",
        "calendar": "gregorian",
        "start": "2026-07-01",
        "end": "2026-09-30",
    }
    assert answer.interpretation == (
        "Revenue by City, the last 3 months (Jul 1, 2026 – Sep 30, 2026)."
    )


def test_relative_periods_resolve_in_the_users_calendar(seeded, admin_user):
    from bi_admin.models import UserPreferences

    UserPreferences.objects.create(user=admin_user, calendar="jalali")
    answer = ask("درآمد سه ماه گذشته", as_request(admin_user), today=TODAY)
    assert answer.spec["relative_period"] == "last_3_months"
    assert answer.spec["calendar"] is None  # the model never fixes the calendar
    # 1405/07/26 → the three Jalali months before Mehr: Tir, Mordad, Shahrivar
    assert (answer.period["start"], answer.period["end"]) == ("2026-06-22", "2026-09-22")
    with translation.override("fa"):
        answer = ask("درآمد سه ماه گذشته", as_request(admin_user), today=TODAY)
    assert "۱ تیر ۱۴۰۵" in answer.interpretation and "۳۱ شهریور ۱۴۰۵" in answer.interpretation


def test_hidden_measure_is_not_offered_to_a_viewer(seeded, viewer):
    answer = ask("refunds by city", as_request(viewer), today=TODAY)
    assert answer.status != "ok" or "refunds" not in answer.spec["measures"]


def test_out_of_scope_value_asks_without_revealing_it(seeded, regional):
    user = regional()
    answer = ask(
        "revenue in Mashhad",
        as_request(user),
        today=TODAY,
        provider=StubProvider(
            {
                "revenue in Mashhad": {
                    **SPEC,
                    "filters": [{"field": "city", "op": "eq", "value": "Mashhad"}],
                }
            }
        ),
    )
    assert answer.status == "clarification"
    assert "Mashhad" in answer.clarification  # the user's own word, echoed
    labels = {s["label"] for s in answer.suggestions}
    assert labels <= {"Tehran", "Karaj", "Qom"}
    # the same question for a manager matches
    manager_answer = ask(
        "revenue in Mashhad",
        as_request(regional("Sales: East region", username="east")),
        today=TODAY,
    )
    assert manager_answer.status == "ok"
    assert manager_answer.spec["filters"] == [{"field": "city", "op": "eq", "value": "Mashhad"}]


def test_value_moves_to_the_dimension_it_belongs_to(seeded, admin_request):
    stub = StubProvider([{**SPEC, "filters": [{"field": "city", "op": "eq", "value": "فارس"}]}])
    answer = ask("revenue in Fars", admin_request, today=TODAY, provider=stub)
    assert answer.status == "ok"
    assert answer.spec["filters"] == [{"field": "province", "op": "eq", "value": "Fars"}]
    assert answer.warnings


def test_value_suggestions_build_follow_up_questions(seeded, admin_request):
    stub = StubProvider([{**SPEC, "filters": [{"field": "city", "op": "eq", "value": "Tehrann"}]}])
    answer = ask("revenue in Tehrann", admin_request, today=TODAY, provider=stub)
    if answer.status == "ok":  # close enough to match outright
        assert answer.spec["filters"][0]["value"] == "Tehran"
        return
    assert answer.suggestions[0] == {"label": "Tehran", "question": "revenue in Tehran"}


# --- providers: stub, retries, clarification ---------------------------------------------


def test_retry_feeds_validation_errors_back(seeded, admin_request):
    stub = StubProvider([{**SPEC, "measures": ["revenu"]}, SPEC])
    answer = ask("revenue", admin_request, today=TODAY, provider=stub)
    assert answer.status == "ok" and answer.attempts == 2 and answer.retries == 1
    retry = stub.prompts[1]
    assert retry[-2] == {
        "role": "assistant",
        "content": json.dumps({**SPEC, "measures": ["revenu"]}),
    }
    assert "unknown_measure" in retry[-1]["content"] and "revenue" in retry[-1]["content"]


def test_retries_are_capped(seeded, admin_request):
    stub = StubProvider([{**SPEC, "measures": ["nope"]}])
    answer = ask("revenue", admin_request, today=TODAY, provider=stub)
    assert answer.status == "invalid"
    assert answer.attempts == 3  # one call + MAX_RETRIES (2)
    assert answer.errors[0]["code"] == "unknown_measure"
    with nl(MAX_RETRIES=0):
        stub = StubProvider([{**SPEC, "measures": ["nope"]}])
        assert ask("revenue", admin_request, today=TODAY, provider=stub).attempts == 1
    log = NLLog.objects.order_by("-id")[1]
    assert (log.status, log.attempts, log.error_code) == ("invalid", 3, "unknown_measure")


def test_non_json_and_schema_errors_are_retried(seeded, admin_request):
    stub = StubProvider(["I think you want revenue", {**SPEC, "colour": "red"}, SPEC])
    answer = ask("revenue", admin_request, today=TODAY, provider=stub)
    assert answer.status == "ok" and answer.attempts == 3
    assert "output_not_json" in stub.prompts[1][-1]["content"]
    assert "schema_error" in stub.prompts[2][-1]["content"]


def test_clarification_stops_the_pipeline(seeded, admin_request):
    stub = StubProvider([{**SPEC, "clarification_needed": "Order date or payment date?"}])
    answer = ask("revenue by date", admin_request, today=TODAY, provider=stub)
    assert answer.status == "clarification"
    assert answer.clarification == "Order date or payment date?"
    assert answer.attempts == 1 and len(stub.calls) == 1
    assert NLLog.objects.get().status == "clarification"


def test_heuristic_clarifies_when_nothing_to_measure(seeded, admin_request):
    answer = ask("hello there", admin_request, today=TODAY)
    assert answer.status == "clarification" and "measure" in answer.clarification


def test_partial_dates_from_the_model_are_expanded_by_code(seeded, admin_request):
    stub = StubProvider(
        [{**SPEC, "filters": [{"field": "created_at", "op": "between", "value": ["1404", "1404"]}]}]
    )
    answer = ask("revenue in 1404", admin_request, today=TODAY, provider=stub)
    assert answer.spec["filters"][0]["value"] == ["2025-03-21", "2026-03-20"]


# --- refinement --------------------------------------------------------------------------


def test_refinement_edits_the_previous_spec(seeded, admin_request):
    first = ask("revenue by city last 3 months", admin_request, today=TODAY)
    second = ask("now only Tehran", admin_request, previous=first.spec, today=TODAY)
    assert second.spec["dimensions"] == ["city"]
    assert second.spec["relative_period"] == "last_3_months"
    assert second.spec["filters"] == [{"field": "city", "op": "eq", "value": "Tehran"}]
    third = ask("make it monthly", admin_request, previous=second.spec, today=TODAY)
    assert third.spec["time_grain"] == "month" and third.spec["filters"] == second.spec["filters"]
    assert third.spec["chart"] == "line"


def test_refinement_prompt_carries_the_previous_spec(seeded, admin_request):
    previous = {**SPEC, "dimensions": ["city"], "relative_period": "last_month"}
    stub = StubProvider([{**previous, "time_grain": "month"}])
    answer = ask("make it monthly", admin_request, previous=previous, today=TODAY, provider=stub)
    assert answer.spec["time_grain"] == "month"
    user_message = stub.prompts[0][-1]["content"]
    assert user_message.startswith("PREVIOUS SPEC:") and "make it monthly" in user_message


def test_previous_spec_on_an_invisible_model_is_ignored(scratch, seeded, viewer):
    @scratch
    class Secret(Semantic):
        model = Order
        measures = {"total": Measure(Sum("amount"))}

        def has_permission(self, request):
            return request.user.is_superuser

    stub = StubProvider([SPEC])
    ask(
        "revenue",
        as_request(viewer),
        previous={"semantic_model": "secret", "measures": ["total"]},
        today=TODAY,
        provider=stub,
    )
    assert stub.calls[0].previous is None
    assert "secret" not in prompt_text(stub)


# --- privacy ---------------------------------------------------------------------------------


def _row_data():
    """Every category value and amount in the demo data."""
    values = set(City.objects.values_list("name", flat=True))
    values |= set(City.objects.values_list("province", flat=True))
    values |= set(Customer.objects.values_list("name", flat=True))
    values |= set(Customer.objects.exclude(phone="").values_list("phone", flat=True))
    values |= {str(int(a)) for a in Order.objects.values_list("amount", flat=True) if a > 1000}
    return values


def test_prompts_contain_no_row_data(seeded, admin_request):
    stub = StubProvider([SPEC])
    ask("revenue by city and province for each customer", admin_request, today=TODAY, provider=stub)
    text = prompt_text(stub)
    leaked = sorted(v for v in _row_data() if v and v in text)
    assert leaked == []
    # what does go in: catalog metadata, today in both calendars, the user's context
    assert '"semantic_model":"order_metrics"' in text and "Revenue" in text and "درآمد" in text
    assert '"gregorian":"2026-10-18"' in text and '"jalali":"1405/07/26"' in text
    assert '"calendar":"gregorian"' in text and '"language":"en"' in text


def test_previous_values_are_redacted_and_restored(seeded, admin_request):
    previous = {**SPEC, "filters": [{"field": "city", "op": "in", "value": ["Karaj", "Tabriz"]}]}

    def echo(task):  # a model that keeps the filter and adds a grain
        return {**task.previous, "time_grain": "month"}

    stub = StubProvider(echo)
    answer = ask("make it monthly", admin_request, previous=previous, today=TODAY, provider=stub)
    text = prompt_text(stub)
    assert "Karaj" not in text and "Tabriz" not in text and "@v1" in text and "@v2" in text
    assert answer.spec["filters"] == previous["filters"]
    assert answer.spec["time_grain"] == "month"


def test_redaction_keeps_choices_dates_and_numbers():
    entry = visible_catalog(None)[0]
    spec = {
        **SPEC,
        "filters": [
            {"field": "status", "op": "eq", "value": "paid"},
            {"field": "amount", "op": "gte", "value": 100},
            {"field": "created_at", "op": "gte", "value": "2026-01-01"},
            {"field": "customer", "op": "eq", "value": "Ali Karimi"},
        ],
    }
    redacted, mapping = redact(spec, entry)
    values = [f["value"] for f in redacted["filters"]]
    assert values == ["paid", 100, "2026-01-01", "@v1"]
    assert mapping == {"@v1": "Ali Karimi"}
    assert restore(redacted, mapping) == spec


def test_hidden_items_never_reach_the_prompt(seeded, viewer):
    stub = StubProvider([SPEC])
    ask("refunds by city", as_request(viewer), today=TODAY, provider=stub)
    system = stub.prompts[0][0]["content"]  # everything but the user's own words
    assert "refunds" not in system and "مبلغ مرجوعی" not in system and "Refunds" not in system


def test_local_providers_alone_get_the_value_index(seeded, admin_request):
    stub = StubProvider([SPEC])
    ask("revenue in Tehran", admin_request, today=TODAY, provider=stub)
    assert stub.calls[0].values is None


# --- OpenAI-compatible provider against a local server --------------------------------------


class FakeLLM:
    """A tiny OpenAI-compatible server on localhost (no network needed)."""

    def __init__(self, replies, status=200):
        self.requests = []
        self.replies = list(replies)
        self.status = status
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                outer.requests.append(
                    {"path": self.path, "headers": dict(self.headers), "body": body}
                )
                status = outer.status(body) if callable(outer.status) else outer.status
                reply = outer.replies.pop(0) if len(outer.replies) > 1 else outer.replies[0]
                content = reply if isinstance(reply, str) else json.dumps(reply)
                payload = {"choices": [{"message": {"role": "assistant", "content": content}}]}
                data = json.dumps(payload if status == 200 else {"error": "bad"}).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/v1"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def fake_llm():
    servers = []

    def start(replies, status=200):
        server = FakeLLM(replies, status)
        servers.append(server)
        return server

    yield start
    for server in servers:
        server.close()


def test_openai_provider_round_trip(seeded, admin_request, fake_llm, monkeypatch):
    server = fake_llm([{**SPEC, "dimensions": ["city"]}])
    monkeypatch.setenv("GOZAR_NL_API_KEY", "sk-test-123")
    with nl(BASE_URL=server.url, MODEL="test-model", TIMEOUT=5):
        provider = get_provider()
        assert isinstance(provider, OpenAICompatibleProvider)
        assert "sk-test" not in json.dumps(provider.describe())
        answer = ask("revenue by city", admin_request, today=TODAY)
    assert answer.status == "ok" and answer.provider == {
        "name": "openai",
        "model": "test-model",
        "local": False,
        "host": server.url.split("/")[2],
    }
    (req,) = server.requests
    assert req["path"] == "/v1/chat/completions"
    assert req["headers"]["Authorization"] == "Bearer sk-test-123"
    body = req["body"]
    assert body["model"] == "test-model" and body["temperature"] == 0
    schema = body["response_format"]["json_schema"]["schema"]
    assert schema["properties"]["semantic_model"]["enum"] == ["order_metrics"]
    assert [m["role"] for m in body["messages"]] == ["system", "user"]
    text = json.dumps(body, ensure_ascii=False)
    assert not [v for v in _row_data() if v and v in text]


def test_openai_provider_falls_back_to_json_mode(seeded, admin_request, fake_llm):
    def status(body):
        return 400 if body.get("response_format", {}).get("type") == "json_schema" else 200

    server = fake_llm([SPEC], status=status)
    with nl(BASE_URL=server.url, MODEL="m"):
        answer = ask("revenue", admin_request, today=TODAY)
    assert answer.status == "ok"
    assert [r["body"]["response_format"]["type"] for r in server.requests] == [
        "json_schema",
        "json_object",
    ]


def test_openai_provider_retries_through_the_conversation(seeded, admin_request, fake_llm):
    server = fake_llm(['{"semantic_model": "order_metrics", "measures": ["sales"]}', SPEC])
    with nl(BASE_URL=server.url, MODEL="m", RESPONSE_FORMAT="none"):
        answer = ask("revenue", admin_request, today=TODAY)
    assert answer.attempts == 2
    second = server.requests[1]["body"]
    assert "response_format" not in second
    assert [m["role"] for m in second["messages"]] == ["system", "user", "assistant", "user"]
    assert "unknown_measure" in second["messages"][-1]["content"]


def test_unreachable_llm_falls_back_to_the_heuristic(seeded, admin_request):
    with nl(BASE_URL="http://127.0.0.1:9/v1", MODEL="m", TIMEOUT=1):
        answer = ask("revenue by city", admin_request, today=TODAY)
    assert answer.status == "ok" and answer.provider["name"] == "heuristic"
    assert any("unavailable" in w for w in answer.warnings)


def test_repeated_llm_failures_raise_an_alert(seeded, admin_request, settings):
    from bi_admin.models import Alert

    settings.GOZAR = {**settings.GOZAR, "ALERTS": {**settings.GOZAR["ALERTS"], "NL_ERRORS": 2}}
    with nl(BASE_URL="http://127.0.0.1:9/v1", MODEL="m", TIMEOUT=1):
        ask("revenue by city", admin_request, today=TODAY, use_cache=False)
        assert not Alert.objects.filter(source="nl").exists()
        ask("revenue by month", admin_request, today=TODAY, use_cache=False)
    alert = Alert.objects.get(source="nl")
    assert alert.details["fallback"] is True and alert.details["count"] == 2


def test_unreachable_llm_without_fallback_is_502(seeded, admin_client):
    with nl(BASE_URL="http://127.0.0.1:9/v1", MODEL="m", TIMEOUT=1, FALLBACK=False):
        resp = admin_client.post(f"{API}/nl/ask", {"question": "revenue"}, "application/json")
    assert resp.status_code == 502 and resp.json()["code"] == "provider_unreachable"
    assert NLLog.objects.get().status == "failed"


# --- provider configuration ------------------------------------------------------------------


def test_provider_auto_is_heuristic_without_configuration(monkeypatch):
    monkeypatch.delenv("GOZAR_NL_BASE_URL", raising=False)
    monkeypatch.delenv("GOZAR_NL_MODEL", raising=False)
    assert isinstance(get_provider(), HeuristicProvider)


def test_provider_from_environment(monkeypatch):
    monkeypatch.setenv("GOZAR_NL_BASE_URL", "http://localhost:11434/v1/")
    monkeypatch.setenv("GOZAR_NL_MODEL", "qwen2.5:7b")
    monkeypatch.setenv("GOZAR_NL_TIMEOUT", "12")
    monkeypatch.delenv("GOZAR_NL_API_KEY", raising=False)
    provider = get_provider()
    assert isinstance(provider, OpenAICompatibleProvider)
    assert (provider.base_url, provider.model, provider.timeout) == (
        "http://localhost:11434/v1",
        "qwen2.5:7b",
        12.0,
    )
    assert "Authorization" not in provider.headers()  # a keyless local server


def test_settings_override_environment_and_key_env_name(monkeypatch):
    monkeypatch.setenv("GOZAR_NL_MODEL", "from-env")
    monkeypatch.setenv("MY_LLM_KEY", "secret-xyz")
    with nl(BASE_URL="https://llm.example/v1", MODEL="from-settings", API_KEY_ENV="MY_LLM_KEY"):
        provider = get_provider()
        assert provider.model == "from-settings"
        assert provider.headers()["Authorization"] == "Bearer secret-xyz"
    with nl(PROVIDER="heuristic", BASE_URL="https://llm.example/v1", MODEL="x"):
        assert isinstance(get_provider(), HeuristicProvider)
    with nl(PROVIDER="stub"):
        assert isinstance(get_provider(), StubProvider)


def test_openai_provider_needs_base_url_and_model():
    with nl(PROVIDER="openai"), pytest.raises(ProviderError) as exc:
        get_provider()
    assert exc.value.code == "provider_not_configured"


def test_api_key_in_settings_is_a_system_check_error():
    with nl(API_KEY="sk-oops"):
        errors = [e for e in checks.run_checks() if e.id == "bi_admin.E003"]
    assert errors
    with nl(RESPONSE_FORMAT="xml"):
        assert [e for e in checks.run_checks() if e.id == "bi_admin.E004"]


def test_nl_setting_defaults():
    assert nl_setting("MAX_RETRIES") == 2
    assert nl_setting("LOG_PROMPTS") is False
    with nl(LOG_PROMPTS=True):
        assert nl_setting("LOG_PROMPTS") is True


# --- cache --------------------------------------------------------------------------------------


def test_nl_results_are_cached_per_user_context(seeded, admin_request, viewer):
    stub = StubProvider([SPEC])
    first = ask("revenue", admin_request, today=TODAY, provider=stub)
    second = ask("revenue", admin_request, today=TODAY, provider=stub)
    assert (first.cached, second.cached) == (False, True) and len(stub.calls) == 1
    assert second.spec == first.spec
    # another user sees another catalog (no Refunds): another cache entry
    ask("revenue", as_request(viewer), today=TODAY, provider=stub)
    assert len(stub.calls) == 2
    # another day, another entry; use_cache=False always calls
    ask("revenue", admin_request, today=TODAY + dt.timedelta(days=1), provider=stub)
    ask("revenue", admin_request, today=TODAY, provider=stub, use_cache=False)
    assert len(stub.calls) == 4
    assert NLLog.objects.filter(cache_hit=True).count() == 1


def test_cache_can_be_disabled(seeded, admin_request):
    stub = StubProvider([SPEC])
    with nl(CACHE_TIMEOUT=0):
        ask("revenue", admin_request, today=TODAY, provider=stub)
        ask("revenue", admin_request, today=TODAY, provider=stub)
    assert len(stub.calls) == 2


def test_cached_refinements_restore_each_users_values(seeded, admin_request):
    def echo(task):
        return {**task.previous, "time_grain": "month"}

    stub = StubProvider(echo)
    for city in ("Karaj", "Tabriz"):
        previous = {**SPEC, "filters": [{"field": "city", "op": "eq", "value": city}]}
        answer = ask("monthly", admin_request, previous=previous, today=TODAY, provider=stub)
        assert answer.spec["filters"][0]["value"] == city
    assert len(stub.calls) == 1  # the redacted request was identical


def test_failures_are_not_cached(seeded, admin_request):
    stub = StubProvider([{**SPEC, "measures": ["nope"]}])
    ask("revenue", admin_request, today=TODAY, provider=stub)
    ask("revenue", admin_request, today=TODAY, provider=stub)
    assert len(stub.calls) == 6


# --- audit & opt-in prompt logging ---------------------------------------------------------------


def test_every_request_is_audited(seeded, admin_request):
    answer = ask("revenue by city", admin_request, today=TODAY)
    log = NLLog.objects.get()
    assert log.pk == answer.log_id
    assert (log.username, log.question, log.status, log.provider) == (
        "root",
        "revenue by city",
        "ok",
        "heuristic",
    )
    assert log.spec["dimensions"] == ["city"] and log.interpretation.startswith("Revenue by City")
    assert log.transcript == []  # prompts are only kept when LOG_PROMPTS is on
    assert log.language == "en" and log.calendar == "gregorian" and log.duration_ms is not None


def test_prompt_logging_is_opt_in(seeded, admin_request):
    stub = StubProvider([SPEC])
    with nl(LOG_PROMPTS=True):
        ask("revenue", admin_request, today=TODAY, provider=stub)
    (entry,) = NLLog.objects.get().transcript
    assert entry["output"] == json.dumps(SPEC)
    assert entry["messages"][0]["role"] == "system"


def test_audit_can_be_turned_off(seeded, admin_request, settings):
    settings.GOZAR = {**settings.GOZAR, "AUDIT_REPORTS": False}
    ask("revenue", admin_request, today=TODAY)
    assert not NLLog.objects.exists()


# --- interpretation ------------------------------------------------------------------------------


def _report(spec, user=None):
    from bi_admin.engine.runner import prepare
    from bi_admin.engine.spec import ReportSpec

    return prepare(ReportSpec.model_validate(spec), as_request(user) if user else None, TODAY)


def test_interpretation_in_english_and_persian(seeded, admin_user):
    spec = {
        **SPEC,
        "dimensions": ["city"],
        "filters": [
            {"field": "status", "op": "eq", "value": "paid"},
            {"field": "amount", "op": "gte", "value": 1000000},
        ],
        "relative_period": "last_month",
        "sort": [{"field": "revenue", "direction": "desc"}],
        "limit": 5,
    }
    report = _report(spec)
    assert describe(report, "en") == (
        "Revenue by City, last month (Sep 1, 2026 – Sep 30, 2026), where Status is Paid and "
        "Order amount at least 1,000,000, top 5 by Revenue."
    )
    fa = describe(report, "fa")
    assert fa.startswith("درآمد به تفکیک شهر")
    assert "۵" in fa and "۱٬۰۰۰٬۰۰۰" in fa and "پرداخت" in fa
    jalali = describe(
        _report(
            {**SPEC, "time_grain": "month", "calendar": "jalali", "relative_period": "this_year"}
        ),
        "fa",
    )
    assert "ماهانه (شمسی)" in jalali and "۱ فروردین ۱۴۰۵" in jalali


def test_interpretation_of_dates_and_lists(seeded):
    report = _report(
        {
            **SPEC,
            "time_dimension": "paid_at",
            "filters": [
                {"field": "paid_at", "op": "between", "value": ["2026-01-01", "2026-03-31"]},
                {"field": "city", "op": "not_in", "value": ["Tehran", "Karaj"]},
            ],
        }
    )
    assert describe(report, "en") == (
        "Revenue, where Payment date from Jan 1, 2026 to Mar 31, 2026 and City is neither "
        "Tehran nor Karaj."
    )


# --- API -------------------------------------------------------------------------------------


def post(client, path, body):
    return client.post(f"{API}/nl/{path}", body, "application/json")


def test_api_ask(seeded, admin_client):
    resp = post(admin_client, "ask", {"question": "۵ شهر برتر از نظر فروش"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["spec"]["sort"] == [{"field": "revenue", "direction": "desc"}]
    assert body["spec"]["limit"] == 5
    assert body["interpretation"] == "Revenue by City, top 5 by Revenue."
    assert body["provider"] == {"name": "heuristic", "model": "rules-v1"}
    assert "result" not in body and body["log_id"]


def test_api_ask_execute_runs_the_report(seeded, admin_client):
    body = post(admin_client, "ask", {"question": "orders by status", "execute": True}).json()
    result = body["result"]
    assert [c["key"] for c in result["columns"]] == ["status", "orders"]
    assert sum(r["orders"] for r in result["rows"]) == Order.objects.count()
    from bi_admin.models import ReportLog

    assert ReportLog.objects.filter(kind="run", status="ok").count() == 1


def test_api_ask_execute_respects_row_rules(seeded, regional):
    client = Client()
    client.force_login(regional())
    body = post(client, "ask", {"question": "orders by city", "execute": True}).json()
    assert {r["city"] for r in body["result"]["rows"]} <= {"Tehran", "Karaj", "Qom"}


def test_api_ask_execute_queues_async_models(seeded, admin_client, settings):
    settings.GOZAR = {**settings.GOZAR, "REPORT_ASYNC": True}
    body = post(admin_client, "ask", {"question": "orders by status", "execute": True}).json()
    assert body["job"]["status"] == "queued" and "result" not in body


def test_api_ask_refinement(seeded, admin_client):
    first = post(admin_client, "ask", {"question": "revenue by city"}).json()
    body = post(admin_client, "ask", {"question": "فقط تهران", "previous": first["spec"]}).json()
    assert body["spec"]["filters"] == [{"field": "city", "op": "eq", "value": "Tehran"}]
    assert body["spec"]["dimensions"] == ["city"]


def test_api_ask_validation_and_auth(seeded, admin_client, make_staff):
    assert post(Client(), "ask", {"question": "revenue"}).status_code == 401
    assert post(admin_client, "ask", {"question": "  "}).json()["code"] == "empty_question"
    long = post(admin_client, "ask", {"question": "revenue " * 100})
    assert long.status_code == 422 and long.json()["code"] == "question_too_long"
    nobody = Client()
    nobody.force_login(make_staff("shop.view_city", username="cityonly"))
    denied = post(nobody, "ask", {"question": "revenue"})
    assert denied.status_code == 403 and denied.json()["code"] == "permission_denied"
    assert NLLog.objects.filter(status="denied").count() == 1


def test_api_ask_rate_limit(seeded, admin_client):
    with nl(RATE_LIMIT=2):
        codes = [post(admin_client, "ask", {"question": "revenue"}).status_code for _ in range(3)]
    assert codes == [200, 200, 429]
    assert NLLog.objects.filter(error_code="rate_limited").exists()


def test_api_ask_previous_must_be_a_spec(seeded, admin_client):
    resp = post(admin_client, "ask", {"question": "monthly", "previous": {"foo": 1}})
    assert resp.status_code == 422


def test_api_status(seeded, admin_client, admin_user):
    from bi_admin.models import UserPreferences

    def persian(q):
        return any("\u0600" <= c <= "\u06ff" for c in q)

    body = admin_client.get(f"{API}/nl/status").json()
    assert body["enabled"] and body["provider"]["name"] == "heuristic" and body["llm"] is False
    assert body["examples"] and not any(persian(q) for q in body["examples"])
    UserPreferences.objects.create(user=admin_user, language="fa")
    fa = admin_client.get(f"{API}/nl/status").json()
    assert fa["examples"] and all(persian(q) for q in fa["examples"])


def test_api_interpret_and_validate(seeded, admin_client, report_api):
    body = post(admin_client, "interpret", {"spec": {**SPEC, "dimensions": ["city"]}}).json()
    assert body["interpretation"] == "Revenue by City."
    bad = post(admin_client, "interpret", {"spec": {**SPEC, "measures": ["nope"]}})
    assert bad.status_code == 422
    check = report_api(admin_client, "validate", {**SPEC, "time_grain": "month"}).json()
    assert check["interpretation"] == "Revenue, monthly (Gregorian)."


def test_api_question_log(seeded, admin_client, make_staff):
    post(admin_client, "ask", {"question": "revenue by city"})
    body = admin_client.get(f"{API}/audit/questions").json()
    (entry,) = body["entries"]
    assert entry["question"] == "revenue by city" and entry["status"] == "ok"
    assert "transcript" not in entry
    user = make_staff(*SHOP_VIEW, username="plain")
    plain = Client()
    plain.force_login(user)
    assert plain.get(f"{API}/audit/questions").status_code == 403
    user.user_permissions.add(Permission.objects.get(codename="view_nllog"))
    plain.force_login(user)
    assert plain.get(f"{API}/audit/questions").status_code == 200
    assert plain.get(f"{API}/auth/me").json()["user"]["permissions"]["questions"] is True


def test_prompt_builder_includes_examples_and_both_calendars(admin_user):
    from bi_admin.nl.providers import Task

    task = Task("x", "fa", "jalali", TODAY, visible_catalog(as_request(admin_user)))
    messages = prompt.build_messages(task)
    system = messages[0]["content"]
    assert "EXAMPLES" in system and "1405/07/26" in system and "مهر" in system
    assert '"language":"fa"' in system and '"calendar":"jalali"' in system


def test_heuristic_reports_words_it_did_not_use(seeded, admin_request):
    answer = ask("revenue in Gotham", admin_request, today=TODAY)
    assert answer.status == "ok" and answer.warnings == ["Not used: Gotham"]
    answer = ask("revenue for city Gotham", admin_request, today=TODAY)
    assert answer.status == "clarification" and "“Gotham”" in answer.clarification
    for suggestion in answer.suggestions:  # follow-ups replace the unknown word
        assert suggestion["question"] == f"revenue for city {suggestion['label']}"
