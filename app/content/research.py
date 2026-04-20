"""Stage 1 of the content pipeline: research via OpenAI Deep Research.

Takes a topic/keyword and returns a structured JSON research brief: key
facts, sources, suggested angle, outline. Output feeds ``content.writer``.
"""
from __future__ import annotations

import json
import logging

from openai import OpenAI

from app.config import settings

log = logging.getLogger(__name__)

# Deep research model (light variant — faster, cheaper than o3-deep-research).
DEEP_RESEARCH_MODEL = "o4-mini-deep-research-2025-06-26"

SYSTEM_PROMPT = """You are a research assistant building a brief for a blog post.
Return a JSON object with keys:
- summary (string, 2-3 sentences)
- key_facts (list of {fact, source_url})
- outline (list of section headings)
- recommended_angle (string)
- serp_landscape (string describing what already ranks)
Respond ONLY with valid JSON, no markdown fences.
"""


def research(topic: str, primary_keyword: str, language: str = "de") -> dict:
    if not settings.openai_api_key:
        raise RuntimeError("OPENAI_API_KEY not set")
    client = OpenAI(api_key=settings.openai_api_key)
    user_prompt = (
        f"Topic: {topic}\nPrimary keyword: {primary_keyword}\nLanguage: {language}\n"
        "Run deep research and return the JSON brief."
    )
    resp = client.responses.create(
        model=DEEP_RESEARCH_MODEL,
        input=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        tools=[{"type": "web_search_preview"}],
    )
    text = resp.output_text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        log.warning("deep-research returned non-JSON, wrapping as summary")
        return {"summary": text, "key_facts": [], "outline": [], "recommended_angle": "", "serp_landscape": ""}
