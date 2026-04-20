"""Backlink graph helpers.

Links flow strictly upward: Tier 1 -> Tier 2 -> Tier 3 -> Money Site.
This module provides the validation + candidate-target selection used by
the content pipeline when it resolves ``[[BACKLINK:anchor]]`` placeholders.
"""
from __future__ import annotations

import random
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Domain, DomainStatus, MoneySite, Post, PostStatus, Site, SiteStatus, Tier


@dataclass
class LinkTarget:
    url: str
    site_id: int | None = None
    post_id: int | None = None


class TierViolation(ValueError):
    """Raised when attempting to place a link that points downward in the tier stack."""


def validate_direction(source_tier: Tier, target_tier: Tier) -> None:
    if int(target_tier) < int(source_tier):
        raise TierViolation(
            f"tier-{source_tier.value} cannot link down to tier-{target_tier.value}"
        )


async def pick_targets(
    session: AsyncSession,
    source_site: Site,
    slots: int,
) -> list[LinkTarget]:
    """Pick ``slots`` backlink targets for a post on ``source_site``.

    Policy: every Tier-3 site points at one MoneySite. Tier-1/Tier-2 posts
    point at a post on a site exactly one tier above when available, else
    fall back to any higher-tier site's homepage.
    """
    source_tier = source_site.domain.tier
    if source_tier == Tier.GOOD:
        money_sites = (await session.execute(select(MoneySite))).scalars().all()
        if not money_sites:
            return []
        return [LinkTarget(url=random.choice(money_sites).url) for _ in range(slots)]

    higher_tier = Tier(int(source_tier) + 1)
    stmt = (
        select(Post, Site, Domain)
        .join(Site, Site.id == Post.site_id)
        .join(Domain, Domain.id == Site.domain_id)
        .where(
            Post.status == PostStatus.PUBLISHED,
            Site.status == SiteStatus.LIVE,
            Domain.status == DomainStatus.ACTIVE,
            Domain.tier == higher_tier,
            Site.id != source_site.id,
        )
    )
    rows = (await session.execute(stmt)).all()
    if not rows:
        return []
    picks = random.sample(rows, min(slots, len(rows)))
    return [
        LinkTarget(
            url=f"https://{domain.name}/{post.slug}",
            site_id=site.id,
            post_id=post.id,
        )
        for post, site, domain in picks
    ]
