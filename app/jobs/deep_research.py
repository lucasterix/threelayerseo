"""Long-running deep research: seed → proposal list the admin approves.

Phases:
  1. Broad keyword expansion — 5 seed variations × DataForSEO
     keyword_ideas (falls back to Haiku brainstorm if the API 403s).
  2. Cluster the merged keyword set (Claude Haiku).
  3. Brainstorm 30-60 candidate domain bases, biased toward each
     cluster's intent (Haiku).
  4. Cartesian × selected TLDs → up to ~200 candidates.
  5. Bulk availability at INWX (chunked into 50s).
  6. For AVAILABLE candidates: single-call category_fit.
  7. For TAKEN candidates: Wayback CDX count + DataForSEO backlinks
     (flags expired-with-history opportunities for drop-watch).
  8. Composite score + persist to ResearchRun.candidates /
     expired_opportunities. Admin reviews and approves what to buy.
"""
from __future__ import annotations

import asyncio
import json
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from sqlalchemy import select

from app.config import settings
from app.db import SessionLocal
from app.models import ResearchRun
from app.services import budget
from app.services.auto_research import (
    _haiku_brainstorm_keywords,
    _haiku_domain_brainstorm,
)
from app.services.backlinks import BacklinkSummary, summary as backlink_summary
from app.services.category_fit import score_bulk
from app.services.clustering import cluster_keywords
from app.services.domains import check_availability
from app.services.keywords import KeywordIdea, keyword_ideas
from app.services.wayback import snapshot_count

log = logging.getLogger(__name__)


DEPTH_CONFIGS = {
    "quick":  {"brainstorms": 1, "kw_variations": 2, "max_candidates": 40},
    "normal": {"brainstorms": 2, "kw_variations": 4, "max_candidates": 120},
    "deep":   {"brainstorms": 3, "kw_variations": 6, "max_candidates": 240},
}

_SEED_VARIATIONS = [
    "",                   # original seed
    " tipps",
    " ratgeber",
    " test",
    " beste",
    " guide",
    " online",
]


async def _update(run_id: int, **fields) -> None:
    async with SessionLocal() as session:
        run = await session.get(ResearchRun, run_id)
        if not run:
            return
        for k, v in fields.items():
            setattr(run, k, v)
        await session.commit()


