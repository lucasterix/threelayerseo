"""Minimal RSS 2.0 feed generator for a blog site."""
from __future__ import annotations

from datetime import datetime, timezone
from html import escape
from typing import Sequence

from app.models import Post, Site


def _rfc822(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.strftime("%a, %d %b %Y %H:%M:%S %z")


def render(site: Site, posts: Sequence[Post], host: str) -> str:
    base = f"https://{host}"
    now = datetime.now(timezone.utc)
    items: list[str] = []
    for p in posts:
        pub = _rfc822(p.published_at or p.created_at or now)
        desc = p.meta_description or (p.body_markdown or "")[:280].replace("\n", " ")
        item_url = f"{base}/{p.slug}"
        items.append(
            "<item>\n"
            f"  <title>{escape(p.title)}</title>\n"
            f"  <link>{item_url}</link>\n"
            f"  <guid isPermaLink='true'>{item_url}</guid>\n"
            f"  <pubDate>{pub}</pubDate>\n"
            f"  <description>{escape(desc)}</description>\n"
            "</item>"
        )
    body = (
        "<?xml version='1.0' encoding='UTF-8'?>\n"
        "<rss version='2.0' xmlns:atom='http://www.w3.org/2005/Atom'>\n"
        "<channel>\n"
        f"  <title>{escape(site.title)}</title>\n"
        f"  <link>{base}/</link>\n"
        f"  <description>{escape(site.topic or site.title)}</description>\n"
        f"  <language>{site.language or 'de'}</language>\n"
        f"  <lastBuildDate>{_rfc822(now)}</lastBuildDate>\n"
        f"  <atom:link href='{base}/rss.xml' rel='self' type='application/rss+xml'/>\n"
        + "\n".join(items)
        + "\n</channel>\n</rss>\n"
    )
    return body
