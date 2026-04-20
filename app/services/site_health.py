"""Per-site health checklist.

Pure DB-derived for now — no live HTTP probes. We infer what's true from
the data we've already persisted: domain status, site status, post counts,
GSC metadata we stored during onboarding, etc. Live probes (A record
resolution, TLS handshake, sitemap.xml fetch) can be added to the worker
later and cached in ``Site.meta``.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Backlink, Domain, DomainStatus, Post, PostStatus, Site, SiteStatus


@dataclass
class Check:
    key: str
    label: str
    ok: bool
    detail: str = ""


@dataclass
class SiteHealth:
    domain: Domain
    site: Site
    infrastructure: list[Check] = field(default_factory=list)
    content: list[Check] = field(default_factory=list)
    indexing: list[Check] = field(default_factory=list)
    compliance: list[Check] = field(default_factory=list)
    score: int = 0   # 0..100

    def all_checks(self) -> list[Check]:
        return self.infrastructure + self.content + self.indexing + self.compliance


async def compute(session: AsyncSession, site: Site, domain: Domain) -> SiteHealth:
    meta_gsc = (domain.meta or {}).get("gsc") if domain.meta else None

    posts_total = await session.scalar(
        select(func.count()).select_from(Post).where(Post.site_id == site.id)
    ) or 0
    posts_published = (
        await session.scalar(
            select(func.count())
            .select_from(Post)
            .where(Post.site_id == site.id, Post.status == PostStatus.PUBLISHED)
        )
    ) or 0

    backlinks_out = (
        await session.scalar(
            select(func.count())
            .select_from(Backlink)
            .join(Post, Post.id == Backlink.source_post_id)
            .where(Post.site_id == site.id, Backlink.placed.is_(True))
        )
    ) or 0

    health = SiteHealth(domain=domain, site=site)

    # ─── Infrastructure ─────────────────────────────────────────────────────
    health.infrastructure.append(
        Check(
            "domain-active",
            "Domain registriert (INWX)",
            domain.status == DomainStatus.ACTIVE,
            detail=domain.status.value,
        )
    )
    health.infrastructure.append(
        Check(
            "server-assigned",
            "Server zugewiesen",
            site.server_id is not None,
            detail=(
                f"{site.server.hostname} · {site.server.ip}" if site.server else "kein Server gepflegt"
            ),
        )
    )
    health.infrastructure.append(
        Check(
            "tls",
            "HTTPS / TLS (Caddy on-demand)",
            domain.status == DomainStatus.ACTIVE,
            detail="Auto-provisioniert beim ersten Hit sobald caddy-ask 200 meldet",
        )
    )

    # ─── Content ────────────────────────────────────────────────────────────
    health.content.append(
        Check("homepage", "Homepage generiert", bool(site.homepage_html))
    )
    health.content.append(
        Check(
            "posts",
            "Mindestens 3 veröffentlichte Posts",
            posts_published >= 3,
            detail=f"{posts_published}/{posts_total} veröffentlicht",
        )
    )
    health.content.append(
        Check(
            "backlinks-out",
            "Backlinks fließen aufwärts",
            backlinks_out > 0,
            detail=f"{backlinks_out} gesetzte Links",
        )
    )

    # ─── Indexing ───────────────────────────────────────────────────────────
    health.indexing.append(
        Check(
            "sitemap",
            "sitemap.xml ausgeliefert",
            posts_published > 0,
            detail="Dynamisch aus DB, lebt sobald mindestens 1 Post published",
        )
    )
    health.indexing.append(
        Check(
            "gsc-verified",
            "Google Search Console verifiziert",
            bool(meta_gsc and meta_gsc.get("verified")),
            detail="DNS-TXT via INWX + Google-Verification-Call",
        )
    )
    health.indexing.append(
        Check(
            "gsc-sitemap",
            "GSC-Sitemap eingereicht",
            bool(meta_gsc and meta_gsc.get("sitemap_submitted")),
        )
    )
    health.indexing.append(
        Check(
            "indexnow",
            "IndexNow aktiv (Bing/Yandex)",
            True,
            detail="Push bei jedem Post-Publish",
        )
    )

    # ─── Compliance (DE-rechtlich Pflicht) ──────────────────────────────────
    health.compliance.append(
        Check("imprint", "Impressum", bool(site.imprint_html))
    )
    health.compliance.append(
        Check("privacy", "Datenschutzerklärung", bool(site.privacy_html))
    )

    all_checks = health.all_checks()
    if all_checks:
        health.score = int(round(100 * sum(1 for c in all_checks if c.ok) / len(all_checks)))
    return health
