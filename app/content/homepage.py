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
) -> tuple[str, dict | None]:
    # Light research to ground the copy in real topic framing. Skippable —
    # for bare site bootstraps we tolerate a failure here.
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
    if brief:
        user_parts.append(f"Research brief:\n{brief}")
    user_parts.append("Produce the homepage Markdown now.")
    user = "\n\n".join(user_parts)

    md = complete_text(SYSTEM_PROMPT, user, max_tokens=1500)
    return md.strip(), brief


def render_homepage_html(markdown_text: str) -> str:
    return md_lib.markdown(markdown_text, extensions=["extra", "sane_lists"])
