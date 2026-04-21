"""Per-site favicon generator.

Uses Pillow (no LLM round-trip — favicons are too small for DALL-E to be
useful at 32×32) to render a deterministic monogram from the site's
design tokens. The output is a single 192×192 PNG that browsers downscale
cleanly for both 32×32 and the apple-touch-icon use case.

We always overwrite the file under ``images_dir/favicons/<site_id>.png``
so re-runs are idempotent.
"""
from __future__ import annotations

import logging
import os
import re
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from app.config import settings

log = logging.getLogger(__name__)

SIZE = 192
RADIUS = 38   # rounded-square corner radius
DEFAULT_BG = "#0F172A"
DEFAULT_FG = "#FFFFFF"

_FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
]


def _font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for path in _FONT_CANDIDATES:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size=size)
            except Exception:  # noqa: BLE001
                continue
    return ImageFont.load_default()


def _initials(title: str) -> str:
    """Two-letter monogram derived from the site title."""
    words = [w for w in re.split(r"[\s\-_/.]+", title) if w]
    if not words:
        return "?"
    if len(words) == 1:
        w = words[0]
        return (w[0] + (w[1] if len(w) > 1 else "")).upper()
    return (words[0][0] + words[1][0]).upper()


def _hex(s: str | None, fallback: str) -> str:
    if not s:
        return fallback
    s = s.strip()
    if not s.startswith("#"):
        s = "#" + s
    if len(s) not in (4, 7):
        return fallback
    return s


def generate_for_site(site_id: int, title: str, design_tokens: dict | None) -> str | None:
    """Render and persist a favicon. Returns the relative path stored in
    ``Site.favicon_path`` (served by the renderer at /favicon-<id>.png)."""
    palette = (design_tokens or {}).get("palette") or {}
    bg = _hex(palette.get("primary"), DEFAULT_BG)
    fg = _hex(palette.get("surface") or palette.get("bg"), DEFAULT_FG)

    img = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle((0, 0, SIZE, SIZE), radius=RADIUS, fill=bg)

    text = _initials(title)
    # Pillow's textbbox is the reliable measurement; fit by trying sizes.
    for s in (140, 120, 100, 80, 64):
        font = _font(s)
        bbox = draw.textbbox((0, 0), text, font=font)
        w, h = bbox[2] - bbox[0], bbox[3] - bbox[1]
        if w <= SIZE * 0.78 and h <= SIZE * 0.72:
            break
    x = (SIZE - w) // 2 - bbox[0]
    y = (SIZE - h) // 2 - bbox[1]
    draw.text((x, y), text, fill=fg, font=font)

    out_dir = Path(settings.images_dir) / "favicons"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{site_id}.png"
    try:
        img.save(out_path, format="PNG", optimize=True)
    except Exception:  # noqa: BLE001
        log.warning("favicon write failed for site %s", site_id, exc_info=True)
        return None
    log.info("favicon written: %s (%s on %s)", out_path, text, bg)
    return f"favicons/{site_id}.png"
