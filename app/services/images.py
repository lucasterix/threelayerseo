"""OpenAI Images API client.

Generates a featured image for a blog post, downloads it to a shared
volume (``settings.images_dir``), and returns the filename so the
renderer can serve it via /media/<filename>.

Model default is dall-e-3 standard 1024x1024 ($0.040/image). Override
via OPENAI_IMAGE_MODEL / OPENAI_IMAGE_QUALITY / OPENAI_IMAGE_SIZE in
.env — e.g. OPENAI_IMAGE_MODEL=dall-e-2 drops cost to $0.020.
"""
from __future__ import annotations

import logging
from pathlib import Path

import httpx
from openai import OpenAI
from slugify import slugify

from app.config import settings

log = logging.getLogger(__name__)


class ImageError(RuntimeError):
    pass


def _client() -> OpenAI:
    if not settings.openai_api_key:
        raise ImageError("OPENAI_API_KEY not set")
    return OpenAI(api_key=settings.openai_api_key)


def build_prompt(title: str, topic: str, style_hint: str = "") -> str:
    """Shape a prompt that biases DALL-E toward editorial blog-hero imagery.

    Avoids text in images (DALL-E 3 is notoriously bad at legible text),
    leans on concrete subject matter for the niche, skips human faces to
    sidestep uncanny-valley issues.
    """
    base = (
        f"Editorial blog hero image for an article titled '{title}'. "
        f"Niche: {topic}. "
        "Natural color palette, clean composition, no text or labels. "
        "Avoid close-ups of human faces. "
        "Photography style, soft lighting, realistic. "
        "Wide aspect composition."
    )
    if style_hint:
        base += f" Style: {style_hint}"
    return base


def generate_for_post(
    *,
    post_id: int,
    slug: str,
    title: str,
    topic: str,
    extra_style: str = "",
) -> tuple[str, str] | None:
    """Generate image, persist to the shared volume, return (filename, prompt).

    None when image generation is disabled or fails — callers log and
    continue without blocking the pipeline.
    """
    prompt = build_prompt(title, topic, style_hint=extra_style)

    try:
        client = _client()
    except ImageError as e:
        log.warning("image skipped: %s", e)
        return None

    model = settings.openai_image_model
    size = settings.openai_image_size
    quality = settings.openai_image_quality

    try:
        if model.startswith("dall-e-3"):
            resp = client.images.generate(
                model=model, prompt=prompt, size=size, quality=quality, n=1
            )
        elif model.startswith("dall-e-2"):
            # dall-e-2 has no quality param, only size (256/512/1024).
            resp = client.images.generate(
                model=model, prompt=prompt, size=size, n=1
            )
        else:  # gpt-image-1 and similar
            resp = client.images.generate(
                model=model, prompt=prompt, size=size, quality=quality, n=1
            )
    except Exception as e:  # noqa: BLE001
        log.warning("image generate failed for post %s: %s", post_id, e)
        return None

    image_url = resp.data[0].url if resp.data else None
    if not image_url:
        return None

    directory = Path(settings.images_dir)
    directory.mkdir(parents=True, exist_ok=True)
    filename = f"{post_id}-{slugify(slug)[:80]}.png"
    filepath = directory / filename

    try:
        with httpx.stream("GET", image_url, timeout=60, follow_redirects=True) as r:
            r.raise_for_status()
            with open(filepath, "wb") as f:
                for chunk in r.iter_bytes():
                    f.write(chunk)
    except Exception as e:  # noqa: BLE001
        log.warning("image download failed for post %s: %s", post_id, e)
        return None

    log.info("image saved: %s", filepath)
    return filename, prompt
