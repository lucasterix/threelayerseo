"""Content pipeline glue.

Takes a site + topic/keyword, runs research -> write -> resolve-backlinks ->
markdown-to-html, and returns a fully materialized Post ready for save.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

import frontmatter
import markdown as md_lib
from slugify import slugify
from sqlalchemy.ext.asyncio import AsyncSession

from app.content.research import research
from app.content.writer import write_post
from app.linking.graph import resolve_placeholders
from app.models import Backlink, Site, Tier

log = logging.getLogger(__name__)


@dataclass
class GeneratedPost:
    title: str
    slug: str
    description: str
    primary_keyword: str
    body_markdown: str
    body_html: str
    research_json: dict
    backlinks: list[Backlink]


async def generate_post(
    session: AsyncSession,
    site: Site,
    topic: str,
    primary_keyword: str,
    backlink_slots: int = 2,
) -> GeneratedPost:
    brief = research(topic=topic, primary_keyword=primary_keyword, language=site.language)
    raw = write_post(
        brief=brief,
        tier=Tier(site.domain.tier),
        primary_keyword=primary_keyword,
        language=site.language,
        backlink_slots=backlink_slots,
    )
    parsed = frontmatter.loads(raw)
    fm = parsed.metadata or {}
    title = str(fm.get("title") or topic).strip()
    slug = str(fm.get("slug") or slugify(title))
    description = str(fm.get("description") or "").strip()
    body_md, links = await resolve_placeholders(session, site, parsed.content)
    body_html = md_lib.markdown(body_md, extensions=["extra", "toc", "sane_lists"])
    return GeneratedPost(
        title=title,
        slug=slug,
        description=description,
        primary_keyword=primary_keyword,
        body_markdown=body_md,
        body_html=body_html,
        research_json=brief,
        backlinks=links,
    )
