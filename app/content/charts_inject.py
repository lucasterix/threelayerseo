"""Resolve [[CHART:...]] placeholders in generated markdown.

Pipeline step: after the writer produces an article, find every
``[[CHART:description]]`` placeholder, plan the Chart.js config via
Haiku, render to PNG via QuickChart, save to shared volume, replace
the placeholder with ``![caption](/media/chart-<slug>-<n>.png)``.
"""
from __future__ import annotations

import logging
import re

from slugify import slugify

from app.services import budget
from app.services.chart_planner import plan
from app.services.charts import render

log = logging.getLogger(__name__)

_CHART_RE = re.compile(r"\[\[CHART:([^\]]+)\]\]")


async def inject(markdown: str, post_slug: str, post_id: int, site_id: int) -> str:
    """Return markdown with every [[CHART:...]] replaced by a rendered PNG.

    Placeholders whose rendering fails are silently dropped so the final
    article has no dangling bracket syntax. Each successful render
    emits a budget event.
    """
    matches = list(_CHART_RE.finditer(markdown))
    if not matches:
        return markdown

    replacements: list[tuple[int, int, str]] = []   # (start, end, replacement)

    for idx, m in enumerate(matches, start=1):
        description = m.group(1).strip()
        config = plan(description)
        if not config:
            replacements.append((m.start(), m.end(), ""))
            continue

        filename = f"chart-{slugify(post_slug)[:60]}-{post_id}-{idx}.png"
        saved = render(config, filename=filename)
        if not saved:
            replacements.append((m.start(), m.end(), ""))
            continue

        title = (
            (config.get("options", {}).get("plugins", {}).get("title", {}).get("text"))
            or description
        )
        md_img = f"![{title}](/media/{saved})"
        replacements.append((m.start(), m.end(), md_img))
        await budget.track(
            "anthropic", "chart-planner", site_id=site_id, post_id=post_id, note=description[:120]
        )

    # Apply replacements back-to-front so indices stay valid.
    out = markdown
    for start, end, repl in reversed(replacements):
        out = out[:start] + repl + out[end:]
    return out
