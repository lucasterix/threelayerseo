"""Keyword clustering via OpenAI.

Given a flat list of keywords, group them into topical silos (clusters)
with intent labels. Cheap and fast vs. building an embedding pipeline.
"""
from __future__ import annotations

import logging

from app.services.llm import complete_json

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are a German-language SEO strategist. Group the
given keyword list into topical silos ("clusters") suitable for a
single pillar+supporting-content strategy. For each cluster output:

- name (short, descriptive)
- intent: one of "info", "commercial", "transactional", "navigational"
- keywords: array of original keywords belonging to this cluster

Each keyword must appear in exactly one cluster. Produce 3-12 clusters
total depending on the input size.

Return ONLY a JSON object of the form:
{"clusters": [{"name": "...", "intent": "info", "keywords": [...]}, ...]}"""


def cluster_keywords(keywords: list[str], focus_category: str | None = None) -> list[dict]:
    if not keywords:
        return []
    user = "Keywords:\n" + "\n".join(f"- {k}" for k in keywords[:300])
    if focus_category:
        user += f"\n\nFokus-Nische: {focus_category}"

    try:
        data = complete_json(SYSTEM_PROMPT, user, max_tokens=3000, strict=False)
    except Exception as e:  # noqa: BLE001
        log.warning("clustering LLM failed: %s", e)
        return [{"name": "ungruppiert", "intent": "info", "keywords": keywords}]

    clusters = data.get("clusters") if isinstance(data, dict) else None
    if not clusters:
        return [{"name": "ungruppiert", "intent": "info", "keywords": keywords}]
    return clusters
