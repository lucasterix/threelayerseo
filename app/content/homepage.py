"""Homepage copy generator (OpenAI gpt-4o-mini).

Produces hero + about sections for a site's landing page. Runs the
same two-stage pipeline as posts but with a homepage-specific prompt
and a shorter word budget.
"""
from __future__ import annotations

import logging

import markdown as md_lib

from app.content.research import research
from app.models import Tier
from app.services.llm import complete_text

log = logging.getLogger(__name__)

TIER_STYLE = {
    Tier.BAD: "simple, short sentences, slightly amateur blog voice, no jargon.",
    Tier.MEDIUM: "friendly, competent magazine voice with concrete language.",
    Tier.GOOD: "authoritative editorial voice, polished, publication-quality.",
}

SYSTEM_PROMPT = """You write German-language homepage copy for niche blog
sites. Output MARKDOWN ONLY (no YAML, no code fences). Structure:

1. An H1 with a natural site title reflecting the topic (not keyword-stuffed).
2. A 1-2 sentence tagline directly below the H1 (no heading).
3. An "## Über diese Seite" section with 2-4 sentences introducing
   the site, the kind of content readers can expect, and who writes it.
4. An "## Was du hier findest" section listing 3-6 bullet items that
   describe the typical article categories.

Do not mention that you're an AI. Do not advertise services. Keep it
concise — the homepage's job is to frame the content list below."""


def generate_homepage_markdown(
    topic: str,
    tier: Tier,
    language: str = "de",
    wayback_context: str | None = None,
    recent_posts: list[dict] | None = None,
) -> tuple[str, dict | None]:
    """Generate homepage copy.

    ``recent_posts`` — optional list of ``{title, meta_description,
    primary_keyword}`` dicts for the site's already-published posts. If
    provided, the prompt asks the homepage to reflect the actual
    content the reader will find, so the homepage matures as the site
    accumulates posts (organic growth).
    """
    brief: dict | None
    try:
        brief = research(topic=topic, primary_keyword=topic, language=language)
    except Exception:  # noqa: BLE001
        log.warning("homepage research skipped", exc_info=True)
        brief = None

    user_parts = [
        f"Topic: {topic}",
        f"Language: {language}",
        f"Tier voice: {TIER_STYLE[tier]}",
    ]
    if wayback_context:
        user_parts.append(
            "Historical context — this domain had a prior site with the "
            "following content (Wayback Machine snapshot, may be cluttered):\n"
            "-----\n"
            f"{wayback_context}\n"
            "-----\n"
            "Take inspiration from the historical theme and vocabulary so the "
            "new site feels continuous with what existed before, but write "
            "fresh copy — don't quote or paraphrase. Preserve the semantic "
            "niche; drop outdated promotional language, dead URLs, specific "
            "brand/author names."
        )
    if recent_posts:
        lines = ["Bisher veröffentlichte Posts auf dieser Site (die Homepage soll auf sie verweisen):"]
        for p in recent_posts[:15]:
            title = p.get("title") or ""
            desc = p.get("meta_description") or ""
            lines.append(f"- {title}" + (f" — {desc[:140]}" if desc else ""))
        lines.append(
            "Baue die 'Was du hier findest'-Liste so, dass sie diese Themen-"
            "Cluster widerspiegelt. Wenn offensichtliche Content-Cluster da "
            "sind, gruppiere sie in Unterpunkten. Falls Posts eine klare "
            "Experten-Positionierung nahelegen (z.B. bestimmte Region, "
            "bestimmte Zielgruppe), übernimm das in der Intro."
        )
        user_parts.append("\n".join(lines))
    if brief:
        user_parts.append(f"Research brief:\n{brief}")
    user_parts.append("Produce the homepage Markdown now.")
    user = "\n\n".join(user_parts)

    md = complete_text(SYSTEM_PROMPT, user, max_tokens=1500)
    return md.strip(), brief


def render_homepage_html(markdown_text: str) -> str:
    return md_lib.markdown(markdown_text, extensions=["extra", "sane_lists"])


def extract_meta_description(markdown_text: str) -> str:
    """Pull a 110–160 char meta-description from the homepage markdown.

    Strategy: grab the first non-heading paragraph (typically the tagline
    directly under the H1), strip markdown, collapse whitespace, hard-cap
    at 160 chars on a word boundary. Falls back to the first H1 if the
    tagline is missing.
    """
    import re

    fallback = ""
    for raw in markdown_text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#"):
            if not fallback:
                fallback = re.sub(r"^#+\s*", "", line).strip()
            continue
        if line.startswith(("-", "*", ">", "|")):
            continue
        # First content paragraph wins.
        text = re.sub(r"[*_`]", "", line)
        text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
        text = re.sub(r"\s+", " ", text).strip()
        if len(text) >= 60:
            break
    else:
        text = fallback

    if len(text) <= 160:
        return text
    cut = text[:160].rsplit(" ", 1)[0]
    return cut.rstrip(",;:") + "…"
