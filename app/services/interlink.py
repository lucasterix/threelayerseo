"""Cross-site interlink recommender.

Surveys the whole network and suggests which published posts on
lower-tier sites should link to which upper-tier sites/posts. Scores
combine tier-upward direction, category match, topical keyword overlap,
and existing backlink load so we distribute link-juice without over-
optimising any single target.

Output is a ranked list of InterlinkSuggestion rows; the admin can
apply them by queueing the source post for regeneration with an extra
[[BACKLINK:...]] placeholder.
"""
from __future__ import annotations

import logging
import re
from collections import Counter
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.models import Backlink, Domain, DomainStatus, Post, PostStatus, Site, SiteStatus, Tier

log = logging.getLogger(__name__)

# Healthcare-flavoured category cluster: a healthcare Tier-1 post can
# naturally anchor to life-science / pharma / medtech Tier-2/3, so give
# partial credit even without an exact category match.
_RELATED_CATEGORIES: dict[str, set[str]] = {
    "healthcare": {"life-science", "pharma", "medtech", "nutrition", "psychology"},
    "life-science": {"healthcare", "pharma", "medtech"},
    "pharma": {"healthcare", "life-science", "medtech"},
    "medtech": {"healthcare", "life-science", "pharma"},
    "nutrition": {"healthcare", "fitness"},
    "fitness": {"healthcare", "nutrition", "psychology"},
    "psychology": {"healthcare", "nutrition"},
    "finance": {"legal"},
    "legal": {"finance"},
    "tech": {"ecommerce"},
    "ecommerce": {"tech", "lifestyle"},
    "lifestyle": {"ecommerce", "fitness", "nutrition"},
}

_STOPWORDS = {
    "der", "die", "das", "und", "oder", "aber", "wie", "was", "wer", "wo",
    "wann", "ein", "eine", "einen", "einem", "einer", "in", "im", "auf",
    "mit", "für", "von", "zu", "am", "an", "bei", "nach", "vor", "über",
    "unter", "sich", "nicht", "auch", "noch", "mehr", "sehr", "sind",
    "the", "and", "for", "with", "from", "this", "that", "have", "your",
}


@dataclass
class InterlinkSuggestion:
    source_site_id: int
    source_post_id: int
    source_tier: int
    source_domain: str
    source_title: str
    target_site_id: int
    target_tier: int
    target_domain: str
    target_category: str | None
    score: int                       # 0..100 ish
    reason: str
    suggested_anchor: str


def _tokenize(*texts: str) -> set[str]:
    combined = " ".join(t for t in texts if t)
    return {
        t for t in re.findall(r"[a-zäöüß0-9]{4,}", combined.lower())
        if t not in _STOPWORDS
    }


def _category_bonus(source_cat: str | None, target_cat: str | None) -> int:
    if not source_cat or not target_cat:
        return 0
    if source_cat == target_cat:
        return 40
    if target_cat in _RELATED_CATEGORIES.get(source_cat, set()):
        return 20
    return 0


async def _inbound_counts(session: AsyncSession) -> dict[int, int]:
    """Existing incoming backlinks per target site — so we can penalise
    already-saturated targets and spread link juice."""
    rows = await session.execute(
        select(Backlink.target_site_id, func.count())
        .where(Backlink.target_site_id.is_not(None), Backlink.placed.is_(True))
        .group_by(Backlink.target_site_id)
    )
    return {r[0]: int(r[1] or 0) for r in rows.all() if r[0] is not None}


async def recommend(
    session: AsyncSession,
    limit: int = 100,
    max_per_source: int = 2,
) -> list[InterlinkSuggestion]:
    # Source: published posts on tier-1 or tier-2 sites (upward flow only)
    src_stmt = (
        select(Post)
        .options(joinedload(Post.site).joinedload(Site.domain))
        .join(Site, Site.id == Post.site_id)
        .join(Domain, Domain.id == Site.domain_id)
        .where(
            Post.status == PostStatus.PUBLISHED,
            Site.status == SiteStatus.LIVE,
            Domain.status == DomainStatus.ACTIVE,
            Domain.tier.in_([Tier.BAD, Tier.MEDIUM]),
        )
    )
    sources = list((await session.execute(src_stmt)).scalars().all())

    # Targets: live sites in higher tiers with active domains
    tgt_stmt = (
        select(Site)
        .options(joinedload(Site.domain))
        .join(Domain, Domain.id == Site.domain_id)
        .where(
            Site.status == SiteStatus.LIVE,
            Domain.status == DomainStatus.ACTIVE,
        )
    )
    targets = list((await session.execute(tgt_stmt)).scalars().all())
    inbound = await _inbound_counts(session)

    suggestions: list[InterlinkSuggestion] = []

    for post in sources:
        src_site = post.site
        src_tier = int(src_site.domain.tier)
        src_cat = src_site.domain.category
        src_tokens = _tokenize(post.body_markdown or "", post.primary_keyword, post.title)

        scored_targets: list[tuple[int, Site, str]] = []
        for t in targets:
            tgt_tier = int(t.domain.tier)
            if tgt_tier <= src_tier:
                continue
            if t.id == src_site.id:
                continue
            tgt_cat = t.domain.category
            score = 0
            reason_parts = []

            # Tier gap bonus — cleaner upward flow, closer tier = stronger
            tier_gap = tgt_tier - src_tier
            if tier_gap == 1:
                score += 15
                reason_parts.append(f"t{src_tier}→t{tgt_tier}")
            elif tier_gap == 2:
                score += 5
                reason_parts.append(f"t{src_tier}→t{tgt_tier} (skip)")

            cat_bonus = _category_bonus(src_cat, tgt_cat)
            score += cat_bonus
            if cat_bonus >= 40:
                reason_parts.append(f"{tgt_cat} match")
            elif cat_bonus >= 20:
                reason_parts.append(f"{tgt_cat}~{src_cat}")

            tgt_tokens = _tokenize(t.topic, t.title)
            overlap = len(src_tokens & tgt_tokens)
            if overlap >= 1:
                score += min(overlap * 8, 40)
                reason_parts.append(f"{overlap} overlap")

            # Saturation penalty
            incoming = inbound.get(t.id, 0)
            if incoming >= 10:
                score -= 20
                reason_parts.append(f"{incoming} already in")
            elif incoming >= 5:
                score -= 10

            if score <= 0:
                continue
            scored_targets.append((score, t, " · ".join(reason_parts)))

        scored_targets.sort(reverse=True, key=lambda t: t[0])
        for score, target, reason in scored_targets[:max_per_source]:
            # Anchor heuristic: start with the target site's title, fall
            # back to the target's topic snippet.
            anchor = (target.title or target.topic or target.domain.name)[:80]
            suggestions.append(
                InterlinkSuggestion(
                    source_site_id=src_site.id,
                    source_post_id=post.id,
                    source_tier=src_tier,
                    source_domain=src_site.domain.name,
                    source_title=post.title,
                    target_site_id=target.id,
                    target_tier=int(target.domain.tier),
                    target_domain=target.domain.name,
                    target_category=target.domain.category,
                    score=score,
                    reason=reason,
                    suggested_anchor=anchor,
                )
            )

    suggestions.sort(reverse=True, key=lambda s: s.score)
    return suggestions[:limit]
