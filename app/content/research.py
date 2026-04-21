"""Stage 1 of the content pipeline: research brief for a blog post.

Returns a structured JSON brief (summary, key_facts, outline, angle,
serp_landscape) that feeds ``content.writer``.

Uses the shared LLM helper → GPT-4o-mini (OpenAI) with Anthropic Haiku
fallback. Originally this used OpenAI's Deep Research / Responses API
with web_search_preview; that's gated behind a subscription tier we
don't have yet, so the brief is compiled from the model's parametric
knowledge. The writer is still told to stay grounded: no invented
sources, no fabricated statistics.
"""
from __future__ import annotations

import logging

from app.services.llm import LlmError, complete_json

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """You research blog-post briefs for German SEO content.
Given a topic + primary keyword, compile a structured brief.

Output a JSON object with exactly these keys:
- summary (string, 2-3 sentences)
- key_facts (array of {fact: string, source_url: string})
  — pick only facts you are confident about. If you don't know a real
  source, use "" for source_url rather than inventing one.
- outline (array of section headings — 5-8 entries)
- recommended_angle (string, 1-2 sentences — what angle the writer
  should take to beat the existing SERP)
- serp_landscape (string describing the kind of results that likely
  already rank for the keyword)

Return ONLY the JSON object. No prose, no code fences."""


def research(topic: str, primary_keyword: str, language: str = "de") -> dict:
    user = (
        f"Topic: {topic}\nPrimary keyword: {primary_keyword}\nLanguage: {language}\n"
        "Compile the brief now."
    )
    try:
        data = complete_json(SYSTEM_PROMPT, user, max_tokens=2500, strict=False)
    except LlmError as e:
        log.warning("research LLM failed: %s", e)
        return {
            "summary": "",
            "key_facts": [],
            "outline": [],
            "recommended_angle": "",
            "serp_landscape": "",
        }
    if not isinstance(data, dict):
        return {"summary": "", "key_facts": [], "outline": [], "recommended_angle": "", "serp_landscape": ""}
    # Light shape normalisation so the writer sees a consistent object
    return {
        "summary": str(data.get("summary") or ""),
        "key_facts": list(data.get("key_facts") or []),
        "outline": list(data.get("outline") or []),
        "recommended_angle": str(data.get("recommended_angle") or ""),
        "serp_landscape": str(data.get("serp_landscape") or ""),
    }
