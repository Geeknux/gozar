"""NL providers: turn a :class:`Task` (question + catalog context) into spec JSON.

* :class:`OpenAICompatibleProvider` — any ``/chat/completions`` endpoint:
  OpenAI and other hosted APIs, or local servers such as Ollama and vLLM, by
  configuration alone (``GOZAR["NL"]`` / ``GOZAR_NL_*`` environment variables).
* :class:`~bi_admin.nl.heuristic.HeuristicProvider` — deterministic and offline;
  used when no LLM is configured.
* :class:`StubProvider` — canned answers for tests; it still builds the exact
  prompt an LLM would get, so tests can inspect it.

A provider only ever receives catalog metadata and the user's text. Local
providers (``local = True``, i.e. they run in this process) may additionally
use the server-side value index through ``task.values``.
"""

from __future__ import annotations

import datetime as dt
import json
import re
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from django.utils.module_loading import import_string

from bi_admin.nl import prompt
from bi_admin.nl.catalog import ModelEntry
from bi_admin.nl.conf import api_key, nl_setting


@dataclass
class Attempt:
    raw: str
    errors: list[dict[str, Any]]


@dataclass
class Task:
    question: str
    language: str  # the user's UI language (en|fa)
    calendar: str  # the user's calendar (gregorian|jalali)
    today: dt.date
    catalog: list[ModelEntry]
    previous: dict[str, Any] | None = None  # redacted previous spec (refinement)
    attempts: list[Attempt] = field(default_factory=list)
    # local providers only: model name → ValueMatcher (server-side value index)
    values: Callable[[str], Any] | None = None


@dataclass
class Generation:
    data: dict[str, Any] | None  # parsed JSON object, None when the output wasn't one
    raw: str
    messages: list[dict[str, str]] = field(default_factory=list)  # for opt-in logging
    notes: list[str] = field(default_factory=list)  # shown to the user as warnings


class ProviderError(Exception):
    """The provider could not answer (network, HTTP error, bad response)."""

    def __init__(self, code: str, message: str, status: int | None = None):
        super().__init__(message)
        self.code = code
        self.status = status


class Provider:
    name = "base"
    local = False  # True: runs in-process and may read the server-side value index
    model = ""

    def generate(self, task: Task) -> Generation:  # pragma: no cover - interface
        raise NotImplementedError

    def describe(self) -> dict[str, Any]:
        return {"name": self.name, "model": self.model, "local": self.local}


_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)


def parse_json(raw: str) -> dict[str, Any] | None:
    """The JSON object in a model's answer (code fences and chatter tolerated)."""
    text = _FENCE.sub("", (raw or "").strip())
    try:
        data = json.loads(text)
    except ValueError:
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            data = json.loads(text[start : end + 1])
        except ValueError:
            return None
    return data if isinstance(data, dict) else None


