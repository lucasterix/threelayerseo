"""Pull Search Console performance metrics via the API.

Returns clicks / impressions / ctr / position aggregated over the last
N days. Falls back to an empty result when GSC isn't configured or the
property isn't yet verified.

Cache: we store the last fetch in Domain.meta["gsc_metrics"] so a
dashboard view doesn't hit Google on every page load.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, timedelta

import httpx

log = logging.getLogger(__name__)

API_BASE = "https://searchconsole.googleapis.com/webmasters/v3"


@dataclass
class PerformanceBucket:
    date_range: str          # e.g. "2026-03-20 → 2026-04-19"
    clicks: int
    impressions: int
    ctr: float               # 0..1
    avg_position: float


def query_performance(site_url: str, days: int = 28) -> PerformanceBucket | None:
    """Aggregate clicks/impressions for the last ``days`` on ``site_url``.

    ``site_url`` must be an exact property identifier — for domain-
    properties that's ``sc-domain:example.de``, for URL-prefix it's
    ``https://example.de/``.
    """
    from app.services.gsc import GscError, _access_token   # lazy to skip import if not configured

    try:
        token = _access_token()
    except GscError as e:
        log.debug("GSC metrics skipped: %s", e)
        return None

    end = date.today()
    start = end - timedelta(days=days)
    try:
        from urllib.parse import quote

        r = httpx.post(
            f"{API_BASE}/sites/{quote(site_url, safe='')}/searchAnalytics/query",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "startDate": start.isoformat(),
                "endDate": end.isoformat(),
                "dimensions": [],                # aggregate only
                "rowLimit": 1,
            },
            timeout=30,
        )
        r.raise_for_status()
        rows = r.json().get("rows") or []
    except Exception as e:  # noqa: BLE001
        log.warning("GSC metrics query failed for %s: %s", site_url, e)
        return None

    if not rows:
        return PerformanceBucket(
            date_range=f"{start} → {end}", clicks=0, impressions=0, ctr=0.0, avg_position=0.0
        )
    r = rows[0]
    return PerformanceBucket(
        date_range=f"{start} → {end}",
        clicks=int(r.get("clicks") or 0),
        impressions=int(r.get("impressions") or 0),
        ctr=float(r.get("ctr") or 0.0),
        avg_position=float(r.get("position") or 0.0),
    )


def top_queries(site_url: str, days: int = 28, limit: int = 20) -> list[dict]:
    """Top queries the site ranks for in the last ``days``."""
    from app.services.gsc import GscError, _access_token

    try:
        token = _access_token()
    except GscError:
        return []

    end = date.today()
    start = end - timedelta(days=days)
    try:
        from urllib.parse import quote

        r = httpx.post(
            f"{API_BASE}/sites/{quote(site_url, safe='')}/searchAnalytics/query",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "startDate": start.isoformat(),
                "endDate": end.isoformat(),
                "dimensions": ["query"],
                "rowLimit": limit,
            },
            timeout=30,
        )
        r.raise_for_status()
        return r.json().get("rows") or []
    except Exception:  # noqa: BLE001
        return []
