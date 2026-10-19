"""``GOZAR["NL"]`` options and their environment-variable fallbacks.

Each key resolves to ``settings.GOZAR["NL"][key]`` when set, else the
environment variable ``GOZAR_NL_<KEY>`` (for the keys listed in ``ENV``), else
the default below. The API key is read **only** from the environment, from the
variable named by ``API_KEY_ENV`` (``GOZAR_NL_API_KEY`` by default); a literal
key in settings is refused by system check ``bi_admin.E003``.
"""

from __future__ import annotations

import os
from typing import Any

from django.conf import settings

DEFAULTS: dict[str, Any] = {
    # "auto": the OpenAI-compatible client when BASE_URL and MODEL are configured,
    # else the offline heuristic. Or "openai", "heuristic", "stub", or a dotted path
    # to a Provider subclass.
    "PROVIDER": "auto",
    "BASE_URL": "",  # e.g. https://api.openai.com/v1, http://localhost:11434/v1 (Ollama)
    "MODEL": "",  # e.g. gpt-4o-mini, qwen2.5:7b-instruct
    "API_KEY_ENV": "GOZAR_NL_API_KEY",  # name of the environment variable holding the key
    "TIMEOUT": 30,  # seconds per LLM call
    "TEMPERATURE": 0.0,
    # "json_schema" (structured outputs), "json_object" (JSON mode) or "none"
    "RESPONSE_FORMAT": "json_schema",
    "EXTRA_HEADERS": {},  # e.g. {"HTTP-Referer": ...} for some gateways; no secrets here
    "MAX_RETRIES": 2,  # extra LLM calls with the validation errors fed back
    "FALLBACK": True,  # use the heuristic when the LLM can't be reached
    # retrieval
    "RETRIEVER": "bi_admin.nl.catalog.retrieve",
    "TOP_K": 20,  # catalog items per semantic model in the prompt
    "MAX_MODELS": 3,  # semantic models in the prompt
    # value matching
    "VALUE_INDEX_MAX": 5000,  # distinct values indexed per dimension
    "VALUE_INDEX_TIMEOUT": 600,  # seconds; 0: rebuild every time
    # cache, logging, limits
    "CACHE_TIMEOUT": 3600,  # NL → spec cache in seconds; 0 disables it
    "LOG_PROMPTS": False,  # opt-in: keep prompts and raw outputs in NLLog.transcript
    "MAX_QUESTION_LENGTH": 500,
    "RATE_LIMIT": 30,  # questions per user per minute; 0: unlimited
}

ENV = {
    "PROVIDER": "GOZAR_NL_PROVIDER",
    "BASE_URL": "GOZAR_NL_BASE_URL",
    "MODEL": "GOZAR_NL_MODEL",
    "TIMEOUT": "GOZAR_NL_TIMEOUT",
    "RESPONSE_FORMAT": "GOZAR_NL_RESPONSE_FORMAT",
    "LOG_PROMPTS": "GOZAR_NL_LOG_PROMPTS",
}
_CAST = {"TIMEOUT": float, "LOG_PROMPTS": lambda v: v.lower() in ("1", "true", "yes", "on")}


def configured() -> dict[str, Any]:
    return dict((getattr(settings, "GOZAR", None) or {}).get("NL") or {})


def nl_setting(name: str) -> Any:
    config = configured()
    if name in config and config[name] is not None and config[name] != "":
        return config[name]
    env = ENV.get(name)
    if env and os.environ.get(env):
        raw = os.environ[env]
        return _CAST.get(name, str)(raw)
    return DEFAULTS.get(name)


def api_key() -> str:
    """The LLM API key from the environment (empty for keyless local servers)."""
    return os.environ.get(str(nl_setting("API_KEY_ENV") or ""), "")
