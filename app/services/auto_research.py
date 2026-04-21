"""One-seed automated research orchestrator.

Given a single seed phrase ("diabetes tipps", "hundefutter"), runs the
full funnel in one call:

  1. Expand the seed to 30+ related keywords via DataForSEO keyword_ideas
     (fallback: OpenAI brainstorm if DataForSEO isn't reachable / verified).
  2. Cluster those keywords into topical silos via OpenAI.
  3. Ask OpenAI to invent 15-20 domain-name candidates (TLD-free) that
     match the niche and the winning cluster.
  4. Cartesian-multiply candidates × configured TLDs.
  5. Bulk-check availability at INWX.
  6. Score every *available* candidate with the category-fit classifier
     so the UI can pick a badge.

Every step degrades gracefully.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from app.services.category_fit import CategoryFit, score_bulk
from app.services.clustering import cluster_keywords
from app.services.domains import DEFAULT_TLDS, check_availability
from app.services.keywords import KeywordIdea, keyword_ideas
from app.services.llm import complete_json

log = logging.getLogger(__name__)


_DOMAIN_BRAINSTORM_SYSTEM = """Du generierst kurze, lesbare Domain-Namen
(ohne TLD) für deutschsprachige SEO-Blogs in einer bestimmten Nische.

Regeln:
- Lesbar, ohne Zahlen, Bindestriche sparsam (max 1 pro Name).
- Deutsche Wörter bevorzugt, englische nur wenn idiomatisch (z.B. "guide").
- 4-20 Zeichen pro Name, inklusive Bindestrich.
- Hauptkeyword der Nische sollte in vielen Namen vorkommen.
- Keine existierenden Marken.
- Mische Typen: Guide/Ratgeber/Tipps/Wissen/Info/Blog/Magazin/Portal.

Gib ein JSON-Objekt der Form {"names": ["...", "..."]} zurück,
15-20 Einträge, nur lowercase-Strings."""


_KEYWORD_BRAINSTORM_SYSTEM = """Erzeuge 30 verwandte deutsche Suchbegriffe
zu einem Seed. Intent-Mix: je ein Drittel informational (Was/Wie/Warum),
commercial (beste / vergleich / test) und transactional (kaufen / preis).
Ausgabe: JSON-Objekt der Form {"keywords": ["...", "..."]}, 30 Einträge."""


@dataclass
class DomainCandidate:
    name: str
    available: bool
    price_cents: int | None
    currency: str
    category: str | None = None
    category_score: int = 0
    category_confidence: str | None = None
    category_reasoning: str | None = None


@dataclass
class AutoResearchResult:
    seed: str
    category_hint: str | None
    keywords: list[KeywordIdea]
    clusters: list[dict]
    domain_candidates: list[DomainCandidate] = field(default_factory=list)
    tlds: list[str] = field(default_factory=list)
    used_fallback_keywords: bool = False


def _haiku_brainstorm_keywords(seed: str) -> list[str]:
    """Historical name kept so deep_research_job's import still works.
    Runs through OpenAI now.
    """
    try:
        data = complete_json(
            _KEYWORD_BRAINSTORM_SYSTEM,
            f"Seed: {seed}",
            max_tokens=1500,
            strict=False,
        )
    except Exception as e:  # noqa: BLE001
        log.warning("keyword brainstorm failed: %s", e)
        return []
    items = (data or {}).get("keywords") or []
    return [str(x).lower() for x in items if isinstance(x, str)][:40]


def _haiku_domain_brainstorm(
    seed: str,
    keywords: list[str],
    category_hint: str | None = None,
) -> list[str]:
    prompt = (
        f"Nische/Seed: {seed}\n"
        f"Beispiel-Keywords aus der Nische: {', '.join(keywords[:25])}\n"
    )
    if category_hint:
        prompt += f"Kategorie-Fokus: {category_hint}\n"
    prompt += "Bitte generiere jetzt die Domain-Namen."
    try:
        data = complete_json(
            _DOMAIN_BRAINSTORM_SYSTEM, prompt, max_tokens=1200, strict=False
        )
    except Exception as e:  # noqa: BLE001
        log.warning("domain brainstorm failed: %s", e)
        return []
    items = (data or {}).get("names") or []
    out: list[str] = []
    seen: set[str] = set()
    for x in items:
        if not isinstance(x, str):
            continue
        s = x.strip().lower()
        s = re.sub(r"^https?://", "", s)
        s = s.split("/")[0].split(".")[0]
        s = re.sub(r"[^a-z0-9-]", "-", s).strip("-")
        if 4 <= len(s) <= 40 and s not in seen:
            seen.add(s)
            out.append(s)
    return out[:20]


def run(
    seed: str,
    category_hint: str | None = None,
    tlds: list[str] | None = None,
    keyword_count: int = 30,
) -> AutoResearchResult:
    tlds = tlds or ["de", "com", "info"]

    keywords: list[KeywordIdea] = []
    used_fallback = False
    try:
        keywords = keyword_ideas(seed, limit=keyword_count)
    except Exception:  # noqa: BLE001
        log.warning("DataForSEO keyword_ideas failed", exc_info=True)
    if not keywords:
        fallback_terms = _haiku_brainstorm_keywords(seed)
        keywords = [
            KeywordIdea(keyword=k, search_volume=None, cpc=None, competition=None)
            for k in fallback_terms
        ]
        used_fallback = True

    clusters: list[dict] = []
    if keywords:
        try:
            clusters = cluster_keywords([k.keyword for k in keywords], focus_category=category_hint)
        except Exception:  # noqa: BLE001
            log.warning("clustering failed", exc_info=True)

    kw_terms = [k.keyword for k in keywords[:25]]
    base_names = _haiku_domain_brainstorm(seed, kw_terms, category_hint)
    if not base_names:
        return AutoResearchResult(
            seed=seed,
            category_hint=category_hint,
            keywords=keywords,
            clusters=clusters,
            tlds=tlds,
            used_fallback_keywords=used_fallback,
        )

    candidates = [f"{name}.{tld}" for name in base_names for tld in tlds]

    try:
        avails = check_availability(candidates)
    except Exception:  # noqa: BLE001
        log.warning("INWX availability check failed", exc_info=True)
        avails = []

    available_names = [a.name for a in avails if a.available]
    fits: dict[str, CategoryFit] = {}
    if available_names:
        try:
            fit_rows = score_bulk(available_names)
            fits = {f.domain: f for f in fit_rows}
        except Exception:  # noqa: BLE001
            log.warning("category_fit failed", exc_info=True)

    domain_candidates: list[DomainCandidate] = []
    for a in avails:
        fit = fits.get(a.name)
        domain_candidates.append(
            DomainCandidate(
                name=a.name,
                available=a.available,
                price_cents=a.price_cents,
                currency=a.currency,
                category=fit.top_category if fit else None,
                category_score=fit.top_score if fit else 0,
                category_confidence=fit.confidence if fit else None,
                category_reasoning=fit.reasoning if fit else None,
            )
        )
    domain_candidates.sort(
        key=lambda c: (not c.available, -c.category_score, c.price_cents or 99999)
    )

    return AutoResearchResult(
        seed=seed,
        category_hint=category_hint,
        keywords=keywords,
        clusters=clusters,
        domain_candidates=domain_candidates,
        tlds=tlds,
        used_fallback_keywords=used_fallback,
    )
