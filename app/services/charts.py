"""Chart rendering via QuickChart.

POSTs a Chart.js config to the QuickChart service and saves the returned
PNG onto the shared images volume. No external dependency — we run
QuickChart in our own docker-compose (same internal network as the
worker), so rendering is free and private.
"""
from __future__ import annotations

import logging
from pathlib import Path

import httpx

from app.config import settings

log = logging.getLogger(__name__)

DEFAULT_WIDTH = 900
DEFAULT_HEIGHT = 450
DEFAULT_DPR = 2              # retina crispness


def render(
    chart_config: dict,
    *,
    filename: str,
    width: int = DEFAULT_WIDTH,
    height: int = DEFAULT_HEIGHT,
    device_pixel_ratio: float = DEFAULT_DPR,
    background_color: str = "white",
) -> str | None:
    """Render ``chart_config`` to a PNG on the images volume and return
    the filename (not the full path) for use in blog markdown.
    Returns None on any render failure — caller should degrade
    gracefully (leave the placeholder out of the post).
    """
    payload = {
        "chart": chart_config,
        "width": width,
        "height": height,
        "devicePixelRatio": device_pixel_ratio,
        "backgroundColor": background_color,
        "format": "png",
        "version": "4",
    }
    try:
        r = httpx.post(settings.quickchart_url, json=payload, timeout=30)
        r.raise_for_status()
    except Exception as e:  # noqa: BLE001
        log.warning("chart render failed: %s", e)
        return None

    directory = Path(settings.images_dir)
    directory.mkdir(parents=True, exist_ok=True)
    filepath = directory / filename
    try:
        with open(filepath, "wb") as f:
            f.write(r.content)
    except OSError as e:
        log.warning("chart write failed: %s", e)
        return None
    log.info("chart saved: %s (%d bytes)", filepath, len(r.content))
    return filename
