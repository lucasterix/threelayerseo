"""Content generation + publishing jobs."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from slugify import slugify
from sqlalchemy import func, select
from sqlalchemy.orm import joinedload

from app.content.homepage import generate_homepage_markdown, render_homepage_html
from app.content.pipeline import generate_post
from app.db import SessionLocal
from app.models import Domain, DomainStatus, Post, PostStatus, Site, SiteStatus, Tier
from app.services import budget
from app.services.images import generate_for_post as generate_image_for_post
from app.services.indexing import indexnow_submit
from app.services.schema import article_schema

log = logging.getLogger(__name__)

DEFAULT_REFRESH_DAYS = 90


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
                existing_post=post,
            )
            post.title = result.title
            post.slug = result.slug or post.slug
            post.body_markdown = result.body_markdown
            post.body_html = result.body_html
            post.meta_description = result.description
            post.research_json = result.research_json
            post.stylometric_profile = result.stylometric_profile
            post.status = PostStatus.READY
            post.refresh_due_at = datetime.now(timezone.utc) + timedelta(days=DEFAULT_REFRESH_DAYS)
            for bl in result.backlinks:
                bl.source_post_id = post.id
                session.add(bl)
            await session.commit()
            log.info("generated post %s: %s [%s]", post.id, post.title, result.stylometric_profile)

            # Schema.org JSON-LD (regenerated on publish too, but stable enough here).
            post.schema_json = article_schema(site, post, site.domain.name)
            await session.commit()

            # Featured image (best-effort — never blocks the pipeline).
            try:
                img = generate_image_for_post(
                    post_id=post.id,
                    slug=post.slug,
                    title=post.title,
                    topic=site.topic,
                )
                if img:
                    filename, prompt_used = img
                    post.featured_image_path = filename
                    post.featured_image_prompt = prompt_used
                    await session.commit()
                    await budget.track(
                        "openai",
                        f"image-{_image_kind()}",
                        site_id=site.id,
                        post_id=post.id,
                    )
                    # schema with image
                    post.schema_json = article_schema(site, post, site.domain.name)
                    await session.commit()
            except Exception:  # noqa: BLE001
                log.warning("featured image skipped for post %s", post.id, exc_info=True)

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


def _image_kind() -> str:
    from app.config import settings
    model = settings.openai_image_model or "dall-e-3"
    return model


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
            site.status = SiteStatus.LIVE if prev_status == SiteStatus.DRAFT else prev_status
            await session.commit()
            await budget.track("openai", "homepage", site_id=site.id)
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
        post.refresh_due_at = post.published_at + timedelta(days=DEFAULT_REFRESH_DAYS)
        if post.site.status == SiteStatus.DRAFT:
            post.site.status = SiteStatus.LIVE
        # refresh schema on publish so datePublished is accurate
        post.schema_json = article_schema(post.site, post, post.site.domain.name)
        await session.commit()
        host = post.site.domain.name
        indexnow_submit(host, [f"https://{host}/{post.slug}", f"https://{host}/"])
        try:
            from app.services.gsc import submit_sitemap

            submit_sitemap(f"https://{host}/", f"https://{host}/sitemap.xml")
        except Exception:  # noqa: BLE001
            log.debug("GSC sitemap submit skipped", exc_info=True)


async def _generate_legal(site_id: int) -> None:
    async with SessionLocal() as session:
        stmt = select(Site).options(joinedload(Site.domain)).where(Site.id == site_id)
        site = (await session.execute(stmt)).scalar_one_or_none()
        if not site:
            return
        from app.services.legal import (
            generate_imprint_markdown,
            generate_privacy_markdown,
            to_html,
        )

        try:
            imprint_md = generate_imprint_markdown(site.title, site.domain.name)
            site.imprint_html = to_html(imprint_md)
            meta = dict(site.meta or {})
            meta["imprint_markdown"] = imprint_md
            site.meta = meta
        except Exception:  # noqa: BLE001
            log.exception("imprint generation failed for site %s", site_id)

        try:
            privacy_md = generate_privacy_markdown(site.title, site.domain.name)
            site.privacy_html = to_html(privacy_md)
            meta = dict(site.meta or {})
            meta["privacy_markdown"] = privacy_md
            site.meta = meta
        except Exception:  # noqa: BLE001
            log.exception("privacy generation failed for site %s", site_id)

        await session.commit()
        await budget.track("openai", "legal", site_id=site_id, note="imprint+privacy")
        log.info("legal pages generated for site %s", site_id)


async def _generate_image(post_id: int, style: str = "") -> None:
    async with SessionLocal() as session:
        stmt = (
            select(Post)
            .options(joinedload(Post.site).joinedload(Site.domain))
            .where(Post.id == post_id)
        )
        post = (await session.execute(stmt)).scalar_one_or_none()
        if not post:
            return
        img = generate_image_for_post(
            post_id=post.id,
            slug=post.slug,
            title=post.title,
            topic=post.site.topic,
            extra_style=style,
        )
        if img:
            filename, prompt_used = img
            post.featured_image_path = filename
            post.featured_image_prompt = prompt_used
            await session.commit()
            await budget.track("openai", f"image-{_image_kind()}", site_id=post.site_id, post_id=post.id)


# ─── One-click orchestration ──────────────────────────────────────────────


async def _launch_site(
    site_id: int,
    keywords: list[str],
    post_topics: list[str] | None = None,
    interval_hours: int = 24,
    auto_publish: bool = True,
) -> None:
    """Spin up an entire site in one call:

    1. Generate homepage if missing.
    2. Generate imprint + privacy if missing and operator is set.
    3. For each (keyword, topic) pair queue a post generation with a
       drip-feed scheduled_at (interval_hours apart, starting now+1min
       so the homepage goes first).
    4. Optionally auto-publish each when its scheduled_at hits.
    """
    from app.queue import content_q

    async with SessionLocal() as session:
        stmt = select(Site).options(joinedload(Site.domain)).where(Site.id == site_id)
        site = (await session.execute(stmt)).scalar_one_or_none()
        if not site:
            return

        # 1. Homepage
        if not site.homepage_html:
            await _generate_homepage(site_id)

        # 2. Legal (only if operator info present)
        from app.config import settings

        if settings.operator_name and settings.operator_email and not site.imprint_html:
            await _generate_legal(site_id)

    # 3. Posts
    topics = post_topics or keywords
    now = datetime.now(timezone.utc) + timedelta(minutes=5)
    async with SessionLocal() as session:
        for i, kw in enumerate(keywords):
            topic = topics[i] if i < len(topics) else kw
            scheduled = now + timedelta(hours=i * interval_hours) if auto_publish else None
            post = Post(
                site_id=site_id,
                slug=slugify(kw)[:200],
                title=topic[:500],
                primary_keyword=kw,
                status=PostStatus.PENDING,
                scheduled_at=scheduled,
            )
            session.add(post)
            await session.flush()
            content_q.enqueue(
                "app.jobs.content.generate_post_job",
                post.id,
                topic,
                job_timeout=900,
            )
        await session.commit()

    log.info("launched site %s with %d posts (%dh drip)", site_id, len(keywords), interval_hours)


async def _refresh_stale(limit: int = 20) -> None:
    """Cron-style: regenerate posts whose refresh_due_at has passed.

    We pick the oldest due posts first and requeue them through the
    normal generate pipeline (content is re-written with current info).
    """
    from app.queue import content_q

    async with SessionLocal() as session:
        stmt = (
            select(Post)
            .where(
                Post.refresh_due_at.is_not(None),
                Post.refresh_due_at <= func.now(),
                Post.status == PostStatus.PUBLISHED,
            )
            .order_by(Post.refresh_due_at)
            .limit(limit)
        )
        posts = list((await session.execute(stmt)).scalars().all())
        if not posts:
            return
        for post in posts:
            post.status = PostStatus.PENDING
            await session.commit()
            content_q.enqueue(
                "app.jobs.content.generate_post_job",
                post.id,
                post.title,
                job_timeout=900,
            )
            log.info("queued refresh for post %s (%s)", post.id, post.title)


# ─── RQ entry points (sync wrappers) ──────────────────────────────────────

def generate_post_job(post_id: int, topic: str) -> None:
    asyncio.run(_generate_post(post_id, topic))


def generate_homepage_job(site_id: int) -> None:
    asyncio.run(_generate_homepage(site_id))


def publish_post_job(post_id: int) -> None:
    asyncio.run(_publish(post_id))


def generate_legal_job(site_id: int) -> None:
    asyncio.run(_generate_legal(site_id))


def generate_image_job(post_id: int, style: str = "") -> None:
    asyncio.run(_generate_image(post_id, style))


def launch_site_job(
    site_id: int,
    keywords: list[str],
    post_topics: list[str] | None = None,
    interval_hours: int = 24,
    auto_publish: bool = True,
) -> None:
    asyncio.run(_launch_site(site_id, keywords, post_topics, interval_hours, auto_publish))


def refresh_stale_job(limit: int = 20) -> None:
    asyncio.run(_refresh_stale(limit))
    # Self-reschedule so the chain keeps running without a cron daemon.
    from app.queue import content_q

    content_q.enqueue_in(
        timedelta(hours=24),
        "app.jobs.content.refresh_stale_job",
        limit,
        job_timeout=3600,
    )
