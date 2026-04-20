"""Homepage copy generator.

Produces hero + about sections for a site's landing page. Runs the same
two-stage pipeline as posts but with a homepage-specific prompt and
a shorter word budget.
"""
from __future__ import annotations

import logging

import markdown as md_lib
from anthropic import Anthropic

from app.config import settings
from app.content.research import research
from app.models import Tier

log = logging.getLogger(__name__)

HOMEPAGE_MODEL = "claude-haiku-4-5-20251001"

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


def generate_homepage_markdown(topic: str, tier: Tier, language: str = "de") -> tuple[str, dict | None]:
    if not settings.anthropic_api_key:
        raise RuntimeError("ANTHROPIC_API_KEY not set")

    # Light research to ground the copy in real topic framing. Skippable —
    # for bare site bootstraps we tolerate a failure here.
    brief: dict | None
    try:
        brief = research(topic=topic, primary_keyword=topic, language=language)
    except Exception:  # noqa: BLE001
        log.warning("homepage research skipped", exc_info=True)
        brief = None

    client = Anthropic(api_key=settings.anthropic_api_key)
    user = (
        f"Topic: {topic}\nLanguage: {language}\nTier voice: {TIER_STYLE[tier]}\n"
        + (f"Research brief:\n{brief}\n" if brief else "")
        + "Produce the homepage Markdown now."
    )
    resp = client.messages.create(
        model=HOMEPAGE_MODEL,
        max_tokens=1500,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user}],
    )
    md = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
    return md.strip(), brief


def render_homepage_html(markdown_text: str) -> str:
    return md_lib.markdown(markdown_text, extensions=["extra", "sane_lists"])
