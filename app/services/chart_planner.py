"""LLM-generated Chart.js configs from natural-language descriptions (OpenAI).

The writer emits placeholders of the form
``[[CHART:short description with realistic numbers]]``. The pipeline
calls ``plan`` with each description to get a complete Chart.js config
object, then hands it to the charts renderer.
"""
from __future__ import annotations

import logging

from app.services.llm import complete_json

log = logging.getLogger(__name__)

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

Return ONLY a JSON object with the Chart.js config fields at the top
level (type, data, options). Example shape:
{"type":"bar","data":{"labels":[...],"datasets":[...]},"options":{...}}"""


def plan(description: str) -> dict | None:
    try:
        data = complete_json(
            SYSTEM_PROMPT,
            f"Chart description: {description}",
            max_tokens=1500,
            strict=False,
        )
    except Exception as e:  # noqa: BLE001
        log.warning("chart planner failed: %s", e)
        return None
    if not isinstance(data, dict) or not data.get("type"):
        return None
    return data
