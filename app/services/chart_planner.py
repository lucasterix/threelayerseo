"""LLM-generated Chart.js configs from natural-language descriptions.

The writer emits placeholders of the form
``[[CHART:short description with realistic numbers]]``. The pipeline
calls ``plan`` with each description to get a complete Chart.js config
object, then hands it to the charts renderer.

Using Claude Haiku because the task is cheap + structured.
"""
from __future__ import annotations

import json
import logging

from anthropic import Anthropic

from app.config import settings

log = logging.getLogger(__name__)

MODEL = "claude-haiku-4-5-20251001"

SYSTEM_PROMPT = """You are a data-visualization assistant. Given a
short description of a chart, produce a complete Chart.js v4 config
object as JSON.

Rules:
- Use realistic representative numbers (approximations, not invented
  statistics; don't present them as authoritative — the writer adds
  their own caveats).
- Pick the chart type that best communicates the point (bar, line,
  doughnut, horizontalBar, combo).
- Include a clear German title at options.plugins.title.text and
  options.plugins.title.display=true.
- Use a soft editorial palette (muted blues, teals, ambers, pinks) —
  not neon.
- Keep labels concise; at most 8 data points on bar/line, 5 on
  doughnut/pie.
- Include axis labels when useful (options.scales.x.title / .y.title).
- Include legend only when there are 2+ datasets.

Return ONLY the JSON object. No prose, no code fences, no markdown."""


def plan(description: str) -> dict | None:
    if not settings.anthropic_api_key:
        return None
    client = Anthropic(api_key=settings.anthropic_api_key)
    try:
        resp = client.messages.create(
            model=MODEL,
            max_tokens=1500,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": f"Chart description: {description}"}],
        )
    except Exception as e:  # noqa: BLE001
        log.warning("chart plan LLM call failed: %s", e)
        return None

    text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text").strip()
    # Strip potential fences if the model slipped
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        log.warning("chart plan: invalid JSON, skipping")
        return None
