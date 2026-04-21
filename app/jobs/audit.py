"""SEO + Lighthouse-proxy audit jobs.

Runs after every content/design event so we always have the latest
score per URL — like a marketing agency that keeps Lighthouse green.
"""
from __future__ import annotations

import asyncio
import logging

from sqlalchemy import select
from sqlalchemy.orm import joinedload

from app.db import SessionLocal
from app.models import Post, PostStatus, SeoAudit, Site
from app.services.seo_audit import audit_url, audit_to_dict

log = logging.getLogger(__name__)


def _persist(session, site_id: int, post_id: int | None, audit, trigger: str) -> SeoAudit:
    payload = audit_to_dict(audit)
    row = SeoAudit(
        site_id=site_id,
        post_id=post_id,
        url=audit.url,
        score=audit.score,
        seo_score=audit.seo_score,
        perf_score=audit.perf_score,
        a11y_score=audit.a11y_score,
        issues=payload["issues"],
        passed=audit.passed,
        metrics=audit.metrics,
        trigger=trigger,
    )
    session.add(row)
    return row


async def _audit_site(site_id: int, trigger: str = "homepage") -> None:
    async with SessionLocal() as session:
        stmt = select(Site).options(joinedload(Site.domain)).where(Site.id == site_id)
        site = (await session.execute(stmt)).scalar_one_or_none()
        if not site or not site.domain:
            return
        url = f"https://{site.domain.name}/"
        audit = audit_url(url)
        row = _persist(session, site.id, None, audit, trigger)
        await session.commit()
        log.info(
            "seo audit homepage site=%s trigger=%s score=%d (seo=%d perf=%d a11y=%d) issues=%d",
            site.id, trigger, row.score, row.seo_score, row.perf_score, row.a11y_score,
            len(row.issues or []),
        )


async def _audit_post(post_id: int, trigger: str = "publish") -> None:
    async with SessionLocal() as session:
        stmt = (
            select(Post)
            .options(joinedload(Post.site).joinedload(Site.domain))
            .where(Post.id == post_id)
        )
        post = (await session.execute(stmt)).scalar_one_or_none()
        if not post or post.status != PostStatus.PUBLISHED or not post.site or not post.site.domain:
            return
        url = f"https://{post.site.domain.name}/{post.slug}"
        audit = audit_url(url, primary_keyword=post.primary_keyword)
        row = _persist(session, post.site_id, post.id, audit, trigger)
        await session.commit()
        log.info(
            "seo audit post=%s trigger=%s score=%d (seo=%d perf=%d a11y=%d) issues=%d",
            post.id, trigger, row.score, row.seo_score, row.perf_score, row.a11y_score,
            len(row.issues or []),
        )


# ─── RQ entry points ──────────────────────────────────────────────────────


def audit_site_job(site_id: int, trigger: str = "homepage") -> None:
    asyncio.run(_audit_site(site_id, trigger))


def audit_post_job(post_id: int, trigger: str = "publish") -> None:
    asyncio.run(_audit_post(post_id, trigger))
