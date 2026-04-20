"""Server inventory + capacity maths.

For now all entries are user-maintained. Hetzner Cloud API auto-provisioning
is a TODO that plugs in here once HETZNER_API_TOKEN is configured.
"""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Server, ServerStatus, Site, SiteStatus


@dataclass
class ServerCapacity:
    server: Server
    used: int
    limit: int
    pct: float       # 0..1
    headroom: int


@dataclass
class FleetCapacity:
    servers: list[ServerCapacity]
    total_used: int
    total_limit: int
    total_pct: float
    headroom: int
    needs_new_server: bool
    # stage of urgency: "green", "yellow", "red"
    severity: str


async def fleet_capacity(session: AsyncSession) -> FleetCapacity:
    stmt = select(Server).where(Server.status == ServerStatus.ACTIVE).order_by(Server.created_at)
    servers = list((await session.execute(stmt)).scalars().all())

    per_server: list[ServerCapacity] = []
    total_used = 0
    total_limit = 0
    for s in servers:
        used = (
            await session.scalar(
                select(func.count())
                .select_from(Site)
                .where(
                    Site.server_id == s.id,
                    Site.status != SiteStatus.DISABLED,
                )
            )
        ) or 0
        limit = s.capacity_limit or 1
        per_server.append(
            ServerCapacity(
                server=s,
                used=used,
                limit=limit,
                pct=used / limit,
                headroom=max(0, limit - used),
            )
        )
        total_used += used
        total_limit += limit

    pct = (total_used / total_limit) if total_limit else 1.0
    if pct >= 0.9:
        severity = "red"
    elif pct >= 0.7:
        severity = "yellow"
    else:
        severity = "green"

    return FleetCapacity(
        servers=per_server,
        total_used=total_used,
        total_limit=total_limit,
        total_pct=pct,
        headroom=max(0, total_limit - total_used),
        needs_new_server=pct >= 0.85,
        severity=severity,
    )


async def pick_least_full_server(session: AsyncSession) -> Server | None:
    """Return the ACTIVE server with the most headroom. None if none fit."""
    fc = await fleet_capacity(session)
    fit = [c for c in fc.servers if c.headroom > 0]
    if not fit:
        return None
    fit.sort(key=lambda c: c.pct)
    return fit[0].server
