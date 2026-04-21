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
        "Authoritative, data-driven, editorial voice — in the spirit of Backlinko "
        "or Ahrefs studies. Every major claim anchored to a concrete number or "
        "citation. Scannable structure with rich visual callouts."
    ),
}

TIER_STRUCTURE = {
    Tier.GOOD: """Structure (backlinko-style):
1. Opening hook in 1-2 sentences with a concrete number.
2. A "## Kernergebnisse" (TL;DR) section — numbered list of 5-9 bullet
   findings, each with a hard number.
3. An introductory paragraph explaining methodology / why you analysed X.
4. H2 sections, each with:
   - ONE descriptive finding headline ("Posts mit X ranken 3× besser als Y")
   - 2-3 paragraphs explaining + example
   - A chart placeholder where data would strengthen the point:
     [[CHART:short description with realistic numbers]]
   - End each section with a key-takeaway box:
     !!! note "Kernpunkt"
         Einzelsatz, was der Leser mitnehmen soll.
   - Use pull-quotes once or twice with:
     !!! quote
         Die zitierte Aussage.
5. A closing "## Fazit" with the three most important practical steps.

Include at least ONE chart placeholder in the article. Use 2-3
Kernpunkt-Boxen across the article. The admonition syntax (!!! note,
!!! quote) requires the indented content on the next line.
""",
    Tier.MEDIUM: """Structure:
- Brief intro.
- 4-6 H2 sections with clear headlines.
- Lists where they help (numbered how-tos, bullet comparisons).
- Optional [[CHART:description]] for the one key comparison.
- Short "## Fazit" or "## Zusammenfassung" at the end.
""",
    Tier.BAD: """Structure:
- Straightforward intro.
- 3-4 H2 sections, short paragraphs.
- A final "## Fazit" one-paragraph takeaway.
""",
}

SYSTEM_PROMPT = """You are a German-language blog post writer. Produce a single
Markdown article from the given research brief. Include a front-matter YAML block
with fields: title, description (<=160 chars), slug (kebab-case), primary_keyword.

After the front-matter, write the article body in Markdown following the
structural guidance for the site's tier.

Placeholders the pipeline resolves for you:
- [[BACKLINK:anchor text]] — becomes a link to another site in our network
  (tier-validated, flowing upward). Insert 1-3 per article.
- [[CHART:short description with numbers]] — becomes a rendered Chart.js
  image. Use for comparisons, trends over time, proportions.

Do not invent sources or specific statistics you weren't given; when
representing data in a chart, describe it as "beispielhafte Verteilung"
or similar so the reader understands it's illustrative."""


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
    tier_structure = TIER_STRUCTURE.get(tier, "")
    user_parts = [
        f"Language: {language}",
        f"Primary keyword: {primary_keyword}",
        f"Tier voice: {tier_voice}",
        f"Stil-Profil: {profile.name} — {profile.tone_hint}",
        f"Zielumfang: {profile.words_min}-{profile.words_max} Wörter.",
        f"Backlink slots to include: {backlink_slots}",
        tier_structure,
    ]
    if competitor_brief:
        user_parts.append(
            "Beat-the-SERP context (existing top results for this keyword — don't "
            "copy, but address gaps they have):\n" + competitor_brief
        )
    user_parts.append(f"Brief JSON:\n{json.dumps(brief, ensure_ascii=False, indent=2)}")
    user = "\n\n".join(user_parts)

    # Claude Opus 4.7+ no longer accepts `temperature`. Older models still
    # support it — useful for the stylometric profile to inject variation.
    # Rather than hard-coding "which model takes what", try with temperature
    # and transparently retry without on a 400-deprecated error.
    create_kwargs: dict = {
        "model": WRITER_MODEL,
        "max_tokens": 6000,
        "system": SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": user}],
    }
    if not WRITER_MODEL.startswith(("claude-opus-4-7", "claude-opus-5")):
        create_kwargs["temperature"] = profile.temperature

    try:
        resp = client.messages.create(**create_kwargs)
    except Exception as e:  # noqa: BLE001
        msg = str(e).lower()
        if "temperature" in msg and "deprecated" in msg and "temperature" in create_kwargs:
            log.info("writer: retrying without temperature (deprecated for %s)", WRITER_MODEL)
            create_kwargs.pop("temperature", None)
            resp = client.messages.create(**create_kwargs)
        else:
            raise
    markdown = "".join(block.text for block in resp.content if getattr(block, "type", "") == "text")
    return markdown, profile.name
