"""Bulk domain → category + tier classifier (OpenAI).

One LLM call classifies in two dimensions:
1. Category (healthcare / life-science / ...) — existing behaviour.
2. Tier (1 bad / 2 medium / 3 good) — new. Uses domain-name heuristics
   baked into the prompt (keyword-stuffed → tier 1, brandable → tier 3).

One OpenAI JSON call per batch (up to 60 domains). Cheap, fits in the
1M-free-tokens-a-day grant, runs in ~3-5s.
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
    # Tier recommendation (new in the combined classifier)
    tier_recommendation: int | None = None     # 1 / 2 / 3
    tier_reasoning: str | None = None
    tier_confidence: str | None = None


def _build_prompt(domains: list[str]) -> tuple[str, str]:
    cats = all_categories()
    cat_lines = "\n".join(f"- {c.key}: {c.label}" for c in cats)
    system = f"""Du klassifizierst deutsche Domainnamen in zwei Dimensionen:

### Dimension 1: Kategorie
Verfügbare Kategorien:
{cat_lines}

Für jede Domain: prüfe die Wörter im Namen (semantisch zerlegen, dt./engl.),
ordne Scores von 0-100 zu jeder Kategorie zu, wähle die beste Kategorie
("top_category") und gib eine knappe Begründung. Confidence: "high" wenn
eine Kategorie klar ≥70 ist, "medium" bei 40-70, "low" sonst.

### Dimension 2: Tier (SEO-Qualitätsstufe)
- **Tier 1 (bad/PBN):** keyword-gestopft, austauschbar, "cheap feel".
  Beispiele: best-xyz-tipps.info, mega-produkt-online.de, xyz-tipps-24.com
- **Tier 2 (medium):** halbwegs lesbar, leicht generisch, brauchbar als
  Support-Content-Site. Beispiele: ernaehrungs-ratgeber.de, finanzen-portal.com
- **Tier 3 (good):** einprägsam, brandable, würde als echte Marke durchgehen.
  Oft ein einzelnes deutsches Wort, Wortspiel oder kurzes Kunstwort.
  Beispiele: froehlichdienste.de, pflege-spielzeit.de, medikura.com

Für jede Domain:
- "tier_recommendation": 1, 2 oder 3 — Empfehlung
- "tier_reasoning": knapper Satz warum
- "tier_confidence": "high" / "medium" / "low"

### Output
Strikt JSON-Objekt mit einem Key "results" als Array. Jedes Array-Element
hat exakt diese Felder:
{{"domain": "...",
  "scores": {{"healthcare": 90, "pharma": 30, ...}},
  "top_category": "healthcare",
  "top_score": 90,
  "reasoning": "enthält 'gesundheit'...",
  "confidence": "high",
  "tier_recommendation": 2,
  "tier_reasoning": "lesbar aber generisch",
  "tier_confidence": "high"}}"""

    user = "Domains:\n" + "\n".join(f"- {d}" for d in domains[:60])
    return system, user


def score_bulk(domains: list[str]) -> list[CategoryFit]:
    if not domains:
        return []
    system, user = _build_prompt(domains)
    try:
        data = complete_json(system, user, max_tokens=4096, strict=False)
    except Exception as e:  # noqa: BLE001
        log.warning("classifier LLM failed: %s", e)
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
        tier_raw = row.get("tier_recommendation")
        try:
            tier_val: int | None = int(tier_raw) if tier_raw is not None else None
            if tier_val not in (1, 2, 3):
                tier_val = None
        except (ValueError, TypeError):
            tier_val = None
        out.append(
            CategoryFit(
                domain=str(row.get("domain", "")),
                scores=scores,
                top_category=str(top),
                top_score=int(row.get("top_score") or scores.get(top, 0)),
                reasoning=str(row.get("reasoning") or ""),
                confidence=str(row.get("confidence") or "medium"),
                tier_recommendation=tier_val,
                tier_reasoning=str(row.get("tier_reasoning") or "") or None,
                tier_confidence=str(row.get("tier_confidence") or "") or None,
            )
        )
    return out
