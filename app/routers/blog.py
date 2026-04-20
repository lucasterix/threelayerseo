from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.db import get_session
from app.models import Domain, Post, PostStatus, Site, SiteStatus

templates = Jinja2Templates(directory="app/templates")
router = APIRouter()


async def _site_for_host(host: str, session: AsyncSession) -> Site:
    host = host.split(":")[0].lower().lstrip("www.")
    stmt = (
        select(Site)
        .join(Domain, Domain.id == Site.domain_id)
        .where(Domain.name == host, Site.status == SiteStatus.LIVE)
        .options(joinedload(Site.domain))
    )
    site = (await session.execute(stmt)).scalar_one_or_none()
    if not site:
        raise HTTPException(status_code=404, detail="site not found")
    return site


@router.get("/healthz/renderer", response_class=PlainTextResponse, include_in_schema=False)
async def ping():
    return "ok"


@router.get("/", response_class=HTMLResponse)
async def home(request: Request, session: AsyncSession = Depends(get_session)):
    site = await _site_for_host(request.url.hostname or "", session)
    posts_stmt = (
        select(Post)
        .where(Post.site_id == site.id, Post.status == PostStatus.PUBLISHED)
        .order_by(Post.published_at.desc().nullslast())
        .limit(20)
    )
    posts = (await session.execute(posts_stmt)).scalars().all()
    template = f"blog/tier_{site.domain.tier.name.lower()}/index.html"
    return templates.TemplateResponse(template, {"request": request, "site": site, "posts": posts})


@router.get("/{slug}", response_class=HTMLResponse)
async def post_detail(slug: str, request: Request, session: AsyncSession = Depends(get_session)):
    site = await _site_for_host(request.url.hostname or "", session)
    stmt = select(Post).where(
        Post.site_id == site.id, Post.slug == slug, Post.status == PostStatus.PUBLISHED
    )
    post = (await session.execute(stmt)).scalar_one_or_none()
    if not post:
        raise HTTPException(status_code=404, detail="post not found")
    template = f"blog/tier_{site.domain.tier.name.lower()}/post.html"
    return templates.TemplateResponse(template, {"request": request, "site": site, "post": post})