def _score_available(category_score: int, price_cents: int | None) -> int:
    s = 20                                   # base for available
    s += min(category_score, 100) // 2       # up to +50
    if price_cents:
        s -= min(price_cents // 100, 15)     # up to -15 for expensive TLDs
    return max(0, s)


def _score_expired(
    wayback_count: int,
    backlinks: BacklinkSummary | None,
) -> int:
    s = 0
    s += min(wayback_count, 500) // 5        # up to +100
    if backlinks:
        if backlinks.rank:
            s += min(backlinks.rank, 800) // 10    # up to +80
        if backlinks.referring_main_domains:
            s += min(backlinks.referring_main_domains, 200)  # up to +200
    return s


async def _run(run_id: int) -> None:
    cfg_source = None
    async with SessionLocal() as session:
        run = await session.get(ResearchRun, run_id)
        if not run:
            return
        seed = run.seed
        category_hint = run.category_hint
        tlds = list(run.tlds or ["de", "com", "info"])
        depth = run.depth or "normal"
        cfg_source = DEPTH_CONFIGS.get(depth, DEPTH_CONFIGS["normal"])

    cfg = cfg_source
    cost_cents = 0

    async def progress(label: str, pct: int) -> None:
        await _update(run_id, status="running", progress_label=label, progress_pct=pct)

    await progress("Keywords expandieren", 5)

    # ── Phase 1: broad keyword expansion ──────────────────────────────
    all_keywords: list[KeywordIdea] = []
    seen_kw: set[str] = set()
    variations = _SEED_VARIATIONS[: cfg["kw_variations"]]
    used_fallback = False

    for variation in variations:
        q = (seed + variation).strip()
        try:
            ideas = keyword_ideas(q, limit=40)
            if ideas:
                cost_cents += 1
        except Exception:  # noqa: BLE001
            ideas = []
        if not ideas and variation == "":
            fallback_terms = _haiku_brainstorm_keywords(seed)
            if fallback_terms:
                used_fallback = True
                ideas = [
                    KeywordIdea(keyword=k, search_volume=None, cpc=None, competition=None)
                    for k in fallback_terms
                ]
                cost_cents += 1
        for k in ideas:
            if k.keyword and k.keyword not in seen_kw:
                seen_kw.add(k.keyword)
                all_keywords.append(k)

    if not all_keywords:
        await _update(
            run_id,
            status="failed",
            progress_label="Keine Keywords generiert",
            progress_pct=100,
            error="Both DataForSEO and Haiku brainstorm came back empty",
            finished_at=datetime.now(timezone.utc),
        )
        return

    await progress("Clustern", 20)

    # ── Phase 2: cluster ──────────────────────────────────────────────
    clusters: list[dict] = []
    try:
        clusters = cluster_keywords(
            [k.keyword for k in all_keywords[:150]],
            focus_category=category_hint,
        )
        cost_cents += 1
    except Exception:  # noqa: BLE001
        log.warning("clustering failed", exc_info=True)

    await progress("Domain-Namen brainstormen", 35)

    # ── Phase 3: domain brainstorm (per cluster + broad) ──────────────
    base_names: list[str] = []
    seen_names: set[str] = set()

    def _merge(names: list[str]) -> None:
        for n in names:
            if n and n not in seen_names:
                seen_names.add(n)
                base_names.append(n)

    # Broad brainstorm
    broad = _haiku_domain_brainstorm(
        seed, [k.keyword for k in all_keywords[:30]], category_hint
    )
    cost_cents += 1
    _merge(broad)

    # Per-cluster brainstorms (if we have multiple clusters)
    for c in clusters[: cfg["brainstorms"]]:
        kw_slice = c.get("keywords") or []
        if not kw_slice:
            continue
        more = _haiku_domain_brainstorm(
            c.get("name", seed), kw_slice[:20], category_hint
        )
        cost_cents += 1
        _merge(more)

    if not base_names:
        await _update(
            run_id,
            status="failed",
            progress_label="Keine Domain-Ideen",
            progress_pct=100,
            error="Haiku domain brainstorm returned nothing",
            finished_at=datetime.now(timezone.utc),
        )
        return

    # Cartesian × TLDs, capped
    candidates = [f"{n}.{t}" for n in base_names for t in tlds][: cfg["max_candidates"]]

    await progress(f"INWX-Availability ({len(candidates)} Kandidaten)", 50)

    # ── Phase 4: INWX bulk availability ──────────────────────────────
    avails = []
    try:
        avails = check_availability(candidates)
    except Exception as e:  # noqa: BLE001
        log.warning("INWX bulk check failed: %s", e)

    available = [a for a in avails if a.available]
    taken = [a for a in avails if not a.available]

    await _update(
        run_id,
        total_checked=len(avails),
        total_available=len(available),
        progress_label="Category-Fit scoren",
        progress_pct=65,
    )

    # ── Phase 5: category fit on available ────────────────────────────
    fit_map: dict = {}
    if available:
        try:
            fits = score_bulk([a.name for a in available])
            fit_map = {f.domain: f for f in fits}
            cost_cents += 1
        except Exception:  # noqa: BLE001
            log.warning("category_fit failed", exc_info=True)

    await progress(f"Expired-Juice scannen ({len(taken)} vergeben)", 75)

    # ── Phase 6: for taken candidates — Wayback + backlinks ──────────
    # Only score the first N taken to keep cost bounded.
    taken_scan_limit = {"quick": 20, "normal": 60, "deep": 120}[depth]
    taken_to_scan = taken[:taken_scan_limit]

    def enrich_taken(name: str) -> tuple[str, int, BacklinkSummary | None]:
        count, _, _ = snapshot_count(name)
        bl = backlink_summary(name)
        return name, count, bl

    taken_enriched: list[tuple[str, int, BacklinkSummary | None]] = []
    if taken_to_scan:
        with ThreadPoolExecutor(max_workers=6) as ex:
            taken_enriched = list(ex.map(lambda a: enrich_taken(a.name), taken_to_scan))
        # Rough budget tracking for backlink lookups
        cost_cents += len(taken_to_scan) * 2     # ~$0.02 per backlink call

    await progress("Ranken", 90)

    # ── Phase 7: build candidate + opportunity lists ──────────────────
    available_out = []
    for a in available:
        fit = fit_map.get(a.name)
        score = _score_available(
            fit.top_score if fit else 0,
            a.price_cents,
        )
        available_out.append({
            "name": a.name,
            "price_cents": a.price_cents,
            "currency": a.currency,
            "category": fit.top_category if fit else None,
            "category_score": fit.top_score if fit else 0,
            "category_confidence": fit.confidence if fit else None,
            "category_reasoning": fit.reasoning if fit else None,
            "score": score,
        })
    available_out.sort(key=lambda c: c["score"], reverse=True)

    expired_out = []
    for name, wb_count, bl in taken_enriched:
        if wb_count == 0 and (not bl or not bl.rank):
            continue  # not interesting
        juice_score = _score_expired(wb_count, bl)
        if juice_score < 20:
            continue
        expired_out.append({
            "name": name,
            "wayback_snapshots": wb_count,
            "backlinks_rank": bl.rank if bl else None,
            "backlinks_total": bl.backlinks if bl else None,
            "referring_domains": bl.referring_main_domains if bl else None,
            "score": juice_score,
            "last_visited": bl.last_visited if bl else None,
        })
    expired_out.sort(key=lambda c: c["score"], reverse=True)

    await _update(
        run_id,
        status="complete",
        progress_label="Fertig",
        progress_pct=100,
        finished_at=datetime.now(timezone.utc),
        keywords=[
            {
                "keyword": k.keyword,
                "search_volume": k.search_volume,
                "cpc": k.cpc,
                "competition": k.competition,
            }
            for k in all_keywords
        ],
        clusters=clusters,
        candidates=available_out,
        expired_opportunities=expired_out[:30],
        cost_cents=cost_cents,
    )

    # Single budget event for the aggregate cost (OpenAI now; DataForSEO
    # portion is small and already rolled into cost_cents).
    await budget.track(
        "openai",
        "deep-research",
        amount_cents=cost_cents,
        note=f"deep_research run={run_id} seed={seed} depth={depth}",
    )

    log.info(
        "deep_research %s done: %d avail / %d expired-w-juice, cost ~%d cents, fallback_kw=%s",
        run_id,
        len(available_out),
        len(expired_out),
        cost_cents,
        used_fallback,
    )


def deep_research_job(run_id: int) -> None:
    try:
        asyncio.run(_run(run_id))
    except Exception as e:  # noqa: BLE001
        log.exception("deep_research crashed run=%s", run_id)
        try:
            async def mark():
                async with SessionLocal() as session:
                    run = await session.get(ResearchRun, run_id)
                    if run:
                        run.status = "failed"
                        run.error = str(e)[:500]
                        run.finished_at = datetime.now(timezone.utc)
                        await session.commit()

            asyncio.run(mark())
        except Exception:  # noqa: BLE001
            pass
