"""Content generation + publishing jobs."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import joinedload

from app.content.pipeline import generate_post
from app.db import SessionLocal
from app.models import Post, PostStatus, Site, SiteStatus
from app.services.indexing import indexnow_submit

log = logging.getLogger(__name__)


async def _generate(post_id: int, topic: str) -> None:
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
        except Exception as e:  # noqa: BLE001
            post.status = PostStatus.FAILED
            await session.commit()
            log.exception("content generation failed for post %s: %s", post_id, e)


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


def generate_post_job(post_id: int, topic: str) -> None:
    asyncio.run(_generate(post_id, topic))


def publish_post_job(post_id: int) -> None:
    asyncio.run(_publish(post_id))
