"""Content generation + publishing jobs."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import joinedload

from app.content.homepage import generate_homepage_markdown, render_homepage_html
from app.content.pipeline import generate_post
from app.db import SessionLocal
from app.models import Post, PostStatus, Site, SiteStatus, Tier
from app.services.indexing import indexnow_submit

log = logging.getLogger(__name__)


async def _generate_post(post_id: int, topic: str) -> None:
    async with SessionLocal() as session:
        post = await session.get(Post, post_id)
        if not post:
            log.error("post %s not found", post_id)
            return
        stmt = select(Site).options(joinedload(Site.domain)).where(Site.id == post.site_id)
        site = (await session.execute(stmt)).scalar_one()

        post.status = PostStatus.RESEARCHING
        await session.commit()

        try:
            result = await generate_post(
                session=session,
                site=site,
                topic=topic,
                primary_keyword=post.primary_keyword,
            )
            post.title = result.title
            post.slug = result.slug or post.slug
            post.body_markdown = result.body_markdown
            post.body_html = result.body_html
            post.research_json = result.research_json
            post.status = PostStatus.READY
            for bl in result.backlinks:
                bl.source_post_id = post.id
                session.add(bl)
            await session.commit()
            log.info("generated post %s: %s", post.id, post.title)

            # Scheduled drip-feed: if the post has scheduled_at set, enqueue
            # the publish job at that time. Past timestamps publish now.
            if post.scheduled_at:
                from app.queue import publish_q

                when = post.scheduled_at
                if when.tzinfo is None:
                    when = when.replace(tzinfo=timezone.utc)
                publish_q.enqueue_at(
                    when, "app.jobs.content.publish_post_job", post.id, job_timeout=60
                )
                log.info("post %s scheduled for publish at %s", post.id, when)
        except Exception as e:  # noqa: BLE001
            post.status = PostStatus.FAILED
            await session.commit()
            log.exception("content generation failed for post %s: %s", post_id, e)


async def _generate_homepage(site_id: int) -> None:
    async with SessionLocal() as session:
        stmt = select(Site).options(joinedload(Site.domain)).where(Site.id == site_id)
        site = (await session.execute(stmt)).scalar_one_or_none()
        if not site:
            return
        prev_status = site.status
        site.status = SiteStatus.BUILDING
        await session.commit()
        try:
            wayback_text = None
            if site.domain.is_expired_purchase:
                wayback_text = (site.domain.meta or {}).get("wayback_text")
            md, brief = generate_homepage_markdown(
                topic=site.topic,
                tier=Tier(site.domain.tier),
                language=site.language,
                wayback_context=wayback_text,
            )
            site.homepage_html = render_homepage_html(md)
            meta = dict(site.meta or {})
            meta["homepage_markdown"] = md
            if brief:
                meta["homepage_brief"] = brief
            if wayback_text:
                meta["homepage_wayback_used"] = True
            site.meta = meta
            # If this was a fresh draft, flip to live so the blog serves
            site.status = SiteStatus.LIVE if prev_status == SiteStatus.DRAFT else prev_status
            await session.commit()
            log.info("homepage generated for site %s (%s)", site.id, site.domain.name)
        except Exception as e:  # noqa: BLE001
            site.status = prev_status
            await session.commit()
            log.exception("homepage generation failed for site %s: %s", site_id, e)


async def _publish(post_id: int) -> None:
    async with SessionLocal() as session:
        stmt = (
            select(Post)
            .options(joinedload(Post.site).joinedload(Site.domain))
            .where(Post.id == post_id)
        )
        post = (await session.execute(stmt)).scalar_one_or_none()
        if not post:
            return
        post.status = PostStatus.PUBLISHED
        post.published_at = datetime.now(timezone.utc)
        if post.site.status == SiteStatus.DRAFT:
            post.site.status = SiteStatus.LIVE
        await session.commit()
        host = post.site.domain.name
        indexnow_submit(host, [f"https://{host}/{post.slug}", f"https://{host}/"])
        # Google: best-effort sitemap resubmission. Skipped if GSC not set up.
        try:
            from app.services.gsc import submit_sitemap

            submit_sitemap(f"https://{host}/", f"https://{host}/sitemap.xml")
        except Exception:  # noqa: BLE001
            log.debug("GSC sitemap submit skipped", exc_info=True)


def generate_post_job(post_id: int, topic: str) -> None:
    asyncio.run(_generate_post(post_id, topic))


def generate_homepage_job(site_id: int) -> None:
    asyncio.run(_generate_homepage(site_id))


def publish_post_job(post_id: int) -> None:
    asyncio.run(_publish(post_id))
