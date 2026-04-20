"""Backlink graph helpers.

Links flow strictly upward: Tier 1 -> Tier 2 -> Tier 3 -> Money Site.
This module picks targets for a given source post and resolves the
``[[BACKLINK:anchor]]`` placeholders the Claude writer emits.
"""
from __future__ import annotations

import random
import re
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.models import Backlink, Domain, DomainStatus, MoneySite, Post, PostStatus, Site, SiteStatus, Tier

_PLACEHOLDER_RE = re.compile(r"\[\[BACKLINK:([^\]]+)\]\]")


@dataclass
class LinkTarget:
    url: str
    site_id: int | None = None
    post_id: int | None = None


class TierViolation(ValueError):
    pass


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

    Policy: every Tier-3 site points at one MoneySite. Tier-1/2 posts aim
    at a published post on a site exactly one tier above. If none exist
    yet, fall back to a same-tier site homepage so the post isn't orphaned
    — that's common during early network bootstrapping.
    """
    source_tier = source_site.domain.tier
    if source_tier == Tier.GOOD:
        money_sites = (await session.execute(select(MoneySite))).scalars().all()
        if not money_sites:
            return []
        return [LinkTarget(url=random.choice(money_sites).url) for _ in range(slots)]

    higher_tier = Tier(int(source_tier) + 1)
    stmt = (
        select(Post)
        .options(joinedload(Post.site).joinedload(Site.domain))
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
    posts = list((await session.execute(stmt)).unique().scalars().all())
    if posts:
        picks = random.sample(posts, min(slots, len(posts)))
        return [
            LinkTarget(
                url=f"https://{p.site.domain.name}/{p.slug}",
                site_id=p.site.id,
                post_id=p.id,
            )
            for p in picks
        ]

    # Fallback: any higher-tier site homepage
    site_stmt = (
        select(Site)
        .options(joinedload(Site.domain))
        .join(Domain, Domain.id == Site.domain_id)
        .where(
            Site.status == SiteStatus.LIVE,
            Domain.tier == higher_tier,
            Site.id != source_site.id,
        )
    )
    sites = list((await session.execute(site_stmt)).unique().scalars().all())
    if not sites:
        return []
    picks = random.sample(sites, min(slots, len(sites)))
    return [LinkTarget(url=f"https://{s.domain.name}/", site_id=s.id) for s in picks]


async def resolve_placeholders(
    session: AsyncSession,
    source_site: Site,
    markdown: str,
) -> tuple[str, list[Backlink]]:
    """Replace every [[BACKLINK:anchor]] with a real link and return the
    resulting markdown plus the Backlink rows that should be persisted.
    """
    anchors = _PLACEHOLDER_RE.findall(markdown)
    if not anchors:
        return markdown, []

    targets = await pick_targets(session, source_site, len(anchors))
    if not targets:
        # Nothing to link to — drop placeholders, keep anchor text as plain.
        return _PLACEHOLDER_RE.sub(lambda m: m.group(1).strip(), markdown), []

    links: list[Backlink] = []
    idx = {"n": 0}

    def _replace(match: re.Match) -> str:
        anchor = match.group(1).strip()
        t = targets[idx["n"] % len(targets)]
        idx["n"] += 1
        links.append(
            Backlink(
                target_site_id=t.site_id,
                target_post_id=t.post_id,
                external_url=t.url,
                anchor_text=anchor,
                rel="",
                placed=True,
            )
        )
        return f"[{anchor}]({t.url})"

    return _PLACEHOLDER_RE.sub(_replace, markdown), links
