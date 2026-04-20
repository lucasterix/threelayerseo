"""Generate Schema.org JSON-LD for blog posts.

Article + optional MedicalWebPage for the healthcare / life-science
tiers. We inject the JSON straight into the rendered blog HTML head.
"""
from __future__ import annotations

from datetime import datetime, timezone

from app.models import Post, Site


HEALTHCARE_CATEGORIES = {"healthcare", "life-science", "pharma", "medtech", "nutrition", "psychology"}


def article_schema(site: Site, post: Post, host: str) -> dict:
    now_iso = (post.published_at or post.updated_at or datetime.now(timezone.utc)).isoformat()
    published = (post.published_at or post.created_at).isoformat()
    base_type: list[str] = ["Article"]
    if site.domain.category in HEALTHCARE_CATEGORIES:
        base_type.append("MedicalWebPage")

    data: dict = {
        "@context": "https://schema.org",
        "@type": base_type if len(base_type) > 1 else base_type[0],
        "headline": post.title,
        "description": post.meta_description or "",
        "author": {"@type": "Person", "name": site.title},
        "publisher": {
            "@type": "Organization",
            "name": site.title,
            "url": f"https://{host}/",
        },
        "datePublished": published,
        "dateModified": now_iso,
        "mainEntityOfPage": {
            "@type": "WebPage",
            "@id": f"https://{host}/{post.slug}",
        },
        "inLanguage": site.language or "de",
    }
    if post.featured_image_path:
        data["image"] = f"https://{host}/media/{post.featured_image_path}"
    return data
