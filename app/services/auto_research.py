"""One-seed automated research orchestrator.

Given a single seed phrase ("diabetes tipps", "hundefutter"), runs the
full funnel in one call:

  1. Expand the seed to 30+ related keywords via DataForSEO keyword_ideas
     (fallback: Haiku brainstorm if DataForSEO isn't reachable / verified).
  2. Cluster those keywords into topical silos via Claude Haiku.
  3. Ask Haiku to invent 15-20 domain-name candidates (TLD-free) that
     match the niche and the winning cluster.
  4. Cartesian-multiply candidates × configured TLDs.
  5. Bulk-check availability at INWX.
  6. Score every *available* candidate with the category-fit classifier
     so the UI can pick a badge.

Every step degrades gracefully — if DataForSEO is 403, Haiku keyword
brainstorm takes over; if category_fit fails we just return empty fits.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

from anthropic import Anthropic

from app.config import settings
from app.services.category_fit import CategoryFit, score_bulk
from app.services.clustering import cluster_keywords
from app.services.domains import DEFAULT_TLDS, check_availability
from app.services.keywords import KeywordIdea, keyword_ideas

log = logging.getLogger(__name__)

BRAINSTORM_MODEL = "claude-haiku-4-5-20251001"

_DOMAIN_BRAINSTORM_SYSTEM = """Du generierst kurze, lesbare Domain-Namen
(ohne TLD) für deutschsprachige SEO-Blogs in einer bestimmten Nische.

Regeln:
- Lesbar, ohne Zahlen, Bindestriche sparsam (max 1 pro Name).
- Deutsche Wörter bevorzugt, englische nur wenn idiomatisch (z.B. "guide").
- 4-20 Zeichen pro Name, inklusive Bindestrich.
- Hauptkeyword der Nische sollte in vielen Namen vorkommen.
- Keine existierenden Marken.
- Mische Typen: Guide/Ratgeber/Tipps/Wissen/Info/Blog/Magazin/Portal.

Gib ein JSON-Array von Strings zurück, 15-20 Namen. Keine Prosa,
keine Code-Fences."""


_KEYWORD_BRAINSTORM_SYSTEM = """Erzeuge 30 verwandte deutsche Suchbegriffe
zu einem Seed. Intent-Mix: je ein Drittel informational (Was/Wie/Warum),
commercial (beste / vergleich / test) und transactional (kaufen / preis).
Ausgabe: JSON-Array von Strings, keine Prosa."""


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
    if not settings.anthropic_api_key:
        return []
    client = Anthropic(api_key=settings.anthropic_api_key)
    try:
        resp = client.messages.create(
            model=BRAINSTORM_MODEL,
            max_tokens=1500,
            system=_KEYWORD_BRAINSTORM_SYSTEM,
            messages=[{"role": "user", "content": f"Seed: {seed}"}],
        )
    except Exception as e:  # noqa: BLE001
        log.warning("keyword brainstorm failed: %s", e)
        return []
    text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text").strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()
    try:
        items = json.loads(text)
    except json.JSONDecodeError:
        return []
    return [str(x).lower() for x in items if isinstance(x, str)][:40]


def _haiku_domain_brainstorm(
    seed: str,
    keywords: list[str],
    category_hint: str | None = None,
) -> list[str]:
    if not settings.anthropic_api_key:
        return []
    client = Anthropic(api_key=settings.anthropic_api_key)
    prompt = (
        f"Nische/Seed: {seed}\n"
        f"Beispiel-Keywords aus der Nische: {', '.join(keywords[:25])}\n"
    )
    if category_hint:
        prompt += f"Kategorie-Fokus: {category_hint}\n"
    prompt += "Bitte generiere jetzt die Domain-Namen als JSON-Array."
    try:
        resp = client.messages.create(
            model=BRAINSTORM_MODEL,
            max_tokens=1000,
            system=_DOMAIN_BRAINSTORM_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception as e:  # noqa: BLE001
        log.warning("domain brainstorm failed: %s", e)
        return []
    text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text").strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()
    try:
        items = json.loads(text)
    except json.JSONDecodeError:
        return []
    # Normalise: lowercase, strip, drop anything that wouldn't be valid as a label
    import re

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

    # 1. Keyword expansion
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

    # 2. Cluster (best-effort)
    clusters: list[dict] = []
    if keywords:
        try:
            clusters = cluster_keywords([k.keyword for k in keywords], focus_category=category_hint)
        except Exception:  # noqa: BLE001
            log.warning("clustering failed", exc_info=True)

    # 3. Domain-name brainstorm
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

    # 4. Cartesian × TLDs
    candidates = [f"{name}.{tld}" for name in base_names for tld in tlds]

    # 5. INWX availability
    try:
        avails = check_availability(candidates)
    except Exception:  # noqa: BLE001
        log.warning("INWX availability check failed", exc_info=True)
        avails = []

    # 6. Category-fit for the available ones
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
    # Sort: available first, then by category_score desc, then by price asc.
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