class OpenAICompatibleProvider(Provider):
    """Chat-completions client (``POST {base_url}/chat/completions``), stdlib only."""

    name = "openai"

    def __init__(
        self,
        base_url: str | None = None,
        model: str | None = None,
        api_key: str | None = None,
        timeout: float | None = None,
        temperature: float | None = None,
        response_format: str | None = None,
        extra_headers: dict[str, str] | None = None,
    ):
        self.base_url = (base_url or nl_setting("BASE_URL") or "").rstrip("/")
        self.model = model or nl_setting("MODEL") or ""
        self.api_key = api_key if api_key is not None else _api_key()
        self.timeout = float(timeout if timeout is not None else nl_setting("TIMEOUT"))
        self.temperature = float(
            temperature if temperature is not None else nl_setting("TEMPERATURE")
        )
        self.response_format = response_format or nl_setting("RESPONSE_FORMAT") or "none"
        self.extra_headers = dict(extra_headers or nl_setting("EXTRA_HEADERS") or {})
        if not self.base_url or not self.model:
            raise ProviderError(
                "provider_not_configured",
                'Set GOZAR["NL"]["BASE_URL"] and ["MODEL"] (or GOZAR_NL_BASE_URL and '
                "GOZAR_NL_MODEL) to use an OpenAI-compatible model",
            )

    def describe(self) -> dict[str, Any]:
        # never the key; the host only (a URL may carry credentials in its query)
        host = urllib.request.urlparse(self.base_url).netloc
        return {"name": self.name, "model": self.model, "local": False, "host": host}

    def headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        headers.update(self.extra_headers)
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def body(self, task: Task, messages: list[dict[str, str]], fmt: str) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
        }
        if fmt == "json_schema":
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "report_spec", "schema": prompt.response_schema(task)},
            }
        elif fmt == "json_object":
            body["response_format"] = {"type": "json_object"}
        return body

    def post(self, url: str, payload: dict[str, Any]) -> dict[str, Any]:
        """One HTTP call; tests replace this or point ``base_url`` at a local server."""
        request = urllib.request.Request(
            url,
            data=json.dumps(payload, ensure_ascii=False).encode(),
            headers=self.headers(),
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as resp:  # noqa: S310
                return json.loads(resp.read().decode() or "{}")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:500]
            raise ProviderError(
                "provider_http_error", f"LLM server answered {exc.code}: {detail}", exc.code
            ) from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            raise ProviderError(
                "provider_unreachable", f"LLM server unreachable: {reason}"
            ) from None
        except ValueError:
            raise ProviderError("provider_bad_response", "LLM server sent invalid JSON") from None

    def generate(self, task: Task) -> Generation:
        messages = prompt.build_messages(task)
        url = f"{self.base_url}/chat/completions"
        fmt = self.response_format
        try:
            response = self.post(url, self.body(task, messages, fmt))
        except ProviderError as exc:
            # Servers without structured outputs reject json_schema: fall back to JSON mode.
            if exc.status != 400 or fmt != "json_schema":
                raise
            response = self.post(url, self.body(task, messages, "json_object"))
        try:
            raw = response["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError):
            raise ProviderError("provider_bad_response", "No message in the LLM response") from None
        return Generation(parse_json(raw), raw, messages)


def _api_key() -> str:
    return api_key()


class StubProvider(Provider):
    """Canned answers: a dict (question → spec/raw text), a list (one per call) or a
    callable ``(task) → spec dict | raw text``. Records every task and prompt."""

    name = "stub"

    def __init__(self, responses: Any = None, model: str = "stub"):
        self.responses = responses if responses is not None else {}
        self.model = model
        self.calls: list[Task] = []
        self.prompts: list[list[dict[str, str]]] = []
        self._index = 0

    def _next(self, task: Task) -> Any:
        if callable(self.responses):
            return self.responses(task)
        if isinstance(self.responses, list):
            out = self.responses[min(self._index, len(self.responses) - 1)]
            self._index += 1
            return out
        return self.responses.get(task.question)

    def generate(self, task: Task) -> Generation:
        messages = prompt.build_messages(task)
        self.calls.append(task)
        self.prompts.append(messages)
        out = self._next(task)
        if isinstance(out, Exception):
            raise out
        if out is None:
            return Generation(None, "", messages)
        raw = out if isinstance(out, str) else json.dumps(out, ensure_ascii=False)
        return Generation(parse_json(raw), raw, messages)


PROVIDERS = {
    "openai": OpenAICompatibleProvider,
    "heuristic": "bi_admin.nl.heuristic.HeuristicProvider",
    "stub": StubProvider,
}


def llm_configured() -> bool:
    return bool(nl_setting("BASE_URL") and nl_setting("MODEL"))


def get_provider(name: str | None = None) -> Provider:
    """The configured provider (``GOZAR["NL"]["PROVIDER"]``)."""
    name = name or nl_setting("PROVIDER") or "auto"
    if name == "auto":
        name = "openai" if llm_configured() else "heuristic"
    target = PROVIDERS.get(name, name)
    cls = import_string(target) if isinstance(target, str) else target
    return cls()
