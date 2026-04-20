"""Stage 2 of the content pipeline: write the article with Anthropic Claude.

Takes the research brief from ``content.research`` and produces the final
article as Markdown, targeted at the tier's quality level. Uses a
stylometric profile to vary voice/length/temperature across posts so
they don't all read identical.
"""
from __future__ import annotations

import json
import logging

from anthropic import Anthropic

from app.config import settings
from app.models import Tier
from app.services.stylometry import StylometricProfile, pick_profile

log = logging.getLogger(__name__)

WRITER_MODEL = "claude-opus-4-7"

TIER_VOICE = {
    Tier.BAD: (
        "Casual, slightly shallow blog voice. Short paragraphs. Keyword-stuffed but "
        "still readable. Minimal sourcing."
    ),
    Tier.MEDIUM: (
        "Competent blog voice with concrete examples. Cite 2-3 "
        "sources inline. Clear headings."
    ),
    Tier.GOOD: (
        "Authoritative, polished voice suitable for a niche expert site. "
        "Cite sources. Use H2/H3 structure, examples, a summary box."
    ),
}

SYSTEM_PROMPT = """You are a German-language blog post writer. Produce a single
Markdown article from the given research brief. Include a front-matter YAML block
with fields: title, description (<=160 chars), slug (kebab-case), primary_keyword.
After the front-matter, write the article body in Markdown. Insert internal-link
placeholders of the form [[BACKLINK:anchor]] where a backlink should go — the
link graph layer will resolve these to real URLs. Do not invent sources; use only
those in the brief."""


def write_post(
    brief: dict,
    tier: Tier,
    primary_keyword: str,
    language: str = "de",
    backlink_slots: int = 2,
    *,
    profile: StylometricProfile | None = None,
    competitor_brief: str | None = None,
) -> tuple[str, str]:
    """Returns (markdown, profile_name)."""
    if not settings.anthropic_api_key:
        raise RuntimeError("ANTHROPIC_API_KEY not set")
    client = Anthropic(api_key=settings.anthropic_api_key)
    profile = profile or pick_profile()
    tier_voice = TIER_VOICE[tier]
    user_parts = [
        f"Language: {language}",
        f"Primary keyword: {primary_keyword}",
        f"Tier voice: {tier_voice}",
        f"Stil-Profil: {profile.name} — {profile.tone_hint}",
        f"Zielumfang: {profile.words_min}-{profile.words_max} Wörter.",
        f"Backlink slots to include: {backlink_slots}",
    ]
    if competitor_brief:
        user_parts.append(
            "Beat-the-SERP context (existing top results for this keyword — don't "
            "copy, but address gaps they have):\n" + competitor_brief
        )
    user_parts.append(f"Brief JSON:\n{json.dumps(brief, ensure_ascii=False, indent=2)}")
    user = "\n\n".join(user_parts)

    resp = client.messages.create(
        model=WRITER_MODEL,
        max_tokens=6000,
        temperature=profile.temperature,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user}],
    )
    markdown = "".join(block.text for block in resp.content if getattr(block, "type", "") == "text")
    return markdown, profile.name
