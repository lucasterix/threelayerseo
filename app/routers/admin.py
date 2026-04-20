from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import require_admin
from app.db import get_session
from app.models import Domain, DomainStatus, Post, PostStatus, Site, Tier

templates = Jinja2Templates(directory="app/templates")
router = APIRouter()


@router.get("/", response_class=HTMLResponse)
async def dashboard(
    request: Request,
    _: str = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    by_tier = {}
    for tier in Tier:
        total = await session.scalar(select(func.count()).select_from(Domain).where(Domain.tier == tier))
        active = await session.scalar(
            select(func.count())
            .select_from(Domain)
            .where(Domain.tier == tier, Domain.status == DomainStatus.ACTIVE)
        )
        by_tier[tier.name] = {"total": total or 0, "active": active or 0}

    sites_count = await session.scalar(select(func.count()).select_from(Site)) or 0
    posts_total = await session.scalar(select(func.count()).select_from(Post)) or 0
    posts_published = (
        await session.scalar(
            select(func.count()).select_from(Post).where(Post.status == PostStatus.PUBLISHED)
        )
        or 0
    )

    return templates.TemplateResponse(
        "admin/index.html",
        {
            "request": request,
            "by_tier": by_tier,
            "sites_count": sites_count,
            "posts_total": posts_total,
            "posts_published": posts_published,
        },
    )


@router.get("/domains", response_class=HTMLResponse)
async def domains_list(
    request: Request,
    _: str = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    rows = (await session.execute(select(Domain).order_by(Domain.created_at.desc()))).scalars().all()
    return templates.TemplateResponse(
        "admin/domains.html", {"request": request, "domains": rows, "tiers": list(Tier)}
    )
