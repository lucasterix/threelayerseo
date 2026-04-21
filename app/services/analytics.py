"""Pageview tracking + aggregate queries.

Feeding:
- The renderer calls ``record`` via FastAPI BackgroundTasks on every
  successful blog page render. Bot user-agents are filtered here so we
  never write Googlebot/Bingbot hits to the DB.

Reading:
- ``totals`` gives dashboard-friendly counters (all-time / 24h / 7d / 30d).
- ``daily_series`` returns per-day buckets for the time-series chart.
- ``top_pages`` and ``top_sites`` drive the analytics table views.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlparse

from sqlalchemy import cast, Date, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.db import SessionLocal
from app.models import Domain, Pageview, Post, Site

log = logging.getLogger(__name__)


_BOT_NEEDLES = (
    "bot", "spider", "crawler", "crawling", "slurp",
    "facebookexternalhit", "embedly", "discordbot",
    "wget", "curl", "scrapy", "httpx", "python-requests",
    "go-http-client", "headlesschrome",
)


def _is_bot(user_agent: str | None) -> bool:
    if not user_agent:
        return True
    ua = user_agent.lower()
    return any(needle in ua for needle in _BOT_NEEDLES)


def _referer_host(referer: str | None) -> str | None:
    if not referer:
        return None
    try:
        host = urlparse(referer).netloc
        return host[:200] if host else None
    except Exception:  # noqa: BLE001
        return None


async def record(
    site_id: int | None,
    post_id: int | None,
    path: str,
    user_agent: str | None,
    referer: str | None,
) -> None:
    """BackgroundTask entry. Drops bots and malformed entries silently."""
    if _is_bot(user_agent):
        return
    try:
        async with SessionLocal() as session:
            pv = Pageview(
                site_id=site_id,
                post_id=post_id,
                path=(path or "/")[:200],
                referer_host=_referer_host(referer),
            )
            session.add(pv)
            await session.commit()
    except Exception:  # noqa: BLE001
        log.debug("pageview record skipped", exc_info=True)


@dataclass
class Totals:
    all_time: int
    last_24h: int
    last_7d: int
    last_30d: int


async def totals(session: AsyncSession) -> Totals:
    now = datetime.now(timezone.utc)

    async def _count(since: datetime | None = None) -> int:
        stmt = select(func.count()).select_from(Pageview)
        if since is not None:
            stmt = stmt.where(Pageview.created_at >= since)
        return int(await session.scalar(stmt) or 0)

    return Totals(
        all_time=await _count(),
        last_24h=await _count(now - timedelta(hours=24)),
        last_7d=await _count(now - timedelta(days=7)),
        last_30d=await _count(now - timedelta(days=30)),
    )


async def daily_series(session: AsyncSession, days: int = 30) -> list[dict]:
    """List of {day: 'YYYY-MM-DD', count: int} for the last ``days`` days,
    including zero-count days so the chart has a continuous x-axis.
    """
    now = datetime.now(timezone.utc)
    since = now - timedelta(days=days - 1)

    rows = await session.execute(
        select(cast(Pageview.created_at, Date), func.count())
        .where(Pageview.created_at >= since)
        .group_by(cast(Pageview.created_at, Date))
    )
    counts = {row[0]: int(row[1] or 0) for row in rows.all()}

    out: list[dict] = []
    for i in range(days):
        d = (since + timedelta(days=i)).date()
        out.append({"day": d.isoformat(), "count": counts.get(d, 0)})
    return out


async def top_pages(session: AsyncSession, days: int = 30, limit: int = 20) -> list[dict]:
    since = datetime.now(timezone.utc) - timedelta(days=days)
    rows = await session.execute(
        select(Pageview.site_id, Pageview.path, func.count())
        .where(Pageview.created_at >= since)
        .group_by(Pageview.site_id, Pageview.path)
        .order_by(func.count().desc())
        .limit(limit)
    )
    # Join for site domain names
    site_ids = {row[0] for row in rows.all() if row[0] is not None}
    rows = await session.execute(
        select(Pageview.site_id, Pageview.path, func.count())
        .where(Pageview.created_at >= since)
        .group_by(Pageview.site_id, Pageview.path)
        .order_by(func.count().desc())
        .limit(limit)
    )
    data = list(rows.all())
    if not data:
        return []
    domain_rows = await session.execute(
        select(Site.id, Domain.name)
        .join(Domain, Domain.id == Site.domain_id)
        .where(Site.id.in_({r[0] for r in data if r[0] is not None}))
    )
    domain_map = {row[0]: row[1] for row in domain_rows.all()}
    return [
        {
            "domain": domain_map.get(row[0], "—"),
            "path": row[1],
            "count": int(row[2] or 0),
        }
        for row in data
    ]


async def top_sites(session: AsyncSession, days: int = 30, limit: int = 20) -> list[dict]:
    since = datetime.now(timezone.utc) - timedelta(days=days)
    rows = (
        await session.execute(
            select(Pageview.site_id, func.count())
            .where(Pageview.created_at >= since, Pageview.site_id.is_not(None))
            .group_by(Pageview.site_id)
            .order_by(func.count().desc())
            .limit(limit)
        )
    ).all()
    if not rows:
        return []
    site_ids = [r[0] for r in rows]
    domain_rows = await session.execute(
        select(Site.id, Domain.name, Domain.tier, Domain.category)
        .join(Domain, Domain.id == Site.domain_id)
        .where(Site.id.in_(site_ids))
    )
    by_site = {row[0]: row for row in domain_rows.all()}
    out: list[dict] = []
    for sid, cnt in rows:
        meta = by_site.get(sid)
        if not meta:
            continue
        out.append({
            "site_id": sid,
            "domain": meta[1],
            "tier": meta[2].value if meta[2] else None,
            "category": meta[3],
            "count": int(cnt or 0),
        })
    return out
