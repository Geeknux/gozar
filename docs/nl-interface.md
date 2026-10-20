# Natural-language interface — reference

This is a reference for Gozar's "Ask a question" feature: what users see, how a
question becomes a report, what leaves the server, how to connect a real
language model and how to measure it. Every statement below was checked
against the code on 2026-10-20 (M6). The sources are `src/bi_admin/nl/`,
`src/bi_admin/api/nl.py`, `frontend/src/reports/AskBox.tsx` and `AskPanel.tsx`,
`tests/test_nl.py`, `tests/test_nl_golden.py` and DESIGN.md §5 with the M5/M6
decisions in §10. Statements about other vendors' APIs cannot be checked from
this repository, so they are marked **verify before use**.

Status in one line: the interface ships with an offline, rule-based
interpreter (`heuristic`) and an OpenAI-compatible client. **No real LLM has
been tested against it yet.**

Contents:

1. [What the user sees](#1-what-the-user-sees)
2. [The pipeline](#2-the-pipeline)
3. [Privacy guarantees](#3-privacy-guarantees)
4. [Providers](#4-providers)
5. [Configuration reference](#5-configuration-reference)
6. [Connecting a model: recipes](#6-connecting-a-model-recipes)
7. [Evaluation](#7-evaluation)
8. [Status, known gaps and "Next time"](#8-status-known-gaps-and-next-time)

---

## 1. What the user sees

### Where

- **Home page** (`AskPanel.tsx`). It has the Ask box, and below it the report
  (table or chart) of the latest answer. Under the result there are **Save
  report…** (which becomes **Open saved report** once saved) and **Open in
  report builder**. When a dashboard is pinned as the home page, the Ask panel
  moves below it, into a collapsed "Ask a question" section.
- **Reports** (`ReportsPage.tsx`). The Ask box sits at the top of the report
  builder. An answer replaces the builder's spec (and the URL), so the normal
  filter UI shows it and can edit it. From there it can be saved, shared,
  exported or scheduled like any manual report.

The box is hidden when the user has no semantic model they may report on
(`GET /api/nl/status` returns `enabled: false`).

### A conversation

1. Before the first question, the box shows up to six example questions in the
   user's language. They come from the semantic models' `nl_examples`, and only
   the examples whose fields the user may see are offered.
2. The user types a question in English or Persian (max
   `MAX_QUESTION_LENGTH`, 500 characters by default). Each answer appears in a
   thread under the question:
   - **ok**: "Understood as: *interpretation sentence*". The report runs through
     the normal report endpoint (`/api/reports/run`, or a background job), so
     the user's permissions, row rules and limits apply as for any report.
   - **clarification**: a question back to the user, e.g. "Which data do you
     mean: Orders or Products?" or "No City matches “Tehrn”. Did you mean
     “Tehran”?". When value matching has in-scope suggestions, they are shown
     as chips. Each chip re-asks the question with the value replaced.
   - **invalid**: "Sorry, I couldn't turn that into a report…" (no valid spec
     after the retries).
   - **warnings** under the answer, e.g. "Not used: New, York" (heuristic),
     "Matched “Tehrn” to City “Tehran”." (a fuzzy value match), or "The
     language model is unavailable; the built-in interpreter answered."
3. **Follow-ups.** After the first *ok* answer, every new question refines the
   current report: the spec on screen is sent as `previous`. On the Reports
   page that includes manual edits made in the builder. **New question**
   starts over. The placeholder changes to "Refine it, e.g. “now only Tehran”
   or “make it monthly”".
4. **Footer.** It shows "Answered by the built-in interpreter (no language model
   configured)." or "Answered by *model*.", plus "Follow-up questions refine
   the current report." while refining.

### Comparisons

The user can ask for a comparison: "compared to last year", "vs last month",
"year over year", «نسبت به سال قبل», «با پارسال مقایسه کن», «دوره مشابه سال
قبل». Gozar then sets `compare_to` (`same_period_last_year` or
`previous_period`), and the result shows:

- comparison and change columns (▲/▼) in tables;
- deltas on KPI cards;
- a dashed second series in line and bar charts.

The interpretation sentence names the comparison span, e.g. "compared with the
same period last year (Jul 1, 2025 – Sep 30, 2025)". "without comparison" or
«بدون مقایسه» removes it in a follow-up.

### Example conversations

The examples below are real heuristic output from the seeded demo database, as
user `manager`, with today pinned to 2026-10-18 (the golden set's date).

**English, Gregorian calendar:**

| Question | Interpretation |
|---|---|
| revenue by city last 3 months | Revenue by City, the last 3 months (Jul 1, 2026 – Sep 30, 2026). |
| now only Tehran | Revenue by City, the last 3 months (Jul 1, 2026 – Sep 30, 2026), where City is Tehran. |
| make it monthly | Revenue by City, monthly (Gregorian), the last 3 months (…), where City is Tehran. |
| compared to last year | Revenue by City, monthly (Gregorian), the last 3 months (Jul 1, 2026 – Sep 30, 2026), compared with the same period last year (Jul 1, 2025 – Sep 30, 2025), where City is Tehran. |
| *New question* → revenue vs last month | Revenue, this month (Oct 1, 2026 – Oct 18, 2026), compared with the previous period (Sep 1, 2026 – Sep 18, 2026). |
| *New question* → top 5 customers by revenue last year | Revenue by Customer, last year (Jan 1, 2025 – Dec 31, 2025), top 5 by Revenue. |

**Persian, Jalali calendar** (the user's preferences):

| پرسش | برداشت |
|---|---|
| فروش ماهانه امسال | درآمد، ماهانه (شمسی)، سال جاری (۱ فروردین ۱۴۰۵ تا ۲۶ مهر ۱۴۰۵). |
| فقط تهران | درآمد، ماهانه (شمسی)، سال جاری (…)، به شرط آنکه شهر Tehran باشد. |
| نسبت به سال قبل | درآمد، ماهانه (شمسی)، سال جاری (۱ فروردین ۱۴۰۵ تا ۲۶ مهر ۱۴۰۵)، در مقایسه با دوره مشابه سال قبل (۱ فروردین ۱۴۰۴ تا ۲۶ مهر ۱۴۰۴)، به شرط آنکه شهر Tehran باشد. |
| *پرسش تازه* ← درآمد شهر تهرون | درآمد، به شرط آنکه شهر Tehran باشد. («تهرون» matched by transliteration) |
| *پرسش تازه* ← درآمد شهر نیویورک | *clarification:* هیچ شهر با «نیویورک» مطابقت ندارد. |
| *پرسش تازه* ← مشتریان | *clarification:* چه چیزی را بسنجم؟ برای نمونه: درآمد، تعداد سفارش، … |

In the Persian sentence, stored values appear as stored ("Tehran"): the demo
data has English city names.

### API

All endpoints use a staff session and CSRF, like the rest of `/api`.

| Endpoint | Body / result |
|---|---|
| `GET /api/nl/status` | `{enabled, provider: {name, model}, llm, max_length, examples}` |
| `POST /api/nl/ask` | `{question, previous?: ReportSpec, execute?: bool}` → `{status: ok \| clarification \| invalid, question, spec, interpretation, clarification, suggestions: [{label, question}], warnings, errors, period, provider: {name, model}, attempts, retries, cached, log_id}`. With `execute` and an *ok* answer, the response also has `result` (the `/reports/run` body) or `job` (a background job). The SPA does not use `execute`: it runs the spec itself. |
| `POST /api/nl/interpret` | `{spec}` → `{interpretation, period}` (422 if the spec is invalid) |

Errors from `/ask`:

| Status | Code | When |
|---|---|---|
| 403 | `permission_denied` | No reportable data |
| 422 | `empty_question`, `question_too_long` | Empty question, or longer than `MAX_QUESTION_LENGTH` |
| 429 | `rate_limited` | More than `RATE_LIMIT` questions per user per minute |
| 502 | `provider_*` | The model failed and `FALLBACK` is off |

`POST /api/reports/validate` also returns the interpretation sentence. The
builder shows it as "Showing: …".

---

## 2. The pipeline

The entry point is `bi_admin.nl.pipeline.ask(question, request, previous=None,
today=None, provider=None, use_cache=True, rate_limit=False)`. It returns an
`Answer`.

```
question (+ previous spec)
   │
   ▼
1 checks ── empty / too long → 422 · no reportable model → 403 · rate limit → 429
   │
   ▼
2 visible catalog  (models the user may report on, fields they may see)
   │
   ▼
3 redact previous spec  (category values → @v1, @v2 …)
   │
   ▼
4 retrieve  (lexical ranking: MAX_MODELS models × TOP_K items)
   │
   ▼
5 cache lookup ─ hit ─────────────────────────────────────────┐
   │ miss                                                     │
   ▼                                                          │
6 provider.generate(task) ◄── validation errors (MAX_RETRIES) │
   │   parse JSON → clean dates → ReportSpec → prepare()     │
   │   under the user's permissions                           │
   │   provider error → heuristic (FALLBACK) or 502           │
   ▼                                                          │
   cache store (accepted answers only) ◄──────────────────────┘
   │
   ▼
7 restore @vN placeholders ── clarification_needed? → ask the user
   │
   ▼
8 value matching against the user's scoped value index
   │   unknown value → clarification with in-scope suggestions
   ▼
9 final validation (prepare) → interpretation sentence → Answer
   │
   ▼
10 NLLog row (always, unless AUDIT_REPORTS is off)

   …then the client runs the spec: /api/reports/run → ReportLog
```

### Steps 1–2: checks and the visible catalog (`nl.catalog`)

`visible_catalog(request)` lists every semantic model whose `has_permission`
passes for the user, with only the dimensions and measures the user may see
(M4 semantic visibility). For each item the catalog holds:

- key, label and synonyms in both languages;
- description;
- type (dimensions) or format (measures);
- declared choices, if any;
- the model's default time dimension.

It holds **metadata only**, never row data. Few-shot examples (`nl_examples`)
are kept only if every field they use is visible to the user.

Row rules don't change the catalog: they decide which rows exist. They act
in step 8 (value index) and when the report runs.

### Step 3: redaction

When a follow-up carries a `previous` spec, every filter value on a category or
geo dimension **without declared choices** is replaced by a placeholder: `@v1`,
`@v2`, …. A filter on an unknown field is treated the same way. Choice values,
dates, numbers and booleans are kept, because they are not row data. A
previous spec on a model the user can't see is dropped, and the question
starts over. The placeholders are restored after the model answers (step 7).
Restored values are trusted as the user's own and are not matched again.

### Step 4: retrieval

`catalog.retrieve()` is lexical, with no embeddings and no vector store:

- whole-phrase matches over keys, labels and synonyms. Persian plural and
  ezafe suffixes, separated «ها» and English plurals are tolerated, and a few
  abbreviations are understood (avg, qty, «متوسط»);
- character-trigram overlap;
- cross-script transliteration keys, so "revenue" written in Persian letters
  still scores.

Models are ranked by their own label plus their three best items. The
previous model gets +10 so it stays in front. The top `MAX_MODELS` (3) models
are kept, each with its best `TOP_K` (20) items. The default time dimension,
every item of the previous spec and at least one measure are always included.
Small catalogs come back whole.

**Local providers** (the heuristic) skip the cut and get the whole visible
catalog. A project can replace the retriever with `RETRIEVER` (a dotted path).

### Steps 5–6: prompt, structured output, validation, retries

The task given to a provider holds:

- the question;
- the user's language (`en` or `fa`) and calendar (`gregorian` or `jalali`);
- today's date;
- the retrieved catalog;
- the redacted previous spec;
- the earlier failed attempts.

**Prompt contents** (`nl.prompt.build_messages`):

- **System message.** `INSTRUCTIONS`, `CONTEXT` and up to 8 `EXAMPLES`
  (`{question, spec}` pairs from `nl_examples`).
  - `INSTRUCTIONS` cover: use only catalog keys; the period grammar (relative
    periods as *names*, never computed dates); explicit dates copied as
    written; category values as the user wrote them; `@vN` placeholders copied
    unchanged; how to use `compare_to`; top/bottom N; chart choice; when to
    set `clarification_needed`; how to edit a previous spec.
  - `CONTEXT` is JSON with:
    - `TODAY`: the ISO date, the Jalali date `YYYY/MM/DD`, the Jalali month
      name and the weekday;
    - `USER`: language and calendar;
    - `RELATIVE_PERIOD_EXAMPLES`: the list in `calendar.periods.EXAMPLES`;
    - `CATALOG`: the retrieved entries in both languages.
- **User message.** The question, or `PREVIOUS SPEC: … FOLLOW-UP: …` when
  refining.
- **On a retry.** Two more turns are appended for every failed attempt: the
  model's previous raw output as an `assistant` turn, then a `user` turn
  saying "That spec is invalid. Fix these errors…" with the error list (code,
  message, path, offending value).

**Structured output.** With `RESPONSE_FORMAT = "json_schema"` (the default),
the request carries `response_format: {type: "json_schema", json_schema:
{name: "report_spec", schema: <ReportSpec JSON schema>}}`. In that schema,
`semantic_model` is narrowed to an enum of the retrieved model names. No
`strict` flag is sent. See §4 for the automatic fallback to `json_object`.

**Checking each answer** (`_Run.check`):

1. **Parse.** Code fences and chatter around the JSON object are tolerated. If
   no object is found, the error is `output_not_json`.
2. **Clean.** Empty optional keys are dropped. Dates on time dimensions become
   ISO, and whole years or months (`1404`, `1404/03`, `2025`, `2025-03`) are
   expanded to their first and last day by `nl.dates` (Jalali leap years
   included).
3. **Clarification.** If `clarification_needed` is set, the answer is accepted
   as a clarification.
4. **Schema.** `ReportSpec.model_validate`; pydantic errors become
   `schema_error`.
5. **Report validation.** The report validator (`engine.runner.prepare`) runs
   under **the user's permissions**: model permission, field visibility,
   limits. Placeholders are opaque strings and valid wherever a category value
   is. A model the user may not see becomes an error on `semantic_model`.

On errors the provider is called again with the errors fed back, up to
`MAX_RETRIES` (2) extra calls, so 3 calls at most. After that the status is
`invalid`, with the errors.

### Step 7: clarification

A clarification can come from three places:

- the provider (`clarification_needed`);
- value matching (step 8);
- the heuristic's own questions: which data, which measure, "top of what?",
  "which period should I compare?".

The answer then has `status: "clarification"`, the question to show, and the
partial spec when it has measures.

### Deterministic relative periods

The model never does date arithmetic. Relative periods are names such as
`today`, `this_month`, `last_year`, `last_3_months` or `past_30_days`
(`this|last|past` + an optional N + a unit of day, week, month, season or
year). `calendar.periods` resolves them in the **user's calendar**: "this year"
for a Jalali user starts on 1 Farvardin. The spec's `calendar` stays null
unless the user names one («شمسی», "Gregorian"). The comparison span is also
computed by code, in both calendars.

### Step 8: value index and matching (`nl.values`)

For each category or geo dimension, the value index lists the distinct values
**the user may see**, from the user's scoped querysets:

- **Dimension with declared choices.** The choices, with both language labels.
- **Path ending on a relation.** The related objects of the target's scoped
  queryset, by `str()`.
- **Field on a related model.** That field over the related table in the
  user's scope, so "Rasht" matches even with no orders yet.
- **Otherwise.** The distinct values of the scoped semantic queryset.

Each index is capped at `VALUE_INDEX_MAX` (5,000) values. It is cached for
`VALUE_INDEX_TIMEOUT` (600 s), keyed by dimension, scope fingerprint, database
and data versions.

Matching tries, in order:

1. the exact text after normalisation: Arabic ي/ك/ة, Persian and Arabic
   digits, ZWNJ, diacritics, case;
2. a cross-script transliteration key (`تهران` ⇄ `Tehran`, «تهرون» → Tehran);
3. a fuzzy match. It is accepted only at ≥ 0.8 similarity and at least 0.05
   ahead of the runner-up, and it adds a warning.

If the value only exists in another category dimension (a province, not a
city), the filter moves there, with a warning. Otherwise the answer becomes a
clarification with up to five in-scope suggestions. **A value outside the
user's scope is indistinguishable from one that doesn't exist.**

### Step 9: interpretation

`nl.interpret.describe(report, lang)` builds the sentence from the *validated*
report. It includes:

- measures, group-bys and grain (with the calendar's name);
- the resolved period with its dates in the report's calendar;
- the comparison span;
- the filters, with choice labels;
- sort and limit.

Templates are gettext strings, with Persian digits and month names for `fa`.

### Execution, permissions and limits

The NL layer only produces a spec. Running it is the normal report path:

- the SPA calls `/api/reports/run` (or `execute: true` on `/ask`);
- it runs with the user's permissions, visibility, row rules, row and filter
  limits, statement timeout, result cache and background jobs, as M4 defines
  them;
- every run is recorded in `ReportLog`.

### Caching

Provider results are cached in the report cache alias (`GOZAR["REPORT_CACHE"]`)
for `CACHE_TIMEOUT` seconds (3600; 0 turns the cache off). The key is SHA-256
of:

- the normalised question;
- the retrieved catalog JSON, so the user's visibility is part of the key;
- the redacted previous spec;
- today;
- calendar and language;
- the provider's `describe()` (name, model, host);
- `MAX_RETRIES`.

Only accepted answers (a spec or a clarification) are cached, never failures.
Placeholders are cached, and the values are restored per request. Value
matching and final validation **always run fresh**, because they depend on
scope and data. The evaluator bypasses the cache (`use_cache=False`).

### NLLog and opt-in prompt logging

Every call to `ask()` writes a `bi_admin.NLLog` row: refused (403/429) and
failed ones too, unless `GOZAR["AUDIT_REPORTS"]` is False. The row holds:

- user and username;
- the question, language and calendar;
- the previous spec (unredacted: it stays on the server);
- status: `ok`, `clarification`, `invalid`, `denied` or `failed`;
- semantic model, spec, interpretation and clarification;
- provider and model name;
- attempts, cache hit and duration;
- error code and error.

Users with `bi_admin.view_nllog` read it on the **Question log** page or through
`GET /api/audit/questions`, which never returns transcripts.

`transcript` (each attempt's messages and raw output, plus provider errors) is
filled **only** when `LOG_PROMPTS` is on. That is opt-in, meant for evaluation;
the transcript then holds whole prompts (catalog and question).

---

## 3. Privacy guarantees

These hold for the OpenAI-compatible provider, which sends data off the server.
The heuristic and stub providers run in-process and send nothing anywhere.

**What leaves the server** (one HTTPS/HTTP POST to `{BASE_URL}/chat/completions`
per attempt):

- the instructions, today's date (both calendars) and the user's language and
  calendar code;
- catalog **metadata** for the retrieved subset of what the user may see:
  - keys, labels, synonyms and descriptions (both languages);
  - dimension types and measure formats;
  - **declared choices** (value and labels), which a developer wrote in code;
- up to 8 few-shot examples from `nl_examples`, also developer-written. A
  value in them is sent as written;
- **the user's question, verbatim**. If a user types a customer's name, that
  name is sent;
- in a follow-up, the previous spec with category values replaced by `@vN`.
  Choice values, dates, numbers and booleans in it are sent;
- on retries, the model's own previous output and the validation errors about
  it;
- HTTP headers: `Content-Type`, `Accept`, `Authorization: Bearer <key>` (only
  when a key is set) and `EXTRA_HEADERS`.

**What never leaves:**

- rows, aggregates, report results, exports;
- the value index: distinct values are matched on the server **after** the
  model answers;
- stored category values from a previous spec (placeholders are sent instead);
- fields and models the user may not see (they aren't in the catalog), and
  few-shot examples that use them;
- the user's identity: no username, id or e-mail is in the request;
- `NLLog` contents and cached answers.

The API key comes **only from the environment**, from the variable named by
`API_KEY_ENV` (`GOZAR_NL_API_KEY` by default). A literal `API_KEY` in
`GOZAR["NL"]` fails system check `bi_admin.E003`. Neither `describe()`, the
API, `NLLog` nor the cache key contains the key. `describe()` exposes name,
model and host; the API exposes name and model only.

Enforced by tests in `tests/test_nl.py`:

- `test_prompts_contain_no_row_data` and `test_openai_provider_round_trip`
  (a real HTTP round trip to a fake server) check that no city, province,
  customer name, phone or amount from the database appears in any prompt;
- `test_previous_values_are_redacted_and_restored` and
  `test_redaction_keeps_choices_dates_and_numbers` cover redaction;
- `test_hidden_items_never_reach_the_prompt` covers visibility;
- `test_api_key_in_settings_is_a_system_check_error` covers the key rule.

---

## 4. Providers

`get_provider()` picks the provider from `PROVIDER`.

| `PROVIDER` | What answers |
|---|---|
| `auto` (default) | `openai` when both `BASE_URL` and `MODEL` are set, else `heuristic` |
| `openai` | `OpenAICompatibleProvider` |
| `heuristic` | `HeuristicProvider` |
| `stub` | `StubProvider` |
| dotted path | any `Provider` subclass (`generate(task) -> Generation`; set `local = True` only if it runs in-process) |

### `openai`: any OpenAI-compatible `/chat/completions`

- Uses the standard library only (`urllib`), with no extra dependency.
- `POST {BASE_URL}/chat/completions` with `model`, `messages`, `temperature`
  (always sent, `TEMPERATURE` default 0.0) and `response_format` as set by
  `RESPONSE_FORMAT`.
- Reads `choices[0].message.content` and parses the JSON object in it.
- Any server that speaks this protocol works by configuration alone: OpenAI,
  gateways, Ollama, vLLM, and others.

**`RESPONSE_FORMAT` and the automatic fallback** (`providers.py`, `generate`):

| Value | Sent |
|---|---|
| `json_schema` (default) | `{"type": "json_schema", "json_schema": {"name": "report_spec", "schema": …}}`. If the server answers **HTTP 400**, the same request is sent once more with `{"type": "json_object"}`. Any other error, or a 400 on the retry, is a provider error. |
| `json_object` | `{"type": "json_object"}` (JSON mode) |
| `none` | no `response_format`; the prompt alone asks for JSON |

The fallback is decided per call and not remembered. A server that always
rejects `json_schema` costs two HTTP requests per attempt. In that case force
the format:

```sh
export GOZAR_NL_RESPONSE_FORMAT=json_object   # or "none" if JSON mode is rejected too
```

or set `GOZAR["NL"]["RESPONSE_FORMAT"]`. Any other value fails system check
`bi_admin.E004`. A 400 caused by something else (a wrong model name, a
rejected `temperature`) also triggers the `json_object` retry, which then
fails with the same 400.

**Provider errors and fallback:**

| Error | Cause |
|---|---|
| `provider_http_error` | HTTP status; the first 500 characters of the body are kept |
| `provider_unreachable` | DNS, connection, timeout |
| `provider_bad_response` | Invalid JSON, no `choices[0].message` |

With `FALLBACK` on (the default), the request is answered by the heuristic
instead: retrieval is redone for a local provider, and the warning "The
language model is unavailable; the built-in interpreter answered." is added.
`provider` in the answer and in `NLLog` says `heuristic`. The next question
tries the model again. With `FALLBACK` off, `/ask` answers `502` with the
provider's code, and `NLLog` records `failed`.

A **misconfiguration is not covered by the fallback**. `PROVIDER = "openai"`
without `BASE_URL`/`MODEL` raises `provider_not_configured` while the provider
is being built: `/ask` then fails with a server error, and `gozar_nl_eval`
stops with a command error. Use `auto` unless you want this strictness.

The timeout is `TIMEOUT` seconds **per HTTP call**. In the worst case a
question makes (1 + `MAX_RETRIES`) × 2 calls, before the heuristic fallback.

### `heuristic`: offline, deterministic, English and Persian

`nl.heuristic.Interpreter` takes no network and no model. It is local, so it
may read the scoped value index in-process. It parses in this order:

1. **Comparison phrases** (first, so "last year" in "compared to last year"
   isn't read as the report's own period). A cue ("compared to/with", "vs",
   "versus", "against", "relative to", «نسبت به», «در مقایسه با», «قیاس با»)
   followed by a target ("last/previous/prior <unit>", "the <unit> before",
   «<unit> قبل/گذشته/پیش/قبلی», «پارسال», "the previous period", «دوره قبل»,
   "the same period last year", «دوره/مدت مشابه سال قبل», «همین دوره سال
   قبل»). Also «با … مقایسه کن», "year over year"/"yoy", "month over month",
   "week over week", "quarter over quarter" and "period over period".
   - A year means `same_period_last_year`; another unit means
     `previous_period`.
   - Without a period, the current unit to date is used: "revenue vs last
     month" → `this_month` vs the previous period.
   - "The previous period" with no period at all asks which.
   - "without comparison" or «بدون مقایسه» removes it.
2. **Explicit dates**:
   - two dates → between;
   - one date → since, until or that day;
   - a month name, Jalali or Gregorian in either script, with or without a
     year. Without a year it means the most recent such month. Ambiguous
     names (*may*, *mar*, «تیر», «دی», …) need a year or a preposition;
   - a bare year: 1300–1499 is Jalali, 1900–2100 is Gregorian.
3. **Relative periods**:
   - "today", "yesterday", «امروز», «امسال», «پارسال», "ytd", "mtd", «از اول
     سال»;
   - "this/last/past [N] <unit>" and «[N] <unit> گذشته/اخیر», «<unit>
     جاری», «این <unit>»;
   - "N <unit>s ago";
   - "all time" removes the period;
   - "Jalali/شمسی" or "Gregorian/میلادی" sets the calendar.
4. **Number comparisons** on *number dimensions*: "over/above/at least",
   "under/at most", "between N and M", «بیشتر از», «بالای», «کمتر از», «بین … و
   …», «۵ میلیون به بالا», with k/thousand/million/billion and
   «هزار/میلیون/میلیارد». The condition goes to the nearest named number
   dimension, else to the only one.
5. **Top/bottom N** and sort order:
   - "top/best/highest/most", «برتر», «بیشترین», «پرفروش‌ترین», "lowest",
     «کمترین»;
   - N defaults to 10; "which … most" → limit 1;
   - "best selling" and «پرفروش» imply the sales measure;
   - "ascending/descending", «صعودی/نزولی».
6. **Grain and chart words**: daily, weekly, monthly, quarterly/seasonal,
   yearly, and «روزانه … سالانه». "trend" means monthly. Chart words: pie,
   bar, line, area, table, pivot, kpi and their Persian names.
7. **Catalog phrases**, longest match first:
   - measure, dimension, model and choice labels and synonyms;
   - "top customers" is a dimension, while "customers" alone may be the
     measure;
   - "revenue from orders" — the second word qualifies the first, it isn't a
     second measure;
   - "by/per/each", «به تفکیک», «هر» mark a group-by.
8. **Category values** through the scoped value index, with negation
   ("except", «به جز», «غیر از» → `neq`/`not_in`). An unknown word right after
   a category dimension's name is treated as a value («شهر نیویورک»), so value
   matching asks "No City matches …".

**Follow-ups** edit the previous spec instead of starting over:

- new measures or group-bys replace the old ones, or add to them with "also",
  "add", «هم», «اضافه»;
- "remove"/«حذف» drops one;
- filters replace those on the same field; "all cities" or «همه شهرها» drops
  the filter;
- period, grain and calendar replace the old ones;
- a chart that was the default is re-inferred.

**Chart default:** a time series is a line chart (a table with two or more
group-bys); one group-by is a bar chart; none is a KPI; otherwise a table.

**The heuristic asks** when:

- no model fits;
- two models fit equally;
- no measure is recognised;
- two measures or dimensions share a phrase;
- "top N" has no group-by;
- a comparison has no period.

Every unused word is reported as a warning, "Not used: …", so a misreading is
visible.

**Known limits of the heuristic:**

- **Fixed vocabulary.** The cue lists above are all it knows. Anything else
  depends on the semantic layer's labels and synonyms, so better synonyms mean
  better answers.
- **Unknown values are dropped, not asked about**, unless a dimension name
  comes right before them. "orders in New York" gives all orders with "Not
  used: New, York". An out-of-scope value behaves the same way: user `tehran`
  asking "revenue by city in Isfahan" gets their own (Tehran) scope, with
  "Not used: Isfahan".
- **No conditions on measures** (HAVING): "customers with more than 5 orders"
  is not understood. Number comparisons only apply to number dimensions.
- One period, one comparison, one top/bottom: the first match wins. Only the
  first two explicit dates are read. N in "top N" must be below 1000.
- No OR across fields, no text search (`contains`), no arithmetic between
  measures.
- The golden set was written alongside it, so its 100% score is not a measure
  of accuracy (see §7).

### `stub`: tests

`StubProvider(responses)` returns canned answers:

- a dict (question → spec or raw text);
- a list (one answer per call);
- or a callable `(task) → spec | text | Exception`.

It builds the **exact prompt** an LLM would get, and records `calls` and
`prompts`, so tests can inspect what would be sent. It is not local.

---

## 5. Configuration reference

The value of each key is resolved as follows:

1. `settings.GOZAR["NL"][KEY]`, if set and not `None` or `""`;
2. else the environment variable, **only for the keys that have one**;
3. else the default.

Settings win over the environment. Source: `src/bi_admin/nl/conf.py`.

| `GOZAR["NL"]` key | Environment variable | Default | Meaning |
|---|---|---|---|
| `PROVIDER` | `GOZAR_NL_PROVIDER` | `"auto"` | `auto` \| `openai` \| `heuristic` \| `stub` \| dotted path |
| `BASE_URL` | `GOZAR_NL_BASE_URL` | `""` | OpenAI-compatible base URL, without `/chat/completions` |
| `MODEL` | `GOZAR_NL_MODEL` | `""` | Model name sent as `model` |
| `API_KEY_ENV` | — | `"GOZAR_NL_API_KEY"` | **Name** of the environment variable holding the key. The key itself is read from that variable only; leave it empty for keyless local servers. |
| `TIMEOUT` | `GOZAR_NL_TIMEOUT` | `30` | Seconds per HTTP call (float) |
| `TEMPERATURE` | — | `0.0` | Always sent |
| `RESPONSE_FORMAT` | `GOZAR_NL_RESPONSE_FORMAT` | `"json_schema"` | `json_schema` \| `json_object` \| `none` (check `E004`) |
| `EXTRA_HEADERS` | — | `{}` | Extra HTTP headers (no secrets: settings are not secret) |
| `MAX_RETRIES` | — | `2` | Extra provider calls with the validation errors fed back |
| `FALLBACK` | — | `True` | Heuristic answers when the model fails |
| `RETRIEVER` | — | `"bi_admin.nl.catalog.retrieve"` | Dotted path to the retrieval function |
| `TOP_K` | — | `20` | Catalog items per model in the prompt (remote providers) |
| `MAX_MODELS` | — | `3` | Semantic models in the prompt (remote providers) |
| `VALUE_INDEX_MAX` | — | `5000` | Distinct values indexed per dimension |
| `VALUE_INDEX_TIMEOUT` | — | `600` | Value-index cache seconds; `0` rebuilds every time |
| `CACHE_TIMEOUT` | — | `3600` | NL → spec cache seconds; `0` disables it |
| `LOG_PROMPTS` | `GOZAR_NL_LOG_PROMPTS` | `False` | Keep prompts and raw outputs in `NLLog.transcript`. The variable is true for `1`/`true`/`yes`/`on`. |
| `MAX_QUESTION_LENGTH` | — | `500` | Characters; the API also caps the field at 4000 |
| `RATE_LIMIT` | — | `30` | Questions per user per minute through the API; `0` means unlimited |
| (`API_KEY`) | — | — | **Not allowed**: system check `bi_admin.E003` |

Environment variables:

- `GOZAR_NL_PROVIDER`, `GOZAR_NL_BASE_URL`, `GOZAR_NL_MODEL`,
  `GOZAR_NL_TIMEOUT`, `GOZAR_NL_RESPONSE_FORMAT`, `GOZAR_NL_LOG_PROMPTS`;
- `GOZAR_NL_API_KEY`, or whatever `API_KEY_ENV` names.

There are no others. The settings outside `"NL"` that also apply are:

- `GOZAR["AUDIT_REPORTS"]` (off disables `NLLog` too);
- `GOZAR["REPORT_CACHE"]` (the cache alias for the NL cache, the value index
  and the rate-limit counter).

---

## 6. Connecting a model: recipes

Common steps for every recipe:

1. Put the key in the **environment** of the web process and the worker. Never
   put it in `settings.py`, a committed `.env`, `EXTRA_HEADERS` or `BASE_URL`.
   Gozar reads it from `$GOZAR_NL_API_KEY` (or the variable `API_KEY_ENV`
   names) and sends it as `Authorization: Bearer …`.
2. Set `BASE_URL` and `MODEL`. With `PROVIDER = "auto"` that is enough.
3. Run `uv run python example_project/manage.py check`. `E003` means a key
   is in settings; `E004` means a bad `RESPONSE_FORMAT`.
4. Smoke-test one question:
   ```sh
   uv run python example_project/manage.py gozar_nl_eval --only en-001 --verbose-failures
   ```
   The output's first line names the provider and model. If it says
   `heuristic`, the configuration was not picked up. Then open the Ask box: the
   footer should say "Answered by <model>.", and there should be no "language
   model is unavailable" warning.
5. Run the full evaluation (§7) before making the model the default.

### OpenAI

```sh
export GOZAR_NL_BASE_URL=https://api.openai.com/v1
export GOZAR_NL_MODEL=<model name>
export GOZAR_NL_API_KEY=sk-...
```

- The README's example model is `gpt-4o-mini`. Which current OpenAI models
  accept `response_format: json_schema` without `strict` — and whether they
  accept `temperature: 0` — is **verify before use**.
- If `json_schema` is rejected, the automatic `json_object` fallback applies.
  Set `GOZAR_NL_RESPONSE_FORMAT=json_object` to avoid the double request.

### Anthropic Claude through its OpenAI-compatible endpoint

Nothing in Gozar is Claude-specific. This uses the same `openai` provider.

```sh
export GOZAR_NL_BASE_URL=https://api.anthropic.com/v1   # verify before use
export GOZAR_NL_MODEL=claude-sonnet-5-5                 # or claude-opus-5-5, claude-haiku-5-5
export GOZAR_NL_API_KEY=...                             # environment only, never settings
```

- **The API key must only ever live in the environment**: in the process
  environment or a secrets manager that injects it, never in settings,
  committed files, `EXTRA_HEADERS` or `NLLog`.
- **Verify before use:**
  - the base URL;
  - that the endpoint accepts the key as `Authorization: Bearer`;
  - that it serves `POST /chat/completions` with the OpenAI response shape
    (`choices[0].message.content`).
- **Not tested: whether this endpoint accepts `response_format` `json_schema`
  or `json_object`.**
  - If it rejects `json_schema` with a 400, Gozar retries with `json_object`
    automatically.
  - If it rejects both, force `GOZAR_NL_RESPONSE_FORMAT=none`: the prompt
    alone asks for one JSON object, and the parser tolerates code fences and
    surrounding text.
  - If it silently ignores the field, the answers still parse, because
    validation and retries catch malformed specs.
- **Temperature: verify before use.**
  - On the native Messages API, current Claude models (Opus 5.5, Sonnet 5.5,
    Haiku 5.5) reject non-default sampling parameters with a 400.
  - Gozar always sends `temperature` (`TEMPERATURE`, default `0.0`), and that
    can't be turned off by configuration.
  - Untested: whether the compatibility layer passes it through, ignores it or
    rejects it. If it rejects it, every call fails with a 400 (after the
    `json_object` retry), and the heuristic silently answers instead, with the
    "language model is unavailable" warning.
  - Check `NLLog.error`, or run with `FALLBACK: False` to see the 502. Then
    either try `TEMPERATURE` = the model's default (verify which), or make
    Gozar omit the field (a small code change in
    `OpenAICompatibleProvider.body`).
- Model IDs above are current as of 2026-10; choose by evaluation (§7), not by
  price alone.

### An OpenAI-compatible gateway (OpenRouter and similar)

```sh
export GOZAR_NL_BASE_URL=https://openrouter.ai/api/v1   # verify before use
export GOZAR_NL_MODEL=<vendor>/<model>                  # the gateway's model id
export GOZAR_NL_API_KEY=...
```

- Optional attribution headers go in settings, never secrets:
  `GOZAR = {"NL": {"EXTRA_HEADERS": {"HTTP-Referer": "https://bi.example.com",
  "X-Title": "Gozar"}}}`. Header names are **verify before use**.
- Whether a gateway forwards `response_format` to the underlying model depends
  on the gateway and the model: **verify before use**. The fallback and
  `RESPONSE_FORMAT` override work as above.
- A gateway forwards the prompt (§3) to a third party. Check its retention and
  logging policy against your privacy requirements.

### A local Ollama or vLLM server

No key is needed: leave `GOZAR_NL_API_KEY` unset, and no `Authorization`
header is sent.

```sh
# Ollama
export GOZAR_NL_BASE_URL=http://localhost:11434/v1
export GOZAR_NL_MODEL=qwen2.5:7b-instruct      # any pulled model
# vLLM
export GOZAR_NL_BASE_URL=http://localhost:8000/v1
export GOZAR_NL_MODEL=<served model name>
```

- The URLs above are the servers' usual defaults (**verify before use** for
  your deployment).
- Support for `json_schema` versus JSON mode varies by server version
  (**verify before use**). The automatic fallback covers a 400; for older
  servers set `GOZAR_NL_RESPONSE_FORMAT=json_object` (or `none`).
- Small local models are slow on CPU. Raise `GOZAR_NL_TIMEOUT`: every attempt
  must finish within it.
- Nothing leaves the machine or the network, which is the strongest privacy
  option.

---

## 7. Evaluation

### The golden set

`tests/nl_golden/*.json` holds 198 cases:

| File | Cases | Of which expect a clarification | Use `compare_to` |
|---|---|---|---|
| `fa.json` | 130 | 4 | 8 |
| `en.json` | 44 | 3 | 6 |
| `refine.json` (follow-ups, both languages) | 24 | 0 | 3 |

Each file is a JSON list of cases:

```json
{"id": "fa-001", "question": "درآمد به تفکیک شهر",
 "expected": {"semantic_model": "order_metrics", "measures": ["revenue"],
              "dimensions": ["city"], "chart": "bar"},
 "previous": {…},          // optional: the spec being refined
 "tags": ["…"]}            // optional
```

- A case that expects a question back has `"expected": {"clarification": true}`.
- Ids must be unique across files.
- The file name is added as a tag, so `--only fa` selects a whole file.
- Expected category values are those of the **English demo data**
  (`seed_demo`).
- Relative periods are compared as date ranges at a pinned `today`
  (2026-10-18).

### Scoring (`nl.evaluation`)

Each case goes through the real `ask()` pipeline as a given user, with the
cache bypassed. The case is then scored:

- **exact**: the normalised specs are equal (defaults filled, `in` lists and
  filters sorted);
- **semantic**: the following are the same:
  - model;
  - measures and group-bys (as sets);
  - filters (field, op, normalised values);
  - time dimension and grain;
  - the date range of the period;
  - `compare_to`;
  - sort and limit, when the expected spec has them.

  Chart type and calendar are ignored;
- a case expecting a clarification matches when one was asked.

Metrics:

- `exact_match_rate` and `semantic_match_rate`;
- `clarification_rate` (share of answers that asked);
- `clarification_recall` (expected clarifications that were asked);
- `retry_rate` (questions needing more than one call);
- `mean_attempts`;
- `errors`.

### What the offline score means, and what it doesn't

CI (`tests/test_nl_golden.py`) requires the heuristic to reach ≥ 0.95 semantic
and ≥ 0.9 exact, with full clarification recall and no retries. It scores
**1.0** today. The stub reproduces a perfect model and a model that is wrong
once per question, to test the retry mechanics.

**This is a regression guard, not an accuracy figure.** The golden set was
written alongside the heuristic, by the same people, against one demo schema.
A perfect score says that nothing that used to work broke. It says nothing
about how real users phrase questions, or how either interpreter handles
them. Real-world accuracy needs real questions (from `NLLog`) and real
schemas.

### Running against a real model

```sh
export GOZAR_NL_BASE_URL=... GOZAR_NL_MODEL=... GOZAR_NL_API_KEY=...
uv run python example_project/manage.py gozar_nl_eval \
    --verbose-failures --json nl-report-<model>.json
```

The database must be seeded with the English demo data (`seed_demo --users`),
or the expected values won't match.

| Flag | Meaning |
|---|---|
| `--provider openai\|heuristic\|stub\|<dotted path>` | Override `PROVIDER`; `openai` fails fast if `BASE_URL`/`MODEL` are missing |
| `--golden PATH` | A JSON file or a directory (default: `tests/nl_golden`, searched upwards from the working directory) |
| `--user NAME` | Ask as this user (default: the first active superuser). Use a scoped user to test row rules and visibility. |
| `--today 2026-10-18` | Date relative periods resolve against; keep the golden set's date |
| `--language en\|fa` | UI language for clarification texts and interpretations |
| `--only fa,en-001` | Case ids or file names |
| `--limit N` | First N cases, for a cheap smoke run |
| `--json PATH` | Full report: provider, metrics and every outcome with expected and got |
| `--fail-under 0.9` | Exit 1 below this semantic match rate |
| `--verbose-failures` | Print expected, got and the clarification or error for each failure |

**Cost.** Each case makes 1 to 3 attempts, and each attempt may make two HTTP
calls when the `json_schema` fallback triggers. A full run is about 200–1200
calls. Start with `--limit 20`.

**Fallback.** With `FALLBACK` on, a failing model is replaced by the heuristic
per question, which **inflates the score**. For an honest run, set
`GOZAR = {"NL": {"FALLBACK": False}}` in the settings used for evaluation. The
`--json` report also lists every outcome, so outcomes with
`status: "error"` or a provider error stand out.

`LOG_PROMPTS` (`GOZAR_NL_LOG_PROMPTS=1`) keeps every prompt and raw output in
`NLLog.transcript`, which helps when reading why a model failed.

### Growing the golden set from NLLog

`NLLog` keeps every real question: question, language, calendar, previous
spec, status, final spec and clarification. To turn them into cases:

1. **Pick candidates.** Read the Question log (`/questions`) or query it:
   ```python
   from bi_admin.models import NLLog
   qs = NLLog.objects.exclude(status="denied").order_by("-timestamp")
   for log in qs[:200]:
       print(log.pk, log.status, log.language, log.question, log.spec, log.previous_spec)
   ```
   The interesting ones are `invalid` and `clarification` answers, and `ok`
   answers whose report the user immediately changed in the builder (see the
   next `ReportLog` for that user).
2. **Write the expected spec by hand.** Open the logged spec in the builder,
   correct it, and copy the spec. The logged spec is what the interpreter
   produced, not necessarily what the user meant.
3. **Remove personal data** from the question if needed, keeping the phrasing.
   Golden files are committed.
4. **Match the data.** Expected category values must exist in the database the
   evaluation runs against. For questions about production data, keep a
   separate golden directory (e.g. `nl_golden_prod/`, outside the repository
   if it contains real values). Run it against a copy of that database with
   `--golden` and a representative `--user`.
5. **Keep relative periods as names** (`last_3_months`), not dates, so cases
   stay valid at any `--today`. For follow-ups, put the logged
   `previous_spec` in `previous`.
6. **Run both interpreters** on the new cases. A case the heuristic fails is a
   known gap: either extend the heuristic or move the case to a file CI
   doesn't gate on. The CI threshold applies to every file in
   `tests/nl_golden`.

---

## 8. Status, known gaps and "Next time"

### Status (2026-10-20, after M6)

- **No real LLM has been tested.** The OpenAI-compatible client has been tested
  against a fake HTTP server only: request shape, headers, the `json_schema` →
  `json_object` fallback, retries through the conversation, timeouts and
  fallback.
- The default deployment answers with the offline heuristic.
- The heuristic's **100% offline score is a regression guard, not accuracy**
  (§7).

### Known gaps

- **Embedding retrieval** (deferred). Retrieval is lexical, which is fine for
  the demo but untested on catalogs with hundreds of fields. `RETRIEVER` is
  the hook; pgvector is the planned option (M7).
- **Learning from corrections** (deferred). The plan (DESIGN.md §5.5) is that
  user corrections become few-shots and golden candidates. Today `NLLog` keeps
  question and spec, but nothing links a later builder edit to the question,
  and few-shots are only the static `nl_examples`.
- **HAVING-style conditions** ("customers with more than 5 orders"). The spec
  has no filter on measures, so neither interpreter can express them.
- **Sorting by comparison values** ("biggest movers vs last year") is not in
  the spec.
- **Claude-specific unknowns:** `response_format` support and `temperature`
  handling on the compatibility endpoint (§6).

### Next time: checklist

1. **Choose candidate models.** At least one hosted model (e.g. a Claude model
   and an OpenAI model) and one local model (e.g. a Qwen instruct model on
   Ollama or vLLM). Consider Persian quality, latency, price and where the
   data may go (§3).
2. **Set the environment** per §6. Keys go in the environment only. Run
   `manage.py check`, then one question with `gozar_nl_eval --only en-001`.
3. **Run the evaluation** on a seeded demo database with `FALLBACK` off and
   `LOG_PROMPTS` on:
   ```sh
   gozar_nl_eval --verbose-failures --json nl-report-<model>.json
   ```
   Repeat with `--language fa`, and with a scoped `--user` (e.g. `tehran`) to
   see how row scope affects clarifications.
4. **Compare the candidates** on:
   - semantic and exact match rate, in total and per file (`--only fa`,
     `--only en`, `--only refine`);
   - clarification recall and clarification rate (too many questions is also
     a failure);
   - retry rate and mean attempts;
   - latency (`NLLog.duration_ms`);
   - cost per question.

   Read the failures: wrong measure, wrong period, value handling, refusals.
   Check whether `json_schema` was accepted or the fallback triggered (two
   requests per attempt in the server logs).
5. **Add real questions.** Before deciding, add a batch of real questions from
   `NLLog`, or from users, to the golden set (§7) and re-run. The current set
   favours the heuristic.
6. **Decide the default provider.** Options:
   - keep `heuristic` as the default and document one recommended model;
   - recommend a model and keep the heuristic as `FALLBACK`.

   Record the decision, the numbers and the date in DESIGN.md §10, "Decided".
   If the chosen model needs a code change (e.g. omitting `temperature`, a
   `strict` flag, a different `response_format`), make it with tests in
   `tests/test_nl.py`.
7. **Update DESIGN.md "Still open"**: replace the "Default LLM provider"
   bullet with the decision, and update the README's "Asking questions"
   section and this document's §6 and §8.
