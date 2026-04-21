"""Unified LLM helper — OpenAI primary, Anthropic Haiku fallback.

Writer (Claude Opus) stays on Anthropic since quality per token is much
higher for long-form prose, but everything else — homepage copy, legal,
category classification, clustering, chart config, auto-research
brainstorms — now runs through a single path.

Why two providers:
- Daniel has 1M free tokens/day via the OpenAI data-sharing programme
  once it's enabled (primary).
- Anthropic Haiku as automatic fallback while the OpenAI quota is
  unresolved, so the system keeps working.
"""
from __future__ import annotations

import json
import logging
from typing import Any

import openai
from openai import OpenAI

from app.config import settings

log = logging.getLogger(__name__)

HAIKU_MODEL = "claude-haiku-4-5-20251001"


class LlmError(RuntimeError):
    pass


def _openai_client() -> OpenAI:
    if not settings.openai_api_key:
        raise LlmError("OPENAI_API_KEY not set")
    return OpenAI(api_key=settings.openai_api_key)


def _token_param_name(model: str) -> str:
    if model.startswith(("gpt-5", "o1", "o3", "o4")):
        return "max_completion_tokens"
    return "max_tokens"


def _openai_complete(
    system: str,
    user: str,
    *,
    model: str,
    max_tokens: int,
    temperature: float | None,
    json_mode: bool,
) -> str:
    client = _openai_client()
    kwargs: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        _token_param_name(model): max_tokens,
    }
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}
    if temperature is not None and not model.startswith(("o1", "o3", "o4")):
        kwargs["temperature"] = temperature
    resp = client.chat.completions.create(**kwargs)
    return (resp.choices[0].message.content or "").strip()


def _anthropic_complete(
    system: str,
    user: str,
    *,
    max_tokens: int,
    json_mode: bool,
) -> str:
    """Haiku fallback. For JSON requests we append a marker to the system
    prompt so Claude knows to emit JSON — it doesn't have a native mode.
    """
    from anthropic import Anthropic

    if not settings.anthropic_api_key:
        raise LlmError("ANTHROPIC_API_KEY not set (and OpenAI fallback unavailable)")
    client = Anthropic(api_key=settings.anthropic_api_key)
    sys_text = system
    if json_mode:
        sys_text += (
            "\n\nIMPORTANT: Output must be a single valid JSON object. "
            "No prose before or after. No Markdown code fences."
        )
    resp = client.messages.create(
        model=HAIKU_MODEL,
        max_tokens=max_tokens,
        system=sys_text,
        messages=[{"role": "user", "content": user}],
    )
    text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text").strip()
    # Strip code fences if Claude added them despite instructions.
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()
    return text


def _is_quota_error(exc: Exception) -> bool:
    if isinstance(exc, openai.RateLimitError):
        return True
    msg = str(exc).lower()
    return any(
        needle in msg
        for needle in ("insufficient_quota", "exceeded your current quota", "rate limit")
    )


def complete_text(
    system: str,
    user: str,
    *,
    model: str | None = None,
    max_tokens: int = 2000,
    temperature: float | None = None,
) -> str:
    model = model or settings.openai_simple_model
    if settings.openai_api_key:
        try:
            return _openai_complete(
                system, user,
                model=model, max_tokens=max_tokens,
                temperature=temperature, json_mode=False,
            )
        except Exception as e:  # noqa: BLE001
            if not _is_quota_error(e):
                raise
            log.warning("openai quota hit, falling back to anthropic haiku")
    return _anthropic_complete(system, user, max_tokens=max_tokens, json_mode=False)


def complete_json(
    system: str,
    user: str,
    *,
    model: str | None = None,
    max_tokens: int = 2000,
    strict: bool = True,
) -> Any:
    model = model or settings.openai_simple_model
    text: str | None = None
    if settings.openai_api_key:
        try:
            text = _openai_complete(
                system, user,
                model=model, max_tokens=max_tokens,
                temperature=None, json_mode=True,
            )
        except Exception as e:  # noqa: BLE001
            if not _is_quota_error(e):
                raise
            log.warning("openai quota hit, JSON-fallback to anthropic haiku")
    if text is None:
        text = _anthropic_complete(
            system, user, max_tokens=max_tokens, json_mode=True
        )
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        if strict:
            raise LlmError(f"non-JSON response: {text[:200]}") from e
        log.warning("complete_json: non-JSON, returning {}: %s", text[:200])
        return {}


def is_configured() -> bool:
    return bool(settings.openai_api_key or settings.anthropic_api_key)
