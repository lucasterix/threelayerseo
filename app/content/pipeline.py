"""Content pipeline glue.

Takes a site + topic/keyword, runs research -> competitor analysis ->
write (stylometric) -> resolve-backlinks -> suggest-internal-links ->
markdown-to-html -> schema.org -> cost tracking, and returns a fully
materialized Post.
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
from app.models import Backlink, Post, Site, Tier
from app.services import budget
from app.services.competitor import compact_brief, top_results
from app.services.internal_linking import inject_internal_links, suggest_for_post
from app.services.stylometry import pick_profile

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
    stylometric_profile: str


async def generate_post(
    session: AsyncSession,
    site: Site,
    topic: str,
    primary_keyword: str,
    backlink_slots: int = 2,
    *,
    use_competitor_serp: bool = True,
    existing_post: Post | None = None,
) -> GeneratedPost:
    # 1. Research
    brief = research(topic=topic, primary_keyword=primary_keyword, language=site.language)
    await budget.track("openai", "research", site_id=site.id, post_id=existing_post.id if existing_post else None, note=primary_keyword)

    # 2. Competitor SERP (best-effort)
    competitor_brief = None
    if use_competitor_serp:
        try:
            results = top_results(primary_keyword, language=site.language)
            competitor_brief = compact_brief(results) or None
            if results:
                await budget.track("dataforseo", "serp", site_id=site.id, post_id=existing_post.id if existing_post else None, note=primary_keyword)
        except Exception:  # noqa: BLE001
            log.warning("competitor SERP skipped", exc_info=True)

    # 3. Write (stylometric profile deterministic per-post)
    profile = pick_profile(
        post_id=existing_post.id if existing_post else None,
        site_id=site.id,
    )
    raw, profile_name = write_post(
        brief=brief,
        tier=Tier(site.domain.tier),
        primary_keyword=primary_keyword,
        language=site.language,
        backlink_slots=backlink_slots,
        profile=profile,
        competitor_brief=competitor_brief,
    )
    await budget.track("anthropic", "writer", site_id=site.id, post_id=existing_post.id if existing_post else None, note=primary_keyword)

    # 4. Parse frontmatter + slug/title/desc (with fallbacks if Claude
    # skipped the YAML header).
    parsed = frontmatter.loads(raw)
    fm = parsed.metadata or {}
    title = str(fm.get("title") or topic).strip()
    slug = str(fm.get("slug") or slugify(title))
    description = str(fm.get("description") or "").strip()
    if not description and parsed.content:
        # First non-heading paragraph, clipped to a meta-description length.
        for line in parsed.content.splitlines():
            s = line.strip()
            if not s or s.startswith("#") or s.startswith("!!!") or s.startswith("[[") or s.startswith(">"):
                continue
            description = s[:155].rstrip() + ("…" if len(s) > 155 else "")
            break

    # 5. Tier-backlinks (external, tier-aware)
    body_md, backlinks = await resolve_placeholders(session, site, parsed.content)

    # 6. Internal links within this site
    if existing_post is not None:
        target_stub = existing_post
    else:
        target_stub = Post(id=0, site_id=site.id, title=title, slug=slug,
                           primary_keyword=primary_keyword, body_markdown=body_md,
                           status=None)  # type: ignore[arg-type]
    target_stub.body_markdown = body_md
    try:
        suggestions = await suggest_for_post(session, site, target_stub)
        if suggestions:
            body_md = inject_internal_links(body_md, suggestions)
    except Exception:  # noqa: BLE001
        log.warning("internal link suggestion failed", exc_info=True)

    # 7. Resolve [[CHART:...]] placeholders into rendered PNGs.
    from app.content.charts_inject import inject as inject_charts

    try:
        body_md = await inject_charts(
            body_md,
            post_slug=slug,
            post_id=existing_post.id if existing_post else 0,
            site_id=site.id,
        )
    except Exception:  # noqa: BLE001
        log.warning("chart injection failed", exc_info=True)

    # 8. Markdown -> HTML (admonition enables !!! note / !!! quote callouts
    # which the tier-good template styles as key-takeaway and pull-quote
    # boxes. attr_list lets the writer attach CSS classes to headings.)
    body_html = md_lib.markdown(
        body_md,
        extensions=["extra", "toc", "sane_lists", "admonition", "tables", "attr_list"],
    )

    return GeneratedPost(
        title=title,
        slug=slug,
        description=description,
        primary_keyword=primary_keyword,
        body_markdown=body_md,
        body_html=body_html,
        research_json=brief,
        backlinks=backlinks,
        stylometric_profile=profile_name,
    )
