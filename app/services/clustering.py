"""Keyword clustering via Claude.

Given a flat list of keywords, ask Claude Haiku to group them into
topical silos (clusters) with a short description of each. Cheap and
fast vs. building an embedding pipeline.
"""
from __future__ import annotations

import json
import logging

from anthropic import Anthropic

from app.config import settings

log = logging.getLogger(__name__)

MODEL = "claude-haiku-4-5-20251001"

SYSTEM_PROMPT = """You are a German-language SEO strategist. Group the
given keyword list into topical silos ("clusters") suitable for a
single pillar+supporting-content strategy. For each cluster output:

- name (short, descriptive)
- intent: one of "info", "commercial", "transactional", "navigational"
- keywords: array of original keywords belonging to this cluster

Each keyword must appear in exactly one cluster. Produce 3-12 clusters
total depending on the input size.

Return ONLY a JSON object: {"clusters": [{...}, {...}]}. No markdown,
no code fences, no prose."""


def cluster_keywords(keywords: list[str], focus_category: str | None = None) -> list[dict]:
    if not keywords:
        return []
    if not settings.anthropic_api_key:
        raise RuntimeError("ANTHROPIC_API_KEY not set")

    client = Anthropic(api_key=settings.anthropic_api_key)
    user = "Keywords:\n" + "\n".join(f"- {k}" for k in keywords[:300])
    if focus_category:
        user += f"\n\nFokus-Nische: {focus_category}"

    resp = client.messages.create(
        model=MODEL,
        max_tokens=2048,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user}],
    )
    text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text").strip()
    try:
        data = json.loads(text)
        return data.get("clusters", [])
    except json.JSONDecodeError:
        log.warning("clustering: non-JSON response, returning single-bucket fallback")
        return [{"name": "ungruppiert", "intent": "info", "keywords": keywords}]
