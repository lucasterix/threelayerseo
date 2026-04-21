"""Bulk domain → category classifier.

Given a list of domain names, ask Claude Haiku to score each against
the full category catalog and suggest a best-fit label. Cheap (~$0.01
for 60 domains) and deterministic enough for auto-tagging on bulk
imports.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass

from anthropic import Anthropic

from app.categories import all_categories
from app.config import settings

log = logging.getLogger(__name__)

MODEL = "claude-haiku-4-5-20251001"


@dataclass
class CategoryFit:
    domain: str
    scores: dict[str, int]          # category_key -> 0..100
    top_category: str
    top_score: int
    reasoning: str
    confidence: str                 # "high" | "medium" | "low"


def _build_prompt(domains: list[str]) -> tuple[str, str]:
    cats = all_categories()
    cat_lines = "\n".join(f"- {c.key}: {c.label}" for c in cats)
    system = f"""Du klassifizierst deutsche Domainnamen nach inhaltlicher
Passung zu vorgegebenen Kategorien. Verfügbare Kategorien:

{cat_lines}

Für jede Domain: prüfe die Wörter im Namen (semantisch zerlegen, dt./engl.),
ordne Scores von 0-100 zu jeder Kategorie zu, wähle die beste Kategorie
("top_category") und gib eine knappe Begründung. Confidence: "high" wenn
eine Kategorie klar ≥70 ist, "medium" bei 40-70, "low" sonst.

Ausgabe: JSON-Array, eine Zeile pro Eingabe-Domain, exakt in der Form:
{{"domain": "...", "scores": {{"healthcare": 90, ...}}, "top_category": "healthcare", "top_score": 90, "reasoning": "...", "confidence": "high"}}

Nur das JSON-Array, keine Prosa, keine Code-Fences."""

    user = "Domains:\n" + "\n".join(f"- {d}" for d in domains[:60])
    return system, user


def score_bulk(domains: list[str]) -> list[CategoryFit]:
    if not domains:
        return []
    if not settings.anthropic_api_key:
        raise RuntimeError("ANTHROPIC_API_KEY not set")

    system, user = _build_prompt(domains)
    client = Anthropic(api_key=settings.anthropic_api_key)
    try:
        resp = client.messages.create(
            model=MODEL,
            max_tokens=4096,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
    except Exception as e:  # noqa: BLE001
        log.warning("category fit LLM failed: %s", e)
        return []

    text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text").strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()
    try:
        rows = json.loads(text)
    except json.JSONDecodeError:
        log.warning("category fit: invalid JSON from Haiku")
        return []

    out: list[CategoryFit] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        scores = {k: int(v) for k, v in (row.get("scores") or {}).items() if isinstance(v, (int, float))}
        top = row.get("top_category") or (max(scores, key=scores.get) if scores else "other")
        out.append(
            CategoryFit(
                domain=str(row.get("domain", "")),
                scores=scores,
                top_category=str(top),
                top_score=int(row.get("top_score") or scores.get(top, 0)),
                reasoning=str(row.get("reasoning") or ""),
                confidence=str(row.get("confidence") or "medium"),
            )
        )
    return out
