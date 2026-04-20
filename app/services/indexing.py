"""Search-engine indexing helpers.

- IndexNow (Bing, Yandex, Seznam, Naver) — open protocol, single POST.
- Google: sitemap ping was retired in June 2023. For real Google indexing
  use Search Console API (OAuth) or the official Indexing API (limited to
  JobPosting / BroadcastEvent). This module only exposes a stub so callers
  don't branch on provider.
"""
from __future__ import annotations

import logging

import httpx

from app.config import settings

log = logging.getLogger(__name__)

INDEXNOW_ENDPOINT = "https://api.indexnow.org/indexnow"


def indexnow_submit(host: str, urls: list[str]) -> bool:
    """Submit up to 10000 URLs belonging to ``host`` to IndexNow."""
    if not settings.indexnow_key or not urls:
        return False
    payload = {
        "host": host,
        "key": settings.indexnow_key,
        "keyLocation": f"https://{host}/{settings.indexnow_key}.txt",
        "urlList": urls,
    }
    try:
        r = httpx.post(INDEXNOW_ENDPOINT, json=payload, timeout=15)
        r.raise_for_status()
        return True
    except httpx.HTTPError as e:
        log.warning("indexnow submit failed for %s: %s", host, e)
        return False


def google_submit(urls: list[str]) -> bool:
    """Stub. Real implementation requires OAuth + Search Console API.

    See https://developers.google.com/search/apis/indexing-api/v3/quickstart
    for the (limited) Indexing API, or
    https://developers.google.com/webmaster-tools/v1/searchanalytics/query
    for Search Console.
    """
    log.info("google_submit stub called with %d urls — no-op", len(urls))
    return False
