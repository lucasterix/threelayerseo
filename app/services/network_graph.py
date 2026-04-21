"""Build vis-network-compatible graph data for the site network.

Nodes: every LIVE site (colored by tier) + every ACTIVE MoneySite
(diamond shape). Edges aggregate cross-site backlinks with count =
edge weight and anchor texts in the tooltip.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.models import Backlink, Domain, MoneySite, Post, PostStatus, Site, SiteStatus


@dataclass
class GraphData:
    nodes: list[dict] = field(default_factory=list)
    edges: list[dict] = field(default_factory=list)
    stats: dict = field(default_factory=dict)


async def build(session: AsyncSession) -> GraphData:
    # 1. LIVE sites with their domain
    sites = list(
        (
            await session.execute(
                select(Site)
                .options(joinedload(Site.domain))
                .where(Site.status == SiteStatus.LIVE)
            )
        ).scalars().all()
    )

    # 2. Post counts per site
    post_count_rows = (
        await session.execute(
            select(Post.site_id, func.count())
            .where(Post.status == PostStatus.PUBLISHED)
            .group_by(Post.site_id)
        )
    ).all()
    post_counts = {row[0]: int(row[1] or 0) for row in post_count_rows}

    # 3. Active money sites
    money_sites = list(
        (
            await session.execute(
                select(MoneySite).where(MoneySite.active.is_(True))
            )
        ).scalars().all()
    )

    # 4. Every placed backlink + its source post
    bl_rows = list(
        (
            await session.execute(
                select(Backlink, Post)
                .join(Post, Post.id == Backlink.source_post_id)
                .where(Backlink.placed.is_(True))
            )
        ).all()
    )

    # ── Build nodes ────────────────────────────────────────────────
    nodes: list[dict] = []
    for s in sites:
        pcount = post_counts.get(s.id, 0)
        tier = s.domain.tier.value
        nodes.append({
            "id": f"site:{s.id}",
            "label": s.domain.name,
            "group": f"tier-{tier}",
            "value": max(pcount, 1),
            "tier": tier,
            "category": s.domain.category,
            "site_id": s.id,
            "post_count": pcount,
            "title": (
                f"{s.domain.name}\n"
                f"Tier {tier} · {s.domain.category or '—'}\n"
                f"{pcount} veröffentlichte Posts"
            ),
        })
    for m in money_sites:
        nodes.append({
            "id": f"money:{m.id}",
            "label": m.name,
            "group": "money",
            "value": 3,
            "category": m.category,
            "money_id": m.id,
            "url": m.url,
            "title": (
                f"{m.name} (Money-Site)\n"
                f"{m.url}\n"
                f"{m.category or '—'}"
            ),
        })

    # ── Aggregate edges ────────────────────────────────────────────
    # Key: (src_site_id, target_key). Value: {count, anchors}
    agg: dict[tuple[int, str], dict] = defaultdict(
        lambda: {"count": 0, "anchors": []}
    )

    # Pre-index money sites by URL prefix so target-matching is linear
    money_url_index = [(m.url.rstrip("/"), m.id) for m in money_sites]

    for bl, post in bl_rows:
        src_key = f"site:{post.site_id}"
        tgt_key: str | None = None
        if bl.target_site_id:
            tgt_key = f"site:{bl.target_site_id}"
        elif bl.external_url:
            normalized = bl.external_url.rstrip("/")
            for prefix, mid in money_url_index:
                if normalized.startswith(prefix):
                    tgt_key = f"money:{mid}"
                    break
        if not tgt_key:
            continue
        entry = agg[(post.site_id, tgt_key)]
        entry["count"] += 1
        if bl.anchor_text and len(entry["anchors"]) < 5:
            entry["anchors"].append(bl.anchor_text)

    edges: list[dict] = []
    for (src_id, tgt_key), data in agg.items():
        anchors_preview = " · ".join(data["anchors"][:3])
        edges.append({
            "from": f"site:{src_id}",
            "to": tgt_key,
            "value": data["count"],
            "title": f"{data['count']} Link(s): {anchors_preview}",
            "arrows": "to",
        })

    # ── Stats for the legend / header ──────────────────────────────
    tier_counts: dict[int, int] = {1: 0, 2: 0, 3: 0}
    for s in sites:
        tier_counts[s.domain.tier.value] = tier_counts.get(s.domain.tier.value, 0) + 1

    stats = {
        "sites": len(sites),
        "money_sites": len(money_sites),
        "edges": len(edges),
        "total_backlinks": sum(d["count"] for d in agg.values()),
        "tier_counts": tier_counts,
    }
    return GraphData(nodes=nodes, edges=edges, stats=stats)
