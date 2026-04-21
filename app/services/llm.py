"""Unified OpenAI helper for simple classification / brainstorm / draft tasks.

Writer (Claude Opus) stays on Anthropic since quality per token is much
higher for long-form prose, but everything else — homepage copy, legal,
category classification, clustering, chart config, auto-research
brainstorms — now runs through a single OpenAI call path.

Why OpenAI for the simple stuff:
- Daniel has 1M free tokens/day via the OpenAI data-sharing programme
- gpt-4o-mini / gpt-5-nano are fast, cheap, and good enough for these tasks
- one code path to tune / retry / fall back across services
"""
from __future__ import annotations

import json
import logging
from typing import Any

from openai import OpenAI

from app.config import settings

log = logging.getLogger(__name__)


class LlmError(RuntimeError):
    pass


def _client() -> OpenAI:
    if not settings.openai_api_key:
        raise LlmError("OPENAI_API_KEY not set")
    return OpenAI(api_key=settings.openai_api_key)


def _token_param_name(model: str) -> str:
    """GPT-5 / o-series use max_completion_tokens; older models max_tokens."""
    if model.startswith(("gpt-5", "o1", "o3", "o4")):
        return "max_completion_tokens"
    return "max_tokens"


def complete_text(
    system: str,
    user: str,
    *,
    model: str | None = None,
    max_tokens: int = 2000,
    temperature: float | None = None,
) -> str:
    """Plain text chat completion. Returns the assistant message content."""
    client = _client()
    model = model or settings.openai_simple_model
    kwargs: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        _token_param_name(model): max_tokens,
    }
    # Reasoning models (o1/o3/o4) don't accept temperature; GPT-5 nano
    # accepts only temperature=1. Play it safe: only set when explicitly
    # requested AND not on reasoning models.
    if temperature is not None and not model.startswith(("o1", "o3", "o4")):
        kwargs["temperature"] = temperature
    resp = client.chat.completions.create(**kwargs)
    return (resp.choices[0].message.content or "").strip()


def complete_json(
    system: str,
    user: str,
    *,
    model: str | None = None,
    max_tokens: int = 2000,
    strict: bool = True,
) -> Any:
    """JSON-mode completion. Guarantees the response parses, or raises
    LlmError. If ``strict=False`` we return an empty dict on parse failure
    instead of raising — useful for best-effort paths that degrade to
    no-chart / no-cluster without killing the caller.

    OpenAI JSON mode only guarantees a JSON OBJECT, not an array. Services
    that want arrays should wrap them under a key (e.g. {"items": [...]})
    in their prompt and unwrap here.
    """
    client = _client()
    model = model or settings.openai_simple_model
    kwargs: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "response_format": {"type": "json_object"},
        _token_param_name(model): max_tokens,
    }
    resp = client.chat.completions.create(**kwargs)
    text = (resp.choices[0].message.content or "").strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        if strict:
            raise LlmError(f"non-JSON response: {text[:200]}") from e
        log.warning("complete_json: non-JSON, returning {}: %s", text[:200])
        return {}


def is_configured() -> bool:
    return bool(settings.openai_api_key)
