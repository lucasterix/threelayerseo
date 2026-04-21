"""SEO-informed tier classification.

Combines three signals into one tier recommendation:

1. LLM name-tier (from category_fit / CategoryFit.tier_recommendation)
   — captures brandability, keyword-stuffedness, "cheap-feel".
2. Wayback Machine snapshot depth — proxy for "this has been a real
   site for a while" (established domains).
3. DataForSEO backlinks summary — rank (~0-1000 PageRank-like) and
   referring main domains = direct authority signal.

Composite scoring bucketed into T1 / T2 / T3:
   < 35  → Tier 1
   35–64 → Tier 2
   ≥ 65  → Tier 3

For domains with no SEO data at all (freshly-invented names in
auto-research), only the LLM name tier contributes — same as the old
classifier. For expired or externally owned domains we get genuine
authority signals and push them into the right bucket.
"""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from app.services.backlinks import BacklinkSummary, summary as backlink_summary
from app.services.category_fit import CategoryFit
from app.services.wayback import snapshot_count

log = logging.getLogger(__name__)


@dataclass
class TierClassification:
    tier: int                         # 1 / 2 / 3
    reasoning: str                    # joined reasons
    confidence: str                   # "high" | "medium" | "low"
    score: int                        # 0-100 composite
    name_tier: int | None = None
    wayback_days: int | None = None
    backlink_rank: int | None = None
    referring_domains: int | None = None
    signals: list[str] = field(default_factory=list)


def classify(
    domain: str,
    *,
    name_tier: int | None = None,
    wayback_days: int | None = None,
    backlinks: BacklinkSummary | None = None,
) -> TierClassification:
    score = 0
    signals: list[str] = []

    # Name tier carries a base weight — always present from LLM classifier.
    if name_tier:
        base = {1: 10, 2: 40, 3: 70}.get(name_tier, 25)
        score += base
        signals.append(f"Name T{name_tier}")

    # Wayback depth: up to +30 for an established domain
    if wayback_days and wayback_days >= 30:
        bonus = min(wayback_days // 10, 30)
        score += bonus
        if wayback_days >= 365:
            signals.append(f"{wayback_days // 365}+ Jahre Wayback")
        else:
            signals.append(f"{wayback_days} Wayback-Tage")

    # DataForSEO rank (proxy for DR): up to +40 — dominant signal
    if backlinks and backlinks.rank:
        bonus = min(backlinks.rank // 20, 40)
        score += bonus
        if backlinks.rank >= 300:
            signals.append(f"rank {backlinks.rank}")
        elif backlinks.rank >= 100:
            signals.append(f"rank {backlinks.rank} (moderat)")

    # Referring main domains: up to +30
    if backlinks and backlinks.referring_main_domains:
        bonus = min(backlinks.referring_main_domains // 5, 30)
        score += bonus
        if backlinks.referring_main_domains >= 20:
            signals.append(f"{backlinks.referring_main_domains} Ref-Domains")

    score = min(score, 100)

    if score >= 65:
        tier = 3
    elif score >= 35:
        tier = 2
    else:
        tier = 1

    # Confidence hinges on how much real data we had
    has_seo = (wayback_days and wayback_days > 30) or (backlinks and (backlinks.rank or 0) > 50)
    if has_seo and score >= 65:
        confidence = "high"
    elif has_seo:
        confidence = "medium"
    elif name_tier is not None:
        confidence = "medium" if abs(score - {1: 10, 2: 40, 3: 70}.get(name_tier, 25)) < 15 else "low"
    else:
        confidence = "low"

    reasoning = " · ".join(signals) if signals else "keine Signale"

    return TierClassification(
        tier=tier,
        reasoning=reasoning,
        confidence=confidence,
        score=score,
        name_tier=name_tier,
        wayback_days=wayback_days,
        backlink_rank=backlinks.rank if backlinks else None,
        referring_domains=backlinks.referring_main_domains if backlinks else None,
        signals=signals,
    )


def enrich_and_classify(
    domains: list[str],
    name_fits: list[CategoryFit] | None = None,
    *,
    max_workers: int = 6,
) -> list[TierClassification]:
    """Per-domain SEO enrichment (wayback + backlinks, parallel) + classify.

    Use this when the domains are *real* (external import, expired, or
    already owned): we pay ~$0.002 per backlinks call + free wayback, get
    actual authority data, and the classifier uses it.
    """
    if not domains:
        return []
    by_name = {f.domain: f for f in (name_fits or [])}

    def _one(name: str) -> TierClassification:
        nf = by_name.get(name)
        name_tier = nf.tier_recommendation if nf else None
        try:
            wb_count, _, _ = snapshot_count(name)
        except Exception:  # noqa: BLE001
            wb_count = None
        try:
            bl = backlink_summary(name)
        except Exception:  # noqa: BLE001
            bl = None
        return classify(
            name,
            name_tier=name_tier,
            wayback_days=wb_count,
            backlinks=bl,
        )

    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        return list(ex.map(_one, domains))
