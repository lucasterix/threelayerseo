"""Bulk domain → category classifier (OpenAI).

Given a list of domain names, ask the LLM to score each against the
full category catalog and suggest a best-fit label. Cheap (fits
comfortably inside the 1M free tokens/day OpenAI grant) and
deterministic enough for auto-tagging on bulk imports.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from app.categories import all_categories
from app.services.llm import complete_json

log = logging.getLogger(__name__)


@dataclass
class CategoryFit:
    domain: str
    scores: dict[str, int]
    top_category: str
    top_score: int
    reasoning: str
    confidence: str


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

Ausgabe: strikt JSON-Objekt mit einem Key "results" als Array.
Jedes Array-Element hat exakt die Felder:
{{"domain": "...", "scores": {{"healthcare": 90, ...}}, "top_category": "healthcare",
  "top_score": 90, "reasoning": "...", "confidence": "high"}}"""

    user = "Domains:\n" + "\n".join(f"- {d}" for d in domains[:60])
    return system, user


def score_bulk(domains: list[str]) -> list[CategoryFit]:
    if not domains:
        return []
    system, user = _build_prompt(domains)
    try:
        data = complete_json(system, user, max_tokens=4096, strict=False)
    except Exception as e:  # noqa: BLE001
        log.warning("category fit LLM failed: %s", e)
        return []

    rows = data.get("results") if isinstance(data, dict) else []
    out: list[CategoryFit] = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        scores = {
            k: int(v) for k, v in (row.get("scores") or {}).items()
            if isinstance(v, (int, float))
        }
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
