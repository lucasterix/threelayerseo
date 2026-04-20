"""Generate Schema.org JSON-LD for blog posts + homepage.

Article + MedicalWebPage for healthcare/life-science tiers. WebSite +
ItemList for the homepage. BreadcrumbList helper that renderer
embeds on post pages.
"""
from __future__ import annotations

from datetime import datetime, timezone

from app.models import Post, Site


HEALTHCARE_CATEGORIES = {
    "healthcare", "life-science", "pharma", "medtech", "nutrition", "psychology",
}


def _publisher(site: Site, host: str) -> dict:
    return {
        "@type": "Organization",
        "name": site.title,
        "url": f"https://{host}/",
    }


def article_schema(site: Site, post: Post, host: str) -> dict:
    now = datetime.now(timezone.utc)
    published = (post.published_at or post.created_at or now).isoformat()
    modified = (post.published_at or post.updated_at or post.created_at or now).isoformat()
    base_type: list[str] = ["Article"]
    if site.domain.category in HEALTHCARE_CATEGORIES:
        base_type.append("MedicalWebPage")

    data: dict = {
        "@context": "https://schema.org",
        "@type": base_type if len(base_type) > 1 else base_type[0],
        "headline": post.title,
        "description": post.meta_description or "",
        "author": _publisher(site, host),
        "publisher": _publisher(site, host),
        "datePublished": published,
        "dateModified": modified,
        "mainEntityOfPage": {
            "@type": "WebPage",
            "@id": f"https://{host}/{post.slug}",
        },
        "url": f"https://{host}/{post.slug}",
        "inLanguage": site.language or "de",
    }
    if post.featured_image_path:
        data["image"] = f"https://{host}/media/{post.featured_image_path}"
    if post.primary_keyword:
        data["keywords"] = post.primary_keyword
    if site.domain.category:
        data["articleSection"] = site.domain.category.replace("-", " ").title()
    return data


def breadcrumb_schema(site: Site, post: Post | None, host: str) -> dict:
    items = [{
        "@type": "ListItem",
        "position": 1,
        "name": site.title,
        "item": f"https://{host}/",
    }]
    if post is not None:
        items.append({
            "@type": "ListItem",
            "position": 2,
            "name": post.title,
            "item": f"https://{host}/{post.slug}",
        })
    return {
        "@context": "https://schema.org",
        "@type": "BreadcrumbList",
        "itemListElement": items,
    }


def website_schema(site: Site, host: str) -> dict:
    return {
        "@context": "https://schema.org",
        "@type": "WebSite",
        "name": site.title,
        "url": f"https://{host}/",
        "description": site.topic or site.title,
        "inLanguage": site.language or "de",
        "publisher": _publisher(site, host),
        "potentialAction": {
            "@type": "SearchAction",
            "target": f"https://{host}/?q={{search_term_string}}",
            "query-input": "required name=search_term_string",
        },
    }


def item_list_schema(site: Site, posts: list[Post], host: str) -> dict:
    items = []
    for idx, p in enumerate(posts, start=1):
        items.append({
            "@type": "ListItem",
            "position": idx,
            "url": f"https://{host}/{p.slug}",
            "name": p.title,
        })
    return {
        "@context": "https://schema.org",
        "@type": "ItemList",
        "itemListOrder": "Descending",
        "numberOfItems": len(posts),
        "itemListElement": items,
    }
