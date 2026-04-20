"""Internal-linking suggestions within the same site.

Simple and reliable approach: for a given target post, find other
published posts on the same site whose primary_keyword or title shares
tokens with the target's body text. Returns anchor text + target URL
pairs. The content pipeline injects 2-4 of these into the generated
markdown before rendering.

No embeddings / external APIs — pure keyword overlap. Good enough for
SEO's "related posts" notion and costs nothing.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Post, PostStatus, Site

_STOPWORDS = {
    "der", "die", "das", "und", "oder", "aber", "wie", "was", "wer", "wo",
    "wann", "ein", "eine", "einen", "einem", "einer", "in", "im", "auf",
    "mit", "für", "von", "zu", "am", "an", "bei", "nach", "vor", "über",
    "unter", "sich", "nicht", "auch", "noch", "schon", "mehr", "sehr",
    "this", "that", "the", "and", "for", "with", "from", "about", "which",
    "what", "have", "does", "your",
}


def _tokenize(text: str) -> set[str]:
    return {
        t for t in re.findall(r"[a-zäöüß0-9]{4,}", text.lower())
        if t not in _STOPWORDS
    }


@dataclass
class LinkSuggestion:
    anchor: str
    url: str
    target_slug: str
    score: int


async def suggest_for_post(
    session: AsyncSession,
    site: Site,
    target_post: Post,
    max_suggestions: int = 4,
    exclude_post_ids: set[int] | None = None,
) -> list[LinkSuggestion]:
    """Return up to ``max_suggestions`` internal-link candidates from the
    same site, ranked by keyword overlap with ``target_post.body_markdown``.
    """
    if not target_post.body_markdown:
        return []

    exclude = set(exclude_post_ids or set()) | {target_post.id}
    body_tokens = _tokenize(target_post.body_markdown)
    if not body_tokens:
        return []

    stmt = (
        select(Post)
        .where(
            Post.site_id == site.id,
            Post.status == PostStatus.PUBLISHED,
            Post.id.notin_(exclude),
        )
    )
    candidates = list((await session.execute(stmt)).scalars().all())
    if not candidates:
        return []

    scored: list[tuple[int, Post]] = []
    for p in candidates:
        kw_tokens = _tokenize(f"{p.title} {p.primary_keyword}")
        overlap = len(kw_tokens & body_tokens)
        if overlap >= 2:
            scored.append((overlap, p))
    scored.sort(reverse=True, key=lambda t: t[0])

    suggestions: list[LinkSuggestion] = []
    for score, p in scored[:max_suggestions]:
        anchor = p.primary_keyword or p.title
        suggestions.append(
            LinkSuggestion(
                anchor=anchor,
                url=f"/{p.slug}",
                target_slug=p.slug,
                score=score,
            )
        )
    return suggestions


def inject_internal_links(markdown: str, suggestions: list[LinkSuggestion]) -> str:
    """Replace the first plaintext occurrence of each anchor with a
    markdown link, skipping anchors already inside a link and only
    once per anchor.
    """
    if not suggestions:
        return markdown
    out = markdown
    for s in suggestions:
        if not s.anchor:
            continue
        # Skip if already linked elsewhere in the text
        if f"](/{s.target_slug})" in out:
            continue
        # Match whole word, case-insensitive, first occurrence
        pattern = re.compile(rf"(?<!\[)\b{re.escape(s.anchor)}\b(?!\])", re.IGNORECASE)
        new_out, n = pattern.subn(f"[{s.anchor}]({s.url})", out, count=1)
        if n > 0:
            out = new_out
    return out
